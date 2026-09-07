from __future__ import annotations

import argparse
import hashlib
import html
import json
import math
import re
import sys
import unicodedata
import zipfile
from collections import Counter
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from bloompilot.route_coverage import (  # noqa: E402
    REFERENCE_SCREEN_HEIGHT_PX,
    REFERENCE_SCREEN_WIDTH_PX,
    coverage_radii_from_km,
    grid_step_km,
    viewport_half_km,
)
from ghsl_settlement_bounds import SettlementIndex  # noqa: E402


DEFAULT_CITY_ARCHIVE = PROJECT_ROOT / "data" / "reference" / "geonames" / "cities15000.zip"
DEFAULT_COUNTRY_INFO = PROJECT_ROOT / "data" / "reference" / "geonames" / "countryInfo.txt"
DEFAULT_FREEDOM_SCORES = (
    PROJECT_ROOT / "data" / "reference" / "freedomhouse" / "fiw_2026_scores.html"
)
DEFAULT_OUTPUT = PROJECT_ROOT / "routes" / "pikmin_global"
DEFAULT_GHSL_ARCHIVE = (
    PROJECT_ROOT
    / "data"
    / "reference"
    / "ghsl"
    / "GHS_WUP_DEGURBA_E2025_GLOBE_R2025A_54009_1000_V1_0.zip"
)
DEFAULT_GHSL_DIRECTORY = DEFAULT_GHSL_ARCHIVE.with_suffix("")

VIEWPORT_WIDTH_M = 627.9
MAX_WAYPOINTS = 50_000
MAX_SEARCH_POINTS = 1_000_000_000
POPULAR_CITY_COUNT = 500

# The user explicitly excluded mainland China, Russia and Belarus. Hong Kong
# and Macao are kept with mainland China here because they are Chinese SARs.
EXPLICIT_EXCLUSIONS = {"BY", "CN", "HK", "MO", "RU"}

# Country-name differences between GeoNames countryInfo.txt and Freedom House.
FREEDOM_NAME_ALIASES = {
    "bahamas": "the bahamas",
    "cape verde": "cabo verde",
    "congo republic": "republic of the congo",
    "democratic republic of the congo": "democratic republic of the congo",
    "east timor": "timor leste",
    "gambia": "the gambia",
    "ivory coast": "cote d ivoire",
    "north macedonia": "north macedonia",
    "south korea": "south korea",
    "swaziland": "eswatini",
    "united states": "united states",
}


@dataclass(frozen=True)
class City:
    geoname_id: int
    name: str
    ascii_name: str
    lat: float
    lng: float
    country_code: str
    feature_code: str
    population: int


@dataclass(frozen=True)
class CityCoverage:
    center_lat: float
    center_lng: float
    left_km: float
    right_km: float
    up_km: float
    down_km: float
    left_radius: int
    right_radius: int
    up_radius: int
    down_radius: int
    source: str
    settlement_class: str | None = None
    settlement_id: str | None = None
    match_method: str | None = None
    match_distance_km: float | None = None
    scan_spans: tuple[tuple[int, int, int], ...] | None = None

    @property
    def point_count(self) -> int:
        if self.scan_spans is not None:
            return sum(end_x - start_x + 1 for _y, start_x, end_x in self.scan_spans)
        return self.rectangular_point_count

    @property
    def rectangular_point_count(self) -> int:
        return (
            self.left_radius + self.right_radius + 1
        ) * (
            self.up_radius + self.down_radius + 1
        )


def normalized_name(value: str) -> str:
    decomposed = unicodedata.normalize("NFKD", html.unescape(value))
    ascii_value = "".join(character for character in decomposed if not unicodedata.combining(character))
    return re.sub(r"[^a-z0-9]+", " ", ascii_value.casefold()).strip()


def strip_tags(value: str) -> str:
    return re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", " ", value))).strip()


def freedom_statuses(path: Path) -> dict[str, str]:
    source = path.read_text(encoding="utf-8")
    results: dict[str, str] = {}
    for row in re.findall(r"<tr\b[^>]*>(.*?)</tr>", source, flags=re.IGNORECASE | re.DOTALL):
        country_cell = re.search(
            r'<td\b[^>]*data-group="country_name"[^>]*>(.*?)</td>',
            row,
            flags=re.IGNORECASE | re.DOTALL,
        )
        status_cell = re.search(
            r'<td\b[^>]*data-group="fiw"[^>]*>(.*?)</td>',
            row,
            flags=re.IGNORECASE | re.DOTALL,
        )
        if country_cell is None or status_cell is None:
            continue
        country = normalized_name(strip_tags(country_cell.group(1)).rstrip("*"))
        status_text = strip_tags(status_cell.group(1))
        match = re.search(r"\b(Partly Free|Not Free|Free)\b", status_text)
        if country and match:
            results[country] = match.group(1)
    if len(results) < 180:
        raise RuntimeError(f"Freedom House 國家分類只解析到 {len(results)} 筆。")
    return results


def country_codes(path: Path, statuses: dict[str, str]) -> tuple[dict[str, str], dict[str, str]]:
    names: dict[str, str] = {}
    resolved_statuses: dict[str, str] = {}
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        if not raw_line or raw_line.startswith("#"):
            continue
        columns = raw_line.split("\t")
        if len(columns) < 5:
            continue
        code, country_name = columns[0], columns[4]
        names[code] = country_name
        key = normalized_name(country_name)
        key = FREEDOM_NAME_ALIASES.get(key, key)
        if key in statuses:
            resolved_statuses[code] = statuses[key]
    return names, resolved_statuses


def load_cities(path: Path) -> list[City]:
    with zipfile.ZipFile(path) as archive:
        members = [name for name in archive.namelist() if name.endswith(".txt")]
        if len(members) != 1:
            raise RuntimeError(f"GeoNames 壓縮檔內容異常：{members}")
        with archive.open(members[0]) as raw_stream:
            rows = raw_stream.read().decode("utf-8").splitlines()
    cities: list[City] = []
    for row in rows:
        columns = row.split("\t")
        if len(columns) < 19 or columns[6] != "P":
            continue
        cities.append(
            City(
                geoname_id=int(columns[0]),
                name=columns[1],
                ascii_name=columns[2] or columns[1],
                lat=float(columns[4]),
                lng=float(columns[5]),
                country_code=columns[8],
                feature_code=columns[7],
                population=max(0, int(columns[14] or 0)),
            )
        )
    return cities


def fallback_coverage(city: City) -> CityCoverage:
    # GHSL covers almost every selected city. For the handful of unmatched
    # centres, use a deliberately conservative low-density circular estimate
    # and label it as a fallback instead of presenting it as a measured bound.
    population = city.population or (75_000 if city.feature_code == "PPLC" else 15_000)
    half_extent_km = round(
        max(1.0, math.sqrt(population / (300.0 * math.pi))),
        6,
    )
    radii = coverage_radii_from_km(
        half_extent_km,
        half_extent_km,
        half_extent_km,
        half_extent_km,
    )
    return CityCoverage(
        center_lat=city.lat,
        center_lng=city.lng,
        left_km=half_extent_km,
        right_km=half_extent_km,
        up_km=half_extent_km,
        down_km=half_extent_km,
        left_radius=radii[0],
        right_radius=radii[1],
        up_radius=radii[2],
        down_radius=radii[3],
        source="population_area_fallback_300_per_km2",
    )


def city_coverages(
    cities: list[City],
    settlement_index: SettlementIndex,
) -> tuple[dict[int, CityCoverage], Counter[str]]:
    results: dict[int, CityCoverage] = {}
    summary: Counter[str] = Counter()
    for city in cities:
        match = settlement_index.match(city.lat, city.lng, city.population)
        if match is None:
            coverage = fallback_coverage(city)
        else:
            distances = tuple(
                round(value, 6)
                for value in (
                    match.left_km,
                    match.right_km,
                    match.up_km,
                    match.down_km,
                )
            )
            radii = coverage_radii_from_km(
                distances[0],
                distances[1],
                distances[2],
                distances[3],
            )
            spans = settlement_index.scan_spans(
                match,
                horizontal_step_km=grid_step_km("horizontal"),
                vertical_step_km=grid_step_km("vertical"),
                horizontal_half_km=viewport_half_km("horizontal"),
                vertical_half_km=viewport_half_km("vertical"),
            )
            coverage = CityCoverage(
                center_lat=match.center_lat,
                center_lng=match.center_lng,
                left_km=distances[0],
                right_km=distances[1],
                up_km=distances[2],
                down_km=distances[3],
                left_radius=radii[0],
                right_radius=radii[1],
                up_radius=radii[2],
                down_radius=radii[3],
                source="ghsl_wup_degurba_2025_settlement_boundary",
                settlement_class=match.settlement_class,
                settlement_id=match.settlement_id,
                match_method=match.method,
                match_distance_km=match.distance_m / 1_000.0,
                scan_spans=spans,
            )
        results[city.geoname_id] = coverage
        summary[coverage.source] += 1
        if coverage.settlement_class:
            summary[f"class_{coverage.settlement_class}"] += 1
        if coverage.match_method:
            summary[f"match_{coverage.match_method}"] += 1
        summary["rectangle_grid_cells"] += coverage.rectangular_point_count
        summary["effective_grid_cells"] += coverage.point_count
        summary["masked_empty_grid_cells"] += (
            coverage.rectangular_point_count - coverage.point_count
        )
    return results, summary


def point_count(city: City, coverages: dict[int, CityCoverage]) -> int:
    return coverages[city.geoname_id].point_count


def waypoint(
    city: City,
    country_name: str,
    coverages: dict[int, CityCoverage],
) -> dict[str, object]:
    coverage = coverages[city.geoname_id]
    population = f"{city.population:,}" if city.population else "n/a"
    item: dict[str, object] = {
        "id": f"geonames-{city.geoname_id}",
        "name": f"{city.ascii_name}, {country_name} · pop {population}"[:120],
        "lat": round(coverage.center_lat, 7),
        "lng": round(coverage.center_lng, 7),
        "wait_seconds": 10,
        "range_unit": "km",
        "left_km": round(coverage.left_km, 6),
        "right_km": round(coverage.right_km, 6),
        "up_km": round(coverage.up_km, 6),
        "down_km": round(coverage.down_km, 6),
        "left_radius": coverage.left_radius,
        "right_radius": coverage.right_radius,
        "up_radius": coverage.up_radius,
        "down_radius": coverage.down_radius,
        "horizontal_step_km": round(grid_step_km("horizontal"), 6),
        "vertical_step_km": round(grid_step_km("vertical"), 6),
        "coverage_source": coverage.source,
    }
    if coverage.scan_spans is not None:
        item["scan_spans"] = [list(span) for span in coverage.scan_spans]
    if coverage.settlement_class is not None:
        item.update(
            {
                "settlement_class": coverage.settlement_class,
                "settlement_id": coverage.settlement_id,
                "settlement_match": coverage.match_method,
                "settlement_distance_km": round(coverage.match_distance_km or 0.0, 3),
            }
        )
    return item


def split_routes(
    cities: list[City],
    coverages: dict[int, CityCoverage],
) -> list[list[City]]:
    chunks: list[list[City]] = []
    current: list[City] = []
    current_points = 0
    for city in cities:
        points = point_count(city, coverages)
        if current and (
            len(current) >= MAX_WAYPOINTS or current_points + points > MAX_SEARCH_POINTS
        ):
            chunks.append(current)
            current = []
            current_points = 0
        current.append(city)
        current_points += points
    if current:
        chunks.append(current)
    return chunks


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def write_category(
    output_dir: Path,
    category: str,
    category_label: str,
    cities: list[City],
    country_names: dict[str, str],
    coverages: dict[int, CityCoverage],
    generated_at: str,
) -> list[dict[str, object]]:
    category_dir = output_dir / category
    category_dir.mkdir(parents=True, exist_ok=True)
    for old_part in category_dir.glob(f"{category}_*.json"):
        old_part.unlink()
    chunks = split_routes(cities, coverages)
    entries: list[dict[str, object]] = []
    for index, chunk in enumerate(chunks, start=1):
        total_points = sum(point_count(city, coverages) for city in chunk)
        file_name = f"{category}_{index:03d}_of_{len(chunks):03d}.json"
        sequence_id = f"pikmin-global-{category}-{index:03d}"
        payload = {
            "schema": "bloompilot.geo-sequence.v1",
            "exported_at": generated_at,
            "metadata": {
                "category": category,
                "category_label": category_label,
                "part": index,
                "parts": len(chunks),
                "city_count": len(chunk),
                "total_search_points": total_points,
                "range_unit": "km",
                "viewport_width_m": VIEWPORT_WIDTH_M,
                "meters_per_pixel": round(VIEWPORT_WIDTH_M / 1080.0, 6),
                "reference_screen_px": {
                    "width": REFERENCE_SCREEN_WIDTH_PX,
                    "height": REFERENCE_SCREEN_HEIGHT_PX,
                },
                "grid_step_km": {
                    "horizontal": round(grid_step_km("horizontal"), 6),
                    "vertical": round(grid_step_km("vertical"), 6),
                },
                "coverage_rule": {
                    "source": "GHSL WUP DEGURBA 2025 settlement entity boundary",
                    "conversion": "ceil((centre_to_boundary_km - viewport_half_km) / grid_step_km)",
                    "rounding": "always_up",
                    "centre": "GHSL population-weighted settlement centroid",
                    "capture_mask": "only grid viewports intersecting the settlement polygon",
                },
            },
            "sequence": {
                "version": 1,
                "id": sequence_id,
                "name": f"Pikmin 全球{category_label} {index:03d}/{len(chunks):03d}",
                "updated_at": generated_at,
                "waypoints": [
                    waypoint(city, country_names[city.country_code], coverages)
                    for city in chunk
                ],
            },
        }
        path = category_dir / file_name
        # Scan masks contain many small integer triples. Compact JSON keeps the
        # merged route below the import limit without changing its structure.
        path.write_text(
            json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
            encoding="utf-8",
        )
        entries.append(
            {
                "file": path.relative_to(output_dir).as_posix(),
                "city_count": len(chunk),
                "total_search_points": total_points,
                "first_city": chunk[0].ascii_name,
                "last_city": chunk[-1].ascii_name,
            }
        )
    return entries


def write_merged_route(
    output_dir: Path,
    cities: list[City],
    country_names: dict[str, str],
    coverages: dict[int, CityCoverage],
    generated_at: str,
) -> dict[str, object]:
    total_points = sum(point_count(city, coverages) for city in cities)
    if len(cities) > MAX_WAYPOINTS or total_points > MAX_SEARCH_POINTS:
        raise RuntimeError(
            f"合併路線超過上限：{len(cities)} 城市 / {total_points} 搜尋點"
        )
    file_name = f"pikmin_all_{len(cities)}_cities.json"
    payload = {
        "schema": "bloompilot.geo-sequence.v1",
        "exported_at": generated_at,
        "metadata": {
            "category": "all",
            "category_label": "熱門與冷門合併",
            "city_count": len(cities),
            "popular_city_count": min(POPULAR_CITY_COUNT, len(cities)),
            "less_known_city_count": max(0, len(cities) - POPULAR_CITY_COUNT),
            "total_search_points": total_points,
            "range_unit": "km",
            "viewport_width_m": VIEWPORT_WIDTH_M,
            "meters_per_pixel": round(VIEWPORT_WIDTH_M / 1080.0, 6),
            "reference_screen_px": {
                "width": REFERENCE_SCREEN_WIDTH_PX,
                "height": REFERENCE_SCREEN_HEIGHT_PX,
            },
            "grid_step_km": {
                "horizontal": round(grid_step_km("horizontal"), 6),
                "vertical": round(grid_step_km("vertical"), 6),
            },
            "coverage_rule": {
                "source": "GHSL WUP DEGURBA 2025 settlement entity polygon",
                "centre": "GHSL population-weighted settlement centroid",
                "capture_mask": "only grid viewports intersecting the settlement polygon",
                "rounding": "always_up",
            },
            "memory_strategy": {
                "frontend_waypoint_page_size": 50,
                "backend_grid_expansion": "lazy one city at a time",
                "detection_records": "append-only JSONL; count retained in memory",
            },
        },
        "sequence": {
            "version": 1,
            "id": "pikmin-global-all-cities",
            "name": f"Pikmin 全球全部 {len(cities):,} 城市",
            "updated_at": generated_at,
            "waypoints": [
                waypoint(city, country_names[city.country_code], coverages)
                for city in cities
            ],
        },
    }
    path = output_dir / file_name
    path.write_text(
        json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
        encoding="utf-8",
    )
    return {
        "file": path.relative_to(output_dir).as_posix(),
        "city_count": len(cities),
        "total_search_points": total_points,
        "size_bytes": path.stat().st_size,
    }


def main(args: argparse.Namespace) -> None:
    city_archive = Path(args.city_archive).resolve()
    country_info = Path(args.country_info).resolve()
    freedom_scores = Path(args.freedom_scores).resolve()
    ghsl_directory = Path(args.ghsl_directory).resolve()
    ghsl_archive = Path(args.ghsl_archive).resolve()
    output_dir = Path(args.output_dir).resolve()
    output_dir.mkdir(parents=True, exist_ok=True)

    statuses = freedom_statuses(freedom_scores)
    names, code_statuses = country_codes(country_info, statuses)
    eligible_codes = {
        code
        for code, status in code_statuses.items()
        if status in {"Free", "Partly Free"} and code not in EXPLICIT_EXCLUSIONS
    }
    all_cities = load_cities(city_archive)
    included = [city for city in all_cities if city.country_code in eligible_codes]
    included.sort(key=lambda city: (-city.population, city.country_code, city.ascii_name, city.geoname_id))
    if len(included) <= POPULAR_CITY_COUNT:
        raise RuntimeError(f"符合條件的城市只有 {len(included)} 個，無法拆分熱門與冷門。")

    popular = included[:POPULAR_CITY_COUNT]
    less_known = included[POPULAR_CITY_COUNT:]
    karimnagar = [city for city in less_known if normalized_name(city.ascii_name) == "karimnagar"]
    berlin = [city for city in popular if normalized_name(city.ascii_name) == "berlin"]
    if not karimnagar:
        raise RuntimeError("冷門城市清單沒有 Karimnagar；熱門分界不符合需求。")
    if not berlin:
        raise RuntimeError("熱門城市清單沒有 Berlin；熱門分界不符合需求。")

    settlement_index = SettlementIndex(ghsl_directory)
    coverages, coverage_summary = city_coverages(included, settlement_index)

    generated_at = datetime.now(UTC).replace(microsecond=0).isoformat()
    popular_files = write_category(
        output_dir, "popular", "熱門城市", popular, names, coverages, generated_at
    )
    cold_files = write_category(
        output_dir, "less_known", "冷門城市", less_known, names, coverages, generated_at
    )
    merged_file = write_merged_route(output_dir, included, names, coverages, generated_at)
    countries_with_cities = sorted({city.country_code for city in included})
    manifest = {
        "schema": "bloompilot.pikmin-city-route-index.v1",
        "generated_at": generated_at,
        "definition": {
            "coverage": "GeoNames cities15000: population over 15,000 or national capitals",
            "pikmin_availability": "Pikmin Bloom official help directs availability checks to the local App Store or Google Play and does not publish a static country list.",
            "country_policy": "Freedom House 2026 Free or Partly Free; explicit exclusions BY/CN/HK/MO/RU.",
            "popular": "Top 500 included cities by GeoNames population.",
            "less_known": "All remaining included cities; Karimnagar is in this category.",
            "viewport_width_m": VIEWPORT_WIDTH_M,
            "range_unit": "km",
            "reference_screen_px": {
                "width": REFERENCE_SCREEN_WIDTH_PX,
                "height": REFERENCE_SCREEN_HEIGHT_PX,
            },
            "viewport_half_km": {
                "horizontal": round(viewport_half_km("horizontal"), 6),
                "vertical": round(viewport_half_km("vertical"), 6),
            },
            "grid_step_km": {
                "horizontal": round(grid_step_km("horizontal"), 6),
                "vertical": round(grid_step_km("vertical"), 6),
            },
            "grid_rounding": "ceil after subtracting the centred half viewport",
            "route_centre": "GHSL population-weighted settlement centroid",
            "capture_mask": "compact scan_spans; only grid viewports intersecting the settlement polygon",
            "limits_per_file": {
                "cities": MAX_WAYPOINTS,
                "search_points": MAX_SEARCH_POINTS,
            },
        },
        "sources": {
            "geonames": {
                "url": "https://download.geonames.org/export/dump/cities15000.zip",
                "sha256": sha256(city_archive),
            },
            "freedom_house": {
                "url": "https://freedomhouse.org/country/scores?order=field_fiw_aggregate_score&sort=desc",
                "sha256": sha256(freedom_scores),
            },
            "ghsl_settlement_boundaries": {
                "url": "https://jeodpp.jrc.ec.europa.eu/ftp/jrc-opendata/GHSL/GHS_WUP_DEGURBA_GLOBE_R2025A/GHS_WUP_DEGURBA_E2025_GLOBE_R2025A_54009_1000/V1-0/GHS_WUP_DEGURBA_E2025_GLOBE_R2025A_54009_1000_V1_0.zip",
                "sha256": sha256(ghsl_archive),
                "classes": ["UC", "DUC", "SDUC", "RC"],
            },
            "pikmin_support": "https://niantic.helpshift.com/hc/en/23-pikmin-bloom/faq/3056-can-i-download-pikmin-bloom-in-my-country-region-1654476198/",
        },
        "summary": {
            "cities": len(included),
            "popular_cities": len(popular),
            "less_known_cities": len(less_known),
            "countries_or_regions": len(countries_with_cities),
            "coverage": dict(sorted(coverage_summary.items())),
            "route_files": len(popular_files) + len(cold_files) + 1,
            "categorized_route_files": len(popular_files) + len(cold_files),
            "popular_route_files": len(popular_files),
            "less_known_route_files": len(cold_files),
            "karimnagar": {
                "geoname_id": karimnagar[0].geoname_id,
                "population": karimnagar[0].population,
                "category": "less_known",
            },
            "berlin": {
                "geoname_id": berlin[0].geoname_id,
                "population": berlin[0].population,
                "category": "popular",
            },
        },
        "included_country_codes": countries_with_cities,
        "explicitly_excluded_country_codes": sorted(EXPLICIT_EXCLUSIONS),
        "merged_file": merged_file,
        "popular_files": popular_files,
        "less_known_files": cold_files,
    }
    (output_dir / "index.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(json.dumps(manifest["summary"], ensure_ascii=False, indent=2))


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description="產生可匯入 BloomPilot 的全球城市搜尋路線。")
    result.add_argument("--city-archive", default=str(DEFAULT_CITY_ARCHIVE))
    result.add_argument("--country-info", default=str(DEFAULT_COUNTRY_INFO))
    result.add_argument("--freedom-scores", default=str(DEFAULT_FREEDOM_SCORES))
    result.add_argument("--ghsl-archive", default=str(DEFAULT_GHSL_ARCHIVE))
    result.add_argument("--ghsl-directory", default=str(DEFAULT_GHSL_DIRECTORY))
    result.add_argument("--output-dir", default=str(DEFAULT_OUTPUT))
    return result


if __name__ == "__main__":
    main(parser().parse_args())
