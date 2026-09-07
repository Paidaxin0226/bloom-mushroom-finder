from __future__ import annotations

import json
import unittest
from pathlib import Path

from bloompilot.geo_recognition import GeoRecognitionWaypoint, validate_geo_waypoints
from bloompilot.project_defaults import (
    MAX_GEO_SEQUENCE_SEARCH_POINTS,
    MAX_GEO_SEQUENCE_WAYPOINTS,
)


PROJECT_ROOT = Path(__file__).resolve().parents[1]
ROUTE_ROOT = PROJECT_ROOT / "routes" / "pikmin_global"


class GeneratedPikminRouteTests(unittest.TestCase):
    def test_all_route_parts_are_directly_importable_and_unique(self) -> None:
        manifest = json.loads((ROUTE_ROOT / "index.json").read_text(encoding="utf-8"))
        entries = manifest["popular_files"] + manifest["less_known_files"]
        seen_ids: set[str] = set()
        popular = 0
        less_known = 0

        self.assertEqual(len(entries), manifest["summary"]["categorized_route_files"])
        for entry in entries:
            payload = json.loads((ROUTE_ROOT / entry["file"]).read_text(encoding="utf-8"))
            self.assertEqual(payload["schema"], "bloompilot.geo-sequence.v1")
            raw_waypoints = payload["sequence"]["waypoints"]
            self.assertTrue(all(item["range_unit"] == "km" for item in raw_waypoints))
            self.assertTrue(all("left_km" in item and "vertical_step_km" in item for item in raw_waypoints))
            self.assertTrue(
                all(
                    "scan_spans" in item
                    for item in raw_waypoints
                    if item["coverage_source"].startswith("ghsl_")
                )
            )
            waypoints = [GeoRecognitionWaypoint(**item) for item in raw_waypoints]
            total_points = validate_geo_waypoints(waypoints)
            self.assertEqual(len(waypoints), entry["city_count"])
            self.assertEqual(total_points, entry["total_search_points"])
            self.assertLessEqual(len(waypoints), MAX_GEO_SEQUENCE_WAYPOINTS)
            self.assertLessEqual(total_points, MAX_GEO_SEQUENCE_SEARCH_POINTS)

            category = payload["metadata"]["category"]
            if category == "popular":
                popular += len(waypoints)
            elif category == "less_known":
                less_known += len(waypoints)
            else:
                self.fail(f"未知路線分類：{category}")
            for waypoint in waypoints:
                self.assertNotIn(waypoint.id, seen_ids)
                seen_ids.add(waypoint.id)

        self.assertEqual(popular, 500)
        self.assertEqual(less_known, 24_512)
        self.assertEqual(len(seen_ids), 25_012)
        self.assertIn("geonames-2950159", seen_ids)  # Berlin
        self.assertIn("geonames-1267755", seen_ids)  # Karimnagar

        merged = json.loads(
            (ROUTE_ROOT / manifest["merged_file"]["file"]).read_text(encoding="utf-8")
        )
        merged_waypoints = [
            GeoRecognitionWaypoint(**item) for item in merged["sequence"]["waypoints"]
        ]
        merged_points = validate_geo_waypoints(merged_waypoints)
        self.assertEqual(len(merged_waypoints), 25_012)
        self.assertEqual(merged_points, manifest["merged_file"]["total_search_points"])
        self.assertGreater(merged_points, 10_000_000)
        self.assertEqual({item.id for item in merged_waypoints}, seen_ids)

        malacca = next(item for item in merged_waypoints if item.id == "geonames-1734759")
        rectangular_points = (
            malacca.left_radius + malacca.right_radius + 1
        ) * (
            malacca.up_radius + malacca.down_radius + 1
        )
        self.assertIsNotNone(malacca.scan_spans)
        self.assertLess(malacca.point_count, rectangular_points)
        self.assertNotAlmostEqual(malacca.lng, 102.2405, places=4)


if __name__ == "__main__":
    unittest.main()
