from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from bloompilot.artifacts import ArtifactManager, read_pointer
from bloompilot.training_retention import background_snapshot, finalize_training, publish_completed_model
from bloompilot.monthly_route import BACKGROUND_TOWNS, build_monthly_background_route
from bloompilot.monthly_workflow import MonthlyWorkflowStore
from bloompilot.web.runtime import AppRuntime


class TrainingRetentionTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.config = {"_project_root": str(self.root)}
        self.dataset = self.root / "data/generated/sample"
        self.dataset.mkdir(parents=True)
        (self.dataset / "data.yaml").write_text("names: [Blue]")
        self.background = self.root / "data/source/backgrounds/taipei/one.png"
        self.background.parent.mkdir(parents=True)
        self.background.write_bytes(b"original background")
        self.snapshot = background_snapshot([self.background], self.root)
        (self.dataset / "metadata.json").write_text(json.dumps({"background_snapshot": self.snapshot}))
        (self.dataset.parent / "LATEST.txt").write_text("sample")
        self.run = self.root / "outputs/training/run"
        (self.run / "weights").mkdir(parents=True)
        (self.run / "weights/best.pt").write_bytes(b"completed weights")
        self.metadata = {"status": "completed", "dataset": str(self.dataset)}
        self.save_metadata()
        loader = patch("ultralytics.YOLO", return_value=SimpleNamespace(names={0: "Blue"}))
        self.loader = loader.start()
        self.addCleanup(loader.stop)

    def save_metadata(self):
        (self.run / ".artifact.json").write_text(json.dumps(self.metadata))

    def test_success_preserves_verified_model_and_removes_consumed_inputs(self):
        result = finalize_training(self.config, self.run, self.dataset)
        model = Path(result["model"])
        self.assertEqual(model.read_bytes(), b"completed weights")
        self.assertFalse(self.dataset.exists())
        self.assertFalse(self.background.exists())
        self.assertFalse((self.dataset.parent / "LATEST.txt").exists())
        published = self.root / "models/published"
        self.assertEqual(read_pointer(published / "DEFAULT.txt", published), model)
        self.assertEqual(read_pointer(published / "RESIDENT.txt", published), model)
        # A clone has no private settings, run directories or resident alias.
        with patch("bloompilot.web.runtime.load_studio_settings", return_value={}):
            manager = ArtifactManager(self.config)
            self.assertEqual(AppRuntime._trained_model_paths(manager), [model])
            self.assertEqual(AppRuntime._stored_default_model(manager, [model]), model)
        store = MonthlyWorkflowStore(project_root=self.root, workspace_root=self.root / "data/monthly_activity", state_path=self.root / "state.json", resident_root=self.root / "models/resident")
        self.assertEqual(store.resident_model(), model)

    def test_invalid_or_unfinished_model_preserves_all_inputs(self):
        self.loader.side_effect = ValueError("invalid weight")
        with self.assertRaises(ValueError):
            finalize_training(self.config, self.run, self.dataset)
        self.assertTrue(self.dataset.exists())
        self.assertTrue(self.background.exists())
        self.metadata["status"] = "paused"
        self.save_metadata()
        with self.assertRaises(ValueError):
            finalize_training(self.config, self.run, self.dataset)
        self.assertTrue(self.background.exists())

    def test_monthly_stage_defers_cleanup_until_entire_pipeline_finishes(self):
        (self.dataset / "metadata.json").write_text(json.dumps({"monthly_workflow": {"cycle": "2026-09", "role": "resident"}, "background_snapshot": self.snapshot}))
        result = finalize_training(self.config, self.run, self.dataset)
        self.assertEqual(result["deferred"], "monthly_pipeline")
        self.assertFalse((self.root / "models/published/DEFAULT.txt").exists())
        self.assertTrue(self.dataset.exists())
        self.assertTrue(self.background.exists())

    def test_shared_background_is_retained(self):
        other = self.dataset.parent / "other"
        other.mkdir()
        (other / "metadata.json").write_text(json.dumps({"background_snapshot": self.snapshot}))
        finalize_training(self.config, self.run, self.dataset)
        self.assertTrue(self.background.exists())

    def test_recaptured_image_is_retained(self):
        self.background.write_bytes(b"new frame")
        finalize_training(self.config, self.run, self.dataset)
        self.assertEqual(self.background.read_bytes(), b"new frame")

    def test_incomplete_run_keeps_shared_dataset(self):
        other = self.run.parent / "paused"
        other.mkdir()
        (other / ".artifact.json").write_text(json.dumps({"status": "paused", "dataset": str(self.dataset)}))
        result = finalize_training(self.config, self.run, self.dataset)
        self.assertEqual(result["retained"], "dataset_needed_by_incomplete_run")
        self.assertTrue(self.dataset.exists())
        self.assertTrue(self.background.exists())

    def test_published_model_cannot_be_overwritten(self):
        model = publish_completed_model(self.config, self.run)
        (self.run / "weights/best.pt").write_bytes(b"different weights")
        with self.assertRaises(ValueError):
            finalize_training(self.config, self.run, self.dataset)
        self.assertEqual(model.read_bytes(), b"completed weights")
        self.assertTrue(self.dataset.exists())

    def test_cleanup_cannot_delete_unrelated_dataset(self):
        other = self.dataset.parent / "unrelated"
        other.mkdir()
        with self.assertRaises(ValueError):
            finalize_training(self.config, self.run, other)
        self.assertTrue(other.exists())

    def test_empty_background_directory_still_has_capture_route(self):
        route = build_monthly_background_route(self.root / "empty")
        self.assertEqual(len(route["sequence"]["waypoints"]), len(BACKGROUND_TOWNS))

    def test_monthly_cleanup_refuses_unfinished_second_stage(self):
        store = MonthlyWorkflowStore(project_root=self.root, workspace_root=self.root / "data/monthly_activity", state_path=self.root / "state.json", resident_root=self.root / "models/resident")
        store.update_manifest("2026-09", resident_training_run=str(self.run))
        with self.assertRaises(ValueError):
            store.cleanup_completed_cycle(self.config, "2026-09", [self.dataset])
        self.assertTrue(self.dataset.exists())
        self.assertTrue(self.background.exists())

    def test_manifest_cannot_erase_target_or_external_background(self):
        from bloompilot.training_retention import remove_backgrounds
        target = self.root / "data/source/targets/Blue.png"
        target.parent.mkdir(parents=True)
        target.write_bytes(b"keep sprite")
        snapshots = background_snapshot([target], self.root)
        result = remove_backgrounds(self.config, snapshots, excluding=set())
        self.assertEqual(len(result["retained"]), 1)
        self.assertTrue(target.exists())
