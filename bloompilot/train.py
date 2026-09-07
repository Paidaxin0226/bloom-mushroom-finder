from __future__ import annotations

import argparse
import csv
import ctypes
import json
import math
import os
import re
import sys
import time
from collections import Counter
from datetime import datetime
from pathlib import Path
from collections.abc import Callable, Mapping
from typing import Any

import yaml
from PIL import Image

from .artifacts import ArtifactManager, LATEST_RUN, TRAINING_MANIFEST, read_pointer
from .dataset_builder import IMAGE_EXTENSIONS, load_config, project_path


def normalize_class_names(data: dict[str, Any]) -> dict[int, str]:
    raw_names = data.get("names")
    if isinstance(raw_names, list):
        names = {index: str(name) for index, name in enumerate(raw_names)}
    elif isinstance(raw_names, dict):
        try:
            names = {int(class_id): str(name) for class_id, name in raw_names.items()}
        except (TypeError, ValueError) as exc:
            raise RuntimeError("data.yaml 的 names 類別 ID 必須是整數") from exc
    else:
        raise RuntimeError("data.yaml 缺少 names 類別對照")
    expected_ids = set(range(len(names)))
    if set(names) != expected_ids:
        raise RuntimeError("data.yaml 的 class_id 必須從 0 開始連續編號")
    if len({name.casefold() for name in names.values()}) != len(names):
        raise RuntimeError("data.yaml 含有重複的類別名稱")
    return dict(sorted(names.items()))


def generated_dataset_summary(dataset_dir: Path) -> dict[str, Any]:
    """Read the generator's small manifest, without scanning images or labels."""
    metadata = json.loads((dataset_dir / "metadata.json").read_text(encoding="utf-8"))
    return {
        "dataset": str(dataset_dir),
        "class_count": metadata["class_count"],
        "classes": metadata["class_names"],
        "image_count": metadata.get("generated_image_count", 0),
        "instance_count": metadata.get("instance_count", 0),
        "created_at": metadata.get("created_at", ""),
    }


def list_generated_datasets(config: dict[str, Any]) -> list[dict[str, Any]]:
    root = project_path(config, config["paths"]["generated_root"])
    latest = read_pointer(root / "LATEST.txt", root)
    datasets = []
    for folder in root.glob("dataset_*"):
        candidate = folder.resolve()
        if candidate.parent != root or not (candidate / "data.yaml").is_file():
            continue
        try:
            summary = generated_dataset_summary(candidate)
            modified = candidate.stat().st_mtime
        except (OSError, ValueError, KeyError, TypeError):
            continue
        datasets.append({
            "path": str(candidate), "name": candidate.name,
            "latest": candidate == latest, "modified": modified, **summary,
        })
    return sorted(datasets, key=lambda item: (item["latest"], item["modified"]), reverse=True)


def resolve_dataset(config: dict[str, Any], requested: Path | None) -> Path:
    datasets = list_generated_datasets(config)
    if not datasets:
        raise FileNotFoundError("尚未產生資料集；請先在「資料集產生」建立資料")
    if requested is None:
        return Path(datasets[0]["path"])
    candidate = project_path(config, requested)
    if candidate.name == "data.yaml":
        candidate = candidate.parent
    if not any(Path(item["path"]) == candidate for item in datasets):
        raise ValueError("訓練來源只能選擇專案 data/generated 內已產生完成的資料集")
    return candidate


def validate_dataset(
    dataset_dir: Path,
    *,
    progress_callback: Callable[[str, int, int], None] | None = None,
) -> dict[str, Any]:
    with (dataset_dir / "data.yaml").open("r", encoding="utf-8") as handle:
        data = yaml.safe_load(handle)
    class_names = normalize_class_names(data)
    metadata_path = dataset_dir / "metadata.json"
    metadata = json.loads(metadata_path.read_text(encoding="utf-8")) if metadata_path.is_file() else {}
    expected_square = metadata.get("square_size_pixels")
    metadata_class_names = metadata.get("class_names")
    if metadata_class_names is not None and list(class_names.values()) != metadata_class_names:
        raise RuntimeError("metadata.json 與 data.yaml 的類別對照不一致")
    summary: dict[str, Any] = {
        "dataset": str(dataset_dir),
        "class_count": len(class_names),
        "classes": class_names,
        "splits": {},
    }

    for split_name in ("train", "val"):
        relative_images = data.get(split_name)
        if not relative_images:
            continue
        images_dir = (dataset_dir / relative_images).resolve()
        normalized_images_path = str(relative_images).replace("\\", "/").rstrip("/")
        source_split_name = Path(normalized_images_path).name
        labels_dir = dataset_dir / "labels" / source_split_name
        images = sorted(
            path
            for path in images_dir.glob("*")
            if path.is_file() and path.suffix.lower() in IMAGE_EXTENSIONS
        )
        if not images:
            raise RuntimeError(f"{split_name} split 沒有圖片：{images_dir}")

        if progress_callback is not None:
            progress_callback(split_name, 0, len(images))
        last_report = time.monotonic()
        instance_count = 0
        instances_by_class: Counter[str] = Counter()
        for completed, image_path in enumerate(images, start=1):
            label_path = labels_dir / f"{image_path.stem}.txt"
            if not label_path.is_file():
                raise RuntimeError(f"缺少標註：{label_path}")
            with Image.open(image_path) as image:
                image_width, image_height = image.size
            for line_number, line in enumerate(
                label_path.read_text(encoding="utf-8").splitlines(), start=1
            ):
                if not line.strip():
                    continue
                values = line.split()
                if len(values) != 5:
                    raise RuntimeError(f"YOLO 標註格式錯誤：{label_path}:{line_number}")
                raw_class_id, center_x, center_y, norm_width, norm_height = map(float, values)
                class_id = int(raw_class_id)
                if not math.isclose(raw_class_id, class_id, abs_tol=1e-9):
                    raise RuntimeError(f"class_id 不是整數：{label_path}:{line_number}")
                if class_id not in class_names or not all(
                    0 <= value <= 1 for value in (center_x, center_y, norm_width, norm_height)
                ):
                    raise RuntimeError(f"YOLO 標註數值錯誤：{label_path}:{line_number}")
                pixel_width = norm_width * image_width
                pixel_height = norm_height * image_height
                if not math.isclose(pixel_width, pixel_height, abs_tol=0.05):
                    raise RuntimeError(f"標註不是正方形：{label_path}:{line_number}")
                if expected_square is not None and not math.isclose(
                    pixel_width, float(expected_square), abs_tol=0.05
                ):
                    raise RuntimeError(
                        f"標註邊長 {pixel_width:.2f}px 與設定 {expected_square}px 不一致："
                        f"{label_path}:{line_number}"
                    )
                instance_count += 1
                instances_by_class[class_names[class_id]] += 1
            if progress_callback is not None and (
                completed == len(images)
                or completed % 100 == 0
                or time.monotonic() - last_report >= 2.0
            ):
                progress_callback(split_name, completed, len(images))
                last_report = time.monotonic()
        summary["splits"][split_name] = {
            "image_count": len(images),
            "instance_count": instance_count,
            "instances_by_class": dict(sorted(instances_by_class.items())),
        }
    return summary


def parse_batch(value: Any) -> int | float:
    if isinstance(value, str) and value.casefold() == "auto":
        return -1
    if isinstance(value, float):
        return value
    return float(value) if isinstance(value, str) and "." in value else int(value)


def parse_cache(value: Any) -> bool | str:
    if isinstance(value, bool):
        return value
    normalized = str(value).strip().casefold()
    if normalized in {"disk", "ram"}:
        return normalized
    if normalized in {"false", "none", "off", "0", ""}:
        return False
    if normalized in {"true", "on", "1"}:
        return True
    raise ValueError(f"不支援的 cache 設定：{value}")


def choose_device(requested: Any, torch_module: Any) -> int | str:
    if str(requested).casefold() != "auto":
        return requested
    return 0 if torch_module.cuda.is_available() else "cpu"


def effective_workers(training: dict[str, Any]) -> int:
    requested = max(0, int(training.get("workers", 0)))
    if sys.platform == "win32" and requested > 4:
        print(
            f"Windows 偵測到 workers={requested}；為控制記憶體與子程序風險，限制為 workers=4。",
            flush=True,
        )
        return 4
    return requested


def system_memory_bytes() -> tuple[int, int]:
    """Return available and total physical memory without relying on WMI."""
    if sys.platform == "win32":
        class MemoryStatus(ctypes.Structure):
            _fields_ = [
                ("length", ctypes.c_ulong),
                ("memory_load", ctypes.c_ulong),
                ("total_physical", ctypes.c_ulonglong),
                ("available_physical", ctypes.c_ulonglong),
                ("total_page_file", ctypes.c_ulonglong),
                ("available_page_file", ctypes.c_ulonglong),
                ("total_virtual", ctypes.c_ulonglong),
                ("available_virtual", ctypes.c_ulonglong),
                ("available_extended_virtual", ctypes.c_ulonglong),
            ]

        status = MemoryStatus()
        status.length = ctypes.sizeof(status)
        if not ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(status)):
            raise OSError("GlobalMemoryStatusEx failed")
        return int(status.available_physical), int(status.total_physical)

    page_size = int(os.sysconf("SC_PAGE_SIZE"))
    return (
        int(os.sysconf("SC_AVPHYS_PAGES")) * page_size,
        int(os.sysconf("SC_PHYS_PAGES")) * page_size,
    )


def choose_resource_profile(
    training: dict[str, Any],
    *,
    gpu_free_bytes: int,
    gpu_total_bytes: int,
    ram_available_bytes: int,
    ram_total_bytes: int,
    cpu_count: int,
) -> dict[str, Any]:
    """Choose a fixed batch/workers profile while preserving a resource buffer."""
    gib = 1024**3
    reserve = float(training.get("resource_reserve_fraction", 0.10))
    if not 0.05 <= reserve <= 0.50:
        raise ValueError("resource_reserve_fraction 必須介於 0.05 到 0.50")

    max_batch = int(parse_batch(training.get("batch", 1)))
    if max_batch < 1:
        raise ValueError("資源自動模式的 batch 必須是大於 0 的整數上限")
    minimum_gpu_free = float(training.get("minimum_gpu_free_gib", 4.0)) * gib
    if gpu_free_bytes < minimum_gpu_free:
        raise RuntimeError(
            f"GPU 可用顯存只有 {gpu_free_bytes / gib:.2f} GiB，"
            f"低於安全門檻 {minimum_gpu_free / gib:.2f} GiB。"
            "請先關閉 Unity、Epic 或其他 GPU 程式。"
        )
    gpu_training_budget = max(0.0, gpu_free_bytes - gpu_total_bytes * reserve)
    full_budget = gpu_total_bytes * (1.0 - reserve)
    scaled_batch = max_batch * min(1.0, gpu_training_budget / max(full_budget, 1.0))
    # Always round down: rounding to nearest can silently consume the reserve.
    batch = max(1, min(max_batch, math.floor(scaled_batch)))

    ram_reserve = ram_total_bytes * reserve
    process_ram = float(training.get("training_process_ram_gib", 3.0)) * gib
    ram_after_reserve = ram_available_bytes - ram_reserve
    if ram_after_reserve < process_ram:
        raise RuntimeError(
            f"保留 {reserve:.0%} 緩衝後，可用 RAM 不足以啟動訓練。"
            f"目前可用 {ram_available_bytes / gib:.2f} GiB，"
            f"請至少再釋放 {(process_ram - ram_after_reserve) / gib:.2f} GiB。"
        )
    worker_ram = max(float(training.get("ram_per_worker_gib", 1.0)) * gib, 1.0)
    workers_by_ram = max(0, math.floor((ram_after_reserve - process_ram) / worker_ram))
    workers = min(
        effective_workers(training),
        max(0, cpu_count // 2),
        workers_by_ram,
    )
    return {
        "batch": batch,
        "workers": workers,
        "reserve_fraction": reserve,
        "gpu_free_gib": gpu_free_bytes / gib,
        "gpu_total_gib": gpu_total_bytes / gib,
        "ram_available_gib": ram_available_bytes / gib,
        "ram_total_gib": ram_total_bytes / gib,
    }


def runtime_training_profile(
    training: dict[str, Any], torch_module: Any, device: int | str
) -> dict[str, Any]:
    batch = parse_batch(training.get("batch", "auto"))
    workers = effective_workers(training)
    if not bool(training.get("resource_aware", False)) or device == "cpu":
        return {"batch": batch, "workers": workers, "resource_aware": False}

    device_index = int(str(device).split(":")[-1])
    gpu_free, gpu_total = torch_module.cuda.mem_get_info(device_index)
    ram_available, ram_total = system_memory_bytes()
    profile = choose_resource_profile(
        training,
        gpu_free_bytes=int(gpu_free),
        gpu_total_bytes=int(gpu_total),
        ram_available_bytes=ram_available,
        ram_total_bytes=ram_total,
        cpu_count=os.cpu_count() or 1,
    )
    profile["resource_aware"] = True
    return profile


def _same_path(first: str | Path, second: str | Path) -> bool:
    return os.path.normcase(str(Path(first).resolve())) == os.path.normcase(str(Path(second).resolve()))


def find_incomplete_run(
    config: dict[str, Any], dataset_dir: Path
) -> tuple[Path, int, int] | None:
    runs_root = project_path(config, config["paths"]["training_runs"])
    if not runs_root.is_dir():
        return None
    selected_run = read_pointer(runs_root / LATEST_RUN, runs_root)
    candidates: list[tuple[bool, float, Path, int, int]] = []
    current_data = dataset_dir / "data.yaml"
    for run_dir in runs_root.iterdir():
        if not run_dir.is_dir():
            continue
        args_path = run_dir / "args.yaml"
        results_path = run_dir / "results.csv"
        last_checkpoint = run_dir / "weights" / "last.pt"
        if not (args_path.is_file() and results_path.is_file() and last_checkpoint.is_file()):
            continue
        try:
            manifest_path = run_dir / TRAINING_MANIFEST
            manifest = (
                json.loads(manifest_path.read_text(encoding="utf-8"))
                if manifest_path.is_file()
                else {}
            )
            if manifest.get("status") == "completed":
                continue
            args = yaml.safe_load(args_path.read_text(encoding="utf-8")) or {}
            managed_dataset = manifest.get("dataset")
            if managed_dataset:
                managed_path = Path(str(managed_dataset))
                managed_path = (
                    managed_path
                    if managed_path.is_absolute()
                    else Path(config["_project_root"]) / managed_path
                )
                if managed_path.name == "data.yaml":
                    managed_path = managed_path.parent
                matches_dataset = _same_path(managed_path, dataset_dir)
            else:
                matches_dataset = _same_path(args.get("data", ""), current_data)
            if not matches_dataset:
                continue
            total_epochs = int(args["epochs"])
            with results_path.open("r", encoding="utf-8", newline="") as handle:
                rows = list(csv.DictReader(handle))
            completed_epochs = max(
                (int(float(row["epoch"])) for row in rows if row.get("epoch")),
                default=0,
            )
        except (OSError, TypeError, ValueError, KeyError, json.JSONDecodeError, yaml.YAMLError):
            continue
        if 0 < completed_epochs < total_epochs:
            candidates.append(
                (
                    selected_run == run_dir.resolve(),
                    last_checkpoint.stat().st_mtime,
                    last_checkpoint,
                    completed_epochs,
                    total_epochs,
                )
            )
    if not candidates:
        return None
    _, _, checkpoint, completed, total = max(candidates, key=lambda item: (item[0], item[1]))
    return checkpoint, completed, total


def describe_training_plan(
    config: dict[str, Any], dataset_dir: Path, allow_resume: bool = True
) -> dict[str, Any]:
    training = config["training"]
    resume_info = (
        find_incomplete_run(config, dataset_dir)
        if allow_resume and bool(training.get("resume_incomplete", True))
        else None
    )
    return {
        "workers": effective_workers(training),
        "batch": parse_batch(training.get("batch", "auto")),
        "cache": parse_cache(training.get("cache", False)),
        "resource_aware": bool(training.get("resource_aware", False)),
        "reserve_fraction": float(training.get("resource_reserve_fraction", 0.10)),
        "resume_checkpoint": str(resume_info[0]) if resume_info else None,
        "completed_epochs": resume_info[1] if resume_info else 0,
        "total_epochs": resume_info[2] if resume_info else int(training["epochs"]),
    }


def add_training_callbacks(
    model: Any, callbacks: Mapping[str, Callable[[Any], None]] | None
) -> None:
    """Register optional Ultralytics callbacks before starting or resuming training."""
    if not callbacks:
        return
    for event, callback in callbacks.items():
        model.add_callback(event, callback)


def train(
    config: dict[str, Any],
    dataset_dir: Path,
    *,
    allow_resume: bool = True,
    run_name: str | None = None,
    callbacks: Mapping[str, Callable[[Any], None]] | None = None,
) -> Path:
    import torch
    from ultralytics import YOLO

    training = config["training"]
    device = choose_device(training.get("device", "auto"), torch)
    runtime_profile = runtime_training_profile(training, torch, device)
    workers = int(runtime_profile["workers"])
    batch = runtime_profile["batch"]
    cache = parse_cache(training.get("cache", False))
    if device == "cpu":
        print("警告：未偵測到 CUDA，將使用 CPU 訓練（速度會明顯較慢）。", flush=True)
    else:
        print(f"使用 GPU：{torch.cuda.get_device_name(int(device))}", flush=True)
    if runtime_profile.get("resource_aware"):
        print(
            "資源自動配置："
            f"GPU 可用 {runtime_profile['gpu_free_gib']:.2f}/"
            f"{runtime_profile['gpu_total_gib']:.2f} GiB，"
            f"RAM 可用 {runtime_profile['ram_available_gib']:.2f}/"
            f"{runtime_profile['ram_total_gib']:.2f} GiB；"
            f"保留 {runtime_profile['reserve_fraction']:.0%} 緩衝，"
            f"選擇 batch={batch}、workers={workers}。",
            flush=True,
        )

    resume_info = (
        find_incomplete_run(config, dataset_dir)
        if allow_resume and bool(training.get("resume_incomplete", True))
        else None
    )
    if resume_info:
        checkpoint, completed_epochs, total_epochs = resume_info
        print(
            f"找到未完成訓練：{completed_epochs}/{total_epochs} epochs；"
            f"將從 {checkpoint} 接續。",
            flush=True,
        )
        model = YOLO(str(checkpoint))
        add_training_callbacks(model, callbacks)
        model.train(
            resume=str(checkpoint),
            data=str(dataset_dir / "data.yaml"),
            imgsz=int(training["image_size"]),
            batch=batch,
            workers=workers,
            device=device,
            cache=cache,
        )
        save_dir = Path(model.trainer.save_dir).resolve()
        completed = _completed_epochs(save_dir)
        manager = ArtifactManager(config)
        manager.register_training_run(
            save_dir,
            dataset_dir,
            status="completed",
            completed_epochs=completed,
            total_epochs=int(training["epochs"]),
        )
        manager.refresh_catalog()
        print(f"續訓完成：{save_dir}", flush=True)
        print(f"最佳權重：{save_dir / 'weights' / 'best.pt'}", flush=True)
        return save_dir

    model_name = str(training["model"])
    print(f"載入模型：{model_name}", flush=True)
    model = YOLO(model_name)
    add_training_callbacks(model, callbacks)
    if run_name is None:
        run_name = f"{Path(model_name).stem}_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
    else:
        run_name = re.sub(r"[^A-Za-z0-9_.-]+", "_", str(run_name)).strip("._")[:120]
        if not run_name:
            raise ValueError("訓練 run 名稱無效。")
    project = project_path(config, config["paths"]["training_runs"])
    model.train(
        data=str(dataset_dir / "data.yaml"),
        epochs=int(training["epochs"]),
        imgsz=int(training["image_size"]),
        batch=parse_batch(training.get("batch", "auto")),
        device=device,
        workers=workers,
        patience=int(training.get("patience", 20)),
        cache=cache,
        amp=bool(training.get("amp", True)),
        rect=bool(training.get("rect", True)),
        close_mosaic=int(training.get("close_mosaic", 10)),
        save_period=int(training.get("save_period", 10)),
        seed=int(config["generation"]["random_seed"]),
        deterministic=True,
        pretrained=True,
        plots=True,
        project=str(project),
        name=run_name,
        exist_ok=False,
    )
    save_dir = Path(model.trainer.save_dir).resolve()
    completed = _completed_epochs(save_dir)
    manager = ArtifactManager(config)
    manager.register_training_run(
        save_dir,
        dataset_dir,
        status="completed",
        completed_epochs=completed,
        total_epochs=int(training["epochs"]),
    )
    manager.refresh_catalog()
    print(f"訓練完成：{save_dir}", flush=True)
    print(f"最佳權重：{save_dir / 'weights' / 'best.pt'}", flush=True)
    return save_dir


def _completed_epochs(run_dir: Path) -> int:
    results_path = run_dir / "results.csv"
    if not results_path.is_file():
        return 0
    try:
        with results_path.open("r", encoding="utf-8", newline="") as handle:
            rows = list(csv.DictReader(handle))
        return max(
            (int(float(row["epoch"])) for row in rows if row.get("epoch")),
            default=0,
        )
    except (OSError, ValueError, KeyError, csv.Error):
        return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="使用專案生成資料直接啟動 Ultralytics YOLO 訓練")
    parser.add_argument("--config", type=Path, default=Path("config.yaml"))
    parser.add_argument("--dataset", type=Path, help="資料集目錄；省略時使用 data/generated/LATEST.txt")
    parser.add_argument("--model", type=Path, help="覆寫設定檔中的起始模型權重")
    parser.add_argument("--run-name", help="指定新的訓練 run 名稱")
    parser.add_argument("--dry-run", action="store_true", help="只顯示生成資料摘要與訓練計畫，不開始訓練")
    parser.add_argument("--no-resume", action="store_true", help="忽略未完成 run，強制開始新的訓練")
    return parser


def main() -> int:
    args = build_parser().parse_args()
    try:
        config = load_config(args.config)
        if args.model is not None:
            model_path = args.model.expanduser().resolve()
            if not model_path.is_file():
                raise FileNotFoundError(f"找不到起始模型：{model_path}")
            config["training"] = dict(config["training"])
            config["training"]["model"] = str(model_path)
            print(f"使用指定起始模型：{model_path}", flush=True)
        dataset_dir = resolve_dataset(config, args.dataset)
        summary = generated_dataset_summary(dataset_dir)
        print("使用專案生成資料集（略過圖片與標註檢查）：", flush=True)
        print(json.dumps(summary, ensure_ascii=False, indent=2), flush=True)
        if args.dry_run:
            print("訓練執行計畫：", flush=True)
            print(
                json.dumps(
                    describe_training_plan(config, dataset_dir, allow_resume=not args.no_resume),
                    ensure_ascii=False,
                    indent=2,
                ),
                flush=True,
            )
            return 0
        train(
            config,
            dataset_dir,
            allow_resume=not args.no_resume,
            run_name=args.run_name,
        )
        return 0
    except Exception as exc:
        print(f"錯誤：{exc}", file=sys.stderr, flush=True)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
