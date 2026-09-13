from __future__ import annotations

import threading
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import cv2
import numpy as np

from bloompilot.daily_review import daily_review_action, read_daily_review
from bloompilot.detection import filter_terrain_false_positives
from bloompilot.memory_watchdog import (
    PikminMemoryWatchdog, RESTART_SCREEN_DAILY_REVIEW, classify_pikmin_restart_image,
)
from bloompilot.phone_automation import MapRecoveryError, run_rectangular_scan
from test_memory_watchdog import FakeAdbClient as RestartAdb, restart_frame
from test_phone_automation import FakeAdbClient


FIXTURES = Path(__file__).parent / "fixtures" / "recognition_regressions"


class RecognitionRegressionTests(unittest.TestCase):
    def test_button_templates_work_with_ultralytics_grayscale_imread_wrapper(self):
        from bloompilot.daily_review import _review_button_templates
        original = cv2.imread
        def wrapped(path, flags=cv2.IMREAD_COLOR):
            image = original(path, flags)
            return image[..., None] if image is not None and image.ndim == 2 else image
        _review_button_templates.cache_clear()
        self.addCleanup(_review_button_templates.cache_clear)
        with patch.object(cv2, 'imread', side_effect=wrapped):
            self.test_actual_confirm_and_done_buttons_exclude_share_icon()

    def test_missing_frame_during_review_does_not_reuse_an_action(self):
        self.assertEqual(read_daily_review(None, continuing=True), (False, None))

    def test_recorded_recap_share_icon_is_never_classified_as_map_locator(self):
        for name in ("recap", "mood", "final"):
            frame = cv2.imread(str(FIXTURES / f"daily_{name}.png"))
            self.assertIsNotNone(frame)
            for width in (360, 540, 1080):
                h, w = frame.shape[:2]
                resized = cv2.resize(frame, (width, round(h*width/w)))
                self.assertEqual(classify_pikmin_restart_image(resized), RESTART_SCREEN_DAILY_REVIEW)

    def test_actual_confirm_and_done_buttons_exclude_share_icon(self):
        for name in ("recap", "final"):
            frame = cv2.imread(str(FIXTURES / f"daily_{name}.png"))
            visible, action = read_daily_review(frame)
            self.assertTrue(visible)
            self.assertIsNotNone(action)
            self.assertAlmostEqual(action[0]/frame.shape[1], .5, delta=.02)
            self.assertAlmostEqual(action[1]/frame.shape[0], .923, delta=.01)

    def test_mood_uses_user_selected_middle_card(self):
        frame = cv2.imread(str(FIXTURES / "daily_mood.png"))
        visible, action = read_daily_review(frame)
        self.assertTrue(visible)
        self.assertEqual(action, (frame.shape[1]//2, round(frame.shape[0]*.69)))

    def test_recorded_ponds_are_rejected_even_at_high_model_confidence(self):
        paths = list(FIXTURES.glob("pond_*.png"))
        self.assertEqual(len(paths), 5)
        for path in paths:
            with self.subTest(path=path.name):
                image = cv2.imread(str(path))
                h, w = image.shape[:2]
                detection = {"class_name": "Large_Crystal", "confidence": 0.99, "xyxy": [0, 0, w, h]}
                self.assertEqual(filter_terrain_false_positives(image, [detection]), [])
                self.assertEqual(detection["confidence"], 0.99)

    def test_real_crystal_sprite_on_land_and_water_survives_scale_and_brightness(self):
        sprite = cv2.imread(str(FIXTURES / "crystal.png"), cv2.IMREAD_UNCHANGED)
        self.assertIsNotNone(sprite)
        for size in (96, 180, 256):
            for background in ((70, 190, 70), (235, 172, 80)):
                for brightness in (0.65, 1.0):
                    resized = cv2.resize(sprite, (size, size))
                    alpha = resized[:, :, 3:4].astype(float) / 255
                    image = np.uint8((resized[:, :, :3] * alpha + np.array(background) * (1 - alpha)) * brightness)
                    detection = {"class_name": "Large_Crystal", "xyxy": [0, 0, size, size]}
                    self.assertEqual(filter_terrain_false_positives(image, [detection]), [detection])

    def test_daily_review_requires_title_and_only_recognized_navigation(self):
        def line(text, y=800):
            return {"text": text, "x": 150, "y": y, "width": 200, "height": 40, "screen_height": 1000}
        self.assertEqual(daily_review_action([line("下一步")]), (False, None))
        self.assertEqual(daily_review_action([line("今日 回 顧", 150), line("下一步")]), (True, (250, 820)))
        self.assertEqual(daily_review_action([line("今日回顧", 150), line("購買")]), (True, None))
        self.assertEqual(daily_review_action([line("完成")], continuing=True), (True, (250, 820)))

    def test_daily_review_recovery_advances_then_verifies_and_prepares_map(self):
        adb = RestartAdb()
        watchdog = PikminMemoryWatchdog(adb, stop_event=threading.Event(), transition_wait_seconds=0)
        watchdog._review_action = (540, 2000)
        with (
            patch.object(watchdog, "_restart_screen_state", side_effect=[
                (RESTART_SCREEN_DAILY_REVIEW, False), (RESTART_SCREEN_DAILY_REVIEW, False), ("map", True),
            ]),
            patch.object(watchdog, "_prepare_map_view", return_value=True) as prepare,
        ):
            self.assertTrue(watchdog.ensure_scan_map())
        self.assertIn(("shell", "input", "tap", "540", "2000"), adb.commands)
        self.assertEqual(len([cmd for cmd in adb.commands if cmd[:3] == ("shell", "input", "tap")]), 1)
        prepare.assert_called_once()

    def test_unknown_page_never_gets_a_blind_tap(self):
        adb = RestartAdb()
        watchdog = PikminMemoryWatchdog(adb, stop_event=threading.Event(), transition_wait_seconds=0)
        with patch.object(watchdog, "_restart_screen_state", return_value=("unknown", False)):
            with self.assertRaises(MapRecoveryError):
                watchdog.ensure_scan_map()
        self.assertEqual(adb.commands, [])

    def test_ocr_failure_never_reuses_the_previous_pages_action(self):
        frame = cv2.imread(str(FIXTURES / "daily_recap.png"))
        watchdog = PikminMemoryWatchdog(RestartAdb(), stop_event=threading.Event(), capture_image=lambda: frame)
        watchdog._review_action = (999, 999)
        with patch("bloompilot.daily_review.read_daily_review", return_value=(False, None)):
            self.assertEqual(watchdog._restart_screen_state()[0], RESTART_SCREEN_DAILY_REVIEW)
        self.assertIsNone(watchdog._review_action)

    def test_overlay_frame_is_discarded_and_search_resumes_after_recovery(self):
        adb = FakeAdbClient()
        frames = iter([restart_frame("home"), restart_frame("map")])
        checks = iter([False, False, True, False])
        capture = Mock(return_value=None)
        result = run_rectangular_scan(
            adb, horizontal_radius=0, vertical_radius=0, settle_seconds=0,
            swipe_duration_ms=100, stop_event=threading.Event(), on_capture=capture,
            capture_png=lambda: next(frames), ensure_map=lambda: next(checks, False),
        )
        self.assertTrue(result.completed)
        self.assertEqual(capture.call_count, 1)
        self.assertEqual(capture.call_args.args[1], restart_frame("map"))

    def test_failed_overlay_recovery_sends_no_following_swipe_or_recenter(self):
        adb = FakeAdbClient()
        with self.assertRaises(MapRecoveryError):
            run_rectangular_scan(
                adb, horizontal_radius=1, vertical_radius=0, settle_seconds=0,
                swipe_duration_ms=100, stop_event=threading.Event(), on_capture=Mock(),
                ensure_map=Mock(side_effect=MapRecoveryError("unknown page")),
            )
        self.assertFalse(any(isinstance(action, tuple) and action[0] in {"swipe", "recenter"} for action in adb.actions))

    def test_recovery_during_vertical_leg_replays_both_grid_axes(self):
        adb = FakeAdbClient()
        position = [0, 0]
        recovered = False
        captured = []
        def recenter(_size):
            position[:] = [0, 0]
        def swipe(direction, _size, _duration):
            dx, dy = {"east": (1, 0), "west": (-1, 0), "north": (0, 1), "south": (0, -1)}[direction]
            position[0] += dx
            position[1] += dy
        def ensure():
            nonlocal recovered
            if not recovered and position == [1, 0]:
                position[:] = [0, 0]
                recovered = True
                return True
            return False
        def capture(point, _payload):
            self.assertEqual(position, [point.grid_x, point.grid_y])
            captured.append(point)
        adb.recenter, adb.swipe = recenter, swipe
        result = run_rectangular_scan(
            adb, horizontal_radius=1, vertical_radius=1, settle_seconds=0,
            swipe_duration_ms=100, stop_event=threading.Event(), on_capture=capture,
            capture_png=lambda: restart_frame("map"), ensure_map=ensure,
        )
        self.assertTrue(result.completed)
        self.assertTrue(recovered)
        self.assertEqual(len(captured), 9)


if __name__ == "__main__":
    unittest.main()
