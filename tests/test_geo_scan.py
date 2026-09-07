from __future__ import annotations

import threading
import unittest

from bloompilot.device_control import DeviceResponse
from bloompilot.geo_scan import (
    GeoCaptureOutcome,
    build_geo_scan_points,
    distance_m,
    run_geographic_scan,
)


class FakeController:
    def __init__(self) -> None:
        self.current = {"lat": 25.0, "lng": 121.0}
        self.commands: list[tuple[str, dict[str, object]]] = []

    def command(
        self,
        command: str,
        payload: dict[str, object] | None = None,
        *,
        require_ok: bool = True,
    ) -> DeviceResponse:
        value = dict(payload or {})
        self.commands.append((command, value))
        if command in {"teleport", "navigate"}:
            self.current = {"lat": float(value["lat"]), "lng": float(value["lng"])}
        return DeviceResponse(True, "ok", self.status(), {})

    def status(self) -> dict[str, object]:
        return {"current": dict(self.current), "running": False}


class GeoScanTests(unittest.TestCase):
    def test_builds_center_out_meter_spaced_grid(self) -> None:
        points = build_geo_scan_points(25.0, 121.0, 1, 1, 100.0)
        self.assertEqual(len(points), 9)
        self.assertEqual((points[0].grid_x, points[0].grid_y), (0, 0))
        east = next(point for point in points if (point.grid_x, point.grid_y) == (1, 0))
        self.assertAlmostEqual(distance_m(25.0, 121.0, east.lat, east.lng), 100.0, delta=0.5)

    def test_builds_asymmetric_geo_grid(self) -> None:
        points = build_geo_scan_points(
            25.0,
            121.0,
            3,
            4,
            50.0,
            left_radius=1,
            right_radius=3,
            up_radius=0,
            down_radius=2,
        )
        coordinates = {(point.grid_x, point.grid_y) for point in points}
        self.assertEqual(coordinates, {(x, y) for x in range(-1, 4) for y in range(-2, 1)})
        self.assertEqual(len(points), 15)

    def test_scan_continues_after_every_detection_and_returns_to_origin(self) -> None:
        controller = FakeController()
        points = build_geo_scan_points(25.0, 121.0, 1, 0, 50.0)

        def on_capture(_point: object, _payload: bytes) -> GeoCaptureOutcome:
            return GeoCaptureOutcome(detection_count=1)

        summary = run_geographic_scan(
            controller,
            points=points,
            capture_png=lambda: b"png",
            stop_event=threading.Event(),
            on_capture=on_capture,
            settle_seconds=0,
        )
        self.assertEqual(summary.stop_reason, "completed")
        self.assertTrue(summary.completed)
        self.assertEqual(summary.visited, len(points))
        self.assertEqual(summary.detection_count, len(points))
        self.assertEqual(controller.commands[-1][0], "teleport")
        self.assertEqual(controller.current, {"lat": 25.0, "lng": 121.0})


if __name__ == "__main__":
    unittest.main()
