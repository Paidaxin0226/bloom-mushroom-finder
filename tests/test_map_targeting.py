from __future__ import annotations

import unittest
from unittest.mock import patch

import cv2
import numpy as np

from bloompilot.map_targeting import (
    MapDragCalibration, TargetAlignmentError, map_camera_is_stable, map_translation,
    match_target, plan_map_drag,
    target_is_clipped, target_is_safe,
)


def textured_map() -> np.ndarray:
    rng = np.random.default_rng(28)
    image = np.full((1000, 500, 3), 185, dtype=np.uint8)
    for _ in range(600):
        x, y = int(rng.integers(5, 495)), int(rng.integers(5, 995))
        color = tuple(int(value) for value in rng.integers(20, 230, 3))
        cv2.circle(image, (x, y), int(rng.integers(2, 9)), color, -1)
    return image


class MapTargetingTests(unittest.TestCase):
    def test_drag_calibration_accounts_for_game_motion_and_activation_threshold(self) -> None:
        calibration = MapDragCalibration()
        calibration.observe((0, 0, 174, 0), (286.0, 0.0))
        self.assertAlmostEqual(calibration.correction((-112.0, 0.0))[0], -68.14, delta=0.1)
        calibration.observe((174, 0, 62, 0), (-180.0, 0.0))
        # A small residual needs enough touch movement to cross the threshold.
        dx, dy = calibration.correction((8.0, 0.0))
        self.assertAlmostEqual(dx, 11.4, delta=0.2)
        self.assertEqual(dy, 0.0)

    def test_drag_calibration_rejects_unrelated_map_motion(self) -> None:
        calibration = MapDragCalibration()
        for movement in ((-100.0, 0.0), (0.0, 100.0), (100.0, 100.0)):
            calibration.observe((0, 0, 100, 0), movement)
        self.assertEqual(calibration.correction((20.0, -30.0)), (20.0, -30.0))

    def test_clipped_boxes_still_require_reframing(self) -> None:
        for box in (
            [0, 0, 60, 70], [450, 0, 500, 70],
            [0, 920, 60, 1000], [450, 920, 500, 1000],
            [-10, 400, 70, 500], [420, 400, 510, 500],
            [0.4, 400, 70, 500], [420, 400, 499.6, 500],
            [210, 0.4, 290, 70], [210, 930, 290, 999.6],
        ):
            with self.subTest(box=box):
                self.assertTrue(target_is_clipped({"xyxy": box}, (1000, 500, 3)))
                self.assertFalse(target_is_safe({"xyxy": box}, (1000, 500, 3)))

    def test_complete_targets_near_edges_do_not_require_reframing(self) -> None:
        for box in (
            [3, 400, 70, 500], [430, 400, 497, 500],
            [425, 160, 475, 220], [25, 690, 75, 750],
            [210, 80, 290, 160], [210, 790, 290, 870],
            [100, 895, 160, 945], [210, 470, 290, 570],
        ):
            with self.subTest(box=box):
                self.assertTrue(target_is_safe({"xyxy": box}, (1000, 500, 3)))
        # Villa Lugano: this complete purple mushroom previously triggered a
        # drag solely because its centre was left of 25% of the screen width.
        self.assertTrue(target_is_safe(
            {"xyxy": [98.26171875, 1164.287109375, 357.0556640625, 1422.509765625]},
            (2340, 1080, 3),
        ))

    def test_control_occlusion_is_separate_from_screen_clipping(self) -> None:
        for box in (
            [25, 890, 75, 950], [210, 860, 290, 940],
            [425, 890, 475, 950], [425, 40, 475, 90],
            [465, 340, 495, 390], [210, 5, 290, 55],
            [210, 965, 290, 995],
        ):
            with self.subTest(box=box):
                self.assertFalse(target_is_clipped({"xyxy": box}, (1000, 500, 3)))
                self.assertFalse(target_is_safe({"xyxy": box}, (1000, 500, 3)))

    def test_registration_measures_actual_motion_despite_fixed_ui(self) -> None:
        original = textured_map()
        for dx, dy in ((-150, 230), (155, -270), (23, -17)):
            with self.subTest(shift=(dx, dy)):
                moved = cv2.warpAffine(original, np.float32([[1, 0, dx], [0, 1, dy]]), (500, 1000))
                moved[:130] = original[:130]
                moved[830:] = original[830:]
                measured = map_translation(original, moved)
                self.assertIsNotNone(measured)
                assert measured is not None
                self.assertAlmostEqual(measured[0], dx, delta=2.0)
                self.assertAlmostEqual(measured[1], dy, delta=2.0)

    def test_registration_falls_back_to_sift_for_large_low_overlap_recovery(self) -> None:
        original = textured_map()
        moved = cv2.warpAffine(
            original,
            np.float32([[1, 0, 190], [0, 1, 140]]),
            (500, 1000),
        )

        class EmptyOrb:
            @staticmethod
            def detectAndCompute(_image: np.ndarray, _mask: np.ndarray):
                return [], None

        with patch("bloompilot.map_targeting.cv2.ORB_create", return_value=EmptyOrb()):
            measured = map_translation(original, moved)

        self.assertIsNotNone(measured)
        assert measured is not None
        self.assertAlmostEqual(measured[0], 190, delta=2.0)
        self.assertAlmostEqual(measured[1], 140, delta=2.0)

    def test_camera_stability_ignores_animated_objects_but_detects_a_drag(self) -> None:
        original = textured_map()
        animated = original.copy()
        cv2.circle(animated, (120, 500), 45, (240, 20, 210), -1)
        cv2.circle(animated, (380, 720), 35, (20, 230, 250), -1)
        self.assertTrue(map_camera_is_stable(original, animated))
        moved = cv2.warpAffine(
            animated,
            np.float32([[1, 0, 24], [0, 1, -17]]),
            (500, 1000),
        )
        self.assertFalse(map_camera_is_stable(original, moved))

    def test_registration_rejects_zoom_and_unrelated_or_featureless_views(self) -> None:
        original = textured_map()
        zoomed = cv2.warpAffine(original, cv2.getRotationMatrix2D((250, 500), 0, 1.12), (500, 1000))
        self.assertIsNone(map_translation(original, zoomed))
        self.assertIsNone(map_translation(original, np.flipud(original)))
        self.assertIsNone(map_translation(np.zeros_like(original), np.zeros_like(original)))

    def test_reidentification_uses_shift_and_rejects_ambiguous_or_changed_targets(self) -> None:
        original = np.full((1000, 500, 3), 180, dtype=np.uint8)
        original[170:230, 420:480] = (200, 40, 30)
        moved = np.full_like(original, 180)
        moved[450:510, 270:330] = (200, 40, 30)
        target = {"class_name": "blue", "xyxy": [420, 170, 480, 230]}
        candidate = {"class_name": "blue", "xyxy": [270, 450, 330, 510]}
        result = match_target(target, original, moved, [candidate], (-150, 280))
        self.assertEqual(result["xyxy"], candidate["xyxy"])
        for candidates in (
            [candidate, dict(candidate)],
            [dict(candidate, class_name="red")],
            [dict(candidate, xyxy=[80, 450, 140, 510])],
        ):
            with self.subTest(candidates=candidates), self.assertRaises(TargetAlignmentError):
                match_target(target, original, moved, candidates, (-150, 280))
        moved[450:510, 270:330] = (20, 20, 220)
        with self.assertRaises(TargetAlignmentError):
            match_target(target, original, moved, [candidate], (-150, 280))

    def test_gestures_start_in_blank_map_and_stay_away_from_system_edges(self) -> None:
        blocked = {"xyxy": [180, 410, 320, 540]}
        for delta in ((-500, 1000), (500, -1000), (-150, -280), (0, 0)):
            with self.subTest(delta=delta):
                x1, y1, x2, y2 = plan_map_drag((1000, 500, 3), delta, [blocked])
                self.assertFalse(167 <= x1 <= 333 and 397 <= y1 <= 553)
                self.assertTrue(60 <= x2 <= 440)
                self.assertTrue(200 <= y2 <= 760)
                self.assertLessEqual(abs(x2 - x1), 160)
                self.assertLessEqual(abs(y2 - y1), 280)
        with self.assertRaises(TargetAlignmentError):
            plan_map_drag((1000, 500, 3), (100, 100), [{"xyxy": [0, 0, 500, 1000]}])


if __name__ == "__main__":
    unittest.main()
