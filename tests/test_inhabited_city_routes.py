from __future__ import annotations

import sys
import unittest
import importlib.util
from pathlib import Path

import numpy as np

if any(importlib.util.find_spec(name) is None for name in ("pyproj", "rasterio", "scipy")):
    raise unittest.SkipTest("Install the optional routes dependencies to test inhabited-route generation.")

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from built_up_raster import BuiltUpGrid
from build_inhabited_city_routes import (
    Policy, Town, UnverifiedTown, local_transformers, plan_town, viewport_scale,
)


class InhabitedCityRouteTests(unittest.TestCase):
    def setUp(self) -> None:
        self.town = Town("town-1", 48.8, 2.5, 18_000)
        self.source = {"id": "town-1", "name": "Small town", "lat": 48.91,
                       "lng": 2.36, "wait_seconds": 10}
        self.fraction = np.zeros((90, 90), dtype=float)
        self.valid = np.ones_like(self.fraction, dtype=bool)

    def grid(self) -> BuiltUpGrid:
        return BuiltUpGrid(origin_lat=48.8, origin_lng=2.5, west_m=-4500,
                           north_m=4500, resolution_m=100,
                           fraction=self.fraction, valid=self.valid,
                           valid_fraction=self.valid.astype(float),
                           built_count=np.nan_to_num(self.fraction * 100).astype(np.uint32),
                           valid_count=(self.valid * 100).astype(np.uint32),
                           total_count=np.full(self.fraction.shape, 100, dtype=np.uint32))

    def test_center_moves_from_empty_anchor_into_nearby_buildings(self) -> None:
        self.fraction[39:50, 55:65] = .65
        waypoint, evidence = plan_town(self.town, self.source, self.grid(), Policy())
        to_local, _ = local_transformers(48.8, 2.5)
        x, y = to_local.transform(waypoint["lng"], waypoint["lat"])
        col, row = int((x + 4500) / 100), int((4500 - y) / 100)
        self.assertGreaterEqual(self.fraction[row, col], .25)
        self.assertGreaterEqual(evidence["center_viewport_built_fraction"], .05)
        self.assertGreater(evidence["search_points"], 1)
        self.assertLess(evidence["search_points"], 100)
        self.assertTrue(any(y == 0 and start <= 0 <= end for y, start, end in waypoint["scan_spans"]))
        self.assertLess(evidence["center_distance_from_town_m"], 2500)

    def test_empty_land_has_no_population_radius_fallback(self) -> None:
        with self.assertRaisesRegex(UnverifiedTown, "no_building_cluster"):
            plan_town(self.town, self.source, self.grid(), Policy())

    def test_missing_data_is_distinguished_from_empty_land(self) -> None:
        self.valid[:] = False
        self.fraction[:] = np.nan
        with self.assertRaisesRegex(UnverifiedTown, "missing_land_cover_data"):
            plan_town(self.town, self.source, self.grid(), Policy())

    def test_remote_cluster_does_not_expand_local_town_scan(self) -> None:
        self.fraction[40:50, 40:50] = .65
        self.fraction[17:25, 65:73] = .9
        waypoint, evidence = plan_town(self.town, self.source, self.grid(), Policy())
        self.assertLess(evidence["center_distance_from_town_m"], 500)
        self.assertLess(waypoint["right_km"], 2)
        self.assertLess(waypoint["up_km"], 2)

    def test_small_isolated_farm_buildings_are_insufficient(self) -> None:
        self.fraction[44:46, 44:46] = .7
        with self.assertRaises(UnverifiedTown):
            plan_town(self.town, self.source, self.grid(), Policy())

    def test_calibration_changes_physical_swipe_distance_with_latitude(self) -> None:
        calibration = {"screen_width": 1080, "screen_height": 2340, "origin_lat": 30,
                       "longitude_degrees_per_pixel": 7e-6, "latitude_degrees_per_pixel": 6e-6}
        low = viewport_scale(30, calibration)
        high = viewport_scale(60, calibration)
        for a, b in zip(low, high):
            self.assertAlmostEqual(b / a, 1 / 3 ** .5, places=9)


if __name__ == "__main__":
    unittest.main()
