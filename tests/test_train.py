from __future__ import annotations

import tempfile
import json
import unittest
from pathlib import Path
from unittest.mock import patch

import yaml

from bloompilot.artifacts import TRAINING_MANIFEST

from bloompilot.train import (
    add_training_callbacks,
    choose_resource_profile,
    effective_workers,
    find_incomplete_run,
    parse_batch,
    parse_cache,
    generated_dataset_summary,
    list_generated_datasets,
    resolve_dataset,
    main,
)


class TrainRecoveryTests(unittest.TestCase):
    def test_generated_source_selection_and_cli_do_not_scan_images_or_labels(self) -> None:
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as temporary:
            root = Path(temporary).resolve()
            generated = root / "data/generated"
            config = {"_project_root": str(root), "paths": {"generated_root": "data/generated"}}
            for name in ("dataset_old", "dataset_new", ".dataset_partial.building"):
                folder = generated / name
                folder.mkdir(parents=True)
                (folder / "data.yaml").write_text("names: {0: target}\n", encoding="utf-8")
                (folder / "metadata.json").write_text(json.dumps({
                    "class_count": 1, "class_names": ["target"], "generated_image_count": 20000,
                }), encoding="utf-8")
            incomplete = generated / "dataset_incomplete"
            incomplete.mkdir()
            (incomplete / "data.yaml").write_text("names: {}", encoding="utf-8")
            (generated / "LATEST.txt").write_text("dataset_old\n", encoding="utf-8")
            with patch("bloompilot.train.Image.open", side_effect=AssertionError("must not open images")):
                choices = list_generated_datasets(config)
                self.assertEqual({item["name"] for item in choices}, {"dataset_old", "dataset_new"})
                self.assertEqual(resolve_dataset(config, None), generated / "dataset_old")
                self.assertEqual(resolve_dataset(config, generated / "dataset_new/data.yaml"), generated / "dataset_new")
                self.assertEqual(generated_dataset_summary(generated / "dataset_old")["image_count"], 20000)
                with self.assertRaisesRegex(ValueError, "專案"):
                    resolve_dataset(config, root / "external")
                (generated / "LATEST.txt").write_text("../outside", encoding="utf-8")
                self.assertIn(resolve_dataset(config, None), [generated / "dataset_old", generated / "dataset_new"])
                with (
                    patch("sys.argv", ["train", "--dry-run"]),
                    patch("bloompilot.train.load_config", return_value=config),
                    patch("bloompilot.train.describe_training_plan", return_value={}),
                    patch("bloompilot.train.validate_dataset") as validate,
                    patch("bloompilot.train.train") as train,
                    patch("builtins.print"),
                ):
                    self.assertEqual(main(), 0)
                validate.assert_not_called()
                train.assert_not_called()

    def test_registers_control_callbacks_on_model(self) -> None:
        registered: list[tuple[str, object]] = []

        class FakeModel:
            def add_callback(self, event: str, callback: object) -> None:
                registered.append((event, callback))

        callback = lambda trainer: None
        add_training_callbacks(FakeModel(), {"on_model_save": callback})
        self.assertEqual(registered, [("on_model_save", callback)])

    def test_windows_allows_four_workers_and_caps_larger_values(self) -> None:
        with patch("bloompilot.train.sys.platform", "win32"):
            self.assertEqual(effective_workers({"workers": 4}), 4)
            self.assertEqual(effective_workers({"workers": 12}), 4)
            self.assertEqual(effective_workers({"workers": 0}), 0)

    def test_cache_setting_preserves_disk_mode(self) -> None:
        self.assertEqual(parse_cache("disk"), "disk")
        self.assertEqual(parse_cache("ram"), "ram")
        self.assertFalse(parse_cache(False))

    def test_fractional_batch_target_is_not_truncated(self) -> None:
        self.assertEqual(parse_batch(0.90), 0.90)
        self.assertEqual(parse_batch("0.90"), 0.90)

    def test_resource_profile_keeps_ten_percent_buffer(self) -> None:
        gib = 1024**3
        profile = choose_resource_profile(
            {
                "batch": 3,
                "workers": 4,
                "resource_reserve_fraction": 0.10,
                "minimum_gpu_free_gib": 4.0,
                "training_process_ram_gib": 3.0,
                "ram_per_worker_gib": 1.0,
            },
            gpu_free_bytes=int(7.6 * gib),
            gpu_total_bytes=8 * gib,
            ram_available_bytes=int(11 * gib),
            ram_total_bytes=32 * gib,
            cpu_count=20,
        )
        self.assertEqual(profile["batch"], 2)
        self.assertEqual(profile["workers"], 4)
        self.assertEqual(profile["reserve_fraction"], 0.10)

    def test_resource_profile_scales_down_when_apps_use_resources(self) -> None:
        gib = 1024**3
        profile = choose_resource_profile(
            {
                "batch": 3,
                "workers": 4,
                "resource_reserve_fraction": 0.10,
                "minimum_gpu_free_gib": 4.0,
                "training_process_ram_gib": 3.0,
                "ram_per_worker_gib": 1.0,
            },
            gpu_free_bytes=int(5.0 * gib),
            gpu_total_bytes=8 * gib,
            ram_available_bytes=int(8 * gib),
            ram_total_bytes=32 * gib,
            cpu_count=20,
        )
        self.assertEqual(profile["batch"], 1)
        self.assertEqual(profile["workers"], 1)

    def test_finds_latest_incomplete_run_for_current_dataset(self) -> None:
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as temporary:
            root = Path(temporary).resolve()
            dataset = root / "dataset"
            dataset.mkdir()
            (dataset / "data.yaml").write_text("names: {0: target}\n", encoding="utf-8")

            run = root / "outputs" / "training" / "run_a"
            weights = run / "weights"
            weights.mkdir(parents=True)
            (weights / "last.pt").write_bytes(b"checkpoint")
            (run / "args.yaml").write_text(
                yaml.safe_dump({"data": str(dataset / "data.yaml"), "epochs": 100}),
                encoding="utf-8",
            )
            (run / "results.csv").write_text(
                "epoch,time\n1,10\n20,200\n", encoding="utf-8"
            )

            config = {
                "_project_root": str(root),
                "paths": {"training_runs": "outputs/training"},
            }
            checkpoint, completed, total = find_incomplete_run(config, dataset) or (None, 0, 0)
            self.assertEqual(checkpoint, weights / "last.pt")
            self.assertEqual((completed, total), (20, 100))

            (run / TRAINING_MANIFEST).write_text(
                '{"status": "paused", "dataset": "dataset"}', encoding="utf-8"
            )
            (run / "args.yaml").write_text(
                yaml.safe_dump({"data": "Z:/stale/data.yaml", "epochs": 100}),
                encoding="utf-8",
            )
            self.assertEqual(find_incomplete_run(config, dataset)[0], weights / "last.pt")  # type: ignore[index]

            (run / TRAINING_MANIFEST).write_text(
                '{"status": "completed", "dataset": "dataset"}', encoding="utf-8"
            )
            self.assertIsNone(find_incomplete_run(config, dataset))


if __name__ == "__main__":
    unittest.main()
