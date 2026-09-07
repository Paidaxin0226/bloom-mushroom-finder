from __future__ import annotations

import threading
import tempfile
import subprocess
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import cv2
import numpy as np

from bloompilot.device_control import DeviceResponse
from bloompilot.map_targeting import TargetAlignmentError
from bloompilot.phone_automation import MapRestoreResult
from bloompilot.mushroom_coordinate import (
    MushroomCoordinateResolver,
    PreciseCoordinateError,
    CoordinateRecoveryError,
    PreciseCoordinateCache,
    enrich_detections_with_precise_coordinates,
    mushroom_destination_menu_visible,
    mushroom_detail_visible,
    mushroom_route_cancel_confirmation_visible,
    mushroom_route_active,
    mushroom_selection_card_center,
)
from test_map_targeting import textured_map


class FakeAdb:
    def __init__(self) -> None:
        self.commands: list[tuple[str, ...]] = []

    def run(self, *arguments: str, **_kwargs: object) -> object:
        self.commands.append(arguments)
        return object()


class FakeController:
    connected = True

    def __init__(self, coordinate: tuple[float, float] = (43.70615, -79.641128)) -> None:
        self.coordinate = coordinate
        self.commands: list[tuple[str, dict[str, object]]] = []

    def command(
        self,
        command: str,
        payload: dict[str, object] | None = None,
        *,
        require_ok: bool = True,
    ) -> DeviceResponse:
        del require_ok
        value = dict(payload or {})
        self.commands.append((command, value))
        status = {"current": {"lat": 25.0, "lng": 121.0}}
        raw: dict[str, object] = {}
        if command == "coordinateCaptureStatus":
            raw["capture"] = {
                "captured": True,
                "lat": self.coordinate[0],
                "lng": self.coordinate[1],
            }
        return DeviceResponse(True, "ok", status, raw)

    def connect(self) -> DeviceResponse:
        return DeviceResponse(True, "ok", {}, {})


def selection_frame() -> np.ndarray:
    image = np.full((1000, 500, 3), 180, dtype=np.uint8)
    cv2.rectangle(image, (120, 300), (390, 380), (15, 15, 15), -1)
    return image


def map_frame() -> np.ndarray:
    return np.full((1000, 500, 3), 180, dtype=np.uint8)


def detail_frame() -> np.ndarray:
    image = np.full((1000, 500, 3), 80, dtype=np.uint8)
    cv2.rectangle(image, (0, 470), (499, 960), (245, 245, 245), -1)
    return image


def menu_frame() -> np.ndarray:
    image = np.full((1000, 500, 3), 55, dtype=np.uint8)
    cv2.rectangle(image, (55, 640), (445, 840), (250, 250, 250), -1)
    return image


def active_route_detail_frame() -> np.ndarray:
    image = detail_frame()
    height, width = image.shape[:2]
    y1, y2 = round(height * 0.004), round(height * 0.072)
    x1, x2 = round(width * 0.80), round(width * 0.94)
    image[y1:y2, x1:x2] = (230, 160, 45)
    image[y1:y2, x1 : x1 + round((x2 - x1) * 0.22)] = 0
    return image


class MushroomCoordinateTests(unittest.TestCase):
    def test_settled_map_waits_through_an_unknown_transition_frame(self) -> None:
        now = [0.0]

        def clock() -> float:
            now[0] += 0.25
            return now[0]

        resolver = MushroomCoordinateResolver(
            FakeAdb(), FakeController(), stop_event=threading.Event(),
            capture_image=map_frame, clock=clock,
        )
        states = iter(("unknown", "map", "map"))
        with (
            patch(
                "bloompilot.mushroom_coordinate.classify_pikmin_restart_image",
                side_effect=lambda _image: next(states, "map"),
            ),
            patch("bloompilot.mushroom_coordinate.map_camera_is_stable", return_value=True),
        ):
            self.assertEqual(resolver._settled_map().shape, map_frame().shape)

    def test_return_from_map_intent_closes_menu_then_detail_without_blind_back(self) -> None:
        frames = {"menu": menu_frame(), "detail": detail_frame(), "map": np.zeros((1000, 500, 3), dtype=np.uint8)}
        page = "menu"
        adb = FakeAdb()

        def run(*args: str, **_kwargs: object) -> object:
            nonlocal page
            adb.commands.append(args)
            if args[:3] == ("shell", "input", "keyevent"):
                self.assertEqual(page, "menu", "Android Back must not blindly dismiss the detail page")
                page = "detail"
            elif args[:3] == ("shell", "input", "tap"):
                self.assertEqual(page, "detail")
                self.assertEqual(args[3:5], ("50", "915"))
                page = "map"
            return object()

        adb.run = run
        resolver = MushroomCoordinateResolver(
            adb, FakeController(), stop_event=threading.Event(), capture_image=lambda: frames[page],
        )
        with patch("bloompilot.mushroom_coordinate.classify_pikmin_restart_image", side_effect=lambda _image: "map" if page == "map" else "unknown"):
            self.assertTrue(resolver._return_to_map())
        self.assertEqual(len(adb.commands), 2)

    def test_visual_page_detectors_use_stable_geometry(self) -> None:
        card = mushroom_selection_card_center(selection_frame())
        self.assertIsNotNone(card)
        assert card is not None
        self.assertGreater(card[0], 250)
        self.assertTrue(mushroom_detail_visible(detail_frame()))
        self.assertTrue(mushroom_destination_menu_visible(menu_frame()))
        self.assertFalse(mushroom_destination_menu_visible(detail_frame()))
        self.assertFalse(mushroom_route_cancel_confirmation_visible(detail_frame()))
        self.assertFalse(mushroom_route_active(detail_frame()))
        self.assertTrue(mushroom_route_active(active_route_detail_frame()))

    def test_resolver_captures_coordinate_without_movement_command(self) -> None:
        frames = iter(
            [
                map_frame(),
                selection_frame(),
                detail_frame(),
                detail_frame(),
                menu_frame(),
                menu_frame(),
                selection_frame(),
            ]
        )
        adb = FakeAdb()
        controller = FakeController()
        resolver = MushroomCoordinateResolver(
            adb,  # type: ignore[arg-type]
            controller,  # type: ignore[arg-type]
            stop_event=threading.Event(),
            capture_image=lambda: next(frames),
        )

        with patch(
            "bloompilot.mushroom_coordinate.classify_pikmin_restart_image",
            return_value="map",
        ):
            result = resolver.resolve({"xyxy": [210, 480, 290, 580]})

        self.assertEqual(result["lat"], 43.70615)
        self.assertEqual(result["lng"], -79.641128)
        self.assertTrue(result["captured_without_movement"])
        names = [command for command, _payload in controller.commands]
        self.assertIn("beginCoordinateCapture", names)
        self.assertIn("coordinateCaptureStatus", names)
        self.assertEqual(names[-1], "endCoordinateCapture")
        self.assertFalse(
            {"teleport", "navigate", "addBookmark", "startFlower"}.intersection(names)
        )
        taps = [arguments for arguments in adb.commands if arguments[:3] == ("shell", "input", "tap")]
        self.assertEqual(len(taps), 4)
        self.assertEqual(taps[-1][-2:], ("250", "845"))

    def test_existing_game_route_is_not_cancelled_or_replaced(self) -> None:
        frames = iter([
            map_frame(),
            selection_frame(),
            active_route_detail_frame(),
            active_route_detail_frame(),
            selection_frame(),
        ])
        adb = FakeAdb()
        controller = FakeController()
        resolver = MushroomCoordinateResolver(
            adb,  # type: ignore[arg-type]
            controller,  # type: ignore[arg-type]
            stop_event=threading.Event(),
            capture_image=lambda: next(frames),
        )

        with (
            patch(
                "bloompilot.mushroom_coordinate.classify_pikmin_restart_image",
                return_value="map",
            ),
            self.assertRaisesRegex(PreciseCoordinateError, "避免修改既有路線"),
        ):
            resolver.resolve({"xyxy": [210, 480, 290, 580]})

        names = [command for command, _payload in controller.commands]
        self.assertNotIn("teleport", names)
        self.assertNotIn("navigate", names)
        taps = [arguments for arguments in adb.commands if arguments[:3] == ("shell", "input", "tap")]
        # Only the target and name card were tapped; no route action ran.
        self.assertEqual(len(taps), 2)

    def test_capture_lease_is_ended_when_target_card_does_not_appear(self) -> None:
        adb = FakeAdb()
        controller = FakeController()
        resolver = MushroomCoordinateResolver(
            adb, controller, stop_event=threading.Event(), capture_image=map_frame,
        )
        with (
            patch.object(resolver, "_wait_for", return_value=False),
            patch("bloompilot.mushroom_coordinate.classify_pikmin_restart_image", return_value="map"),
            self.assertRaises(PreciseCoordinateError),
        ):
            resolver.resolve({"xyxy": [210, 480, 290, 580]})
        self.assertEqual(controller.commands[-1][0], "endCoordinateCapture")
        self.assertNotIn("teleport", [name for name, _ in controller.commands])


class SimulatedMap:
    """A map with imperfect drags; screenshots/model boxes follow actual motion."""

    def __init__(self, *, clipped: bool = False) -> None:
        self.commands: list[tuple[str, ...]] = []
        self.background = textured_map()
        self.targets = (
            [[465, 160, 515, 220], [-15, 690, 35, 750]] if clipped
            else [[425, 160, 475, 220], [25, 690, 75, 750]]
        )
        for x1, y1, x2, y2 in self.targets:
            self.background[y1:y2, max(0, x1):min(500, x2)] = (220, 70, 30)
        self.dx = self.dy = 0
        self.page = "map"
        self.clicked: list[int] = []
        self.drag_gain = 0.90
        self.drag_slop = 0.0
        self.drag_threshold = 0.0
        self.timeout_first_drag = False
        self.after_drag = lambda: None

    def frame(self) -> np.ndarray:
        if self.page == "map":
            return cv2.warpAffine(self.background, np.float32([[1, 0, self.dx], [0, 1, self.dy]]), (500, 1000))
        return {"selection": selection_frame, "detail": detail_frame, "menu": menu_frame}[self.page]()

    def detect(self, _image: np.ndarray) -> list[dict[str, object]]:
        results = []
        for x1, y1, x2, y2 in self.targets:
            x1, x2 = max(0, x1 + self.dx), min(500, x2 + self.dx)
            y1, y2 = max(0, y1 + self.dy), min(1000, y2 + self.dy)
            if x2 > x1 and y2 > y1:
                results.append({"class_name": "blue", "xyxy": [x1, y1, x2, y2]})
        return results

    def run(self, *args: str, **_options: object) -> object:
        self.commands.append(args)
        if args[:3] == ("shell", "input", "swipe"):
            x1, y1, x2, y2 = map(int, args[3:7])
            distance = float(np.hypot(x2 - x1, y2 - y1))
            gain = self.drag_gain * max(0.0, 1.0 - self.drag_slop / max(1.0, distance))
            if distance <= self.drag_threshold:
                gain = 0.0
            self.dx += round((x2 - x1) * gain)
            self.dy += round((y2 - y1) * gain)
            self.after_drag()
            if self.timeout_first_drag:
                self.timeout_first_drag = False
                raise subprocess.TimeoutExpired("adb swipe", 8)
        elif args[:3] == ("shell", "input", "tap"):
            x, y = int(args[3]), int(args[4])
            if self.page == "map":
                for index, (x1, y1, x2, y2) in enumerate(self.targets):
                    if x1 + self.dx <= x <= x2 + self.dx and y1 + self.dy <= y <= y2 + self.dy:
                        self.clicked.append(index)
                        self.page = "selection"
                        break
                else:
                    raise AssertionError(f"A stale or incorrect target was tapped: {(x, y)}")
            elif self.page == "selection":
                self.page = "detail"
            elif self.page == "detail":
                self.page = "menu"
            elif self.page == "menu":
                self.page = "map"
        return object()


class ReframingTests(unittest.TestCase):
    def setUp(self) -> None:
        self.adb = SimulatedMap(clipped=True)
        self.controller = FakeController()
        self.event = threading.Event()
        self.resolver = MushroomCoordinateResolver(
            self.adb, self.controller, stop_event=self.event, capture_image=self.adb.frame,
        )
        self.reference = self.adb.frame()
        self.detections = self.adb.detect(self.reference)
        self.options = {"reference_image": self.reference, "detect_targets": self.adb.detect}
        self.enterContext(patch.object(self.resolver, "_settled_map", side_effect=self.resolver._capture))
        self.enterContext(patch(
            "bloompilot.mushroom_coordinate.classify_pikmin_restart_image",
            side_effect=lambda _image: "map" if self.adb.page == "map" else "unknown",
        ))
        self.enterContext(patch(
            "bloompilot.mushroom_coordinate.mushroom_selection_card_center",
            side_effect=lambda _image: (300, 350) if self.adb.page == "selection" else None,
        ))

    def test_complete_near_edge_targets_are_clicked_without_any_drag(self) -> None:
        self.adb = SimulatedMap()
        self.resolver.adb = self.adb
        self.resolver.capture_image = self.adb.frame
        reference = self.adb.frame()
        detections = self.adb.detect(reference)
        enrich_detections_with_precise_coordinates(
            detections, self.resolver, reference_image=reference, detect_targets=self.adb.detect,
        )
        self.assertEqual(self.adb.clicked, [0, 1])
        self.assertFalse(any(cmd[:3] == ("shell", "input", "swipe") for cmd in self.adb.commands))
        for detection in detections:
            self.assertNotIn("coordinate_error", detection)
            self.assertFalse(detection["precise_coordinate"]["target_reframed"])
            self.assertTrue(detection["precise_coordinate"]["exact"])

    def test_two_targets_from_one_frame_are_reidentified_and_view_is_restored(self) -> None:
        original_boxes = [list(item["xyxy"]) for item in self.detections]
        enrich_detections_with_precise_coordinates(self.detections, self.resolver, **self.options)
        self.assertEqual(self.adb.clicked, [0, 1])
        for detection in self.detections:
            self.assertNotIn("coordinate_error", detection)
            self.assertTrue(detection["precise_coordinate"]["target_reframed"])
            self.assertTrue(detection["precise_coordinate"]["exact"])
        self.assertEqual([item["xyxy"] for item in self.detections], original_boxes)
        self.assertLessEqual(max(abs(self.adb.dx), abs(self.adb.dy)), 3)
        names = [name for name, _ in self.controller.commands]
        self.assertEqual(names.count("beginCoordinateCapture"), 2)
        self.assertEqual(names.count("endCoordinateCapture"), 2)
        self.assertFalse({"teleport", "navigate", "addBookmark"}.intersection(names))

    def test_no_movement_is_limited_to_two_adjustments_and_keeps_failed_hit(self) -> None:
        self.adb.drag_gain = 0.0
        with tempfile.TemporaryDirectory() as directory:
            enrich_detections_with_precise_coordinates(
                self.detections[:1], self.resolver, **self.options, failure_dir=Path(directory),
            )
            self.assertIn("兩次", self.detections[0]["coordinate_error"])
            self.assertEqual(len(list(Path(directory).glob("*.json"))), 1)
            self.assertEqual(len(list(Path(directory).glob("*.png"))), 2)
        self.assertEqual(self.adb.clicked, [])
        self.assertEqual(sum(cmd[:3] == ("shell", "input", "swipe") for cmd in self.adb.commands), 2)
        self.assertNotIn("beginCoordinateCapture", [name for name, _ in self.controller.commands])

    def test_timed_out_drag_may_have_moved_map_and_is_still_restored(self) -> None:
        self.adb.timeout_first_drag = True
        with self.assertRaises(PreciseCoordinateError):
            self.resolver.resolve(self.detections[0], **self.options)
        self.assertEqual(self.adb.clicked, [])
        self.assertLessEqual(max(abs(self.adb.dx), abs(self.adb.dy)), 3)

    def test_similar_nearby_target_is_not_guessed_after_drag(self) -> None:
        def ambiguous_detect(image: np.ndarray) -> list[dict[str, object]]:
            detections = self.adb.detect(image)
            if self.adb.dx < -50:
                detections.append(dict(detections[0]))
            return detections

        with self.assertRaisesRegex(PreciseCoordinateError, "唯一確認"):
            self.resolver.resolve(self.detections[0], reference_image=self.reference, detect_targets=ambiguous_detect)
        self.assertEqual(self.adb.clicked, [])
        self.assertLessEqual(max(abs(self.adb.dx), abs(self.adb.dy)), 3)

    def test_failed_recovery_is_fatal_and_no_following_target_is_clicked(self) -> None:
        recover = Mock(return_value=False)
        with (
            patch.object(self.resolver, "_return_to_map", return_value=False),
            self.assertRaises(CoordinateRecoveryError),
        ):
            enrich_detections_with_precise_coordinates(
                self.detections, self.resolver, **self.options, recover_map=recover,
            )
        self.assertEqual(self.adb.clicked, [0])
        recover.assert_called_once()
        self.assertEqual(self.controller.commands[-1][0], "endCoordinateCapture")

    def test_recovery_callback_must_restore_the_original_view_before_continuing(self) -> None:
        def recover() -> bool:
            self.adb.dx = self.adb.dy = 0
            self.adb.page = "map"
            return True

        recovery = Mock(side_effect=recover)
        with patch.object(self.resolver, "_return_to_map", return_value=False):
            result = self.resolver.resolve(self.detections[0], **self.options, recover_map=recovery)
        self.assertTrue(result["exact"])
        recovery.assert_called_once()
        self.assertEqual((self.adb.dx, self.adb.dy), (0, 0))

    def test_recovery_learns_phone_drag_gain_and_touch_threshold(self) -> None:
        # MI 9 at maximum zoom: a 174 px touch moved the map 286 px, and
        # a 112 px touch moved it 180 px. One-to-one corrections oscillated.
        self.adb.drag_gain = 1.72
        self.adb.drag_slop = 8.0
        self.adb.drag_threshold = 12.0
        for displacement in ((-174, 0), (49, 0), (0, 210), (-90, 130), (14, 0), (0, 14)):
            with self.subTest(displacement=displacement):
                self.adb.dx, self.adb.dy = displacement
                self.resolver._restore_view(self.reference, self.adb.detect, None, True)
                self.assertLessEqual(max(abs(self.adb.dx), abs(self.adb.dy)), 3)
        self.assertEqual(self.adb.clicked, [])

    def test_rebuilt_grid_gets_corrections_after_local_attempts_are_exhausted(self) -> None:
        self.adb.dx = 40
        self.adb.drag_gain = 0.0

        def recover() -> bool:
            self.adb.dx = 30
            self.adb.drag_gain = 1.72
            self.adb.drag_slop = 8.0
            return True

        recovery = Mock(side_effect=recover)
        self.resolver._restore_view(self.reference, self.adb.detect, recovery, True)
        recovery.assert_called_once()
        self.assertLessEqual(max(abs(self.adb.dx), abs(self.adb.dy)), 3)
        self.assertEqual(self.adb.clicked, [])

    def test_verified_grid_replay_continues_when_sparse_map_cannot_pixel_align(self) -> None:
        messages: list[str] = []
        self.resolver.status = messages.append
        recovery = Mock(return_value=MapRestoreResult(True, grid_replayed=True))
        with patch.object(
            self.resolver,
            "_alignment",
            side_effect=TargetAlignmentError("無法確認地圖位移或縮放已改變。"),
        ):
            self.resolver._restore_view(
                self.reference,
                self.adb.detect,
                recovery,
                True,
            )

        recovery.assert_called_once()
        self.assertTrue(any("已略過局部微調並繼續序列" in item for item in messages))

    def test_cancel_during_drag_stops_further_gestures(self) -> None:
        self.adb.after_drag = self.event.set
        with self.assertRaisesRegex(PreciseCoordinateError, "已停止"):
            self.resolver.resolve(self.detections[0], **self.options)
        self.assertEqual(len(self.adb.commands), 1)
        self.assertEqual(self.adb.clicked, [])

    def test_recovery_success_signal_without_matching_view_is_rejected(self) -> None:
        recover = Mock(return_value=True)
        with (
            patch.object(self.resolver, "_return_to_map", return_value=False),
            patch("bloompilot.mushroom_coordinate.map_translation", return_value=None),
            self.assertRaises(CoordinateRecoveryError),
        ):
            self.resolver._restore_view(
                np.flipud(self.reference), self.adb.detect, recover, False,
            )
        recover.assert_called_once()

    def test_total_deadline_stops_coordinate_polling_before_another_command(self) -> None:
        self.resolver._deadline = 0.0
        with self.assertRaisesRegex(PreciseCoordinateError, "逾時"):
            self.resolver._wait_for_coordinate("session", 7.0)
        self.assertEqual(self.controller.commands, [])


class CoordinateCacheTests(unittest.TestCase):
    def test_failure_cache_follows_verified_map_motion_across_pixel_bins(self) -> None:
        adb = SimulatedMap()
        frame = adb.frame()
        cache = PreciseCoordinateCache()
        target = adb.detect(frame)[0]
        cache.pending([target], frame)
        target["coordinate_error"] = "暫時無法確認"
        cache.remember([target], frame)
        adb.dx = -17
        moved = adb.frame()
        current = adb.detect(moved)[0]
        self.assertEqual(cache.pending([current], moved), [])
        self.assertEqual(current["coordinate_error"], "暫時無法確認")
        cache.clear()
        self.assertEqual(len(cache.pending([dict(current)], moved)), 1)

    def test_cache_does_not_assign_coordinates_to_changed_or_ambiguous_targets(self) -> None:
        adb = SimulatedMap()
        frame = adb.frame()
        target = adb.detect(frame)[0]
        cache = PreciseCoordinateCache()
        cache.pending([target], frame)
        target["precise_coordinate"] = {"lat": 25.0, "lng": 121.0}
        cache.remember([target], frame)
        fresh = adb.detect(frame)[0]
        ambiguous = [fresh, dict(fresh)]
        self.assertEqual(len(cache.pending(ambiguous, frame)), 2)
        self.assertNotIn("precise_coordinate", fresh)
        changed = frame.copy()
        changed[160:220, 425:475] = (20, 20, 220)
        replacement = adb.detect(changed)[0]
        self.assertEqual(len(cache.pending([replacement], changed)), 1)
        self.assertNotIn("precise_coordinate", replacement)


if __name__ == "__main__":
    unittest.main()
