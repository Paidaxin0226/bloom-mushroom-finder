from __future__ import annotations

import logging
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from bloompilot.diagnostics import configure_diagnostics
from bloompilot.geo_recognition import GeoRecognitionWaypoint, run_geographic_recognition_sequence
from bloompilot.phone_automation import ScanPoint, ScanSummary
from test_geo_recognition_sequence import FakeController, FakeMemoryWatchdog


class UsbRecoveryTests(unittest.TestCase):
    def test_recovered_connection_restores_location_before_resuming_same_grid(self) -> None:
        controller = FakeController()
        controller.connection_generation = 1
        model = Mock()
        model.names = {0: "target"}
        watchdog = Mock(spec=FakeMemoryWatchdog)
        watchdog.before_capture.return_value = False
        watchdog.prepare_for_scan.return_value = True
        recovery_count = 0

        def recover_map() -> bool:
            nonlocal recovery_count
            recovery_count += 1
            if recovery_count == 1:
                controller.connected = False
            return True

        watchdog.recover_after_usb_service_restart.side_effect = recover_map
        watchdog.restart_count = 0
        watchdog.poll_seconds = 10
        watchdog.pressure_samples = 2

        def scan(*_args, **kwargs):
            # UI polling reconnected the same controller, but LocWarp lost
            # the location when its process was killed.
            controller.connection_generation += 1
            controller.current = {"lat": 0.0, "lng": 0.0}
            point = ScanPoint(ordinal=1, direction="grid", step=1, grid_x=1, grid_y=0)
            self.assertTrue(kwargs["before_capture"](point))
            self.assertEqual(controller.current, {"lat": 25.1, "lng": 121.1})
            self.assertFalse(kwargs["before_capture"](point))
            # The scan uses True to replay the grid after map recreation.
            return ScanSummary(captured=1, completed=True, detection_count=0, stop_reason="completed")

        with tempfile.TemporaryDirectory(dir=Path.cwd()) as temp:
            root = Path(temp)
            model_path = root / "model.pt"
            model_path.write_bytes(b"test")
            with (
                patch("torch.cuda.is_available", return_value=False),
                patch("ultralytics.YOLO", return_value=model),
                patch("bloompilot.geo_recognition.AdbClient"),
                patch("bloompilot.geo_recognition.PikminMemoryWatchdog", return_value=watchdog),
                patch("bloompilot.geo_recognition.run_rectangular_scan", side_effect=scan),
            ):
                _, summary = run_geographic_recognition_sequence(
                    controller=controller, adb=root / "adb", serial="phone", model_path=model_path,
                    waypoints=[GeoRecognitionWaypoint("first", "test", 25.1, 121.1, 0, 0, 0, 100)],
                    settle_seconds=0, confidence=0.45, image_size=32, output_root=root / "output",
                )
        self.assertTrue(summary.completed)
        watchdog.prepare_for_scan.assert_called_once_with(force_restart=True)
        self.assertEqual(watchdog.recover_after_usb_service_restart.call_count, 2)
        self.assertTrue(controller.connected)
        self.assertEqual(controller.connection_generation, 3)

    def test_diagnostics_survive_in_a_file_and_setup_does_not_duplicate_messages(self) -> None:
        logger = logging.getLogger("bloompilot")
        previous_handlers = list(logger.handlers)
        previous_level = logger.level
        try:
            with tempfile.TemporaryDirectory(dir=Path.cwd()) as temp:
                path = configure_diagnostics(Path(temp))
                self.assertEqual(configure_diagnostics(Path(temp)), path)
                logger.info("USB recovery confirmed")
                text = path.read_text(encoding="utf-8")
                self.assertEqual(text.count("USB recovery confirmed"), 1)
                for handler in list(logger.handlers):
                    if handler not in previous_handlers:
                        logger.removeHandler(handler)
                        handler.close()
        finally:
            for handler in list(logger.handlers):
                if handler not in previous_handlers:
                    logger.removeHandler(handler)
                    handler.close()
            logger.setLevel(previous_level)
