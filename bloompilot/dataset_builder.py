from __future__ import annotations

import argparse
import csv
import json
import queue
import random
import re
import sys
from collections import defaultdict
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from threading import Event
from typing import Any, Callable, Iterable, TypeVar

import yaml
from PIL import Image, ImageDraw, ImageEnhance, ImageOps

from .artifacts import ArtifactManager, LATEST_DATASET, SCHEMA_VERSION, write_pointer
from .paths import CONFIG_PATH
from .settings_store import apply_config_overrides
from .project_defaults import SQUARE_SIZE_PIXELS, apply_fixed_parameters, synthesis_workers


IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".webp", ".bmp"}
ProgressCallback = Callable[[str], None]
AssetT = TypeVar("AssetT")


@dataclass(frozen=True)
class BackgroundAsset:
    path: Path
    relative_path: Path
    city: str


@dataclass(frozen=True)
class TargetAsset:
    path: Path
    relative_path: Path
    image: Image.Image


@dataclass(frozen=True)
class Box:
    x: int
    y: int
    size: int
    target_name: str
    class_id: int
    class_name: str

    @property
    def right(self) -> int:
        return self.x + self.size

    @property
    def bottom(self) -> int:
        return self.y + self.size

    @property
    def center(self) -> tuple[float, float]:
        return self.x + self.size / 2, self.y + self.size / 2


def log(message: str, callback: ProgressCallback | None = None) -> None:
    if callback:
        callback(message)
    else:
        print(message, flush=True)


def load_config(config_path: Path) -> dict[str, Any]:
    config_path = config_path.resolve()
    if not config_path.is_file():
        raise FileNotFoundError(f"找不到設定檔：{config_path}")
    with config_path.open("r", encoding="utf-8") as handle:
        config = yaml.safe_load(handle) or {}
    if config_path == CONFIG_PATH.resolve():
        config = apply_config_overrides(config)
    if "paths" not in config or "generation" not in config or "training" not in config:
        raise ValueError("config.yaml 必須包含 paths、generation、training 三個區段")
    config["_config_path"] = str(config_path)
    config["_project_root"] = str(config_path.parent)
    return apply_fixed_parameters(config)


def project_path(config: dict[str, Any], value: str | Path) -> Path:
    candidate = Path(value)
    if not candidate.is_absolute():
        candidate = Path(config["_project_root"]) / candidate
    return candidate.resolve()


def _iter_images(root: Path) -> Iterable[Path]:
    if not root.is_dir():
        return []
    return sorted(
        path
        for path in root.rglob("*")
        if path.is_file() and path.suffix.lower() in IMAGE_EXTENSIONS
    )


def discover_backgrounds(config: dict[str, Any]) -> list[BackgroundAsset]:
    root = project_path(config, config["paths"]["backgrounds"])
    backgrounds: list[BackgroundAsset] = []
    for path in _iter_images(root):
        relative = path.relative_to(root)
        city = relative.parts[0] if len(relative.parts) > 1 else "_root"
        backgrounds.append(BackgroundAsset(path=path, relative_path=relative, city=city))
    return backgrounds


def _load_target_image(
    path: Path,
    *,
    require_transparency: bool,
    alpha_threshold: int,
    trim_transparent_padding: bool,
) -> Image.Image | None:
    with Image.open(path) as source:
        rgba = source.convert("RGBA")
    alpha = rgba.getchannel("A")
    extrema = alpha.getextrema()
    if require_transparency and extrema[0] == 255:
        return None
    thresholded = alpha.point(lambda value: 255 if value > alpha_threshold else 0)
    bounds = thresholded.getbbox()
    if bounds is None:
        return None
    return rgba.crop(bounds) if trim_transparent_padding else rgba


def discover_targets(
    config: dict[str, Any], callback: ProgressCallback | None = None
) -> tuple[list[TargetAsset], list[str]]:
    root = project_path(config, config["paths"]["targets"])
    generation = config["generation"]
    excluded = {
        str(name).casefold()
        for name in generation.get("excluded_target_directories", [])
    }
    require_transparency = bool(generation.get("require_transparency", True))
    alpha_threshold = int(generation.get("alpha_threshold", 8))
    trim_transparent_padding = bool(generation.get("trim_transparent_padding", True))
    targets: list[TargetAsset] = []
    skipped: list[str] = []

    for path in _iter_images(root):
        relative = path.relative_to(root)
        parent_parts = {part.casefold() for part in relative.parts[:-1]}
        if parent_parts & excluded:
            skipped.append(f"{relative.as_posix()}（位於排除資料夾）")
            continue
        image = _load_target_image(
            path,
            require_transparency=require_transparency,
            alpha_threshold=alpha_threshold,
            trim_transparent_padding=trim_transparent_padding,
        )
        if image is None:
            skipped.append(f"{relative.as_posix()}（沒有可用透明區域）")
            continue
        targets.append(TargetAsset(path=path, relative_path=relative, image=image))

    if skipped:
        log(f"略過 {len(skipped)} 個非透明或被排除的 target。", callback)
    return targets, skipped


def select_assets(
    assets: list[AssetT],
    selected_paths: Iterable[str] | None,
    *,
    asset_label: str,
) -> list[AssetT]:
    """Filter discovered assets by safe paths relative to their configured root."""
    if selected_paths is None:
        return assets

    def normalize(value: str | Path) -> str:
        text = str(value).replace("\\", "/").strip()
        while text.startswith("./"):
            text = text[2:]
        candidate = Path(text)
        if not text or candidate.is_absolute() or ".." in candidate.parts:
            raise ValueError(f"{asset_label} 選取路徑必須是素材根目錄內的相對路徑：{value}")
        return candidate.as_posix().casefold()

    requested = {normalize(value) for value in selected_paths}
    if not requested:
        raise ValueError(f"至少要選取一個{asset_label}")
    available = {
        normalize(getattr(asset, "relative_path")): asset
        for asset in assets
    }
    missing = sorted(requested - set(available))
    if missing:
        preview = "、".join(missing[:5])
        raise ValueError(f"找不到選取的{asset_label}：{preview}")
    return [asset for key, asset in available.items() if key in requested]


def inventory(config: dict[str, Any]) -> dict[str, Any]:
    backgrounds = discover_backgrounds(config)
    targets, skipped = discover_targets(config, callback=lambda _: None)
    class_names, _ = build_class_registry(targets)
    city_counts: dict[str, int] = defaultdict(int)
    for item in backgrounds:
        city_counts[item.city] += 1
    return {
        "background_count": len(backgrounds),
        "target_count": len(targets),
        "class_count": len(class_names),
        "class_names": class_names,
        "skipped_target_count": len(skipped),
        "cities": dict(sorted(city_counts.items())),
    }


def build_class_registry(
    targets: list[TargetAsset],
) -> tuple[list[str], dict[str, int]]:
    class_names: list[str] = []
    class_ids: dict[str, int] = {}
    seen_names: dict[str, str] = {}
    for asset in sorted(targets, key=lambda item: item.relative_path.as_posix().casefold()):
        class_name = asset.relative_path.stem
        normalized_name = class_name.casefold()
        if normalized_name in seen_names:
            raise ValueError(
                f"target 類別名稱重複：{seen_names[normalized_name]} 與 "
                f"{asset.relative_path.as_posix()}。請讓每個 PNG 檔名唯一。"
            )
        seen_names[normalized_name] = asset.relative_path.as_posix()
        class_id = len(class_names)
        class_names.append(class_name)
        class_ids[asset.relative_path.as_posix()] = class_id
    return class_names, class_ids


def validate_generation_config(config: dict[str, Any]) -> None:
    generation = config["generation"]
    width = int(generation["output_width"])
    height = int(generation["output_height"])
    square = int(generation["square_size_pixels"])
    padding = int(generation["target_padding_pixels"])
    minimum = int(generation["targets_per_image_min"])
    maximum = int(generation["targets_per_image_max"])
    margin = generation["placement_margin"]

    if width <= 0 or height <= 0:
        raise ValueError("output_width 與 output_height 必須大於 0")
    if int(generation["composites_per_background"]) < 1:
        raise ValueError("composites_per_background 必須大於 0")
    if square <= 0 or square > min(width, height):
        raise ValueError("square_size_pixels 必須大於 0 且不超過畫布短邊")
    if padding < 0 or padding * 2 >= square:
        raise ValueError("target_padding_pixels 必須小於正方形邊長的一半")
    if minimum < 0 or maximum < minimum:
        raise ValueError("targets_per_image_min/max 設定不合法")
    probabilities = generation.get("target_count_probabilities")
    if probabilities is not None:
        if not isinstance(probabilities, dict) or not probabilities:
            raise ValueError("target_count_probabilities 必須是非空白的數量/權重對照表")
        total_weight = 0.0
        for raw_count, raw_weight in probabilities.items():
            try:
                count = int(raw_count)
                weight = float(raw_weight)
            except (TypeError, ValueError) as exc:
                raise ValueError("target_count_probabilities 的數量與權重必須是數字") from exc
            if count < 0 or weight < 0:
                raise ValueError("target 數量與權重不可小於 0；0 代表無目標背景")
            total_weight += weight
        if total_weight <= 0:
            raise ValueError("target_count_probabilities 至少要有一個大於 0 的權重")
    if int(margin["left"]) + int(margin["right"]) + square > width:
        raise ValueError("左右 placement_margin 加上正方形後超過畫布寬度")
    if int(margin["top"]) + int(margin["bottom"]) + square > height:
        raise ValueError("上下 placement_margin 加上正方形後超過畫布高度")
    ratio = float(generation["validation_ratio"])
    if not 0 <= ratio < 1:
        raise ValueError("validation_ratio 必須介於 0（含）與 1（不含）")
    synthesis_workers(generation.get("parallel_workers", 1))


def split_backgrounds(
    backgrounds: list[BackgroundAsset], validation_ratio: float, seed: int
) -> dict[str, list[BackgroundAsset]]:
    by_city: dict[str, list[BackgroundAsset]] = defaultdict(list)
    for item in backgrounds:
        by_city[item.city].append(item)

    split: dict[str, list[BackgroundAsset]] = {"train": [], "val": []}
    for city in sorted(by_city):
        items = sorted(by_city[city], key=lambda item: item.relative_path.as_posix())
        city_rng = random.Random(f"{seed}:{city}")
        city_rng.shuffle(items)
        if validation_ratio == 0 or len(items) == 1:
            validation_count = 0
        else:
            validation_count = max(1, round(len(items) * validation_ratio))
            validation_count = min(validation_count, len(items) - 1)
        split["val"].extend(items[:validation_count])
        split["train"].extend(items[validation_count:])
    return split


def fit_background(image: Image.Image, width: int, height: int, mode: str) -> Image.Image:
    image = image.convert("RGB")
    if mode == "cover":
        return ImageOps.fit(
            image,
            (width, height),
            method=Image.Resampling.LANCZOS,
            centering=(0.5, 0.5),
        )
    if mode == "contain":
        contained = ImageOps.contain(image, (width, height), Image.Resampling.LANCZOS)
        canvas = Image.new("RGB", (width, height), (0, 0, 0))
        offset = ((width - contained.width) // 2, (height - contained.height) // 2)
        canvas.paste(contained, offset)
        return canvas
    if mode == "stretch":
        return image.resize((width, height), Image.Resampling.LANCZOS)
    raise ValueError(f"未知 resize_mode：{mode}")


def transform_target(
    asset: TargetAsset, generation: dict[str, Any], rng: random.Random
) -> Image.Image:
    if bool(generation.get("preserve_original_target_pixels", False)):
        return asset.image.copy()

    image = asset.image.copy()
    if rng.random() < float(generation["horizontal_flip_probability"]):
        image = ImageOps.mirror(image)

    rotation = float(generation["rotation_degrees"])
    if rotation > 0:
        image = image.rotate(
            rng.uniform(-rotation, rotation),
            resample=Image.Resampling.BICUBIC,
            expand=True,
            fillcolor=(0, 0, 0, 0),
        )
        alpha_bounds = image.getchannel("A").getbbox()
        if alpha_bounds:
            image = image.crop(alpha_bounds)

    alpha = image.getchannel("A")
    rgb = image.convert("RGB")
    rgb = ImageEnhance.Brightness(rgb).enhance(
        rng.uniform(float(generation["brightness_min"]), float(generation["brightness_max"]))
    )
    rgb = ImageEnhance.Contrast(rgb).enhance(
        rng.uniform(float(generation["contrast_min"]), float(generation["contrast_max"]))
    )
    rgba = rgb.convert("RGBA")
    rgba.putalpha(alpha)

    square = int(generation["square_size_pixels"])
    padding = int(generation["target_padding_pixels"])
    inner_side = square - padding * 2
    target_max_side = inner_side * rng.uniform(
        float(generation["target_scale_min"]),
        float(generation["target_scale_max"]),
    )
    scale = target_max_side / max(rgba.width, rgba.height)
    new_size = (
        max(1, round(rgba.width * scale)),
        max(1, round(rgba.height * scale)),
    )
    return rgba.resize(new_size, Image.Resampling.LANCZOS)


def box_iou(first: Box, second: Box) -> float:
    intersection_width = max(0, min(first.right, second.right) - max(first.x, second.x))
    intersection_height = max(0, min(first.bottom, second.bottom) - max(first.y, second.y))
    intersection = intersection_width * intersection_height
    if intersection == 0:
        return 0.0
    area = first.size * first.size
    return intersection / (area + area - intersection)


def choose_box(
    generation: dict[str, Any],
    existing: list[Box],
    target_name: str,
    class_id: int,
    class_name: str,
    rng: random.Random,
) -> Box:
    width = int(generation["output_width"])
    height = int(generation["output_height"])
    square = int(generation["square_size_pixels"])
    margins = generation["placement_margin"]
    min_x = int(margins["left"])
    max_x = width - int(margins["right"]) - square
    min_y = int(margins["top"])
    max_y = height - int(margins["bottom"]) - square
    max_iou = float(generation["max_box_iou"])

    for _ in range(int(generation["placement_attempts"])):
        candidate = Box(
            x=rng.randint(min_x, max_x),
            y=rng.randint(min_y, max_y),
            size=square,
            target_name=target_name,
            class_id=class_id,
            class_name=class_name,
        )
        if all(box_iou(candidate, previous) <= max_iou for previous in existing):
            return candidate
    raise RuntimeError("無法在指定範圍內放入所有 target；請減少數量、縮小方框或放寬重疊限制")


def render_composite(
    background: Image.Image,
    targets: list[TargetAsset],
    class_names: list[str],
    class_ids: dict[str, int],
    generation: dict[str, Any],
    rng: random.Random,
) -> tuple[Image.Image, list[Box]]:
    canvas = background.convert("RGBA")
    probabilities = generation.get("target_count_probabilities")
    if probabilities:
        count_weights = sorted(
            (int(raw_count), float(raw_weight))
            for raw_count, raw_weight in probabilities.items()
        )
        target_count = rng.choices(
            [item[0] for item in count_weights],
            weights=[item[1] for item in count_weights],
            k=1,
        )[0]
    else:
        target_count = rng.randint(
            int(generation["targets_per_image_min"]),
            int(generation["targets_per_image_max"]),
        )
    boxes: list[Box] = []
    for _ in range(target_count):
        asset = rng.choice(targets)
        target_name = asset.relative_path.as_posix()
        class_id = class_ids[target_name]
        sprite = transform_target(asset, generation, rng)
        box = choose_box(
            generation,
            boxes,
            target_name,
            class_id,
            class_names[class_id],
            rng,
        )
        paste_x = box.x + (box.size - sprite.width) // 2
        paste_y = box.y + (box.size - sprite.height) // 2
        canvas.alpha_composite(sprite, (paste_x, paste_y))
        boxes.append(box)
    return canvas.convert("RGB"), boxes


def yolo_label(box: Box, width: int, height: int) -> str:
    center_x, center_y = box.center
    return (
        f"{box.class_id} {center_x / width:.8f} {center_y / height:.8f} "
        f"{box.size / width:.8f} {box.size / height:.8f}"
    )


def safe_stem(relative_path: Path) -> str:
    raw = "__".join(relative_path.with_suffix("").parts)
    return re.sub(r"[^A-Za-z0-9_.-]+", "_", raw).strip("._") or "image"


def draw_preview(image: Image.Image, boxes: list[Box]) -> Image.Image:
    preview = image.copy()
    draw = ImageDraw.Draw(preview)
    line_width = max(2, round(min(image.size) / 270))
    for box in boxes:
        draw.rectangle(
            (box.x, box.y, box.right - 1, box.bottom - 1),
            outline=(255, 45, 45),
            width=line_width,
        )
        label = f"{box.class_name} | {box.size}px"
        text_bounds = draw.textbbox((0, 0), label)
        text_width = text_bounds[2] - text_bounds[0]
        text_height = text_bounds[3] - text_bounds[1]
        text_y = max(0, box.y - text_height - 8)
        draw.rectangle(
            (box.x, text_y, box.x + text_width + 8, text_y + text_height + 6),
            fill=(255, 45, 45),
        )
        draw.text((box.x + 4, text_y + 3), label, fill=(255, 255, 255))
    return preview


def _new_run_paths(root: Path) -> tuple[Path, Path]:
    stamp = datetime.now().strftime("dataset_%Y%m%d_%H%M%S")
    final = root / stamp
    counter = 1
    while final.exists() or (root / f".{final.name}.building").exists():
        final = root / f"{stamp}_{counter:02d}"
        counter += 1
    staging = root / f".{final.name}.building"
    return staging, final


def validate_target_sizes(targets: list[TargetAsset], generation: dict[str, Any]) -> None:
    if not bool(generation.get("preserve_original_target_pixels", False)):
        return
    square = SQUARE_SIZE_PIXELS
    padding = int(generation["target_padding_pixels"])
    oversized = [
        f"{asset.relative_path.as_posix()}（{asset.image.width}×{asset.image.height}px）"
        for asset in targets
        if max(asset.image.size) + padding * 2 > square
    ]
    if oversized:
        raise RuntimeError(
            f"原尺寸 target 加上留白後超過 {square}px 固定方框：{'、'.join(oversized[:5])}。"
            f"目前每邊留白 {padding}px，target 寬高最多 {square - padding * 2}px；"
            "請調整 target_padding_pixels 或使用較小的 target，程式不會自動縮放 target。"
        )


def generate_dataset(
    config: dict[str, Any],
    *,
    composites_override: int | None = None,
    parallel_workers_override: int | None = None,
    limit_backgrounds: int | None = None,
    selected_backgrounds: Iterable[str] | None = None,
    selected_targets: Iterable[str] | None = None,
    callback: ProgressCallback | None = None,
) -> Path:
    generation = dict(config["generation"])
    generation["placement_margin"] = dict(generation["placement_margin"])
    if composites_override is not None:
        generation["composites_per_background"] = composites_override
    generation["square_size_pixels"] = SQUARE_SIZE_PIXELS
    if parallel_workers_override is not None:
        generation["parallel_workers"] = parallel_workers_override
    generation["parallel_workers"] = synthesis_workers(generation.get("parallel_workers", 1))
    generation["progress_interval"] = generation["parallel_workers"]
    active_config = dict(config)
    active_config["generation"] = generation
    validate_generation_config(active_config)

    backgrounds = discover_backgrounds(active_config)
    targets, skipped_targets = discover_targets(active_config, callback)
    backgrounds = select_assets(
        backgrounds,
        selected_backgrounds,
        asset_label="背景",
    )
    targets = select_assets(
        targets,
        selected_targets,
        asset_label="target",
    )
    if not backgrounds:
        raise RuntimeError("data/source/backgrounds 中找不到背景圖片")
    if not targets:
        raise RuntimeError("data/source/targets 中找不到具有透明背景的 target 圖片")
    class_names, class_ids = build_class_registry(targets)
    validate_target_sizes(targets, generation)
    if limit_backgrounds is not None:
        if limit_backgrounds < 1:
            raise ValueError("--limit-backgrounds 必須大於 0")
        backgrounds = backgrounds[:limit_backgrounds]

    city_names = sorted({item.city for item in backgrounds})
    log(
        f"找到 {len(backgrounds)} 張背景（{', '.join(city_names)}）與 "
        f"{len(targets)} 個透明 target／{len(class_names)} 個類別。",
        callback,
    )
    log(
        "類別對照：" + "、".join(
            f"{class_id}={class_name}"
            for class_id, class_name in enumerate(class_names)
        ),
        callback,
    )

    split = split_backgrounds(
        backgrounds,
        float(generation["validation_ratio"]),
        int(generation["random_seed"]),
    )
    if not split["val"]:
        log("警告：背景數不足，這次沒有 validation split。", callback)

    generated_root = project_path(active_config, active_config["paths"]["generated_root"])
    generated_root.mkdir(parents=True, exist_ok=True)
    staging, final = _new_run_paths(generated_root)
    for split_name in ("train", "val"):
        (staging / "images" / split_name).mkdir(parents=True, exist_ok=True)
        (staging / "labels" / split_name).mkdir(parents=True, exist_ok=True)
    (staging / "previews").mkdir(parents=True, exist_ok=True)

    width = int(generation["output_width"])
    height = int(generation["output_height"])
    per_background = int(generation["composites_per_background"])
    preview_limit = int(generation["preview_count"])
    parallel_workers = int(generation.get("parallel_workers", 1))
    progress_interval = int(generation.get("progress_interval", 10))
    output_format = str(generation.get("output_format", "png")).casefold()
    if output_format not in {"png", "jpg", "jpeg"}:
        raise ValueError("output_format 只支援 png、jpg、jpeg")
    image_extension = ".png" if output_format == "png" else ".jpg"
    total_expected = len(backgrounds) * per_background
    completed = 0
    annotation_rows: list[dict[str, Any]] = []
    # Workers report completed images; only the calling thread invokes callbacks.
    events: queue.Queue[Future[list[dict[str, Any]]] | None] = queue.Queue()
    stop_requested = Event()

    tasks: list[tuple[str, BackgroundAsset, int]] = []
    background_ordinal = 0
    for split_name in ("train", "val"):
        for background_asset in split[split_name]:
            tasks.append((split_name, background_asset, background_ordinal))
            background_ordinal += 1

    def synthesize_background(
        task: tuple[str, BackgroundAsset, int],
    ) -> list[dict[str, Any]]:
        split_name, background_asset, ordinal = task
        rows: list[dict[str, Any]] = []
        if stop_requested.is_set():
            return rows
        with Image.open(background_asset.path) as source:
            prepared_background = fit_background(
                source,
                width,
                height,
                str(generation["resize_mode"]),
            )
        source_stem = safe_stem(background_asset.relative_path)
        for composite_index in range(per_background):
            if stop_requested.is_set():
                break
            seed_material = (
                f"{generation['random_seed']}:{split_name}:"
                f"{background_asset.relative_path.as_posix()}:{composite_index}"
            )
            rng = random.Random(seed_material)
            image, boxes = render_composite(
                prepared_background,
                targets,
                class_names,
                class_ids,
                generation,
                rng,
            )
            output_stem = f"{source_stem}__aug_{composite_index:03d}"
            image_relative = Path("images") / split_name / f"{output_stem}{image_extension}"
            label_relative = Path("labels") / split_name / f"{output_stem}.txt"
            if output_format == "png":
                image.save(
                    staging / image_relative,
                    "PNG",
                    compress_level=int(generation.get("png_compress_level", 4)),
                )
            else:
                image.save(
                    staging / image_relative,
                    "JPEG",
                    quality=int(generation["jpeg_quality"]),
                    subsampling=0,
                    optimize=True,
                )
            labels = [yolo_label(box, width, height) for box in boxes]
            (staging / label_relative).write_text(
                "".join(label + "\n" for label in labels),
                encoding="utf-8",
            )

            global_composite_index = ordinal * per_background + composite_index
            if global_composite_index < preview_limit:
                draw_preview(image, boxes).save(
                    staging / "previews" / f"{output_stem}.jpg",
                    "JPEG",
                    quality=90,
                )

            for instance_index, box in enumerate(boxes):
                rows.append(
                    {
                        "split": split_name,
                        "image": image_relative.as_posix(),
                        "city": background_asset.city,
                        "source_background": background_asset.relative_path.as_posix(),
                        "instance": instance_index,
                        "target": box.target_name,
                        "class_id": box.class_id,
                        "class_name": box.class_name,
                        "x": box.x,
                        "y": box.y,
                        "width": box.size,
                        "height": box.size,
                    }
                )
            events.put(None)
        return rows

    log(
        f"合成速度：{parallel_workers} 個平行工作；每 {progress_interval} 張回報進度。",
        callback,
    )
    executor = ThreadPoolExecutor(max_workers=parallel_workers)
    try:
        futures = [executor.submit(synthesize_background, task) for task in tasks]
        for future in futures:
            future.add_done_callback(events.put)
        finished_backgrounds = 0
        while finished_backgrounds < len(futures):
            event = events.get()
            if event is not None:
                event.result()  # Propagate worker errors without waiting for other jobs.
                finished_backgrounds += 1
                continue
            completed += 1
            if completed % progress_interval == 0 or completed == total_expected:
                log(f"合成進度：{completed}/{total_expected}", callback)
        # Keep annotations in source order regardless of worker completion order.
        for future in futures:
            annotation_rows.extend(future.result())
    except BaseException:
        stop_requested.set()
        raise
    finally:
        executor.shutdown(wait=True, cancel_futures=True)

    with (staging / "annotations.csv").open("w", encoding="utf-8-sig", newline="") as handle:
        fields = [
            "split",
            "image",
            "city",
            "source_background",
            "instance",
            "target",
            "class_id",
            "class_name",
            "x",
            "y",
            "width",
            "height",
        ]
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        writer.writerows(annotation_rows)

    created_at = datetime.now().astimezone().isoformat()
    metadata = {
        "created_at": created_at,
        "artifact": {
            "schema_version": SCHEMA_VERSION,
            "kind": "dataset",
            "id": f"dataset:{final.name}",
            "status": "ready",
        },
        "background_count": len(backgrounds),
        "city_count": len(city_names),
        "cities": city_names,
        "target_count": len(targets),
        "class_count": len(class_names),
        "class_names": class_names,
        "negative_image_count": completed - len({row["image"] for row in annotation_rows}),
        "target_pixel_sizes": {
            asset.relative_path.as_posix(): [asset.image.width, asset.image.height]
            for asset in targets
        },
        "skipped_targets": skipped_targets,
        "generated_image_count": completed,
        "instance_count": len(annotation_rows),
        "train_background_count": len(split["train"]),
        "val_background_count": len(split["val"]),
        "square_size_pixels": int(generation["square_size_pixels"]),
        "output_size": [width, height],
        "generation_config": generation,
        "source_selection": {
            "backgrounds": [asset.relative_path.as_posix() for asset in backgrounds],
            "targets": [asset.relative_path.as_posix() for asset in targets],
        },
    }
    (staging / "metadata.json").write_text(
        json.dumps(metadata, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    final_data_yaml = {
        "train": "images/train",
        "val": "images/val" if split["val"] else "images/train",
        "names": {class_id: name for class_id, name in enumerate(class_names)},
    }
    (staging / "data.yaml").write_text(
        yaml.safe_dump(final_data_yaml, allow_unicode=True, sort_keys=False),
        encoding="utf-8",
    )

    staging.rename(final)
    write_pointer(generated_root / LATEST_DATASET, final, generated_root)
    try:
        ArtifactManager(active_config).refresh_catalog()
    except Exception as exc:
        log(f"警告：資料集已完成，但產物 catalog 更新失敗：{exc}", callback)
    log(f"資料集完成：{final}", callback)
    log(
        f"共 {completed} 張圖片、{len(annotation_rows)} 個固定 {generation['square_size_pixels']}px 正方形標註。",
        callback,
    )
    return final


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="合成 target 到多城市背景並產生固定正方形 YOLO 標註")
    parser.add_argument("--config", type=Path, default=Path("config.yaml"))
    parser.add_argument("--inventory", action="store_true", help="只顯示素材統計")
    parser.add_argument("--composites-per-background", type=int)
    parser.add_argument("--parallel-workers", type=int, help="同時合成的背景工作數（正整數，無程式上限；進度間隔同步）")
    parser.add_argument("--limit-backgrounds", type=int, help="測試用：只處理前 N 張背景")
    parser.add_argument(
        "--background",
        action="append",
        dest="selected_backgrounds",
        help="只使用指定的背景相對路徑；可重複使用",
    )
    parser.add_argument(
        "--target",
        action="append",
        dest="selected_targets",
        help="只使用指定的 target 相對路徑；可重複使用",
    )
    return parser


def main() -> int:
    args = build_parser().parse_args()
    try:
        config = load_config(args.config)
        if args.inventory:
            print(json.dumps(inventory(config), ensure_ascii=False, indent=2))
            return 0
        output = generate_dataset(
            config,
            composites_override=args.composites_per_background,
            parallel_workers_override=args.parallel_workers,
            limit_backgrounds=args.limit_backgrounds,
            selected_backgrounds=args.selected_backgrounds,
            selected_targets=args.selected_targets,
        )
        print(f"OUTPUT_DATASET={output}")
        return 0
    except Exception as exc:
        print(f"錯誤：{exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
