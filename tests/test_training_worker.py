from __future__ import annotations

import errno
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from bloompilot.training_control import TrainingControl
from bloompilot.training_worker import is_dataloader_worker_failure, main


class TrainingWorkerFallbackTests(unittest.TestCase):
    def test_each_saved_checkpoint_publishes_recoverable_progress(self) -> None:
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as temporary:
            root = Path(temporary)
            control = TrainingControl(root / "control")
            args = SimpleNamespace(config=root / "config.yaml", dataset=root, control_dir=control.directory)

            def train(_config, _dataset, **kwargs):
                checkpoint = root / "weights/last.pt"
                checkpoint.parent.mkdir()
                checkpoint.write_bytes(b"saved")
                kwargs["callbacks"]["on_model_save"](SimpleNamespace(epoch=4, epochs=100, last=checkpoint))
                status = control.read_status()
                self.assertEqual(status["completed_epoch"], 5)
                self.assertEqual(status["checkpoint"], str(checkpoint.resolve()))
                self.assertEqual(status["dataset"], str(root))
                self.assertEqual(status["state"], "running")
                return root

            with (
                patch("bloompilot.training_worker.build_parser", return_value=Mock(parse_args=lambda: args)),
                patch("bloompilot.training_worker.load_config", return_value={}),
                patch("bloompilot.training_worker.resolve_dataset", return_value=root),
                patch("bloompilot.training_worker.generated_dataset_summary", return_value={"class_count": 14}),
                patch("bloompilot.training_worker.describe_training_plan", return_value={
                    "resume_checkpoint": None, "completed_epochs": 0, "total_epochs": 100, "workers": 4,
                }),
                patch("bloompilot.training_worker.ArtifactManager"),
                patch("bloompilot.training_worker.train", side_effect=train),
                patch("builtins.print"),
            ):
                self.assertEqual(main(), 0)

    def test_locked_progress_file_does_not_prevent_training(self) -> None:
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as temporary:
            root = Path(temporary)
            control = TrainingControl(root / "control")
            args = SimpleNamespace(config=root / "config.yaml", dataset=root, control_dir=control.directory)

            original_replace = Path.replace

            def locked_progress(source, target):
                if json.loads(source.read_text(encoding="utf-8"))["state"] in {"starting", "running"}:
                    raise PermissionError(errno.EACCES, "locked")
                return original_replace(source, target)

            with (
                patch("bloompilot.training_worker.build_parser", return_value=Mock(parse_args=lambda: args)),
                patch("bloompilot.training_worker.load_config", return_value={}),
                patch("bloompilot.training_worker.resolve_dataset", return_value=root),
                patch("bloompilot.training_worker.generated_dataset_summary", return_value={"class_count": 13}),
                patch("bloompilot.train.validate_dataset", side_effect=AssertionError("must not validate")) as validate,
                patch.object(Path, "replace", locked_progress),
                patch("bloompilot.training_control.time.sleep"),
                self.assertLogs("bloompilot.training_control", level="WARNING"),
                patch("bloompilot.training_worker.describe_training_plan", return_value={
                    "resume_checkpoint": None, "completed_epochs": 0, "total_epochs": 100, "workers": 4,
                }),
                patch("bloompilot.training_worker.ArtifactManager"),
                patch("bloompilot.training_worker.train", return_value=root) as train,
                patch("bloompilot.training_worker.completed_epochs", return_value=100),
                patch("builtins.print") as output,
            ):
                self.assertEqual(main(), 0)
            train.assert_called_once()
            self.assertEqual(control.read_status()["state"], "completed")
            validate.assert_not_called()
            self.assertTrue(any("略過圖片與標註檢查" in str(call.args[0]) for call in output.call_args_list))

    def test_status_write_failure_does_not_hide_original_training_error(self) -> None:
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as temporary:
            root = Path(temporary)
            args = SimpleNamespace(config=root / "config.yaml", dataset=root, control_dir=root / "control")
            with (
                patch("bloompilot.training_worker.build_parser", return_value=Mock(parse_args=lambda: args)),
                patch("bloompilot.training_worker.load_config", side_effect=ValueError("invalid config")),
                patch.object(Path, "replace", side_effect=PermissionError(errno.EACCES, "locked")),
                patch("bloompilot.training_control.time.sleep"),
                patch("builtins.print") as output,
                patch("bloompilot.training_worker.traceback.print_exc") as traceback,
                self.assertLogs("bloompilot.training_control", level="WARNING"),
            ):
                self.assertEqual(main(), 1)
            self.assertTrue(any("invalid config" in str(call.args[0]) for call in output.call_args_list))
            traceback.assert_called_once()

    def test_detects_windows_dataloader_worker_exit(self) -> None:
        error = RuntimeError("DataLoader worker (pid(s) 13396) exited unexpectedly")
        self.assertTrue(is_dataloader_worker_failure(error))

    def test_does_not_mask_unrelated_training_errors(self) -> None:
        self.assertFalse(is_dataloader_worker_failure(RuntimeError("CUDA out of memory")))

    def test_missing_generation_summary_is_reported_before_training(self) -> None:
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as temporary:
            root = Path(temporary)
            control = TrainingControl(root / "control")
            args = SimpleNamespace(config=root / "config.yaml", dataset=root, control_dir=control.directory)

            def summary(dataset):
                self.assertEqual(control.read_status()["state"], "starting")
                raise FileNotFoundError("missing metadata.json")

            with (
                patch("bloompilot.training_worker.build_parser", return_value=Mock(parse_args=lambda: args)),
                patch("bloompilot.training_worker.load_config", return_value={}),
                patch("bloompilot.training_worker.resolve_dataset", return_value=root),
                patch("bloompilot.training_worker.generated_dataset_summary", side_effect=summary),
                patch("bloompilot.training_worker.train") as train,
                patch("builtins.print") as output,
                patch("bloompilot.training_worker.traceback.print_exc"),
            ):
                self.assertEqual(main(), 1)
            train.assert_not_called()
            self.assertEqual(control.read_status()["state"], "error")
            self.assertTrue(any("missing metadata.json" in str(call.args[0]) for call in output.call_args_list))


if __name__ == "__main__":
    unittest.main()
