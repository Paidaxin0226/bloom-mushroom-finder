from __future__ import annotations

import base64
import json
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from PIL import Image

from bloompilot.monthly_workflow import MonthlyWorkflowStore
from bloompilot.web.runtime import AppRuntime


class MonthlyTrainingResumeTests(unittest.TestCase):
    def setUp(self) -> None:
        publisher = patch("bloompilot.training_retention.publish_completed_model")
        publisher.start()
        self.addCleanup(publisher.stop)
        self.temporary = tempfile.TemporaryDirectory(dir=Path.cwd())
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.store = MonthlyWorkflowStore(
            project_root=self.root,
            workspace_root=self.root / "data/monthly_activity",
            state_path=self.root / "state.json",
            resident_root=self.root / "models/resident",
        )
        self.config = {
            "_project_root": str(self.root),
            "paths": {"targets": "targets", "backgrounds": "backgrounds"},
            "generation": {"preserve_original_target_pixels": True, "target_padding_pixels": 4},
        }
        self.context = SimpleNamespace(
            job_id="resume-test", stop_event=threading.Event(),
            message=Mock(), progress=Mock(), check_cancelled=Mock(),
        )
        backgrounds = self.store.backgrounds_dir("2026-09")
        backgrounds.mkdir(parents=True)
        Image.new("RGB", (300, 500)).save(backgrounds / "one.png")
        self.store.approve_backgrounds("2026-09")
        targets = self.root / "targets"
        targets.mkdir()
        image = Image.new("RGBA", (248, 184), (100, 80, 60, 255))
        image.putpixel((0, 0), (0, 0, 0, 0))
        image.save(targets / "Large_Blue.png")
        self.store.save_activity_target(
            "2026-09", "Event.png",
            base64.b64encode((targets / "Large_Blue.png").read_bytes()).decode(),
        )
        self.resident_dataset = self.dataset("resident", ["Large_Blue"])
        self.resident_run = self.create_run("resident", self.resident_dataset)
        self.store.set_resident_model(
            self.resident_run / "weights/best.pt", class_names=["Large_Blue"], cycle="2026-09",
        )
        self.store.update_manifest("2026-09", resident_training_run=str(self.resident_run))

    def dataset(self, role: str, classes: list[str]) -> Path:
        path = self.root / "data/generated" / role
        path.mkdir(parents=True, exist_ok=True)
        for name in ("images", "labels"):
            (path / name).mkdir(exist_ok=True)
        (path / "data.yaml").write_text("names: []", encoding="utf-8")
        (path / "metadata.json").write_text(json.dumps({"class_names": classes}), encoding="utf-8")
        self.store.annotate_dataset(path, cycle="2026-09", role=role)
        self.store.update_manifest("2026-09", **{f"{role}_dataset": str(path)})
        return path

    def create_run(self, role: str, dataset: Path, *, completed: bool = True) -> Path:
        path = self.root / "outputs/training" / role
        (path / "weights").mkdir(parents=True, exist_ok=True)
        (path / "weights/best.pt").write_bytes(role.encode())
        (path / ".artifact.json").write_text(json.dumps({
            "status": "completed" if completed else "paused",
            "completed_epochs": 39, "total_epochs": 100,
            "dataset": str(dataset), "monthly_workflow": {"cycle": "2026-09", "role": role},
        }), encoding="utf-8")
        return path

    def test_early_stopped_resident_is_reused_after_failed_retry_overwrites_dataset(self) -> None:
        self.store.update_manifest("2026-09", resident_dataset=str(self.root / "missing-retry"))
        self.assertEqual(self.store.completed_training_run(self.config, "2026-09", "resident"), self.resident_run)
        (self.resident_run / "weights/best.pt").unlink()
        self.assertIsNone(self.store.completed_training_run(self.config, "2026-09", "resident"))

    def test_retry_only_trains_event_and_reuses_its_dataset_for_checkpoint_resume(self) -> None:
        dataset = self.dataset("event", ["Event", "Large_Blue"])
        event_run = self.create_run("event", dataset, completed=False)
        (event_run / "weights/last.pt").write_bytes(b"checkpoint")
        runtime = AppRuntime()
        runtime.monthly_workflow = self.store
        with (
            patch.object(runtime, "config", return_value=self.config),
            patch.object(runtime.jobs, "start", side_effect=lambda _kind, runner: runner(self.context)),
            patch("bloompilot.web.runtime.generate_dataset") as generate,
            patch.object(runtime, "_run_training_process", return_value={
                "status": {"state": "completed", "checkpoint": str(event_run / "weights/best.pt")},
            }) as train,
            patch.object(runtime, "set_default_recognition_model"),
            patch.object(self.store, "cleanup_completed_cycle"),
            patch("bloompilot.web.runtime.ArtifactManager"),
        ):
            runtime.start_monthly_training({"cycle": "2026-09"})
        generate.assert_not_called()
        train.assert_called_once()
        self.assertEqual(train.call_args.args[1], dataset)
        self.assertEqual(train.call_args.kwargs["phase"], "本月活動菇模型")
        self.assertTrue(train.call_args.kwargs["allow_resume"])
        self.assertEqual(self.store.resident_model().read_bytes(), b"resident")

    def test_completed_event_is_recovered_before_stage_pointer_was_saved(self) -> None:
        dataset = self.dataset("event", ["Event", "Large_Blue"])
        run = self.create_run("event", dataset)
        artifact = json.loads((run / ".artifact.json").read_text())
        artifact.pop("monthly_workflow")
        (run / ".artifact.json").write_text(json.dumps(artifact))
        self.assertEqual(self.store.completed_training_run(self.config, "2026-09", "event"), run)
        self.store.update_manifest("2026-09", event_dataset=None)
        self.assertIsNone(self.store.completed_training_run(self.config, "2026-09", "event"))

    def test_new_background_review_does_not_reuse_old_resident_stage(self) -> None:
        self.store.update_manifest("2026-09", reviewed_at="2099-01-01T00:00:00+00:00")
        self.assertIsNone(self.store.reusable_dataset(self.config, "2026-09", "resident"))
        self.assertIsNone(self.store.completed_training_run(self.config, "2026-09", "resident"))

    def test_only_completed_monthly_models_are_deleted_after_replacements_exist(self) -> None:
        event_dataset = self.dataset("event", ["Event", "Large_Blue"])
        event_run = self.create_run("event", event_dataset)
        old_run = self.create_run("legacy", self.resident_dataset)
        unrelated = self.create_run("experiment", self.resident_dataset)
        metadata = json.loads((old_run / ".artifact.json").read_text())
        metadata["monthly_workflow"] = {"cycle": "2026-08", "role": "event"}
        (old_run / ".artifact.json").write_text(json.dumps(metadata))
        incomplete_run = self.create_run("incomplete", self.resident_dataset, completed=False)
        kept = [self.resident_run / "weights/best.pt", event_run / "weights/best.pt", self.store.resident_model()]
        with self.assertRaises(FileNotFoundError):
            self.store.cleanup_superseded_models(self.config, keep=[*kept, self.root / "missing.pt"])
        self.assertTrue((old_run / "weights/best.pt").exists())
        removed = self.store.cleanup_superseded_models(self.config, keep=kept)
        self.assertEqual(removed, [str(old_run / "weights/best.pt")])
        self.assertTrue(all(path.is_file() for path in kept))
        self.assertTrue((incomplete_run / "weights/best.pt").exists())
        self.assertFalse((old_run / "weights/best.pt").exists())
        self.assertTrue((unrelated / "weights/best.pt").exists())

    def test_oversized_event_is_rejected_before_any_training(self) -> None:
        self.config["generation"]["target_padding_pixels"] = 16
        runtime = AppRuntime()
        runtime.monthly_workflow = self.store
        with (
            patch.object(runtime, "config", return_value=self.config),
            patch.object(runtime.jobs, "start", side_effect=lambda _kind, runner: runner(self.context)),
            patch.object(runtime, "_run_training_process") as train,
            self.assertRaisesRegex(RuntimeError, "248×184px"),
        ):
            runtime.start_monthly_training({"cycle": "2026-09"})
        train.assert_not_called()

    def test_training_output_is_durable_and_final_lines_are_drained(self) -> None:
        runtime = AppRuntime()
        real_popen = subprocess.Popen
        control = self.root / "control"
        source = (
            "from bloompilot.training_control import TrainingControl; "
            "from pathlib import Path; "
            "import time; "
            f"TrainingControl(Path({str(control)!r})).write_status('completed', "
            f"dataset={str(self.resident_dataset)!r}, completed_epoch=39, total_epochs=100); "
            "print('final training output', flush=True); time.sleep(1.2)"
        )

        def launch(_command, **kwargs):
            self.assertNotEqual(kwargs["stdout"], subprocess.PIPE)
            child = real_popen([sys.executable, "-X", "utf8", "-c", source], **kwargs)
            # Match a launcher whose PID differs from the actual training worker.
            return SimpleNamespace(pid=-1, poll=child.poll, wait=child.wait)

        with (
            patch("bloompilot.web.runtime.TRAINING_CONTROL_DIR", control),
            patch("bloompilot.web.runtime.subprocess.Popen", side_effect=launch),
            patch.object(runtime.jobs, "sync_training_stop"),
        ):
            result = runtime._run_training_process(self.context, self.resident_dataset, require_completed=True)
        self.assertEqual(result["status"]["completed_epoch"], 39)
        self.assertTrue(any(call.args[:2] == (39, 100) for call in self.context.progress.call_args_list))
        self.assertIn("final training output", next(control.glob("*.log")).read_text())
        self.assertTrue(any("final training output" in call.args[0] for call in self.context.message.call_args_list))


if __name__ == "__main__":
    unittest.main()
