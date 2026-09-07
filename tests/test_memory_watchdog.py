from __future__ import annotations

import subprocess
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import cv2
import numpy as np

from bloompilot.memory_watchdog import (
    MAX_MAP_ZOOM_OUT_PINCHES,
    PIKMIN_PACKAGE,
    RESTART_SCREEN_CLOSABLE,
    RESTART_SCREEN_GARDEN,
    RESTART_SCREEN_HOME,
    RESTART_SCREEN_LOADING,
    RESTART_SCREEN_LOGIN,
    RESTART_SCREEN_LOGO,
    RESTART_SCREEN_MAP,
    RESTART_SCREEN_SAFETY,
    RESTART_SCREEN_SPLASH,
    RESTART_SCREEN_UNKNOWN,
    PikminMemoryWatchdog,
    _pikmin_loading_progress_visible,
    classify_pikmin_restart_image,
    classify_pikmin_restart_screen,
    memory_snapshot,
    parse_proc_meminfo,
    parse_total_pss_kib,
    pikmin_map_locator_visible,
)


def restart_frame(state: str) -> bytes:
    image = np.full((2340, 1080, 3), (48, 180, 76), dtype=np.uint8)
    y = round(2340 * 0.923)
    if state in (RESTART_SCREEN_MAP, RESTART_SCREEN_GARDEN, "map_centered", "map_bright"):
        left_x = round(1080 * 0.098)
        cv2.line(image, (left_x + 12, y - 25), (left_x - 18, y), (255, 255, 255), 8)
        cv2.line(image, (left_x - 18, y), (left_x + 12, y + 25), (255, 255, 255), 8)
        cv2.rectangle(image, (455, y - 40), (625, y + 40), (40, 120, 55), -1)
        left_color = (235, 245, 235) if state == RESTART_SCREEN_GARDEN else (80, 190, 80)
        right_color = (80, 190, 80) if state == RESTART_SCREEN_GARDEN else (235, 245, 235)
        cv2.rectangle(image, (470, y - 28), (530, y + 28), left_color, -1)
        cv2.rectangle(image, (550, y - 28), (610, y + 28), right_color, -1)
        if state == "map_bright":
            for dx, dy in ((-42, -42), (42, -42), (-42, 42), (42, 42)):
                cv2.circle(image, (left_x + dx, y + dy), 14, (255, 255, 255), -1)
    if state == RESTART_SCREEN_MAP:
        x = round(1080 * 0.898)
        cv2.circle(image, (x, y), 13, (255, 255, 255), 5, cv2.LINE_AA)
        cv2.line(image, (x - 24, y), (x - 12, y), (255, 255, 255), 5, cv2.LINE_AA)
        cv2.line(image, (x + 12, y), (x + 24, y), (255, 255, 255), 5, cv2.LINE_AA)
        cv2.line(image, (x, y - 24), (x, y - 12), (255, 255, 255), 5, cv2.LINE_AA)
        cv2.line(image, (x, y + 12), (x, y + 24), (255, 255, 255), 5, cv2.LINE_AA)
    elif state == RESTART_SCREEN_HOME:
        x = round(1080 * 0.098)
        for offset in (-18, 0, 18):
            cv2.line(image, (x - 30, y + offset), (x + 30, y + offset), (255, 255, 255), 8)
    elif state == RESTART_SCREEN_CLOSABLE:
        x = round(1080 * 0.098)
        cv2.line(image, (x - 25, y - 25), (x + 25, y + 25), (255, 255, 255), 8)
        cv2.line(image, (x - 25, y + 25), (x + 25, y - 25), (255, 255, 255), 8)
    elif state == RESTART_SCREEN_SAFETY:
        cv2.rectangle(image, (60, 900), (1020, 1430), (255, 255, 255), -1)
        cv2.ellipse(image, (540, 1250), (175, 70), 0, 0, 360, (165, 183, 0), 5)
    elif state == RESTART_SCREEN_SPLASH:
        image[:] = 0
        cv2.circle(image, (540, 1100), 190, (70, 220, 70), -1)
        cv2.circle(image, (500, 1040), 55, (255, 255, 255), -1)
        cv2.circle(image, (585, 1130), 45, (30, 30, 230), -1)
    elif state == RESTART_SCREEN_LOGO:
        image[:] = 250
        cv2.rectangle(image, (220, 1340), (860, 1625), (20, 25, 235), -1)
    elif state == RESTART_SCREEN_LOADING:
        image[:] = 0
    elif state == "art_loading":
        image[:] = (125, 170, 210)
        cv2.rectangle(image, (0, 1850), (1080, 2100), (80, 110, 145), -1)
        cv2.line(image, (110, 2010), (900, 2010), (255, 255, 255), 9)
        cv2.line(image, (110, 2010), (470, 2010), (190, 185, 25), 9)
    elif state == RESTART_SCREEN_LOGIN:
        image[:] = (245, 245, 245)
        cv2.rectangle(image, (260, 1590), (820, 1715), (170, 185, 10), -1)
    ok, encoded = cv2.imencode(".png", image)
    if not ok:
        raise AssertionError("test PNG encoding failed")
    return encoded.tobytes()


def recorded_map_image(state: str) -> np.ndarray:
    """Rebuild a frame from the lower 20% of a real MI 9 screenshot."""
    filename = "map_3d.png" if state == RESTART_SCREEN_GARDEN else "map_2d.png"
    fixture = Path(__file__).parent / "fixtures" / "restart_controls" / filename
    if not fixture.is_file():
        raise unittest.SkipTest(f"Private local screenshot fixture is not distributed: {filename}")
    strip = cv2.imread(str(fixture))
    if strip is None:
        raise AssertionError(f"Missing restart fixture: {filename}")
    height = strip.shape[0] * 5
    image = np.full((height, strip.shape[1], 3), (48, 180, 76), dtype=np.uint8)
    image[-strip.shape[0]:] = strip
    return image


class FakeAdbClient:
    def __init__(
        self,
        mem_available_mib: int = 64,
        game_pss_mib: int = 2800,
        restart_states: tuple[str, ...] = (
            RESTART_SCREEN_HOME,
            RESTART_SCREEN_GARDEN,
            RESTART_SCREEN_MAP,
        ),
    ) -> None:
        self.mem_available_mib = mem_available_mib
        self.game_pss_mib = game_pss_mib
        self.commands: list[tuple[str, ...]] = []
        self.recentered = False
        self.zoom_repetitions: list[int] = []
        self.restart_states = list(restart_states)
        self.last_restart_state = self.restart_states[-1] if self.restart_states else RESTART_SCREEN_MAP
        self.workflow_actions: list[tuple[object, ...]] = []

    def run(self, *arguments: str, timeout: float) -> subprocess.CompletedProcess:
        del timeout
        self.commands.append(tuple(arguments))
        if len(arguments) >= 5 and arguments[:3] == ("shell", "input", "tap"):
            self.workflow_actions.append(("tap", arguments[3], arguments[4]))
        if arguments[-1] == "/proc/meminfo":
            stdout = (
                f"MemTotal:        {8192 * 1024} kB\n"
                f"MemAvailable:     {self.mem_available_mib * 1024} kB\n"
            )
        elif "meminfo" in arguments:
            stdout = f" App Summary\n TOTAL PSS: {self.game_pss_mib * 1024:,}\n"
        else:
            stdout = "Events injected: 1\n"
        return subprocess.CompletedProcess(list(arguments), 0, stdout=stdout, stderr="")

    def screen_size(self) -> SimpleNamespace:
        return SimpleNamespace(width=1080, height=2340)

    def capture_png(self, *, attempts: int, timeout: float) -> bytes:
        del attempts, timeout
        if self.restart_states:
            self.last_restart_state = self.restart_states.pop(0)
        state = self.last_restart_state
        return restart_frame(state)

    def recenter(self, _size: object) -> None:
        self.recentered = True
        self.workflow_actions.append(("recenter",))

    def pinch_zoom_out(self, *, repetitions: int) -> None:
        self.zoom_repetitions.append(repetitions)
        self.workflow_actions.append(("zoom", repetitions))


class MemoryWatchdogTests(unittest.TestCase):
    def test_restart_screen_classifier_uses_fixed_bottom_controls(self) -> None:
        for state in (
            RESTART_SCREEN_MAP,
            RESTART_SCREEN_GARDEN,
            RESTART_SCREEN_HOME,
            RESTART_SCREEN_CLOSABLE,
            RESTART_SCREEN_SAFETY,
            RESTART_SCREEN_SPLASH,
            RESTART_SCREEN_LOGO,
            RESTART_SCREEN_LOADING,
            RESTART_SCREEN_LOGIN,
        ):
            self.assertEqual(classify_pikmin_restart_screen(restart_frame(state)), state)
        self.assertEqual(
            classify_pikmin_restart_screen(restart_frame("art_loading")),
            RESTART_SCREEN_LOADING,
        )
        self.assertEqual(
            classify_pikmin_restart_screen(restart_frame(RESTART_SCREEN_UNKNOWN)),
            RESTART_SCREEN_UNKNOWN,
        )
        centered_map = restart_frame("map_centered")
        self.assertEqual(classify_pikmin_restart_screen(centered_map), RESTART_SCREEN_MAP)
        self.assertFalse(pikmin_map_locator_visible(centered_map))
        self.assertEqual(
            classify_pikmin_restart_screen(restart_frame("map_bright")),
            RESTART_SCREEN_MAP,
        )

    def test_decoded_stream_frame_uses_the_same_screen_classifier(self) -> None:
        payload = restart_frame(RESTART_SCREEN_HOME)
        image = cv2.imdecode(np.frombuffer(payload, dtype=np.uint8), cv2.IMREAD_COLOR)
        self.assertEqual(classify_pikmin_restart_image(image), RESTART_SCREEN_HOME)

    def test_real_map_tabs_distinguish_pale_tiles_at_stream_resolutions(self) -> None:
        for state in (RESTART_SCREEN_GARDEN, RESTART_SCREEN_MAP):
            original = recorded_map_image(state)
            for width in (360, 480, 720, 1080):
                for jpeg in (False, True):
                    with self.subTest(state=state, width=width, jpeg=jpeg):
                        image = cv2.resize(original, (width, round(original.shape[0] * width / original.shape[1])), interpolation=cv2.INTER_AREA)
                        if jpeg:
                            ok, encoded = cv2.imencode(".jpg", image, [cv2.IMWRITE_JPEG_QUALITY, 75])
                            self.assertTrue(ok)
                            image = cv2.imdecode(encoded, cv2.IMREAD_COLOR)
                        self.assertEqual(classify_pikmin_restart_image(image), state)

    def test_real_3d_map_controls_override_loading_like_flower_and_road_band(self) -> None:
        image = recorded_map_image(RESTART_SCREEN_GARDEN)
        analysis = cv2.resize(image, (360, 780), interpolation=cv2.INTER_AREA)
        self.assertTrue(_pikmin_loading_progress_visible(analysis))
        self.assertEqual(classify_pikmin_restart_image(image), RESTART_SCREEN_GARDEN)
        # Without map controls the same band remains a possible loading screen.
        image[round(image.shape[0] * 0.90):] = (48, 180, 76)
        self.assertEqual(classify_pikmin_restart_image(image), RESTART_SCREEN_LOADING)

    def test_safety_warning_still_takes_priority_over_map_controls(self) -> None:
        image = recorded_map_image(RESTART_SCREEN_GARDEN)
        cv2.rectangle(image, (60, 900), (1020, 1430), (255, 255, 255), -1)
        cv2.ellipse(image, (540, 1250), (175, 70), 0, 0, 360, (165, 183, 0), 5)
        self.assertEqual(classify_pikmin_restart_image(image), RESTART_SCREEN_SAFETY)

    def test_restart_switches_recorded_3d_map_to_2d_before_preparing_scan(self) -> None:
        client = FakeAdbClient()
        image_3d = recorded_map_image(RESTART_SCREEN_GARDEN)
        image_2d = recorded_map_image(RESTART_SCREEN_MAP)
        switch_tap = ("tap", "595", "2160")
        watchdog = PikminMemoryWatchdog(
            client,
            stop_event=threading.Event(),
            capture_image=lambda: image_2d if switch_tap in client.workflow_actions else image_3d,
            launch_wait_seconds=0,
            transition_wait_seconds=0,
            waiter=lambda _seconds: False,
        )
        self.assertTrue(watchdog.restart_game(startup=True))
        self.assertEqual(client.workflow_actions.count(switch_tap), 1)
        self.assertLess(client.workflow_actions.index(switch_tap), client.workflow_actions.index(("zoom", MAX_MAP_ZOOM_OUT_PINCHES)))
        self.assertEqual(client.workflow_actions[-1], ("tap", "970", "147"))

    def test_restart_classifier_preserves_thin_safety_warning_outline(self) -> None:
        image = np.full((2340, 1080, 3), (48, 180, 76), dtype=np.uint8)
        cv2.rectangle(image, (60, 900), (1020, 1430), (255, 255, 255), -1)
        cv2.ellipse(
            image,
            (540, 1250),
            (172, 67),
            0,
            0,
            360,
            (165, 183, 0),
            1,
            cv2.LINE_AA,
        )

        self.assertEqual(classify_pikmin_restart_image(image), RESTART_SCREEN_SAFETY)

    def test_parses_available_ram_and_game_pss(self) -> None:
        total, available = parse_proc_meminfo(
            "MemTotal: 8388608 kB\nMemFree: 100 kB\nMemAvailable: 65536 kB\n"
        )
        self.assertEqual((total, available), (8388608, 65536))
        self.assertEqual(parse_total_pss_kib("TOTAL PSS: 2,621,440\n"), 2621440)
        snapshot = memory_snapshot(total, available, 2621440)
        self.assertTrue(snapshot.pressure)
        self.assertIn("system_available", snapshot.pressure_reason)
        self.assertNotIn("game_pss", snapshot.pressure_reason)

    def test_memory_reserve_scales_with_ram_and_stays_bounded(self) -> None:
        for total_mib, threshold_mib in ((4096, 256), (5520, 276), (8192, 384)):
            with self.subTest(total_mib=total_mib):
                at_threshold = memory_snapshot(total_mib * 1024, threshold_mib * 1024, 3000 * 1024)
                below_threshold = memory_snapshot(total_mib * 1024, (threshold_mib - 1) * 1024, 1000 * 1024)
                self.assertFalse(at_threshold.pressure)
                self.assertTrue(below_threshold.pressure)
                self.assertEqual(at_threshold.available_threshold_mib, threshold_mib)

    def test_memory_pressure_is_detected_before_the_old_emergency_threshold(self) -> None:
        snapshot = memory_snapshot(5520 * 1024, 220 * 1024, 1500 * 1024)
        self.assertTrue(snapshot.pressure)
        self.assertEqual(snapshot.pressure_reason, "system_available")

    def test_usb_sequence_uses_custom_low_memory_recovery_instead_of_restarting_game(self) -> None:
        client = FakeAdbClient(mem_available_mib=200)
        recovery = Mock(return_value=True)
        now = [0.0]
        watchdog = PikminMemoryWatchdog(
            client,
            stop_event=threading.Event(),
            poll_seconds=1,
            pressure_samples=2,
            cooldown_seconds=300,
            clock=lambda: now[0],
            waiter=lambda _seconds: False,
            memory_pressure_recovery=recovery,
        )

        self.assertFalse(watchdog.before_capture())
        now[0] = 1.0
        self.assertTrue(watchdog.before_capture())
        recovery.assert_called_once_with()
        self.assertEqual(watchdog.restart_count, 0)
        self.assertEqual(watchdog.last_restart_at, 1.0)
        self.assertFalse(any(
            command[:3] == ("shell", "am", "force-stop") for command in client.commands
        ))

    def test_healthy_memory_does_not_request_restart(self) -> None:
        snapshot = memory_snapshot(8 * 1024 * 1024, 3 * 1024 * 1024, 1200 * 1024)
        self.assertFalse(snapshot.pressure)
        self.assertEqual(snapshot.available_percent, 37.5)

    def test_sustained_pressure_restarts_and_restores_pikmin_map(self) -> None:
        client = FakeAdbClient()
        now = [0.0]
        updates: list[dict[str, object]] = []
        messages: list[str] = []
        watchdog = PikminMemoryWatchdog(
            client,
            stop_event=threading.Event(),
            status=messages.append,
            update=updates.append,
            poll_seconds=1,
            pressure_samples=2,
            cooldown_seconds=300,
            launch_wait_seconds=0,
            transition_wait_seconds=0,
            clock=lambda: now[0],
            waiter=lambda _seconds: False,
        )
        self.assertFalse(watchdog.before_capture())
        now[0] = 1.0
        self.assertTrue(watchdog.before_capture())
        self.assertEqual(watchdog.restart_count, 1)
        self.assertTrue(client.recentered)
        self.assertEqual(client.zoom_repetitions, [MAX_MAP_ZOOM_OUT_PINCHES])
        self.assertIn(("shell", "am", "force-stop", PIKMIN_PACKAGE), client.commands)
        self.assertIn(("shell", "svc", "power", "stayon", "usb"), client.commands)
        self.assertIn(("shell", "input", "keyevent", "KEYCODE_WAKEUP"), client.commands)
        self.assertTrue(any(command[:3] == ("shell", "monkey", "-p") for command in client.commands))
        self.assertTrue(any(bool(update["restarting"]) for update in updates))
        self.assertFalse(bool(updates[-1]["pressure"]))
        self.assertTrue(any("重新啟動" in message for message in messages))

    def test_long_game_session_restarts_before_known_gpu_crash_window(self) -> None:
        client = FakeAdbClient()
        now = [0.0]
        messages: list[str] = []
        watchdog = PikminMemoryWatchdog(
            client,
            stop_event=threading.Event(),
            status=messages.append,
            poll_seconds=1,
            preventive_restart_seconds=1800,
            launch_wait_seconds=0,
            transition_wait_seconds=0,
            clock=lambda: now[0],
            waiter=lambda _seconds: False,
        )
        watchdog.last_restart_at = 0.0
        now[0] = 1800.0

        self.assertTrue(watchdog.before_capture())
        self.assertEqual(watchdog.restart_count, 1)
        self.assertTrue(any("預防性重啟" in message for message in messages))
        self.assertTrue(any("Unity／GPU 當機" in message for message in messages))

    def test_scan_start_restarts_and_prepares_map_without_counting_memory_restart(self) -> None:
        client = FakeAdbClient()
        messages: list[str] = []
        watchdog = PikminMemoryWatchdog(
            client,
            stop_event=threading.Event(),
            status=messages.append,
            poll_seconds=10,
            launch_wait_seconds=0,
            transition_wait_seconds=0,
            clock=lambda: 25.0,
            waiter=lambda _seconds: False,
        )

        self.assertTrue(watchdog.prepare_for_scan())
        self.assertEqual(watchdog.restart_count, 0)
        self.assertEqual(watchdog.next_poll_at, 35.0)
        self.assertEqual(client.zoom_repetitions, [MAX_MAP_ZOOM_OUT_PINCHES])
        self.assertTrue(any("辨識即將開始" in message for message in messages))
        self.assertTrue(any("2D、最大範圍" in message for message in messages))

    def test_scan_start_skips_full_restart_when_stream_already_shows_2d_map(self) -> None:
        client = FakeAdbClient(restart_states=(RESTART_SCREEN_UNKNOWN,))
        map_image = cv2.imdecode(
            np.frombuffer(restart_frame(RESTART_SCREEN_MAP), dtype=np.uint8),
            cv2.IMREAD_COLOR,
        )
        watchdog = PikminMemoryWatchdog(
            client,
            stop_event=threading.Event(),
            capture_image=lambda: map_image,
            transition_wait_seconds=0,
            waiter=lambda _seconds: False,
        )

        self.assertTrue(watchdog.prepare_for_scan())
        self.assertFalse(
            any(command[:3] == ("shell", "am", "force-stop") for command in client.commands)
        )
        self.assertEqual(client.zoom_repetitions, [MAX_MAP_ZOOM_OUT_PINCHES])

    def test_usb_service_recovery_keeps_live_game_and_existing_zoom(self) -> None:
        client = FakeAdbClient(restart_states=(
            RESTART_SCREEN_MAP, RESTART_SCREEN_MAP, RESTART_SCREEN_MAP,
        ))
        messages: list[str] = []
        watchdog = PikminMemoryWatchdog(
            client,
            stop_event=threading.Event(),
            status=messages.append,
            transition_wait_seconds=0,
            waiter=lambda _seconds: False,
            clock=lambda: 20.0,
        )

        self.assertTrue(watchdog.recover_after_usb_service_restart())
        self.assertTrue(client.recentered)
        self.assertEqual(client.zoom_repetitions, [])
        self.assertFalse(any(
            command[:3] == ("shell", "am", "force-stop") for command in client.commands
        ))
        self.assertEqual(watchdog.next_poll_at, 30.0)
        self.assertTrue(any("接續目前搜尋格" in message for message in messages))

    def test_usb_service_recovery_restarts_only_when_game_is_no_longer_on_map(self) -> None:
        client = FakeAdbClient(restart_states=(RESTART_SCREEN_UNKNOWN,) * 3)
        watchdog = PikminMemoryWatchdog(
            client,
            stop_event=threading.Event(),
            transition_wait_seconds=0,
            waiter=lambda _seconds: False,
        )
        watchdog.recover_after_foreground_loss = Mock(return_value=True)  # type: ignore[method-assign]

        self.assertTrue(watchdog.recover_after_usb_service_restart())
        watchdog.recover_after_foreground_loss.assert_called_once_with()
        self.assertEqual(client.zoom_repetitions, [])

    def test_map_preparation_retries_transitional_frames_after_each_gesture(self) -> None:
        client = FakeAdbClient()
        messages: list[str] = []
        watchdog = PikminMemoryWatchdog(
            client,
            stop_event=threading.Event(),
            status=messages.append,
            capture_image=lambda: np.zeros((10, 10, 3), dtype=np.uint8),
            transition_wait_seconds=0,
            waiter=lambda _seconds: False,
        )
        states = iter((
            (RESTART_SCREEN_UNKNOWN, False), (RESTART_SCREEN_MAP, True),
            (RESTART_SCREEN_UNKNOWN, False), (RESTART_SCREEN_MAP, False),
            (RESTART_SCREEN_UNKNOWN, False), (RESTART_SCREEN_MAP, False),
        ))
        watchdog._restart_screen_state = lambda: next(states)  # type: ignore[method-assign]
        watchdog._wait_for_map_camera = lambda: True  # type: ignore[method-assign]

        self.assertTrue(watchdog._prepare_map_view(
            client.screen_size(), True, startup=True,
        ))
        self.assertTrue(client.recentered)
        self.assertEqual(client.zoom_repetitions, [MAX_MAP_ZOOM_OUT_PINCHES])

    def test_map_camera_wait_rejects_motion_then_accepts_stable_roads(self) -> None:
        client = FakeAdbClient()
        base = recorded_map_image(RESTART_SCREEN_MAP)
        moved = cv2.warpAffine(
            base, np.float32([[1, 0, 28], [0, 1, 0]]),
            (base.shape[1], base.shape[0]),
        )
        frames = iter((base, moved, moved, moved, moved))
        watchdog = PikminMemoryWatchdog(
            client,
            stop_event=threading.Event(),
            capture_image=lambda: next(frames),
            transition_wait_seconds=0,
            waiter=lambda _seconds: False,
        )
        self.assertTrue(watchdog._wait_for_map_camera())

    def test_restart_skips_optional_confirmation_when_home_is_already_visible(self) -> None:
        client = FakeAdbClient(
            restart_states=(RESTART_SCREEN_HOME, RESTART_SCREEN_GARDEN, RESTART_SCREEN_MAP)
        )
        watchdog = PikminMemoryWatchdog(
            client,
            stop_event=threading.Event(),
            launch_wait_seconds=0,
            transition_wait_seconds=0,
            waiter=lambda _seconds: False,
        )

        self.assertTrue(watchdog.restart_game())
        self.assertEqual(client.zoom_repetitions, [MAX_MAP_ZOOM_OUT_PINCHES])
        taps = [command for command in client.commands if command[:3] == ("shell", "input", "tap")]
        self.assertIn(("shell", "input", "tap", "540", "2160"), taps)
        self.assertIn(("shell", "input", "tap", "595", "2160"), taps)
        self.assertNotIn(("shell", "input", "tap", "540", "1250"), taps)

    def test_restart_ignores_unknown_frames_and_confirms_explicit_safety_warning(self) -> None:
        client = FakeAdbClient(
            restart_states=(
                RESTART_SCREEN_UNKNOWN,
                RESTART_SCREEN_UNKNOWN,
                RESTART_SCREEN_SAFETY,
                RESTART_SCREEN_HOME,
                RESTART_SCREEN_GARDEN,
                RESTART_SCREEN_MAP,
            )
        )
        watchdog = PikminMemoryWatchdog(
            client,
            stop_event=threading.Event(),
            launch_wait_seconds=0,
            transition_wait_seconds=0,
            waiter=lambda _seconds: False,
        )

        self.assertTrue(watchdog.restart_game())
        self.assertEqual(client.zoom_repetitions, [MAX_MAP_ZOOM_OUT_PINCHES])
        taps = [command for command in client.commands if command[:3] == ("shell", "input", "tap")]
        self.assertEqual(taps[0], ("shell", "input", "tap", "540", "1250"))
        self.assertIn(("shell", "input", "tap", "540", "2160"), taps)

    def test_logged_in_startup_follows_each_explicit_page_state(self) -> None:
        client = FakeAdbClient(
            restart_states=(
                RESTART_SCREEN_SPLASH,
                RESTART_SCREEN_LOGO,
                RESTART_SCREEN_LOADING,
                RESTART_SCREEN_SAFETY,
                RESTART_SCREEN_HOME,
                RESTART_SCREEN_GARDEN,
                RESTART_SCREEN_MAP,
            )
        )
        messages: list[str] = []
        watchdog = PikminMemoryWatchdog(
            client,
            stop_event=threading.Event(),
            status=messages.append,
            launch_wait_seconds=0,
            transition_wait_seconds=0,
            waiter=lambda _seconds: False,
        )

        self.assertTrue(watchdog.restart_game())
        for expected in ("啟動畫面", "商標", "加載畫面", "安全警示", "主畫面", "3D 地圖", "2D 地圖"):
            self.assertTrue(any(expected in message for message in messages), expected)

    def test_unknown_3d_transition_never_guesses_the_2d_button(self) -> None:
        client = FakeAdbClient(
            restart_states=(
                RESTART_SCREEN_HOME,
                RESTART_SCREEN_UNKNOWN,
                RESTART_SCREEN_MAP,
            )
        )
        watchdog = PikminMemoryWatchdog(
            client,
            stop_event=threading.Event(),
            launch_wait_seconds=0,
            transition_wait_seconds=0,
            waiter=lambda _seconds: False,
        )

        self.assertTrue(watchdog.restart_game())
        taps = [command for command in client.commands if command[:3] == ("shell", "input", "tap")]
        self.assertIn(("shell", "input", "tap", "540", "2160"), taps)
        self.assertNotIn(("shell", "input", "tap", "595", "2160"), taps)
        self.assertNotIn(("shell", "input", "tap", "540", "1250"), taps)

    def test_login_page_stops_without_tapping_game_controls(self) -> None:
        client = FakeAdbClient(restart_states=(RESTART_SCREEN_LOGIN,))
        messages: list[str] = []
        watchdog = PikminMemoryWatchdog(
            client,
            stop_event=threading.Event(),
            status=messages.append,
            launch_wait_seconds=0,
            transition_wait_seconds=0,
            waiter=lambda _seconds: False,
        )

        self.assertFalse(watchdog.restart_game())
        self.assertTrue(any("完成登入" in message for message in messages))
        self.assertFalse(any(command[:3] == ("shell", "input", "tap") for command in client.commands))

    def test_map_preparation_orders_zoom_then_location_then_north(self) -> None:
        client = FakeAdbClient(
            restart_states=(RESTART_SCREEN_HOME, RESTART_SCREEN_GARDEN, RESTART_SCREEN_MAP)
        )
        watchdog = PikminMemoryWatchdog(
            client,
            stop_event=threading.Event(),
            launch_wait_seconds=0,
            transition_wait_seconds=0,
            waiter=lambda _seconds: False,
        )

        self.assertTrue(watchdog.restart_game())
        zoom_index = client.workflow_actions.index(("zoom", MAX_MAP_ZOOM_OUT_PINCHES))
        recenter_index = client.workflow_actions.index(("recenter",))
        north_index = client.workflow_actions.index(("tap", "970", "147"))
        self.assertLess(zoom_index, recenter_index)
        self.assertLess(recenter_index, north_index)

    def test_restart_does_not_tap_hidden_locator_on_an_already_centered_map(self) -> None:
        client = FakeAdbClient(
            restart_states=(RESTART_SCREEN_HOME, RESTART_SCREEN_GARDEN, "map_centered")
        )
        watchdog = PikminMemoryWatchdog(
            client,
            stop_event=threading.Event(),
            launch_wait_seconds=0,
            transition_wait_seconds=0,
            waiter=lambda _seconds: False,
        )

        self.assertTrue(watchdog.restart_game())
        self.assertFalse(client.recentered)
        self.assertEqual(client.zoom_repetitions, [MAX_MAP_ZOOM_OUT_PINCHES])

    def test_persistent_unknown_screen_fails_without_sending_android_back(self) -> None:
        client = FakeAdbClient(restart_states=(RESTART_SCREEN_UNKNOWN,) * 10)
        watchdog = PikminMemoryWatchdog(
            client,
            stop_event=threading.Event(),
            launch_wait_seconds=0,
            transition_wait_seconds=0,
            waiter=lambda _seconds: False,
        )

        self.assertFalse(watchdog.restart_game())
        self.assertFalse(any(command[-2:] == ("keyevent", "BACK") for command in client.commands))
        self.assertNotIn(
            ("shell", "input", "tap", "540", "1250"),
            client.commands,
        )


if __name__ == "__main__":
    unittest.main()
