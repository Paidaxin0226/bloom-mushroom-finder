from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import cv2
import numpy as np

from bloompilot.geo_recognition import GeoRecognitionWaypoint, run_geographic_recognition_sequence
from bloompilot.mushroom_coordinate import PreciseCoordinateError
from bloompilot.phone_automation import ScanPoint, ScanSummary, run_recognition_scan
from test_geo_recognition_sequence import FakeController


class PreciseScanIntegrationTests(unittest.TestCase):
    def test_both_scan_modes_wire_reidentification_recovery_and_per_grid_failure_cache(self) -> None:
        for mode in ("automatic", "sequence"):
            with self.subTest(mode=mode), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                model_path = root / "model.pt"
                model_path.write_bytes(b"model")
                frame = np.full((240, 120, 3), 80, dtype=np.uint8)
                payload = cv2.imencode(".png", frame)[1].tobytes()
                target = {"class_name": "target", "class_id": 0, "confidence": 0.9, "xyxy": [35, 80, 70, 140]}
                model = Mock()
                model.names = {0: "target", 1: "other"}
                model.predict.return_value = [object()]
                watchdog = SimpleNamespace(
                    prepare_for_scan=lambda **_kw: True, before_capture=lambda _point: False,
                    restart_count=0, poll_seconds=10, pressure_samples=2,
                )
                captured = []
                resolved = []

                def scanner(_client: object, **options: object) -> ScanSummary:
                    # A second photograph in the same grid must keep the failed
                    # hit without interacting again; a new grid may try again.
                    for ordinal in (0, 0, 1):
                        options["on_capture"](ScanPoint(ordinal, "grid", ordinal, ordinal, 0), payload)
                    return ScanSummary(2, 3, True, "completed")

                def resolve(_target: object, **options: object) -> dict[str, object]:
                    np.testing.assert_array_equal(options["reference_image"], frame)
                    self.assertTrue(callable(options["recover_map"]))
                    self.assertEqual(options["failure_dir"].name, "coordinate_failures")
                    self.assertEqual(options["detect_targets"](frame)[0]["xyxy"], target["xyxy"])
                    self.assertNotIn("classes", model.predict.call_args.kwargs)
                    resolved.append(options)
                    if len(resolved) == 1:
                        raise PreciseCoordinateError("邊角目標暫時無法確認")
                    return {"lat": 25.0, "lng": 121.0, "exact": True, "method": "pikmin_map_intent"}

                module = "bloompilot.phone_automation" if mode == "automatic" else "bloompilot.geo_recognition"
                with (
                    patch("torch.cuda.is_available", return_value=False),
                    patch("ultralytics.YOLO", return_value=model),
                    patch(module + ".AdbClient", return_value=Mock()),
                    patch(module + ".PikminMemoryWatchdog", return_value=watchdog),
                    patch(module + ".run_rectangular_scan", side_effect=scanner),
                    patch(module + ".detections_from_result", side_effect=lambda _result: [dict(target)]),
                    patch("bloompilot.badge_number.enrich_detections_with_badge_numbers"),
                    patch("bloompilot.mushroom_coordinate.MushroomCoordinateResolver.resolve", side_effect=resolve),
                ):
                    options = dict(
                        adb=root / "adb.exe", serial="phone", model_path=model_path,
                        confidence=0.45, image_size=32, settle_seconds=0,
                        target_names=["target"], precise_coordinates=True, output_root=root / "outputs",
                        detection_update=lambda _where, hits: captured.append(hits),
                    )
                    if mode == "automatic":
                        run_recognition_scan(
                            **options, horizontal_radius=1, vertical_radius=0,
                            coordinate_controller=FakeController(),
                        )
                    else:
                        run_geographic_recognition_sequence(
                            **options, controller=FakeController(),
                            waypoints=[GeoRecognitionWaypoint("one", "one", 25, 121, 0, 1, 0)],
                        )
                self.assertEqual(len(resolved), 2)
                self.assertEqual(len(captured), 3)
                self.assertIn("coordinate_error", captured[0][0])
                self.assertIn("coordinate_error", captured[1][0])
                self.assertNotIn("precise_coordinate", captured[1][0])
                self.assertTrue(captured[2][0]["precise_coordinate"]["exact"])


if __name__ == "__main__":
    unittest.main()
