from __future__ import annotations

import json
import random
import tempfile
import threading
import unittest
from pathlib import Path

import yaml
from PIL import Image, ImageDraw

from bloompilot.dataset_builder import (
    Box,
    build_parser,
    build_class_registry,
    discover_targets,
    generate_dataset,
    inventory,
    load_config,
    transform_target,
    yolo_label,
    validate_generation_config,
)
from bloompilot.train import validate_dataset


class DatasetBuilderTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory(dir=Path.cwd())
        self.root = Path(self.temp_dir.name)
        (self.root / "data" / "source" / "backgrounds" / "city_a").mkdir(parents=True)
        (self.root / "data" / "source" / "backgrounds" / "city_b").mkdir(parents=True)
        (self.root / "data" / "source" / "targets" / "source").mkdir(parents=True)

        for city, color in (("city_a", (40, 120, 60)), ("city_b", (50, 70, 140))):
            for index in range(2):
                Image.new("RGB", (300, 500), color).save(
                    self.root / "data" / "source" / "backgrounds" / city / f"bg_{index}.png"
                )

        target = Image.new("RGBA", (80, 60), (0, 0, 0, 0))
        draw = ImageDraw.Draw(target)
        draw.ellipse((8, 5, 72, 55), fill=(255, 180, 20, 255))
        target.save(self.root / "data" / "source" / "targets" / "target.png")
        Image.new("RGB", (100, 100), (255, 255, 255)).save(
            self.root / "data" / "source" / "targets" / "source" / "raw.png"
        )

        config = {
            "paths": {
                "backgrounds": "data/source/backgrounds",
                "targets": "data/source/targets",
                "generated_root": "data/generated",
                "training_runs": "outputs/training",
            },
            "generation": {
                "output_width": 300,
                "output_height": 500,
                "resize_mode": "cover",
                "composites_per_background": 1,
                "target_count_probabilities": {1: 1.0, 2: 0.0},
                "targets_per_image_min": 1,
                "targets_per_image_max": 2,
                "square_size_pixels": 80,
                "target_padding_pixels": 0,
                "preserve_original_target_pixels": True,
                "trim_transparent_padding": False,
                "target_scale_min": 0.8,
                "target_scale_max": 1.0,
                "placement_margin": {"left": 5, "top": 5, "right": 5, "bottom": 5},
                "max_box_iou": 0.0,
                "placement_attempts": 100,
                "horizontal_flip_probability": 0.0,
                "rotation_degrees": 0.0,
                "brightness_min": 1.0,
                "brightness_max": 1.0,
                "contrast_min": 1.0,
                "contrast_max": 1.0,
                "validation_ratio": 0.5,
                "random_seed": 7,
                "jpeg_quality": 90,
                "output_format": "png",
                "png_compress_level": 1,
                "preview_count": 2,
                "excluded_target_directories": ["source"],
                "require_transparency": True,
                "alpha_threshold": 8,
            },
            "training": {"model": "models/pretrained/yolo26s.pt"},
        }
        (self.root / "config.yaml").write_text(
            yaml.safe_dump(config, sort_keys=False), encoding="utf-8"
        )

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_discovers_new_cities_and_skips_raw_sources(self) -> None:
        config = load_config(self.root / "config.yaml")
        result = inventory(config)
        self.assertEqual(result["cities"], {"city_a": 2, "city_b": 2})
        self.assertEqual(result["target_count"], 1)
        self.assertEqual(result["skipped_target_count"], 1)

        targets, _ = discover_targets(config)
        transformed = transform_target(targets[0], config["generation"], random.Random(1))
        self.assertEqual(transformed.size, (80, 60))
        self.assertEqual(transformed.tobytes(), targets[0].image.tobytes())

    def test_generates_fixed_square_annotations_and_grouped_split(self) -> None:
        config = load_config(self.root / "config.yaml")
        dataset = generate_dataset(config)
        validation_progress: list[tuple[str, int, int]] = []
        summary = validate_dataset(
            dataset,
            progress_callback=lambda split, completed, total: validation_progress.append(
                (split, completed, total)
            ),
        )
        self.assertEqual(summary["splits"]["train"]["image_count"], 2)
        self.assertEqual(summary["splits"]["val"]["image_count"], 2)
        self.assertEqual(
            validation_progress,
            [("train", 0, 2), ("train", 2, 2), ("val", 0, 2), ("val", 2, 2)],
        )

        metadata = json.loads((dataset / "metadata.json").read_text(encoding="utf-8"))
        self.assertEqual(metadata["square_size_pixels"], 256)
        self.assertEqual(metadata["city_count"], 2)
        self.assertEqual(metadata["class_names"], ["target"])
        self.assertEqual(summary["class_count"], 1)
        self.assertEqual((dataset.parent / "LATEST.txt").read_text(encoding="utf-8").strip(), dataset.name)
        data_yaml = yaml.safe_load((dataset / "data.yaml").read_text(encoding="utf-8"))
        self.assertNotIn("path", data_yaml)

        for label_path in (dataset / "labels").rglob("*.txt"):
            lines = label_path.read_text(encoding="utf-8").splitlines()
            self.assertEqual(len(lines), 1)
            for line in lines:
                _, _, _, normalized_width, normalized_height = map(float, line.split())
                self.assertAlmostEqual(normalized_width * 300, 256, places=4)
                self.assertAlmostEqual(normalized_height * 500, 256, places=4)

    def test_class_ids_are_stable_and_written_to_yolo_labels(self) -> None:
        second = Image.new("RGBA", (70, 50), (0, 0, 0, 0))
        ImageDraw.Draw(second).rectangle((5, 5, 65, 45), fill=(230, 40, 40, 255))
        second.save(self.root / "data" / "source" / "targets" / "Large_Red.png")

        config = load_config(self.root / "config.yaml")
        targets, _ = discover_targets(config)
        class_names, class_ids = build_class_registry(targets)
        self.assertEqual(class_names, ["Large_Red", "target"])
        self.assertEqual(class_ids["Large_Red.png"], 0)
        self.assertEqual(class_ids["target.png"], 1)

        box = Box(
            x=10,
            y=20,
            size=80,
            target_name="target.png",
            class_id=1,
            class_name="target",
        )
        self.assertTrue(yolo_label(box, 300, 500).startswith("1 "))

    def test_parallel_generation_matches_single_worker_and_reports_per_image(self) -> None:
        config = load_config(self.root / "config.yaml")
        config["generation"]["progress_interval"] = 999
        config["generation"]["square_size_pixels"] = 80
        serial = generate_dataset(config, composites_override=3, parallel_workers_override=1)
        messages: list[str] = []
        callback_threads: list[int] = []

        def report(message: str) -> None:
            messages.append(message)
            callback_threads.append(threading.get_ident())

        parallel = generate_dataset(
            config,
            composites_override=3,
            parallel_workers_override=2,
            callback=report,
        )
        self.assertEqual(
            [message for message in messages if message.startswith("合成進度：")],
            [f"合成進度：{count}/12" for count in (2, 4, 6, 8, 10, 12)],
        )
        self.assertIn("合成速度：2 個平行工作；每 2 張回報進度。", messages)
        self.assertEqual(set(callback_threads), {threading.get_ident()})
        for folder in ("images", "labels", "previews"):
            original_files = sorted(path.relative_to(serial) for path in (serial / folder).rglob("*") if path.is_file())
            parallel_files = sorted(path.relative_to(parallel) for path in (parallel / folder).rglob("*") if path.is_file())
            self.assertEqual(original_files, parallel_files)
            for relative_path in original_files:
                self.assertEqual((serial / relative_path).read_bytes(), (parallel / relative_path).read_bytes())
        self.assertEqual((serial / "annotations.csv").read_bytes(), (parallel / "annotations.csv").read_bytes())
        metadata = json.loads((parallel / "metadata.json").read_text(encoding="utf-8"))
        self.assertEqual(metadata["generated_image_count"], 12)
        self.assertEqual(metadata["generation_config"]["parallel_workers"], 2)
        self.assertEqual(metadata["generation_config"]["progress_interval"], 2)

    def test_rejects_invalid_speed_settings(self) -> None:
        for value in (0, -1, 1.5, True, "bad", float("inf")):
            with self.subTest(value=value):
                config = load_config(self.root / "config.yaml")
                config["generation"]["parallel_workers"] = value
                with self.assertRaisesRegex(ValueError, "parallel_workers"):
                    validate_generation_config(config)

    def test_large_parallel_count_is_not_capped_and_final_progress_is_reported(self) -> None:
        config = load_config(self.root / "config.yaml")
        messages = []
        dataset = generate_dataset(config, parallel_workers_override=1001, callback=messages.append)
        metadata = json.loads((dataset / "metadata.json").read_text(encoding="utf-8"))
        self.assertEqual(metadata["generation_config"]["parallel_workers"], 1001)
        self.assertEqual(metadata["generation_config"]["progress_interval"], 1001)
        self.assertEqual([m for m in messages if m.startswith("合成進度：")], ["合成進度：4/4"])

    def test_cancel_during_parallel_generation_does_not_publish_dataset(self) -> None:
        def cancel_on_progress(message: str) -> None:
            if message.startswith("合成進度："):
                raise RuntimeError("test cancelled")

        config = load_config(self.root / "config.yaml")
        with self.assertRaisesRegex(RuntimeError, "test cancelled"):
            generate_dataset(
                config,
                composites_override=20,
                parallel_workers_override=2,
                callback=cancel_on_progress,
            )
        self.assertFalse((self.root / "data/generated/LATEST.txt").exists())
        self.assertFalse(list((self.root / "data/generated").glob("dataset_*")))

    def test_speed_cli_options(self) -> None:
        args = build_parser().parse_args(["--parallel-workers", "1001"])
        self.assertEqual(args.parallel_workers, 1001)

    def test_single_background_reuses_train_split_for_validation(self) -> None:
        solo_root = self.root / "solo_background" / "only_city"
        solo_root.mkdir(parents=True)
        Image.new("RGB", (300, 500), (70, 90, 110)).save(solo_root / "only.png")

        config_path = self.root / "config.yaml"
        config_data = yaml.safe_load(config_path.read_text(encoding="utf-8"))
        config_data["paths"]["backgrounds"] = "solo_background"
        config_path.write_text(yaml.safe_dump(config_data, sort_keys=False), encoding="utf-8")

        dataset = generate_dataset(load_config(config_path))
        summary = validate_dataset(dataset)
        self.assertEqual(summary["splits"]["train"]["image_count"], 1)
        self.assertEqual(summary["splits"]["val"]["image_count"], 1)

    def test_generates_from_selected_backgrounds_and_targets_only(self) -> None:
        second = Image.new("RGBA", (70, 50), (0, 0, 0, 0))
        ImageDraw.Draw(second).rectangle((5, 5, 65, 45), fill=(230, 40, 40, 255))
        second.save(self.root / "data" / "source" / "targets" / "other.png")

        config = load_config(self.root / "config.yaml")
        dataset = generate_dataset(
            config,
            selected_backgrounds=["city_a/bg_0.png"],
            selected_targets=["target.png"],
        )
        metadata = json.loads((dataset / "metadata.json").read_text(encoding="utf-8"))
        self.assertEqual(metadata["background_count"], 1)
        self.assertEqual(metadata["target_count"], 1)
        self.assertEqual(metadata["class_names"], ["target"])
        self.assertEqual(
            metadata["source_selection"],
            {
                "backgrounds": ["city_a/bg_0.png"],
                "targets": ["target.png"],
            },
        )

        with self.assertRaisesRegex(ValueError, "找不到選取的背景"):
            generate_dataset(config, selected_backgrounds=["missing.png"])


if __name__ == "__main__":
    unittest.main()
