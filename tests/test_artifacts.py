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

    def test_model_classes_survive_dataset_cleanup_and_catalog_refresh(self) -> None:
        from bloompilot.web.runtime import _model_class_names
        from unittest.mock import patch

        metadata_path = self.dataset / "metadata.json"
        metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
        metadata["class_names"] = ["Event_2026_09", "Large_Blue"]
        metadata_path.write_text(json.dumps(metadata), encoding="utf-8")
        manager = ArtifactManager(self.config)
        manager.register_training_run(self.run, self.dataset, status="completed")
        metadata_path.unlink()
        (self.dataset / "data.yaml").unlink()
        manager.register_training_run(self.run, self.dataset, status="completed")
        with patch("bloompilot.web.runtime._checkpoint_class_names", side_effect=AssertionError("should use saved classes")):
            self.assertEqual(_model_class_names(self.run / "weights/best.pt", self.root), metadata["class_names"])

    def test_stale_checkpoint_progress_does_not_reopen_early_stopped_run(self) -> None:
        manager = ArtifactManager(self.config)
        manager.register_training_run(self.run, self.dataset, status="completed", completed_epochs=8, total_epochs=10)
        manager.control_root.mkdir(parents=True)
        (manager.control_root / "status.json").write_text(json.dumps({
            "state": "running", "checkpoint": str(self.run / "weights/last.pt"),
            "dataset": str(self.dataset), "completed_epoch": 8, "total_epochs": 10,
        }), encoding="utf-8")
        manager.reconcile_control_status()
        manifest = json.loads((self.run / ".artifact.json").read_text(encoding="utf-8"))
        self.assertEqual(manifest["status"], "completed")
        self.assertEqual(manifest["completed_epochs"], 8)

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

    def test_cache_cleanup_preserves_arrays_without_source_images(self) -> None:
        orphan = self.dataset / "images/train/orphan.npy"
        orphan.write_bytes(b"irreplaceable")
        manager = ArtifactManager(self.config)
        self.assertEqual(manager.clear_rebuildable_cache(apply=True)["file_count"], 0)
        self.assertTrue(orphan.exists())

    def test_cache_cleanup_refuses_active_training_but_allows_preview(self) -> None:
        cache = self.dataset / "images/train/one.npy"
        cache.write_bytes(b"cache")
        manager = ArtifactManager(self.config)
        manager.register_training_run(self.run, self.dataset, status="running")
        self.assertEqual(manager.clear_rebuildable_cache()["file_count"], 1)
        with self.assertRaisesRegex(RuntimeError, "訓練"):
            manager.clear_rebuildable_cache(apply=True)
        self.assertTrue(cache.exists())


if __name__ == "__main__":
    unittest.main()
