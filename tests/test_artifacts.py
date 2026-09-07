from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

import yaml

from bloompilot.artifacts import ArtifactManager, read_pointer, write_pointer


class ArtifactManagerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(dir=Path.cwd())
        self.root = Path(self.temporary.name).resolve()
        self.config = {
            "_project_root": str(self.root),
            "paths": {
                "generated_root": "data/generated",
                "training_runs": "outputs/training",
                "pretrained_models": "models/pretrained",
                "training_control": "runtime/training_control",
                "artifact_catalog": "runtime/artifacts/catalog.json",
            },
            "training": {"model": "models/pretrained/base.pt"},
        }
        self.dataset = self.root / "data" / "generated" / "dataset_a"
        (self.dataset / "images" / "train").mkdir(parents=True)
        (self.dataset / "labels" / "train").mkdir(parents=True)
        (self.dataset / "images" / "train" / "one.png").write_bytes(b"image")
        (self.dataset / "labels" / "train" / "one.txt").write_text(
            "0 0.5 0.5 0.2 0.2\n", encoding="utf-8"
        )
        (self.dataset / "metadata.json").write_text(
            json.dumps(
                {
                    "created_at": "2026-08-22T10:00:00+08:00",
                    "generated_image_count": 1,
                    "instance_count": 1,
                    "class_count": 1,
                }
            ),
            encoding="utf-8",
        )
        (self.dataset / "data.yaml").write_text(
            yaml.safe_dump({"train": "images/train", "val": "images/train", "names": {0: "target"}}),
            encoding="utf-8",
        )
        write_pointer(self.dataset.parent / "LATEST.txt", self.dataset, self.dataset.parent)

        self.run = self.root / "outputs" / "training" / "run_a"
        (self.run / "weights").mkdir(parents=True)
        (self.run / "weights" / "best.pt").write_bytes(b"best")
        (self.run / "weights" / "last.pt").write_bytes(b"last")
        (self.run / "args.yaml").write_text(
            yaml.safe_dump({"data": str(self.dataset / "data.yaml"), "epochs": 10}),
            encoding="utf-8",
        )
        (self.run / "results.csv").write_text("epoch,time\n1,1\n8,8\n", encoding="utf-8")

        pretrained = self.root / "models" / "pretrained" / "base.pt"
        pretrained.parent.mkdir(parents=True)
        pretrained.write_bytes(b"base")

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_catalog_tracks_relationships_without_latest_model_pointer(self) -> None:
        manager = ArtifactManager(self.config)
        manager.register_training_run(
            self.run,
            self.dataset,
            status="completed",
            completed_epochs=8,
            total_epochs=10,
        )
        catalog = manager.refresh_catalog()
        artifacts = {item["id"]: item for item in catalog["artifacts"]}

        self.assertTrue(artifacts["dataset:dataset_a"]["latest"])
        self.assertTrue(artifacts["dataset:dataset_a"]["protected"])
        self.assertEqual(artifacts["run:run_a"]["status"], "completed")
        self.assertTrue(artifacts["run:run_a"]["latest"])
        self.assertFalse(artifacts["model:trained:run_a:best"]["latest"])
        self.assertEqual(catalog["summary"]["datasets"], 1)
        self.assertEqual(catalog["summary"]["training_runs"], 1)
        self.assertEqual(catalog["summary"]["models"], 3)

        self.assertFalse((manager.training_root / "LATEST_MODEL.txt").exists())
        self.assertEqual(manager.verify(manager.artifact("dataset:dataset_a")), [])

    def test_delete_completed_run_removes_directory_and_pointer(self) -> None:
        manager = ArtifactManager(self.config)
        manager.register_training_run(
            self.run,
            self.dataset,
            status="completed",
            completed_epochs=10,
            total_epochs=10,
        )

        catalog = manager.delete("run:run_a")

        self.assertFalse(self.run.exists())
        self.assertIsNone(read_pointer(manager.training_root / "LATEST_RUN.txt", manager.training_root))
        self.assertFalse(any(item["id"] == "run:run_a" for item in catalog["artifacts"]))

    def test_delete_rejects_referenced_dataset_and_pretrained_model(self) -> None:
        manager = ArtifactManager(self.config)
        manager.register_training_run(
            self.run,
            self.dataset,
            status="completed",
            completed_epochs=10,
            total_epochs=10,
        )

        with self.assertRaisesRegex(ValueError, "引用"):
            manager.delete("dataset:dataset_a")
        with self.assertRaisesRegex(ValueError, "受保護"):
            manager.delete("model:pretrained:base.pt")

    def test_delete_trained_model_removes_only_selected_weight(self) -> None:
        manager = ArtifactManager(self.config)
        manager.register_training_run(
            self.run,
            self.dataset,
            status="completed",
            completed_epochs=10,
            total_epochs=10,
        )

        manager.delete("model:trained:run_a:best")

        self.assertFalse((self.run / "weights" / "best.pt").exists())
        self.assertTrue((self.run / "weights" / "last.pt").exists())

    def test_cache_cleanup_is_preview_first(self) -> None:
        cache = self.dataset / "images" / "train" / "one.npy"
        cache.write_bytes(b"cache")
        manager = ArtifactManager(self.config)
        preview = manager.clear_rebuildable_cache()
        self.assertEqual(preview["file_count"], 1)
        self.assertFalse(preview["applied"])
        self.assertTrue(cache.exists())

        applied = manager.clear_rebuildable_cache(apply=True)
        self.assertTrue(applied["applied"])
        self.assertFalse(cache.exists())

    def test_model_pruning_preserves_configured_base_and_each_run_best(self) -> None:
        manager = ArtifactManager(self.config)
        manager.register_training_run(
            self.run,
            self.dataset,
            status="completed",
            completed_epochs=8,
            total_epochs=10,
        )
        extra_base = self.root / "models" / "pretrained" / "extra.pt"
        periodic = self.run / "weights" / "epoch5.pt"
        extra_base.write_bytes(b"extra")
        periodic.write_bytes(b"periodic")

        preview = manager.prune_model_files()
        self.assertEqual(
            set(preview["paths"]),
            {
                "models/pretrained/extra.pt",
                "outputs/training/run_a/weights/epoch5.pt",
                "outputs/training/run_a/weights/last.pt",
            },
        )
        self.assertTrue((self.root / "models" / "pretrained" / "base.pt").is_file())
        self.assertTrue((self.run / "weights" / "best.pt").is_file())

        manager.prune_model_files(apply=True)
        self.assertFalse(extra_base.exists())
        self.assertFalse(periodic.exists())
        self.assertFalse((self.run / "weights" / "last.pt").exists())


if __name__ == "__main__":
    unittest.main()
