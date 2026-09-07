from __future__ import annotations

import errno
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from bloompilot.training_control import TrainingControl, TrainingControlSignal


class FakeTrainer:
    epoch = 20
    epochs = 100

    def __init__(self, checkpoint: Path) -> None:
        self.last = checkpoint


class TrainingControlTests(unittest.TestCase):
    def test_retries_locked_replace_without_exposing_partial_json(self) -> None:
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as temporary:
            control = TrainingControl(Path(temporary))
            control.write_status("starting")
            replace = Path.replace
            attempts = 0

            def locked_then_available(source, target):
                nonlocal attempts
                attempts += 1
                self.assertEqual(control.read_status()["state"], "starting")
                if attempts < 3:
                    raise PermissionError(errno.EACCES, "status.json is locked")
                return replace(source, target)

            with (
                patch.object(Path, "replace", autospec=True, side_effect=locked_then_available),
                patch("bloompilot.training_control.time.sleep") as sleep,
            ):
                self.assertTrue(control.write_status("validating", validated_images=5000))
            self.assertEqual(attempts, 3)
            self.assertEqual(sleep.call_count, 2)
            self.assertEqual(control.read_status()["validated_images"], 5000)
            self.assertEqual(list(control.directory.glob(".status.json.*.tmp")), [])

    def test_advisory_write_warns_once_preserves_previous_status_and_recovers(self) -> None:
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as temporary:
            control = TrainingControl(Path(temporary))
            control.write_status("starting")
            error = PermissionError(errno.EACCES, "status.json is locked")
            with (
                patch.object(Path, "replace", side_effect=error),
                patch("bloompilot.training_control.time.sleep"),
                self.assertLogs("bloompilot.training_control", level="WARNING") as logs,
            ):
                self.assertFalse(control.write_status("validating", required=False, validated_images=5000))
                self.assertFalse(control.write_status("validating", required=False, validated_images=5100))
            self.assertEqual(len(logs.output), 1)
            self.assertEqual(control.read_status()["state"], "starting")
            self.assertEqual(list(control.directory.glob(".status.json.*.tmp")), [])
            self.assertTrue(control.write_status("validating", required=False, validated_images=5200))
            self.assertEqual(control.read_status()["validated_images"], 5200)

    def test_required_checkpoint_status_is_not_silently_lost(self) -> None:
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as temporary:
            control = TrainingControl(Path(temporary))
            control.write_status("running")
            control.request("pause")
            with (
                patch.object(Path, "replace", side_effect=PermissionError(errno.EACCES, "locked")),
                patch("bloompilot.training_control.time.sleep"),
                self.assertRaises(PermissionError),
            ):
                control.checkpoint_callback(FakeTrainer(Path(temporary) / "last.pt"))
            self.assertEqual(control.read_status()["state"], "running")
            self.assertEqual(control.requested_action(), "pause")
            self.assertEqual(list(control.directory.glob(".status.json.*.tmp")), [])

    def test_unrelated_io_failure_is_not_retried_or_hidden(self) -> None:
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as temporary:
            control = TrainingControl(Path(temporary))
            with (
                patch.object(Path, "replace", side_effect=OSError(errno.ENOSPC, "disk full")) as replace,
                patch("bloompilot.training_control.time.sleep") as sleep,
                self.assertRaises(OSError) as raised,
            ):
                control.write_status("completed")
            self.assertEqual(raised.exception.errno, errno.ENOSPC)
            self.assertEqual(replace.call_count, 1)
            sleep.assert_not_called()
            self.assertEqual(list(control.directory.glob(".status.json.*.tmp")), [])

    @unittest.skipUnless(sys.platform == "win32", "requires Windows file sharing semantics")
    def test_real_windows_reader_lock_does_not_abort_progress(self) -> None:
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as temporary:
            control = TrainingControl(Path(temporary))
            control.write_status("starting")
            with (
                control.status_path.open("rb") as reader,
                patch("bloompilot.training_control.time.sleep"),
                self.assertLogs("bloompilot.training_control", level="WARNING"),
            ):
                self.assertFalse(control.write_status("validating", required=False, validated_images=5100))
                self.assertEqual(json.load(reader)["state"], "starting")
            self.assertTrue(control.write_status("validating", validated_images=5200))
            self.assertEqual(control.read_status()["validated_images"], 5200)

    def test_pause_is_raised_only_from_checkpoint_callback(self) -> None:
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as temporary:
            control = TrainingControl(Path(temporary))
            control.request("pause")

            with self.assertRaises(TrainingControlSignal) as raised:
                control.checkpoint_callback(FakeTrainer(Path(temporary) / "last.pt"))

            self.assertEqual(raised.exception.action, "pause")
            self.assertEqual(raised.exception.completed_epoch, 21)
            status = control.read_status()
            self.assertEqual(status["state"], "paused")
            self.assertEqual(status["completed_epoch"], 21)

    def test_stop_request_has_priority_and_can_be_cleared(self) -> None:
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as temporary:
            control = TrainingControl(Path(temporary))
            control.request("pause")
            control.request("stop")
            self.assertEqual(control.requested_action(), "stop")

            with self.assertRaises(TrainingControlSignal):
                control.checkpoint_callback(FakeTrainer(Path(temporary) / "last.pt"))
            self.assertEqual(control.read_status()["state"], "stopped")

            control.clear_requests()
            self.assertIsNone(control.requested_action())

    def test_final_epoch_completes_normally_even_if_pause_was_requested(self) -> None:
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as temporary:
            control = TrainingControl(Path(temporary))
            control.request("pause")
            trainer = FakeTrainer(Path(temporary) / "last.pt")
            trainer.epoch = 99

            control.checkpoint_callback(trainer)

            self.assertEqual(control.read_status(), {})

    def test_cancel_stop_before_checkpoint_allows_continued_training_and_new_stop(self) -> None:
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as temporary:
            control = TrainingControl(Path(temporary))
            trainer = FakeTrainer(Path(temporary) / "last.pt")
            control.request("stop")
            control.cancel_stop()
            control.checkpoint_callback(trainer)
            self.assertFalse(control.stop_accepted)
            control.request("stop")
            with self.assertRaises(TrainingControlSignal):
                control.checkpoint_callback(trainer)
            with self.assertRaisesRegex(RuntimeError, "已生效"):
                control.cancel_stop()
            control.clear_requests()
            self.assertFalse(control.stop_accepted)

    def test_cancel_between_action_read_and_checkpoint_claim_wins(self) -> None:
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as temporary:
            control = TrainingControl(Path(temporary))
            control.request("stop")
            original = control.requested_action

            def cancel_after_read():
                action = original()
                control.cancel_stop()
                return action

            with patch.object(control, "requested_action", side_effect=cancel_after_read):
                control.checkpoint_callback(FakeTrainer(Path(temporary) / "last.pt"))
            self.assertFalse(control.stop_accepted)
            self.assertEqual(control.read_status(), {})

    def test_claim_between_cancel_check_and_unlink_rejects_cancellation(self) -> None:
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as temporary:
            control = TrainingControl(Path(temporary))
            control.request("stop")
            original = Path.unlink

            def claim_first(path, *args, **kwargs):
                if path.name == "stop.request":
                    path.replace(path.with_name("stop.accepted"))
                return original(path, *args, **kwargs)

            with patch.object(Path, "unlink", claim_first):
                with self.assertRaisesRegex(RuntimeError, "已生效"):
                    control.cancel_stop()


if __name__ == "__main__":
    unittest.main()
