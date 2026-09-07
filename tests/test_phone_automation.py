from __future__ import annotations

import tempfile
import threading
import subprocess
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import cv2
import numpy as np

from bloompilot.phone_automation import (
    AdbClient,
    AutomationError,
    ForegroundAppError,
    MapRecoveryError,
    CaptureOutcome,
    ScanPoint,
    ScreenSize,
    background_filename,
    build_rectangular_scan_points,
    build_scan_points,
    detections_from_result,
    focused_packages_from_activity_dump,
    gesture_for_direction,
    next_capture_index,
    observe_screen_until_stable,
    parse_screen_size,
    normalize_model_class_names,
    resolve_target_selection,
    run_directional_scan,
    run_rectangular_scan,
    restore_recognition_map,
    slugify_city,
)


class FakeAdbClient:
    def __init__(self) -> None:
        self.actions: list[object] = []

    def ensure_ready(self) -> None:
        self.actions.append("ready")

    def screen_size(self) -> ScreenSize:
        self.actions.append("size")
        return ScreenSize(1080, 2340)

    def recenter(self, size: ScreenSize) -> None:
        self.actions.append(("recenter", size))

    def swipe(self, direction: str, size: ScreenSize, duration_ms: int) -> None:
        self.actions.append(("swipe", direction, size, duration_ms))

    def capture_png(self) -> bytes:
        self.actions.append("capture")
        return b"png"


class FakeTensor:
    def __init__(self, value: object) -> None:
        self.value = np.asarray(value)

    def detach(self) -> "FakeTensor":
        return self

    def cpu(self) -> "FakeTensor":
        return self

    def numpy(self) -> np.ndarray:
        return self.value


class FakeBoxes:
    def __init__(self) -> None:
        self.xyxy = FakeTensor([[10, 20, 30, 40]])
        self.conf = FakeTensor([0.875])
        self.cls = FakeTensor([1])

    def __len__(self) -> int:
        return 1


class FakeResult:
    boxes = FakeBoxes()
    names = {0: "red", 1: "blue"}


class PhoneAutomationTests(unittest.TestCase):
    def test_adb_input_timeout_is_a_recoverable_automation_error(self) -> None:
        client = object.__new__(AdbClient)
        client.adb = Path("adb.exe")
        client.serial = "phone"
        with (
            patch("bloompilot.phone_automation.subprocess.run", side_effect=subprocess.TimeoutExpired("adb", 8)),
            self.assertRaisesRegex(AutomationError, "ADB 指令逾時"),
        ):
            client.run("shell", "input", "tap", "10", "10")

    def test_unverified_map_stops_scan_without_final_blind_recenter(self) -> None:
        client = FakeAdbClient()
        on_capture = Mock(side_effect=MapRecoveryError("cannot restore map"))
        with self.assertRaises(MapRecoveryError):
            run_rectangular_scan(
                client, horizontal_radius=1, vertical_radius=1,
                settle_seconds=0, swipe_duration_ms=650,
                stop_event=threading.Event(), on_capture=on_capture,
            )
        on_capture.assert_called_once()
        self.assertEqual(sum(isinstance(action, tuple) and action[0] == "recenter" for action in client.actions), 1)
        self.assertFalse(any(isinstance(action, tuple) and action[0] == "swipe" for action in client.actions))

    def test_recovery_replays_current_grid_and_honors_stop(self) -> None:
        client = FakeAdbClient()
        watchdog = Mock()
        watchdog.prepare_for_scan.return_value = True
        event = threading.Event()
        with patch("bloompilot.phone_automation.SCREEN_STABLE_SECONDS", 0):
            self.assertTrue(restore_recognition_map(
                client, watchdog, ScanPoint(5, "grid", 2, -2, 1),
                stop_event=event, swipe_duration_ms=650,
            ))
        self.assertEqual([action[1] for action in client.actions if isinstance(action, tuple) and action[0] == "swipe"], ["west", "west", "north"])
        event.set()
        self.assertFalse(restore_recognition_map(
            client, watchdog, ScanPoint(0, "center", 0), stop_event=event, swipe_duration_ms=650,
        ))
        watchdog.prepare_for_scan.assert_called_once()

    @staticmethod
    def png_frame(value: int) -> bytes:
        image = np.full((240, 120, 3), value, dtype=np.uint8)
        ok, encoded = cv2.imencode(".png", image)
        if not ok:
            raise AssertionError("test PNG encoding failed")
        return encoded.tobytes()

    def test_full_screen_capture_retries_a_transient_adb_timeout(self) -> None:
        client = object.__new__(AdbClient)
        valid_png = b"\x89PNG\r\n\x1a\ncomplete-frame"
        client.run = Mock(  # type: ignore[method-assign]
            side_effect=[
                subprocess.TimeoutExpired(["adb", "screencap"], 30),
                subprocess.CompletedProcess(["adb"], 0, stdout="device", stderr=""),
                subprocess.CompletedProcess(["adb"], 0, stdout=valid_png, stderr=b""),
            ]
        )

        self.assertEqual(client.capture_png(), valid_png)
        self.assertEqual(client.run.call_count, 3)

    def test_full_screen_capture_reports_repeated_adb_timeouts_cleanly(self) -> None:
        client = object.__new__(AdbClient)
        client.run = Mock(  # type: ignore[method-assign]
            side_effect=subprocess.TimeoutExpired(["adb", "screencap"], 30)
        )

        with self.assertRaisesRegex(AutomationError, "連續 3 次逾時"):
            client.capture_png()

    def test_pinch_zoom_out_uses_adb_accessibility_without_scrcpy(self) -> None:
        client = object.__new__(AdbClient)
        client.serial = "phone-1"
        client.run = Mock(  # type: ignore[method-assign]
            side_effect=[
                subprocess.CompletedProcess(["adb"], 0, stdout="other.app/.Reader\n", stderr=""),
                subprocess.CompletedProcess(["adb"], 0, stdout="", stderr=""),
                subprocess.CompletedProcess(["adb"], 0, stdout="", stderr=""),
                subprocess.CompletedProcess(
                    ["adb"],
                    0,
                    stdout='Broadcast completed: result=-1, data="Map pinch zoom-out completed"\n',
                    stderr="",
                ),
            ]
        )

        client.pinch_zoom_out(repetitions=6)

        commands = [call.args for call in client.run.call_args_list]
        enabled = next(
            command
            for command in commands
            if command[:6] == (
                "shell", "settings", "put", "secure", "enabled_accessibility_services",
                "other.app/.Reader:com.locwarp.mock/com.locwarp.mock.PikminGestureAccessibilityService",
            )
        )
        self.assertTrue(enabled)
        broadcast = commands[-1]
        self.assertIn("com.locwarp.mock/.GestureCommandReceiver", broadcast)
        self.assertEqual(broadcast[-1], "6")

    def test_slugify_city_is_stable_for_folder_names(self) -> None:
        self.assertEqual(slugify_city(" New York "), "new_york")
        self.assertEqual(slugify_city("巴黎"), "巴黎")

    def test_parse_screen_size_prefers_active_override(self) -> None:
        output = "Physical size: 1080x2340\nOverride size: 720x1560\n"
        self.assertEqual(parse_screen_size(output), ScreenSize(720, 1560))

    def test_gestures_scale_to_screen_and_move_in_expected_direction(self) -> None:
        size = ScreenSize(1080, 2340)
        north = gesture_for_direction("north", size)
        south = gesture_for_direction("south", size)
        east = gesture_for_direction("east", size)
        self.assertEqual(north.x1, north.x2)
        self.assertLess(north.y1, north.y2)
        self.assertEqual(east.y1, east.y2)
        self.assertGreater(east.x1, east.x2)
        self.assertEqual((north.y1, north.y2), (234, 1778))
        self.assertEqual((south.y1, south.y2), (1778, 234))
        # Moving the stroke upward must not invalidate existing route spacing.
        self.assertEqual(north.y2 - north.y1, round(2340 * 0.81) - round(2340 * 0.15))

    def test_foreground_parser_finds_resumed_pikmin_activity(self) -> None:
        output = """
          topResumedActivity=ActivityRecord{123 u0 com.nianticlabs.pikmin/com.example.GameActivity t102}
          ResumedActivity: ActivityRecord{123 u0 com.nianticlabs.pikmin/com.example.GameActivity t102}
          mFocusedApp=ActivityRecord{123 u0 com.nianticlabs.pikmin/com.example.GameActivity t102}
        """
        self.assertEqual(
            focused_packages_from_activity_dump(output),
            {"com.nianticlabs.pikmin"},
        )

    def test_adb_swipe_refuses_to_move_google_discover(self) -> None:
        client = object.__new__(AdbClient)
        client.run = Mock(return_value=subprocess.CompletedProcess(
            ["adb"], 0,
            stdout=(
                "topResumedActivity=ActivityRecord{abc u0 "
                "com.google.android.googlequicksearchbox/com.google.android.apps.search.Foo}\n"
            ),
            stderr="",
        ))

        with self.assertRaisesRegex(MapRecoveryError, "Google 頁面"):
            client.swipe("east", ScreenSize(1080, 2340), 800)
        self.assertEqual(client.run.call_count, 1)

    def test_scan_points_include_center_and_four_directions(self) -> None:
        points = build_scan_points(2)
        self.assertEqual(len(points), 9)
        self.assertEqual(points[0], ScanPoint(0, "center", 0))
        self.assertEqual(points[-1], ScanPoint(8, "west", 2))
        self.assertEqual([point.ordinal for point in points], list(range(9)))

    def test_rectangular_points_cover_every_cell_from_center_outward(self) -> None:
        points = build_rectangular_scan_points(1, 2)
        coordinates = {(point.grid_x, point.grid_y) for point in points}
        expected = {(x, y) for x in range(-1, 2) for y in range(-2, 3)}
        self.assertEqual(len(points), 15)
        self.assertEqual(coordinates, expected)
        self.assertEqual(points[0], ScanPoint(0, "center", 0, 0, 0))
        self.assertEqual(
            [point.step for point in points],
            sorted(point.step for point in points),
        )

    def test_rectangular_points_support_asymmetric_direction_ranges(self) -> None:
        points = build_rectangular_scan_points(
            3,
            4,
            left_radius=0,
            right_radius=2,
            up_radius=1,
            down_radius=3,
        )
        coordinates = {(point.grid_x, point.grid_y) for point in points}
        expected = {(x, y) for x in range(0, 3) for y in range(-3, 2)}
        self.assertEqual(coordinates, expected)
        self.assertEqual(len(points), 15)
        self.assertEqual(points[0], ScanPoint(0, "center", 0, 0, 0))

    def test_rectangular_scan_repositions_current_grid_after_game_restart(self) -> None:
        client = FakeAdbClient()
        statuses: list[str] = []
        captured: list[tuple[int, int]] = []

        summary = run_rectangular_scan(
            client,
            horizontal_radius=1,
            vertical_radius=0,
            settle_seconds=0,
            swipe_duration_ms=800,
            stop_event=threading.Event(),
            on_capture=lambda point, _payload: captured.append((point.grid_x, point.grid_y)) or CaptureOutcome(),
            before_capture=lambda point: point.grid_x == 1,
            status=statuses.append,
        )

        swipe_directions = [
            action[1]
            for action in client.actions
            if isinstance(action, tuple) and action[0] == "swipe"
        ]
        self.assertTrue(summary.completed)
        self.assertEqual(captured, [(0, 0), (1, 0), (-1, 0)])
        self.assertEqual(swipe_directions[:2], ["east", "east"])
        self.assertTrue(any("遊戲重啟後已恢復" in message for message in statuses))

    def test_rectangular_scan_recovers_foreground_crash_and_replays_target_grid(self) -> None:
        class CrashOnceClient(FakeAdbClient):
            def __init__(self) -> None:
                super().__init__()
                self.crashed = False

            def ensure_pikmin_foreground(self) -> None:
                return None

            def swipe(self, direction: str, size: ScreenSize, duration_ms: int) -> None:
                if direction == "west" and not self.crashed:
                    self.crashed = True
                    raise ForegroundAppError("launcher")
                super().swipe(direction, size, duration_ms)

        client = CrashOnceClient()
        recovered: list[bool] = []
        statuses: list[str] = []
        captured: list[tuple[int, int]] = []

        summary = run_rectangular_scan(
            client,
            horizontal_radius=1,
            vertical_radius=0,
            settle_seconds=0,
            swipe_duration_ms=800,
            stop_event=threading.Event(),
            on_capture=lambda point, _payload: captured.append(
                (point.grid_x, point.grid_y)
            ),
            recover_foreground=lambda: recovered.append(True) or True,
            status=statuses.append,
        )

        swipe_directions = [
            action[1]
            for action in client.actions
            if isinstance(action, tuple) and action[0] == "swipe"
        ]
        self.assertTrue(summary.completed)
        self.assertEqual(captured, [(0, 0), (1, 0), (-1, 0)])
        self.assertEqual(recovered, [True])
        # The failed west gesture is never injected. Recovery starts at center,
        # so only one successful west gesture is needed to restore x=-1.
        self.assertEqual(swipe_directions, ["east", "west"])
        self.assertTrue(any("Unity／GPU 當機" in message for message in statuses))

    def test_rectangular_scan_recaptures_grid_if_game_crashes_during_screenshot(self) -> None:
        class CaptureCrashClient(FakeAdbClient):
            def __init__(self) -> None:
                super().__init__()
                self.foreground = True
                self.capture_count = 0

            def ensure_pikmin_foreground(self) -> None:
                if not self.foreground:
                    raise ForegroundAppError("launcher")

            def capture_png(self) -> bytes:
                self.capture_count += 1
                if self.capture_count == 1:
                    self.foreground = False
                    return b"launcher"
                return b"map"

        client = CaptureCrashClient()
        payloads: list[bytes] = []
        messages: list[str] = []

        def recover() -> bool:
            client.foreground = True
            return True

        summary = run_rectangular_scan(
            client,
            horizontal_radius=0,
            vertical_radius=0,
            settle_seconds=0,
            swipe_duration_ms=800,
            stop_event=threading.Event(),
            on_capture=lambda _point, payload: payloads.append(payload),
            recover_foreground=recover,
            status=messages.append,
        )

        self.assertTrue(summary.completed)
        self.assertEqual(client.capture_count, 2)
        self.assertEqual(payloads, [b"map"])
        self.assertTrue(any("拍攝期間" in message for message in messages))

    def test_rectangular_scan_restores_current_grid_after_hit_excursion(self) -> None:
        client = FakeAdbClient()
        statuses: list[str] = []
        captured: list[tuple[int, int]] = []
        restored = False

        def pause_checkpoint() -> bool:
            nonlocal restored
            swipe_count = sum(
                1
                for action in client.actions
                if isinstance(action, tuple) and action[0] == "swipe"
            )
            if not restored and swipe_count == 1:
                restored = True
                return True
            return False

        summary = run_rectangular_scan(
            client,
            horizontal_radius=1,
            vertical_radius=0,
            settle_seconds=0,
            swipe_duration_ms=800,
            stop_event=threading.Event(),
            on_capture=lambda point, _payload: captured.append((point.grid_x, point.grid_y)) or CaptureOutcome(),
            pause_checkpoint=pause_checkpoint,
            status=statuses.append,
        )

        swipe_directions = [
            action[1]
            for action in client.actions
            if isinstance(action, tuple) and action[0] == "swipe"
        ]
        self.assertTrue(summary.completed)
        self.assertEqual(captured, [(0, 0), (1, 0), (-1, 0)])
        self.assertEqual(swipe_directions[:2], ["east", "east"])
        self.assertTrue(any("恢復原本搜尋格" in message for message in statuses))
        self.assertTrue(any("x=+1, y=+0" in message for message in statuses))

    def test_background_names_append_after_highest_existing_index(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            (directory / "paris_bg_000_center.png").touch()
            (directory / "paris_bg_026_east_03.png").touch()
            (directory / "unrelated.png").touch()
            self.assertEqual(next_capture_index(directory), 27)
        point = ScanPoint(1, "north", 1)
        self.assertEqual(background_filename("paris", 27, point), "paris_bg_027_north_01.png")
        grid = ScanPoint(2, "grid", 2, -1, 2)
        self.assertEqual(
            background_filename("paris", 28, grid),
            "paris_bg_028_xm01_yp02.png",
        )

    def test_rectangular_scan_covers_a_two_dimensional_ring(self) -> None:
        client = FakeAdbClient()
        captured: list[ScanPoint] = []
        summary = run_rectangular_scan(
            client,  # type: ignore[arg-type]
            horizontal_radius=1,
            vertical_radius=1,
            settle_seconds=0,
            swipe_duration_ms=800,
            stop_event=threading.Event(),
            on_capture=lambda point, _payload: captured.append(point),
        )
        swipes = [
            action
            for action in client.actions
            if isinstance(action, tuple) and action[0] == "swipe"
        ]
        recenters = [
            action
            for action in client.actions
            if isinstance(action, tuple) and action[0] == "recenter"
        ]
        self.assertEqual(len(captured), 9)
        self.assertEqual(
            {(point.grid_x, point.grid_y) for point in captured},
            {(x, y) for x in range(-1, 2) for y in range(-1, 2)},
        )
        self.assertEqual(len(swipes), 8)
        self.assertEqual(len(recenters), 3)
        self.assertTrue(summary.completed)

    def test_rectangular_scan_only_captures_effective_grid_spans(self) -> None:
        client = FakeAdbClient()
        captured: list[tuple[int, int]] = []
        summary = run_rectangular_scan(
            client,  # type: ignore[arg-type]
            horizontal_radius=2,
            vertical_radius=1,
            settle_seconds=0,
            swipe_duration_ms=800,
            stop_event=threading.Event(),
            on_capture=lambda point, _payload: captured.append(
                (point.grid_x, point.grid_y)
            ),
            scan_spans=((0, -1, 1), (1, 0, 0)),
        )

        self.assertTrue(summary.completed)
        self.assertEqual(summary.captured, 4)
        self.assertEqual(set(captured), {(-1, 0), (0, 0), (1, 0), (0, 1)})
        self.assertNotIn((2, 0), captured)

    def test_recognition_photographs_changes_and_skips_unchanged_positions(self) -> None:
        client = FakeAdbClient()
        frame_a = self.png_frame(20)
        frame_b = self.png_frame(220)
        frames = iter([frame_a, frame_a, frame_b, frame_b])
        photographed: list[ScanPoint] = []
        progress: list[int] = []
        messages: list[str] = []

        summary = run_rectangular_scan(
            client,  # type: ignore[arg-type]
            horizontal_radius=1,
            vertical_radius=0,
            settle_seconds=99,
            swipe_duration_ms=800,
            stop_event=threading.Event(),
            on_capture=lambda point, _payload: photographed.append(point),
            capture_png=lambda: next(frames),
            detect_screen_changes=True,
            screen_stable_seconds=0,
            status=messages.append,
            progress=lambda done, _total, _point: progress.append(done),
        )

        self.assertEqual(len(photographed), 2)
        self.assertEqual(photographed[0].direction, "center")
        self.assertEqual(progress, [1, 2, 3])
        self.assertEqual(summary.captured, 3)
        self.assertTrue(summary.completed)
        self.assertTrue(any("沒有畫面變化" in message for message in messages))

    def test_screen_monitor_photographs_each_change_until_one_stable_window(self) -> None:
        frames = [self.png_frame(20), self.png_frame(120), self.png_frame(220)]
        capture_index = 0
        photographed: list[bytes] = []

        def capture() -> bytes:
            nonlocal capture_index
            frame = frames[min(capture_index, len(frames) - 1)]
            capture_index += 1
            return frame

        observation = observe_screen_until_stable(
            capture,
            stop_event=threading.Event(),
            reference_signature=None,
            stable_seconds=0.005,
            poll_seconds=0.001,
            maximum_seconds=0.1,
            on_change=photographed.append,
        )

        self.assertEqual(len(photographed), 3)
        self.assertEqual(observation.change_count, 3)
        self.assertTrue(observation.changed)
        self.assertFalse(observation.timed_out)

    def test_full_scan_recenters_between_directions_and_when_finished(self) -> None:
        client = FakeAdbClient()
        captured: list[ScanPoint] = []
        progress: list[int] = []

        summary = run_directional_scan(
            client,  # type: ignore[arg-type]
            steps_per_direction=2,
            settle_seconds=0,
            swipe_duration_ms=800,
            stop_event=threading.Event(),
            on_capture=lambda point, _payload: captured.append(point),
            progress=lambda done, _total, _point: progress.append(done),
        )

        swipes = [action for action in client.actions if isinstance(action, tuple) and action[0] == "swipe"]
        recenters = [action for action in client.actions if isinstance(action, tuple) and action[0] == "recenter"]
        self.assertEqual(len(captured), 9)
        self.assertEqual(len(swipes), 8)
        self.assertEqual(len(recenters), 6)
        self.assertEqual(progress, list(range(1, 10)))
        self.assertTrue(summary.completed)
        self.assertEqual(summary.stop_reason, "completed")

    def test_detections_never_stop_the_scan_and_still_recenter(self) -> None:
        client = FakeAdbClient()
        summary = run_directional_scan(
            client,  # type: ignore[arg-type]
            steps_per_direction=3,
            settle_seconds=0,
            swipe_duration_ms=800,
            stop_event=threading.Event(),
            on_capture=lambda _point, _payload: CaptureOutcome(detection_count=2),
        )
        recenters = [action for action in client.actions if isinstance(action, tuple) and action[0] == "recenter"]
        self.assertEqual(summary.captured, 13)
        self.assertEqual(summary.detection_count, 26)
        self.assertEqual(summary.stop_reason, "completed")
        self.assertTrue(summary.completed)
        self.assertEqual(len(recenters), 6)

    def test_detection_records_are_json_ready(self) -> None:
        detections = detections_from_result(FakeResult())
        self.assertEqual(detections[0]["class_name"], "blue")
        self.assertEqual(detections[0]["class_id"], 1)
        self.assertAlmostEqual(detections[0]["confidence"], 0.875)
        self.assertEqual(detections[0]["xyxy"], [10.0, 20.0, 30.0, 40.0])

    def test_target_selection_resolves_only_requested_model_classes(self) -> None:
        classes = normalize_model_class_names({0: "Large_Red", 1: "Large_Blue"})
        names, class_ids = resolve_target_selection(
            classes,
            ["Large_Blue", "Large_Red", "Large_Blue"],
        )
        self.assertEqual(names, ["Large_Blue", "Large_Red"])
        self.assertEqual(class_ids, [1, 0])
        with self.assertRaisesRegex(Exception, "找不到所選 target"):
            resolve_target_selection(classes, ["Missing"])


if __name__ == "__main__":
    unittest.main()
