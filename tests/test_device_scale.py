from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

import cv2
import numpy as np

from bloompilot.device_scale import (
    DeviceScaleStore,
    MarkerDisplacement,
    measure_player_marker_displacement,
    new_device_scale_calibration,
    validate_round_trip,
)


def encoded_marker_frame(*, marker_x: int, marker_y: int, marker_seed: int = 91) -> bytes:
    rng = np.random.default_rng(731)
    image = rng.integers(25, 180, size=(900, 600, 3), dtype=np.uint8)
    marker_rng = np.random.default_rng(marker_seed)
    marker = marker_rng.integers(0, 255, size=(64, 64, 3), dtype=np.uint8)
    cv2.circle(marker, (32, 32), 30, (250, 250, 250), 4, cv2.LINE_AA)
    image[marker_y : marker_y + 64, marker_x : marker_x + 64] = marker
    ok, encoded = cv2.imencode(".png", image)
    if not ok:
        raise AssertionError("test image encoding failed")
    return encoded.tobytes()


class DeviceScaleTests(unittest.TestCase):
    def test_optical_flow_measures_only_the_moving_marker(self) -> None:
        reference = encoded_marker_frame(marker_x=220, marker_y=410)
        moved = encoded_marker_frame(marker_x=274, marker_y=410)

        displacement = measure_player_marker_displacement(
            reference,
            moved,
            axis="x",
            expected_sign=1,
        )

        self.assertAlmostEqual(displacement.dx, 54, delta=2.5)
        self.assertAlmostEqual(displacement.dy, 0, delta=2.5)
        self.assertGreaterEqual(displacement.tracked_points, 2)

    def test_white_avatar_ring_survives_animated_interior(self) -> None:
        reference = encoded_marker_frame(marker_x=220, marker_y=410, marker_seed=91)
        moved = encoded_marker_frame(marker_x=220, marker_y=350, marker_seed=912)

        displacement = measure_player_marker_displacement(
            reference,
            moved,
            axis="y",
            expected_sign=-1,
        )

        self.assertAlmostEqual(displacement.dx, 0, delta=2.5)
        self.assertAlmostEqual(displacement.dy, -60, delta=2.5)

    def test_inverse_round_trip_is_required(self) -> None:
        forward = MarkerDisplacement(50, 1, 100, 100, 150, 101, 8)
        validate_round_trip(
            forward,
            MarkerDisplacement(-48, -1, 150, 101, 102, 100, 7),
            axis="x",
        )
        with self.assertRaisesRegex(ValueError, "往返"):
            validate_round_trip(
                forward,
                MarkerDisplacement(49, 0, 150, 101, 199, 101, 8),
                axis="x",
            )

    def test_store_keeps_separate_calibration_for_each_serial(self) -> None:
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as directory:
            store = DeviceScaleStore(Path(directory) / "scales.json")
            first = new_device_scale_calibration(
                serial="phone-a",
                screen_width=1080,
                screen_height=2400,
                origin_lat=25,
                origin_lng=121,
                east_probe_m=36,
                north_probe_m=36,
                east_coordinate_delta=0.00036,
                north_coordinate_delta=0.00032,
                east_pixels=60,
                north_pixels=-72,
            )
            second = new_device_scale_calibration(
                serial="phone-b",
                screen_width=720,
                screen_height=1600,
                origin_lat=35,
                origin_lng=139,
                east_probe_m=40,
                north_probe_m=40,
                east_coordinate_delta=0.00044,
                north_coordinate_delta=0.00036,
                east_pixels=50,
                north_pixels=-65,
            )

            store.save(first)
            store.save(second)

            self.assertEqual(store.get("phone-a"), first)
            self.assertEqual(store.get("phone-b"), second)
            self.assertIsNone(store.get("missing"))


if __name__ == "__main__":
    unittest.main()
