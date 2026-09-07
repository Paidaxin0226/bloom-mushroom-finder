from __future__ import annotations

import unittest

from bloompilot.viewport_geo import coordinate_from_offset, estimate_detection_coordinate


class ViewportGeoTests(unittest.TestCase):
    def test_center_detection_keeps_center_coordinate(self) -> None:
        estimate = estimate_detection_coordinate(
            center_lat=25.012908,
            center_lng=121.543107,
            viewport_width_m=1080,
            screen_width=1080,
            screen_height=2340,
            grid_x=0,
            grid_y=0,
            xyxy=(500, 1130, 580, 1210),
        )
        self.assertAlmostEqual(float(estimate["lat"]), 25.012908, places=7)
        self.assertAlmostEqual(float(estimate["lng"]), 121.543107, places=7)
        self.assertEqual(estimate["east_m"], 0.0)
        self.assertEqual(estimate["north_m"], 0.0)

    def test_grid_and_box_offsets_follow_map_directions(self) -> None:
        east = estimate_detection_coordinate(
            center_lat=25.0,
            center_lng=121.0,
            viewport_width_m=1000,
            screen_width=1000,
            screen_height=2000,
            grid_x=1,
            grid_y=0,
            xyxy=(490, 990, 510, 1010),
        )
        north = estimate_detection_coordinate(
            center_lat=25.0,
            center_lng=121.0,
            viewport_width_m=1000,
            screen_width=1000,
            screen_height=2000,
            grid_x=0,
            grid_y=1,
            xyxy=(490, 990, 510, 1010),
        )
        self.assertGreater(float(east["lng"]), 121.0)
        self.assertAlmostEqual(float(east["east_m"]), 660.0, places=1)
        self.assertGreater(float(north["lat"]), 25.0)
        self.assertAlmostEqual(float(north["north_m"]), 1320.0, places=1)

    def test_pixel_offset_and_small_distance_conversion(self) -> None:
        moved_lat, moved_lng = coordinate_from_offset(
            25.0,
            121.0,
            east_m=100,
            north_m=100,
        )
        self.assertGreater(moved_lat, 25.0)
        self.assertGreater(moved_lng, 121.0)
        estimate = estimate_detection_coordinate(
            center_lat=25.0,
            center_lng=121.0,
            viewport_width_m=1000,
            screen_width=1000,
            screen_height=2000,
            grid_x=0,
            grid_y=0,
            xyxy=(590, 890, 610, 910),
        )
        self.assertEqual(estimate["east_m"], 100.0)
        self.assertEqual(estimate["north_m"], 100.0)

    def test_invalid_scale_is_rejected(self) -> None:
        with self.assertRaisesRegex(ValueError, "畫面寬度"):
            estimate_detection_coordinate(
                center_lat=25.0,
                center_lng=121.0,
                viewport_width_m=0,
                screen_width=1080,
                screen_height=2340,
                grid_x=0,
                grid_y=0,
                xyxy=(0, 0, 10, 10),
            )

    def test_device_degree_per_pixel_scale_uses_separate_axes(self) -> None:
        estimate = estimate_detection_coordinate(
            center_lat=25.0,
            center_lng=121.0,
            screen_width=1000,
            screen_height=2000,
            grid_x=0,
            grid_y=0,
            xyxy=(590, 790, 610, 810),
            longitude_degrees_per_pixel=0.00001,
            latitude_degrees_per_pixel=0.00002,
            calibration_latitude=25.0,
            calibration_screen_width=1000,
            calibration_screen_height=2000,
        )
        self.assertAlmostEqual(float(estimate["lng"]), 121.001, places=7)
        self.assertAlmostEqual(float(estimate["lat"]), 25.004, places=7)
        self.assertEqual(estimate["method"], "device_pixel_geographic_scale")
        self.assertNotEqual(
            estimate["meters_per_pixel_x"],
            estimate["meters_per_pixel_y"],
        )

    def test_device_scale_rescales_when_capture_resolution_changes(self) -> None:
        estimate = estimate_detection_coordinate(
            center_lat=25.0,
            center_lng=121.0,
            screen_width=500,
            screen_height=1000,
            grid_x=0,
            grid_y=0,
            xyxy=(295, 395, 305, 405),
            longitude_degrees_per_pixel=0.00001,
            latitude_degrees_per_pixel=0.00002,
            calibration_latitude=25.0,
            calibration_screen_width=1000,
            calibration_screen_height=2000,
        )
        self.assertAlmostEqual(float(estimate["lng"]), 121.001, places=7)
        self.assertAlmostEqual(float(estimate["lat"]), 25.004, places=7)


if __name__ == "__main__":
    unittest.main()
