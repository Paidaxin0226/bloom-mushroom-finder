"""Build town-local routes from observed built-up land instead of region centroids.

WorldCover supplies built-up land cover, not a census of occupied buildings.
GeoNames supplies the inhabited-place anchor and population. Low-confidence or
unavailable raster coverage is recorded separately and never becomes a wide
population-derived rectangle.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import sys
import threading
import zipfile
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path

import numpy as np
from pyproj import CRS, Transformer
from scipy import ndimage

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from bloompilot.route_coverage import grid_step_km, viewport_half_km  # noqa: E402
from built_up_raster import BuiltUpGrid, BuiltUpRaster  # noqa: E402


@dataclass(frozen=True)
class Town:
    id: str
    lat: float
    lng: float
    population: int


@dataclass(frozen=True)
class Policy:
    resolution_m: float = 100.0
    minimum_cell_built_fraction: float = 0.12
    minimum_center_built_fraction: float = 0.25
    minimum_neighborhood_built_fraction: float = 0.08
    minimum_cluster_built_m2: float = 50_000.0
    minimum_viewport_built_fraction: float = 0.15
    minimum_valid_viewport_fraction: float = 0.98
    anchor_preference_m: float = 1500.0
    minimum_search_radius_m: float = 3000.0
    maximum_search_radius_m: float = 8000.0


class UnverifiedTown(ValueError):
    """A town cannot be assigned a building-supported route from these data."""


def search_radius_m(town: Town, policy: Policy) -> float:
    # Only a retrieval limit. The output footprint comes from observed buildings.
    return min(policy.maximum_search_radius_m, max(
        policy.minimum_search_radius_m,
        1000.0 * math.sqrt(max(0, town.population) / (2000.0 * math.pi)),
    ))


def local_transformers(lat: float, lng: float) -> tuple[Transformer, Transformer]:
    local = CRS.from_proj4(f"+proj=aeqd +lat_0={lat} +lon_0={lng} +datum=WGS84 +units=m")
    return (
        Transformer.from_crs("EPSG:4326", local, always_xy=True),
        Transformer.from_crs(local, "EPSG:4326", always_xy=True),
    )


def viewport_scale(lat: float, calibration: dict | None) -> tuple[float, float, float, float]:
    if calibration is None:
        return (grid_step_km("horizontal") * 1000, grid_step_km("vertical") * 1000,
                viewport_half_km("horizontal") * 1000, viewport_half_km("vertical") * 1000)
    width = int(calibration["screen_width"])
    height = int(calibration["screen_height"])
    lon_per_px = float(calibration["longitude_degrees_per_pixel"])
    lat_per_px = float(calibration["latitude_degrees_per_pixel"])
    lat_per_px *= math.cos(math.radians(lat)) / math.cos(math.radians(float(calibration["origin_lat"])))
    px_x = lon_per_px * math.pi / 180.0 * 6_378_137.0 * math.cos(math.radians(lat))
    px_y = lat_per_px * math.pi / 180.0 * 6_378_137.0
    # Radius decoding uses the reference swipe fractions. Keep its viewport
    # interpretation identical even when a phone has a different pixel count.
    reference_swipe_x = abs(round(1080 * .17) - round(1080 * .83))
    reference_swipe_y = abs(round(2340 * .15) - round(2340 * .81))
    step_x = abs(round(width * .17) - round(width * .83)) * px_x
    step_y = abs(round(height * .15) - round(height * .81)) * px_y
    return step_x, step_y, step_x * 1080 / (2 * reference_swipe_x), step_y * 2340 / (2 * reference_swipe_y)


def _integral(array: np.ndarray) -> np.ndarray:
    return np.pad(np.cumsum(np.cumsum(array, axis=0), axis=1), ((1, 0), (1, 0)))


def _window_sum(integral: np.ndarray, grid: BuiltUpGrid,
                x: float, y: float, half_x: float, half_y: float) -> float:
    height, width = grid.fraction.shape
    left = max(0, min(width, math.ceil((x - half_x - grid.west_m) / grid.resolution_m - .5)))
    right = max(0, min(width, math.ceil((x + half_x - grid.west_m) / grid.resolution_m - .5)))
    top = max(0, min(height, math.ceil((grid.north_m - y - half_y) / grid.resolution_m - .5)))
    bottom = max(0, min(height, math.ceil((grid.north_m - y + half_y) / grid.resolution_m - .5)))
    return float(integral[bottom, right] - integral[top, right]
                 - integral[bottom, left] + integral[top, left])


def plan_town(town: Town, source: dict, grid: BuiltUpGrid, policy: Policy,
              calibration: dict | None = None) -> tuple[dict, dict]:
    fraction = np.asarray(grid.fraction, dtype=np.float64)
    valid = np.asarray(grid.valid, dtype=bool)
    if fraction.ndim != 2 or fraction.shape != valid.shape:
        raise ValueError("Built-up fractions and validity must be matching 2D grids.")
    if not np.all(np.isfinite(fraction[valid])) or np.any((fraction[valid] < 0) | (fraction[valid] > 1)):
        raise ValueError("Built-up fractions must be finite values in [0, 1].")
    fraction = np.where(valid, fraction, 0)
    if not np.any(valid):
        raise UnverifiedTown("missing_land_cover_data")
    rows, columns = fraction.shape
    xs = grid.west_m + (np.arange(columns) + .5) * grid.resolution_m
    ys = grid.north_m - (np.arange(rows) + .5) * grid.resolution_m
    xx, yy = np.meshgrid(xs, ys)
    anchor_to_local, local_to_geo = local_transformers(grid.origin_lat, grid.origin_lng)
    anchor_x, anchor_y = anchor_to_local.transform(town.lng, town.lat)
    anchor_distance = np.hypot(xx - anchor_x, yy - anchor_y)
    allowed = valid & (anchor_distance <= search_radius_m(town, policy))
    neighborhood_size = max(1, round(500.0 / grid.resolution_m))
    density = ndimage.uniform_filter(fraction, size=neighborhood_size, mode="constant")
    occupied = allowed & (fraction >= policy.minimum_cell_built_fraction)
    occupied &= density >= policy.minimum_neighborhood_built_fraction
    # Bridge a single street-sized gap, without making those gap pixels evidence
    # of buildings or admitting isolated farmhouses into the scan footprint.
    connected = (ndimage.binary_closing(occupied, structure=np.ones((3, 3))) | occupied) & allowed
    labels, component_count = ndimage.label(connected, structure=np.ones((3, 3)))
    if component_count == 0:
        raise UnverifiedTown("no_building_cluster")
    component_built = np.bincount(labels.ravel(), weights=(fraction * occupied).ravel())
    component_built *= grid.resolution_m ** 2
    component_built[0] = 0
    candidates = occupied & (fraction >= policy.minimum_center_built_fraction)
    candidates &= component_built[labels] >= policy.minimum_cluster_built_m2
    if not np.any(candidates):
        raise UnverifiedTown("no_dense_building_supported_center")
    scores = density / (1.0 + (anchor_distance / policy.anchor_preference_m) ** 2)
    # Try several actual built cells if the best candidate's viewport is too
    # close to missing data or its cluster is very narrow.
    ordering = np.argsort(np.where(candidates, scores, -np.inf).ravel())[::-1]
    pixel_integral = _integral(np.ones_like(fraction))
    valid_integral = _integral(valid.astype(float))
    best = None
    for index in ordering[:min(100, int(candidates.sum()))]:
        row, col = np.unravel_index(index, fraction.shape)
        selected = (labels == labels[row, col]) & occupied
        built_integral = _integral(fraction * selected)
        center_x, center_y = float(xs[col]), float(ys[row])
        center_lng, center_lat = local_to_geo.transform(center_x, center_y)
        step_x, step_y, half_x, half_y = viewport_scale(center_lat, calibration)
        # Round once: persisted steps and generated masks must use the same grid.
        step_x, step_y = round(step_x, 6), round(step_y, 6)
        half_x = step_x * viewport_half_km("horizontal") / grid_step_km("horizontal")
        half_y = step_y * viewport_half_km("vertical") / grid_step_km("vertical")
        viewport_area = 4 * half_x * half_y
        center_built = _window_sum(built_integral, grid, center_x, center_y, half_x, half_y) * grid.resolution_m ** 2 / viewport_area
        center_pixels = _window_sum(pixel_integral, grid, center_x, center_y, half_x, half_y)
        center_valid = _window_sum(valid_integral, grid, center_x, center_y, half_x, half_y) / max(1, center_pixels)
        # Pixel-center counting can over/underestimate area at the edge by a
        # pixel. A separate geometric check guarantees the viewport is in-grid.
        fully_in_grid = (center_x - half_x >= grid.west_m and center_x + half_x <= grid.west_m + columns * grid.resolution_m
                         and center_y + half_y <= grid.north_m and center_y - half_y >= grid.north_m - rows * grid.resolution_m)
        if center_built >= policy.minimum_viewport_built_fraction and fully_in_grid and center_valid >= policy.minimum_valid_viewport_fraction:
            best = (row, col, selected, built_integral, valid_integral, center_x, center_y,
                    center_lat, center_lng, step_x, step_y, half_x, half_y, viewport_area, center_built)
            break
    if best is None:
        raise UnverifiedTown("no_supported_center_viewport")
    (row, col, selected, built_integral, valid_integral, center_x, center_y,
     center_lat, center_lng, step_x, step_y, half_x, half_y, viewport_area, center_built) = best
    selected_rows, selected_cols = np.where(selected)
    west = float(xs[selected_cols].min() - grid.resolution_m / 2)
    east = float(xs[selected_cols].max() + grid.resolution_m / 2)
    south = float(ys[selected_rows].min() - grid.resolution_m / 2)
    north = float(ys[selected_rows].max() + grid.resolution_m / 2)
    left = max(0, math.ceil((center_x - west + half_x) / step_x))
    right = max(0, math.ceil((east - center_x + half_x) / step_x))
    up = max(0, math.ceil((north - center_y + half_y) / step_y))
    down = max(0, math.ceil((center_y - south + half_y) / step_y))
    cells: list[tuple[int, int]] = []
    for gy in range(-down, up + 1):
        for gx in range(-left, right + 1):
            x, y = center_x + gx * step_x, center_y + gy * step_y
            if not (x - half_x >= grid.west_m and x + half_x <= grid.west_m + columns * grid.resolution_m
                    and y + half_y <= grid.north_m and y - half_y >= grid.north_m - rows * grid.resolution_m):
                continue
            # Check known-data pixels independently of built-up pixels.
            pixel_count = _window_sum(pixel_integral, grid, x, y, half_x, half_y)
            valid_count = _window_sum(valid_integral, grid, x, y, half_x, half_y)
            if not pixel_count or valid_count / pixel_count < policy.minimum_valid_viewport_fraction:
                continue
            ratio = _window_sum(built_integral, grid, x, y, half_x, half_y) * grid.resolution_m ** 2 / viewport_area
            if ratio >= policy.minimum_viewport_built_fraction:
                cells.append((gy, gx))
    if (0, 0) not in cells:
        raise UnverifiedTown("center_failed_viewport_validation")
    # Keep the connected scan neighborhood reachable from the chosen town
    # center. Remote fragments are never joined by a large empty rectangle.
    remaining = set(cells)
    connected_cells = {(0, 0)}
    stack = [(0, 0)]
    remaining.remove((0, 0))
    while stack:
        cy, cx = stack.pop()
        for dy, dx in ((-1, 0), (1, 0), (0, -1), (0, 1)):
            neighbor = cy + dy, cx + dx
            if neighbor in remaining:
                remaining.remove(neighbor)
                connected_cells.add(neighbor)
                stack.append(neighbor)
    cells = sorted(connected_cells)
    left = max(0, -min(x for y, x in cells))
    right = max(0, max(x for y, x in cells))
    up = max(0, max(y for y, x in cells))
    down = max(0, -min(y for y, x in cells))
    spans: list[list[int]] = []
    for gy, gx in cells:
        if spans and spans[-1][0] == gy and spans[-1][2] + 1 == gx:
            spans[-1][2] = gx
        else:
            spans.append([gy, gx, gx])
    waypoint = {k: source[k] for k in ("id", "name", "wait_seconds") if k in source}
    # Use a small inward epsilon so ceil is stable after decimal serialization.
    def extent(radius: int, half: float, step: float) -> float:
        return round(max(0, half + radius * step - .01) / 1000, 9)
    waypoint.update({
        "lat": round(center_lat, 7), "lng": round(center_lng, 7), "range_unit": "km",
        "left_radius": left, "right_radius": right, "up_radius": up, "down_radius": down,
        "left_km": extent(left, half_x, step_x), "right_km": extent(right, half_x, step_x),
        "up_km": extent(up, half_y, step_y), "down_km": extent(down, half_y, step_y),
        "horizontal_step_km": round(step_x / 1000, 9), "vertical_step_km": round(step_y / 1000, 9),
        "coverage_source": "worldcover_2021_builtup_near_geonames_town",
        "scan_spans": spans,
    })
    evidence = {
        "id": town.id, "name": source["name"], "population": town.population,
        "anchor": {"lat": town.lat, "lng": town.lng},
        "previous_center": {"lat": source["lat"], "lng": source["lng"]},
        "center": {"lat": waypoint["lat"], "lng": waypoint["lng"]},
        "center_distance_from_town_m": round(math.hypot(center_x - anchor_x, center_y - anchor_y), 1),
        "center_cell_built_fraction": round(float(fraction[row, col]), 4),
        "center_viewport_built_fraction": round(center_built, 4),
        "cluster_built_m2": round(float((fraction * selected).sum() * grid.resolution_m ** 2)),
        "search_points": len(cells), "search_limit_m": round(search_radius_m(town, policy)),
    }
    return waypoint, evidence


def load_towns(path: Path) -> dict[str, Town]:
    with zipfile.ZipFile(path) as archive:
        text_name = next(name for name in archive.namelist() if name.endswith(".txt"))
        rows = archive.read(text_name).decode("utf-8").splitlines()
    result = {}
    for row in rows:
        parts = row.split("\t")
        if len(parts) >= 19 and parts[6] == "P":
            key = f"geonames-{parts[0]}"
            result[key] = Town(key, float(parts[4]), float(parts[5]), max(0, int(parts[14] or 0)))
    return result


def atomic_json(path: Path, value: object, *, compact: bool = True) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, separators=(",", ":") if compact else None,
                                    indent=None if compact else 2), encoding="utf-8")
    temporary.replace(path)


def main(args: argparse.Namespace) -> None:
    source_path = Path(args.source).resolve()
    source = json.loads(source_path.read_text(encoding="utf-8"))
    source_points = source["sequence"]["waypoints"]
    requested_ids = set(args.city_id or [])
    if requested_ids:
        source_points = [p for p in source_points if p["id"] in requested_ids]
    if args.limit:
        source_points = source_points[:args.limit]
    if not source_points:
        raise ValueError("No matching source waypoints.")
    towns = load_towns(Path(args.city_archive))
    policy = Policy()
    calibration = None
    if args.device_serial:
        scales = json.loads(Path(args.device_scales).read_text(encoding="utf-8"))
        calibration = scales["devices"][args.device_serial]
    fingerprint = hashlib.sha256(json.dumps({"policy": asdict(policy), "calibration": calibration,
                                            "algorithm": 2, "raster_aggregation": "summary_average"}, sort_keys=True).encode()).hexdigest()[:16]
    cache = Path(args.cache)
    result_dir = cache / "plans" / fingerprint
    local_readers = threading.local()
    readers: list[BuiltUpRaster] = []
    reader_lock = threading.Lock()
    outcomes = {}
    def build(point: dict) -> tuple[str, dict]:
        town = towns.get(point["id"])
        if town is None:
            return point["id"], {"status": "unverified", "reason": "missing_geonames_anchor", "id": point["id"]}
        key = hashlib.sha256(json.dumps(asdict(town), sort_keys=True).encode()).hexdigest()[:12]
        result_path = result_dir / f"{town.id}-{key}.json"
        if result_path.exists():
            outcome = json.loads(result_path.read_text(encoding="utf-8"))
            if outcome["status"] != "data_error":
                if outcome["status"] == "ready":
                    outcome["waypoint"]["name"] = point["name"]
                    outcome["waypoint"]["wait_seconds"] = point.get("wait_seconds", 10)
                return town.id, outcome
        try:
            if not hasattr(local_readers, "raster"):
                local_readers.raster = BuiltUpRaster(cache / "raster", min_valid_fraction=.98, summary_mode=True)
                with reader_lock:
                    readers.append(local_readers.raster)
            grid = local_readers.raster.read(town.lat, town.lng, search_radius_m(town, policy) + 1500,
                               resolution_m=policy.resolution_m)
            waypoint, evidence = plan_town(town, point, grid, policy, calibration)
            outcome = {"status": "ready", "waypoint": waypoint, "evidence": evidence}
        except UnverifiedTown as exc:
            outcome = {"status": "unverified", "id": town.id, "name": point["name"], "reason": str(exc)}
        except Exception as exc:
            outcome = {"status": "data_error", "id": town.id, "name": point["name"], "reason": f"{type(exc).__name__}: {exc}"}
        atomic_json(result_path, outcome)
        return town.id, outcome
    # Read neighboring towns together so both range requests and native chunk
    # summaries are shared. Preserve the user's sequence order in the output.
    def locality(point: dict) -> tuple:
        town = towns.get(point["id"])
        return ((math.floor(town.lat / 3), math.floor(town.lng / 3),
                 math.floor(town.lat * 10), math.floor(town.lng * 10)) if town else (999, 0, 0, 0))
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        futures = [pool.submit(build, point) for point in sorted(source_points, key=locality)]
        for completed, future in enumerate(as_completed(futures), 1):
            key, outcome = future.result()
            outcomes[key] = outcome
            if completed % 25 == 0 or len(source_points) <= 25 or completed == len(source_points):
                ready = sum(o["status"] == "ready" for o in outcomes.values())
                print(f"{completed}/{len(source_points)}: {ready} ready; {outcome['status']} {key}", flush=True)
    for reader in readers:
        reader.close()
    ready = [outcomes[p["id"]] for p in source_points if outcomes[p["id"]]["status"] == "ready"]
    unresolved = [outcomes[p["id"]] for p in source_points if outcomes[p["id"]]["status"] != "ready"]
    generated = datetime.now(UTC).isoformat(timespec="seconds")
    report = {"generated_at": generated, "source": source_path.name, "requested": len(source_points),
              "ready": len(ready), "unresolved": unresolved, "policy": asdict(policy),
              "evidence": [o["evidence"] for o in ready]}
    output_dir = Path(args.output_dir)
    atomic_json(output_dir / "report.json", report)
    errors = [o for o in unresolved if o["status"] == "data_error"]
    if errors and not args.allow_partial:
        raise RuntimeError(f"{len(errors)} raster retrieval failures; cached progress and report saved. Route not published.")
    if not ready:
        raise RuntimeError("No building-supported cities; report saved.")
    points = [o["waypoint"] for o in ready]
    payload = {
        "schema": "bloompilot.geo-sequence.v1", "exported_at": generated,
        "metadata": {"waypoint_count": len(points), "city_count": len(points),
                     "total_search_points": sum(o["evidence"]["search_points"] for o in ready),
                     "range_unit": "km", "source_city_count": len(source_points),
                     "unverified_city_count": len(unresolved), "policy": asdict(policy),
                     "source": "ESA WorldCover 2021 v200, built-up class 50; GeoNames cities15000",
                     "source_url": "https://esa-worldcover.org/en/data-access",
                     "source_doi": "https://doi.org/10.5281/zenodo.7254221",
                     "attribution": "© ESA WorldCover project 2021 / Contains modified Copernicus Sentinel data (2021) processed by ESA WorldCover consortium",
                     "device_calibrated": calibration is not None},
        "sequence": {"version": 1, "id": "pikmin-inhabited-less-known", "name": args.name,
                     "updated_at": generated, "waypoints": points},
    }
    path = output_dir / "less_known_inhabited.json"
    atomic_json(path, payload)
    print(json.dumps({"route": str(path), "cities": len(points), "unverified": len(unresolved),
                      "points": payload["metadata"]["total_search_points"]}, ensure_ascii=False), flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", default=str(PROJECT_ROOT / "routes/pikmin_global/less_known/less_known_001_of_001.json"))
    parser.add_argument("--city-archive", default=str(PROJECT_ROOT / "data/reference/geonames/cities15000.zip"))
    parser.add_argument("--cache", default=str(PROJECT_ROOT / "data/reference/inhabited_routes"))
    parser.add_argument("--output-dir", default=str(PROJECT_ROOT / "routes/pikmin_inhabited"))
    parser.add_argument("--name", default="Pikmin 冷門城鎮・建成區搜尋")
    parser.add_argument("--device-scales", default=str(PROJECT_ROOT / "runtime/phone_automation/device_scales.json"))
    parser.add_argument("--device-serial")
    parser.add_argument("--city-id", action="append")
    parser.add_argument("--limit", type=int)
    parser.add_argument("--workers", type=int, default=4)
    parser.add_argument("--allow-partial", action="store_true")
    main(parser.parse_args())
