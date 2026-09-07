"""Build the private monthly capture route from the current background towns."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .dataset_builder import IMAGE_EXTENSIONS


MONTHLY_BACKGROUND_ROUTE = "_monthly/current_background_towns.json"


@dataclass(frozen=True)
class BackgroundTown:
    slug: str
    label: str
    lat: float
    lng: float
    left: int
    right: int
    up: int
    down: int
    cross: bool = False

    @property
    def point_count(self) -> int:
        if self.cross:
            return self.left + self.right + self.up + self.down + 1
        return (self.left + self.right + 1) * (self.up + self.down + 1)


# These centers and bounds are the ones used to create data/source/backgrounds.
BACKGROUND_TOWNS = (
    BackgroundTown("antalya", "Antalya", 36.8969, 30.7133, 24, 24, 24, 4, True),
    BackgroundTown("bangkok", "Bangkok", 13.7563, 100.5018, 4, 4, 4, 3),
    BackgroundTown("berlin", "Berlin", 52.5200, 13.4050, 12, 12, 12, 12, True),
    BackgroundTown("hong_kong", "Hong Kong", 22.3193, 114.1694, 4, 3, 2, 3),
    BackgroundTown("las_vegas", "Las Vegas", 36.1699, -115.1398, 1, 13, 1, 1, True),
    BackgroundTown("london", "London", 51.5074, -0.1278, 20, 20, 19, 18, True),
    BackgroundTown("osaka", "Osaka", 34.7025, 135.4960, 4, 3, 3, 3),
    BackgroundTown("paris", "Paris", 48.8566, 2.3522, 12, 12, 12, 12, True),
    BackgroundTown("seoul", "Seoul", 37.5665, 126.9780, 5, 3, 3, 3),
    BackgroundTown("singapore", "Singapore", 1.2903, 103.8520, 3, 4, 3, 3),
    BackgroundTown("sydney", "Sydney", -33.8688, 151.2093, 5, 3, 3, 3),
    BackgroundTown("taipei", "Taipei", 25.0330, 121.5654, 4, 3, 3, 3),
    BackgroundTown("tokyo", "Tokyo", 35.6812, 139.7671, 5, 3, 4, 3),
)


def _cross_spans(town: BackgroundTown) -> list[list[int]]:
    spans: list[list[int]] = []
    for y in range(-town.down, town.up + 1):
        spans.append([y, -town.left, town.right] if y == 0 else [y, 0, 0])
    return spans


def build_monthly_background_route(background_root: Path) -> dict[str, Any]:
    """Create a route containing exactly the towns present in the source dataset."""
    available = {
        path.name.casefold()
        for path in background_root.iterdir()
        if path.is_dir()
        and any(item.is_file() and item.suffix.casefold() in IMAGE_EXTENSIONS for item in path.iterdir())
    } if background_root.is_dir() else set()
    selected = [town for town in BACKGROUND_TOWNS if town.slug.casefold() in available]
    unknown = sorted(available - {town.slug.casefold() for town in BACKGROUND_TOWNS})
    if unknown:
        raise ValueError("背景資料集包含尚未設定座標的城鎮：" + "、".join(unknown))
    if not selected:
        raise ValueError("目前背景資料集沒有可建立路線的城鎮。")

    waypoints: list[dict[str, Any]] = []
    for town in selected:
        waypoint: dict[str, Any] = {
            "id": f"background-{town.slug}",
            "name": town.label,
            "lat": town.lat,
            "lng": town.lng,
            "wait_seconds": 10.0,
            "horizontal_radius": max(town.left, town.right),
            "vertical_radius": max(town.up, town.down),
            "spacing_m": 100.0,
            "left_radius": town.left,
            "right_radius": town.right,
            "up_radius": town.up,
            "down_radius": town.down,
            "range_unit": "km",
            "coverage_source": "current_background_dataset",
        }
        if town.cross:
            waypoint["scan_spans"] = _cross_spans(town)
        waypoints.append(waypoint)

    now = datetime.now(timezone.utc).isoformat()
    return {
        "schema": "bloompilot.geo-sequence.v1",
        "exported_at": now,
        "metadata": {
            "waypoint_count": len(selected),
            "city_count": len(selected),
            "total_search_points": sum(town.point_count for town in selected),
            "source": "data/source/backgrounds",
            "towns": [town.slug for town in selected],
        },
        "sequence": {
            "version": 1,
            "id": "monthly-current-background-towns",
            "name": f"目前背景資料集 {len(selected)} 城鎮",
            "updated_at": now,
            "waypoints": waypoints,
        },
    }


def ensure_monthly_background_route(background_root: Path, routes_root: Path) -> tuple[str, dict[str, Any]]:
    document = build_monthly_background_route(background_root)
    destination = routes_root / MONTHLY_BACKGROUND_ROUTE
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.is_file():
        try:
            current = json.loads(destination.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            current = None
        if (
            isinstance(current, dict)
            and current.get("metadata", {}).get("towns") == document["metadata"]["towns"]
            and current.get("metadata", {}).get("total_search_points")
            == document["metadata"]["total_search_points"]
            and current.get("sequence", {}).get("waypoints") == document["sequence"]["waypoints"]
        ):
            return MONTHLY_BACKGROUND_ROUTE, current
    temporary = destination.with_suffix(".json.tmp")
    encoded = (json.dumps(document, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
    try:
        temporary.write_bytes(encoded)
        os.replace(temporary, destination)
    finally:
        temporary.unlink(missing_ok=True)
    return MONTHLY_BACKGROUND_ROUTE, document
