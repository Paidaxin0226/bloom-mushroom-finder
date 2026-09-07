from __future__ import annotations

import json
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import cv2
import numpy as np

from bloompilot.device_control import DeviceControlError, DeviceResponse
from bloompilot.geo_recognition import (
    GeoRecognitionWaypoint,
    _command_with_reconnect,
    _finish_sequence_movement,
    _keep_usb_control_alive,
    run_geographic_recognition_sequence,
)
from bloompilot.phone_automation import AutomationError, ScanSummary
from bloompilot.phone_stream import ScrcpyFrameSource
from bloompilot.phone_tools import Toolchain


def _png_frame(value: int = 80) -> bytes:
    image = np.full((240, 120, 3), value, dtype=np.uint8)
    ok, encoded = cv2.imencode(".png", image)
    if not ok:
        raise AssertionError("test PNG encoding failed")
    return encoded.tobytes()


TEST_PNG = _png_frame()


class FakeMemoryWatchdog:
    restart_count = 0
    poll_seconds = 10.0
    pressure_samples = 2

    @staticmethod
    def prepare_for_scan(*, force_restart: bool = False) -> bool:
        del force_restart
        return True

    @staticmethod
    def before_capture(_point: object | None = None) -> bool:
        return False

    @staticmethod
    def recover_after_usb_service_restart() -> bool:
        return True


class FakeController:
    def __init__(self) -> None:
        self.connected = True
        self.connection_generation = 0
        self.current = {"lat": 24.0, "lng": 120.0}
        self.commands: list[tuple[str, dict[str, object]]] = []

    def connect(
        self, *, timeout: float = 30.0, only_if_disconnected: bool = False,
    ) -> DeviceResponse:
        del timeout, only_if_disconnected
        self.connected = True
        self.connection_generation += 1
        return DeviceResponse(True, "paired", self.status(), {})

    def command(
        self,
        command: str,
        payload: dict[str, object] | None = None,
        *,
        require_ok: bool = True,
        timeout: float | None = None,
    ) -> DeviceResponse:
        del timeout
        value = dict(payload or {})
        self.commands.append((command, value))
        if command in {"teleport", "navigate"}:
            self.current = {"lat": float(value["lat"]), "lng": float(value["lng"])}
        return DeviceResponse(True, "ok", self.status(), {})

    def status(self) -> dict[str, object]:
        return {"current": dict(self.current), "running": False}

    def keepalive(self, *, idle_seconds: float, timeout: float) -> bool:
        self.command("status", require_ok=False, timeout=timeout)
        return True


class GeoRecognitionSequenceTests(unittest.TestCase):
    def test_finish_shares_one_deadline_with_heartbeat_shutdown(self) -> None:
        controller = Mock()
        controller.command.return_value = DeviceResponse(True, "ok", {}, {})
        stop = threading.Event()
        heartbeat = Mock()
        messages: list[str] = []
        with patch("bloompilot.geo_recognition.time.monotonic", side_effect=[100.0, 100.0, 102.0]):
            result = _finish_sequence_movement(
                controller, stop, heartbeat, origin=(24, 120), return_to_origin=True,
                speed_kmh=10.8, report=messages.append,
            )
        self.assertTrue(stop.is_set())
        heartbeat.join.assert_called_once_with(timeout=2.0)
        controller.command.assert_called_once_with(
            "teleport", {"lat": 24, "lng": 120, "speedKmh": 10.8}, timeout=6.0,
        )
        self.assertTrue(result["ok"])
        self.assertIn("已回到最初座標。", messages)

    def test_finish_reports_rejection_for_return_and_stop(self) -> None:
        for origin, return_to_origin, command in (((24, 120), True, "teleport"), ((24, 120), False, "stop"), (None, True, "stop")):
            with self.subTest(command=command, origin=origin):
                controller = Mock()
                controller.command.return_value = DeviceResponse(False, "rejected", {}, {})
                messages: list[str] = []
                result = _finish_sequence_movement(
                    controller, threading.Event(), Mock(), origin=origin,
                    return_to_origin=return_to_origin, speed_kmh=10.8, report=messages.append,
                )
                self.assertFalse(result["ok"])
                self.assertEqual(result["command"], command)
                self.assertIn("rejected", result["message"])
                self.assertTrue(any("無法確認" in message for message in messages))
                controller.connect.assert_not_called()

    def test_stopping_heartbeat_does_not_reconnect_or_report_after_failure(self) -> None:
        stop = threading.Event()
        controller = Mock()
        messages: list[str] = []

        def fail_status(*_args, **kwargs):
            self.assertEqual(kwargs["timeout"], 3.0)
            stop.set()
            raise DeviceControlError("USB 控制連線中斷：timed out")

        controller.keepalive.side_effect = fail_status
        with patch("bloompilot.geo_recognition.USB_CONTROL_KEEPALIVE_SECONDS", 0.001):
            _keep_usb_control_alive(controller, stop, messages.append)
        controller.keepalive.assert_called_once()
        controller.connect.assert_not_called()
        self.assertEqual(messages, [])

    def test_heartbeat_reports_once_per_outage_until_a_response_confirms_recovery(self) -> None:
        stop = Mock()
        stop.wait.side_effect = [False] * 6 + [True]
        stop.is_set.return_value = False
        controller = Mock()
        controller.keepalive.side_effect = [
            DeviceControlError("USB 控制連線中斷：timed out"),
            DeviceControlError("USB 控制連線已關閉。"),
            False,  # Busy is a skipped probe, not a confirmed recovery.
            DeviceControlError("USB 控制連線已關閉。"),
            True,
            DeviceControlError("USB 控制連線中斷：timed out"),
        ]
        messages: list[str] = []

        _keep_usb_control_alive(controller, stop, messages.append)

        self.assertEqual(len(messages), 2)
        self.assertTrue(all("timed out" in message for message in messages))
        controller.connect.assert_not_called()

    def test_cancelled_sequence_saves_results_when_return_times_out(self) -> None:
        controller = FakeController()
        controller.connect = Mock()
        real_command = controller.command

        def command(name, payload=None, **kwargs):
            if name == "teleport" and payload["lat"] == 24.0:
                self.assertGreater(kwargs["timeout"], 0)
                self.assertLessEqual(kwargs["timeout"], 8.0)
                raise DeviceControlError("USB 控制連線中斷：timed out")
            return real_command(name, payload, **kwargs)

        controller.command = command
        stop = threading.Event()
        messages: list[str] = []
        model = Mock(names={0: "target"})
        model.predict.return_value = [object()]

        def cancel_scan(*_args, **_kwargs):
            stop.set()
            return ScanSummary(1, 0, False, "cancelled")

        with tempfile.TemporaryDirectory(dir=Path.cwd()) as temp:
            root = Path(temp)
            model_path = root / "model.pt"
            model_path.write_bytes(b"model")
            with (
                patch("torch.cuda.is_available", return_value=False),
                patch("ultralytics.YOLO", return_value=model),
                patch("bloompilot.geo_recognition.AdbClient"),
                patch("bloompilot.geo_recognition.PikminMemoryWatchdog", return_value=FakeMemoryWatchdog()),
                patch("bloompilot.geo_recognition.run_rectangular_scan", side_effect=cancel_scan),
            ):
                output, summary = run_geographic_recognition_sequence(
                    controller=controller, adb=root / "adb.exe", serial="device", model_path=model_path,
                    waypoints=[GeoRecognitionWaypoint("first", "第一站", 25.1, 121.1, 0, 0, 0, 100)],
                    settle_seconds=0, confidence=0.45, image_size=32,
                    output_root=root / "outputs", status=messages.append, stop_event=stop,
                )
            saved = json.loads((output / "summary.json").read_text(encoding="utf-8"))
            self.assertTrue((output / "detections.jsonl").exists())
        self.assertEqual(summary.stop_reason, "cancelled")
        self.assertEqual(summary.visited, 1)
        self.assertFalse(saved["final_movement"]["ok"])
        self.assertEqual(saved["final_movement"]["command"], "teleport")
        self.assertTrue(any("無法確認已回到最初座標" in message for message in messages))
        self.assertEqual(messages[-1], "正在儲存辨識結果與序列摘要…")
        controller.connect.assert_not_called()

    def test_usb_command_reconnects_once_after_connection_reset(self) -> None:
        controller = Mock()
        expected = DeviceResponse(True, "ok", {"running": False}, {})
        controller.command.side_effect = [
            DeviceControlError(
                "USB 控制連線中斷：[WinError 10053] 連線已被您主機上的軟體中止。"
            ),
            expected,
        ]
        messages: list[str] = []

        response = _command_with_reconnect(
            controller,
            "status",
            status=messages.append,
        )

        self.assertIs(response, expected)
        controller.connect.assert_called_once_with(timeout=8.0, only_if_disconnected=True)
        self.assertEqual(controller.command.call_count, 2)
        self.assertTrue(any("自動重新連接" in message for message in messages))
        self.assertTrue(any("繼續目前序列" in message for message in messages))

    def test_moves_waits_and_searches_each_waypoint_in_order(self) -> None:
        controller = FakeController()
        waypoints = [
            GeoRecognitionWaypoint("first", "第一站", 25.1, 121.1, 0, 0, 0, 100),
            GeoRecognitionWaypoint("second", "第二站", 25.2, 121.2, 0, 0, 0, 100),
        ]
        fake_model = Mock()
        fake_model.names = {0: "target"}
        fake_model.predict.return_value = [object()]
        fake_adb = Mock()
        fake_adb.capture_png.return_value = TEST_PNG
        progress: list[tuple[int, int, str]] = []

        with tempfile.TemporaryDirectory(dir=Path.cwd()) as temp:
            root = Path(temp)
            model_path = root / "model.pt"
            model_path.write_bytes(b"model")
            with (
                patch("torch.cuda.is_available", return_value=False),
                patch("ultralytics.YOLO", return_value=fake_model),
                patch("bloompilot.geo_recognition.AdbClient", return_value=fake_adb),
                patch("bloompilot.geo_recognition.PikminMemoryWatchdog", return_value=FakeMemoryWatchdog()),
                patch("bloompilot.phone_automation.SCREEN_STABLE_SECONDS", 0),
                patch("bloompilot.geo_recognition._decode_png", return_value=np.zeros((8, 8, 3), dtype=np.uint8)),
                patch("bloompilot.geo_recognition.detections_from_result", return_value=[]),
                patch("bloompilot.detection.draw_detections", side_effect=lambda _result, frame: (frame, [])),
            ):
                _output, summary = run_geographic_recognition_sequence(
                    controller=controller,
                    adb=root / "adb.exe",
                    serial="device",
                    model_path=model_path,
                    waypoints=waypoints,
                    settle_seconds=0,
                    confidence=0.45,
                    image_size=32,
                    output_root=root / "outputs",
                    progress=lambda done, total, detail: progress.append((done, total, str(detail["waypoint_id"]))),
                )

        teleports = [payload for command, payload in controller.commands if command == "teleport"]
        self.assertEqual([(item["lat"], item["lng"]) for item in teleports[:2]], [(25.1, 121.1), (25.2, 121.2)])
        self.assertEqual((teleports[-1]["lat"], teleports[-1]["lng"]), (24.0, 120.0))
        self.assertNotIn("stop", [command for command, _payload in controller.commands])
        self.assertEqual(progress, [(1, 2, "first"), (2, 2, "second")])
        self.assertEqual(fake_adb.capture_png.call_count, 2)
        self.assertTrue(summary.completed)
        self.assertEqual(summary.completed_waypoints, 2)
        self.assertEqual(summary.visited, 2)
        self.assertEqual(summary.capture_failures, 0)

    def test_skips_one_failed_capture_and_continues_the_sequence(self) -> None:
        controller = FakeController()
        waypoints = [
            GeoRecognitionWaypoint("first", "第一站", 25.1, 121.1, 0, 0, 0, 100),
            GeoRecognitionWaypoint("second", "第二站", 25.2, 121.2, 0, 0, 0, 100),
        ]
        fake_model = Mock()
        fake_model.names = {0: "target"}
        fake_model.predict.return_value = [object()]
        fake_adb = Mock()
        fake_adb.capture_png.side_effect = [AutomationError("USB timeout"), TEST_PNG]
        messages: list[str] = []

        with tempfile.TemporaryDirectory(dir=Path.cwd()) as temp:
            root = Path(temp)
            model_path = root / "model.pt"
            model_path.write_bytes(b"model")
            with (
                patch("torch.cuda.is_available", return_value=False),
                patch("ultralytics.YOLO", return_value=fake_model),
                patch("bloompilot.geo_recognition.AdbClient", return_value=fake_adb),
                patch("bloompilot.geo_recognition.PikminMemoryWatchdog", return_value=FakeMemoryWatchdog()),
                patch("bloompilot.phone_automation.SCREEN_STABLE_SECONDS", 0),
                patch("bloompilot.geo_recognition._decode_png", return_value=np.zeros((8, 8, 3), dtype=np.uint8)),
                patch("bloompilot.geo_recognition.detections_from_result", return_value=[]),
                patch("bloompilot.detection.draw_detections", side_effect=lambda _result, frame: (frame, [])),
            ):
                _output, summary = run_geographic_recognition_sequence(
                    controller=controller,
                    adb=root / "adb.exe",
                    serial="device",
                    model_path=model_path,
                    waypoints=waypoints,
                    settle_seconds=0,
                    confidence=0.45,
                    image_size=32,
                    output_root=root / "outputs",
                    status=messages.append,
                )

        self.assertTrue(summary.completed)
        self.assertEqual(summary.completed_waypoints, 2)
        self.assertEqual(summary.visited, 2)
        self.assertEqual(summary.capture_failures, 1)
        self.assertTrue(any("已略過本次取樣" in message for message in messages))

    def test_scrcpy_timeout_at_second_waypoint_falls_back_and_finishes_sequence(self) -> None:
        controller = FakeController()
        waypoints = [
            GeoRecognitionWaypoint(str(index), f"城市 {index}", 25 + index / 10, 121, 0, 0, 0, 100)
            for index in range(1, 4)
        ]
        fake_model = Mock()
        fake_model.names = {0: "target"}
        fake_model.predict.return_value = [object()]
        fake_adb = Mock()
        fake_adb.capture_png.return_value = TEST_PNG
        messages: list[str] = []
        image = cv2.imdecode(np.frombuffer(TEST_PNG, np.uint8), cv2.IMREAD_COLOR)
        fallback = Mock(return_value=image)
        source = ScrcpyFrameSource(
            Toolchain(Path("scrcpy"), Path("adb"), Path("server"), "3.3"),
            "device",
            fallback_capture_image=fallback,
            status=messages.append,
        )
        # The first city receives video; subsequent cities receive no new
        # video, exercising the real timeout and fallback path.
        source.frames.put((image, 1.0))
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as temp:
            root = Path(temp)
            model_path = root / "model.pt"
            model_path.write_bytes(b"model")
            with (
                patch("torch.cuda.is_available", return_value=False),
                patch("ultralytics.YOLO", return_value=fake_model),
                patch("bloompilot.geo_recognition.AdbClient", return_value=fake_adb),
                patch("bloompilot.geo_recognition.PikminMemoryWatchdog", return_value=FakeMemoryWatchdog()),
                patch("bloompilot.phone_automation.SCREEN_STABLE_SECONDS", 0),
                patch("bloompilot.geo_recognition.detections_from_result", return_value=[]),
            ):
                _output, summary = run_geographic_recognition_sequence(
                    controller=controller,
                    adb=root / "adb.exe",
                    serial="device",
                    model_path=model_path,
                    waypoints=waypoints,
                    settle_seconds=0,
                    confidence=0.45,
                    image_size=32,
                    output_root=root / "outputs",
                    status=messages.append,
                    capture_png=lambda: source.capture_png(timeout=0.1),
                )

        self.assertTrue(summary.completed)
        self.assertEqual(summary.completed_waypoints, 3)
        self.assertEqual(summary.visited, 3)
        self.assertEqual(summary.capture_failures, 0)
        # One model warmup, then one recognition for each of the three cities.
        self.assertEqual(fake_model.predict.call_count, 4)
        self.assertEqual(fallback.call_count, 2)
        self.assertTrue(any("ADB 即時截圖繼續辨識" in message for message in messages))

    def test_live_hit_contains_target_crop_and_estimated_grid_coordinate(self) -> None:
        controller = FakeController()
        waypoint = GeoRecognitionWaypoint("city", "測試城市", 25.1, 121.1, 0, 0, 0, 414)
        fake_model = Mock()
        fake_model.names = {0: "target"}
        fake_model.predict.return_value = [object()]
        fake_adb = Mock()
        fake_adb.capture_png.return_value = TEST_PNG
        events: list[tuple[object, list[dict[str, object]]]] = []
        detection = {
            "class_id": 0,
            "class_name": "target",
            "confidence": 0.9,
            "xyxy": [40.0, 90.0, 80.0, 150.0],
        }

        with tempfile.TemporaryDirectory(dir=Path.cwd()) as temp:
            root = Path(temp)
            model_path = root / "model.pt"
            model_path.write_bytes(b"model")
            with (
                patch("torch.cuda.is_available", return_value=False),
                patch("ultralytics.YOLO", return_value=fake_model),
                patch("bloompilot.geo_recognition.AdbClient", return_value=fake_adb),
                patch("bloompilot.geo_recognition.PikminMemoryWatchdog", return_value=FakeMemoryWatchdog()),
                patch("bloompilot.phone_automation.SCREEN_STABLE_SECONDS", 0),
                patch("bloompilot.geo_recognition._decode_png", return_value=np.full((240, 120, 3), 80, dtype=np.uint8)),
                patch("bloompilot.geo_recognition.detections_from_result", return_value=[detection]),
                patch("bloompilot.detection.draw_detections", side_effect=lambda _result, frame: (frame, 1)),
            ):
                _output, summary = run_geographic_recognition_sequence(
                    controller=controller,
                    adb=root / "adb.exe",
                    serial="device",
                    model_path=model_path,
                    waypoints=[waypoint],
                    settle_seconds=0,
                    confidence=0.45,
                    image_size=32,
                    output_root=root / "outputs",
                    bookmark_detection=False,
                    detection_update=lambda location, hits: events.append((location, hits)),
                )

        self.assertTrue(summary.completed)
        self.assertEqual(len(events), 1)
        location, hits = events[0]
        self.assertEqual(location["center"], {"lat": 25.1, "lng": 121.1})
        self.assertEqual(location["point"]["grid_x"], 0)
        self.assertEqual(location["point"]["grid_y"], 0)
        self.assertEqual(len(hits), 1)
        self.assertTrue(str(hits[0]["crop_data_url"]).startswith("data:image/jpeg;base64,"))
        estimate = hits[0]["estimated_coordinate"]
        self.assertAlmostEqual(float(estimate["lat"]), 25.1, places=7)
        self.assertAlmostEqual(float(estimate["lng"]), 121.1, places=7)

    def test_rectangular_area_uses_map_swipes_without_grid_gps_injection(self) -> None:
        controller = FakeController()
        waypoint = GeoRecognitionWaypoint(
            "city",
            "測試城市",
            25.1,
            121.1,
            0,
            1,
            1,
            414,
        )
        fake_model = Mock()
        fake_model.names = {0: "target"}
        fake_model.predict.return_value = [object()]
        fake_adb = Mock()
        fake_adb.capture_png.return_value = TEST_PNG
        statuses: list[str] = []

        with tempfile.TemporaryDirectory(dir=Path.cwd()) as temp:
            root = Path(temp)
            model_path = root / "model.pt"
            model_path.write_bytes(b"model")
            with (
                patch("torch.cuda.is_available", return_value=False),
                patch("ultralytics.YOLO", return_value=fake_model),
                patch("bloompilot.geo_recognition.AdbClient", return_value=fake_adb),
                patch("bloompilot.geo_recognition.PikminMemoryWatchdog", return_value=FakeMemoryWatchdog()),
                patch("bloompilot.phone_automation.SCREEN_STABLE_SECONDS", 0),
                patch("bloompilot.geo_recognition._decode_png", return_value=np.zeros((8, 8, 3), dtype=np.uint8)),
                patch("bloompilot.geo_recognition.detections_from_result", return_value=[]),
                patch("bloompilot.detection.draw_detections", side_effect=lambda _result, frame: (frame, [])),
            ):
                _output, summary = run_geographic_recognition_sequence(
                    controller=controller,
                    adb=root / "adb.exe",
                    serial="device",
                    model_path=model_path,
                    waypoints=[waypoint],
                    settle_seconds=0,
                    confidence=0.45,
                    image_size=32,
                    output_root=root / "outputs",
                    status=statuses.append,
                )

        teleports = [payload for command, payload in controller.commands if command == "teleport"]
        self.assertEqual(
            [(item["lat"], item["lng"]) for item in teleports],
            [(25.1, 121.1), (24.0, 120.0)],
        )
        self.assertEqual(fake_adb.capture_png.call_count, 10)
        self.assertGreater(fake_adb.swipe.call_count, 0)
        self.assertTrue(fake_adb.recenter.called)
        self.assertTrue(any("手機地圖滑動" in message for message in statuses))
        self.assertTrue(summary.completed)
        self.assertEqual(summary.completed_waypoints, 1)
        self.assertEqual(summary.visited, 9)


if __name__ == "__main__":
    unittest.main()
