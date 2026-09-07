from __future__ import annotations

import math
import struct
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO, Iterable


# ESRI:54009 uses the WGS84 semi-major axis, not the authalic radius.
# Both directions must agree with the bundled GHSL .prj files.
MOLLWEIDE_RADIUS_M = 6_378_137.0
INDEX_CELL_M = 100_000.0
NEAREST_SETTLEMENT_LIMIT_M = 20_000.0


# Deliberately accept only the bundled GHSL WKT1 definition (ignoring
# whitespace). Other CRS definitions need explicit support, not a guessed
# conversion based on a matching projection name or authority alone.
_GHSL_MOLLWEIDE_WKT = (
    'PROJCS["World_Mollweide",GEOGCS["WGS 84",DATUM["WGS_1984",'
    'SPHEROID["WGS 84",6378137,298.257223563,AUTHORITY["EPSG","7030"]],'
    'AUTHORITY["EPSG","6326"]],PRIMEM["Greenwich",0],'
    'UNIT["Degree",0.0174532925199433]],PROJECTION["Mollweide"],'
    'PARAMETER["central_meridian",0],PARAMETER["false_easting",0],'
    'PARAMETER["false_northing",0],UNIT["metre",1,AUTHORITY["EPSG","9001"]],'
    'AXIS["Easting",EAST],AXIS["Northing",NORTH],AUTHORITY["ESRI","54009"]]'
)


def mollweide_xy(lat: float, lng: float) -> tuple[float, float]:
    """Project WGS84 degrees to the ESRI:54009 World Mollweide plane."""

    latitude = math.radians(max(-90.0, min(90.0, float(lat))))
    longitude = math.radians(((float(lng) + 180.0) % 360.0) - 180.0)
    if abs(abs(latitude) - math.pi / 2) < 1e-12:
        theta = math.copysign(math.pi / 2, latitude)
    else:
        theta = latitude
        target = math.pi * math.sin(latitude)
        for _ in range(12):
            delta = (
                2.0 * theta + math.sin(2.0 * theta) - target
            ) / (2.0 + 2.0 * math.cos(2.0 * theta))
            theta -= delta
            if abs(delta) < 1e-13:
                break
    x = (
        2.0
        * math.sqrt(2.0)
        * MOLLWEIDE_RADIUS_M
        / math.pi
        * longitude
        * math.cos(theta)
    )
    y = math.sqrt(2.0) * MOLLWEIDE_RADIUS_M * math.sin(theta)
    return x, y


def mollweide_lat_lng(x: float, y: float) -> tuple[float, float]:
    """Invert the spherical ESRI:54009 projection used by the GHSL files."""

    theta = math.asin(
        max(-1.0, min(1.0, float(y) / (math.sqrt(2.0) * MOLLWEIDE_RADIUS_M)))
    )
    latitude = math.asin(
        max(-1.0, min(1.0, (2.0 * theta + math.sin(2.0 * theta)) / math.pi))
    )
    cosine = math.cos(theta)
    longitude = (
        0.0
        if abs(cosine) < 1e-12
        else math.pi
        * float(x)
        / (2.0 * math.sqrt(2.0) * MOLLWEIDE_RADIUS_M * cosine)
    )
    return math.degrees(latitude), ((math.degrees(longitude) + 180.0) % 360.0) - 180.0


@dataclass(frozen=True, slots=True)
class ShapeBounds:
    source_index: int
    record_index: int
    content_offset: int
    content_length: int
    min_x: float
    min_y: float
    max_x: float
    max_y: float
    settlement_id: str
    population: float
    centroid_x: float
    centroid_y: float

    @property
    def bbox_area(self) -> float:
        return max(0.0, self.max_x - self.min_x) * max(0.0, self.max_y - self.min_y)


@dataclass(frozen=True, slots=True)
class SettlementMatch:
    settlement_class: str
    settlement_id: str
    method: str
    distance_m: float
    left_km: float
    right_km: float
    up_km: float
    down_km: float
    center_lat: float
    center_lng: float
    source_index: int
    record_index: int


@dataclass(frozen=True, slots=True)
class PolygonGeometry:
    rings: tuple[tuple[tuple[float, float], ...], ...]

    @classmethod
    def from_payload(cls, payload: bytes) -> "PolygonGeometry":
        if len(payload) < 44:
            return cls(())
        part_count, point_count = struct.unpack_from("<2I", payload, 36)
        part_offset = 44
        points_offset = part_offset + part_count * 4
        if (
            part_count < 1
            or point_count < 3
            or points_offset + point_count * 16 > len(payload)
        ):
            return cls(())
        starts = list(struct.unpack_from(f"<{part_count}I", payload, part_offset))
        starts.append(point_count)
        rings: list[tuple[tuple[float, float], ...]] = []
        for start, end in zip(starts, starts[1:]):
            if end - start < 3:
                continue
            rings.append(
                tuple(
                    struct.unpack_from("<2d", payload, points_offset + index * 16)
                    for index in range(start, end)
                )
            )
        return cls(tuple(rings))

    def contains(self, x: float, y: float) -> bool:
        inside = False
        for ring in self.rings:
            previous_x, previous_y = ring[-1]
            for current_x, current_y in ring:
                if (current_y > y) != (previous_y > y):
                    crossing_x = (
                        (previous_x - current_x)
                        * (y - current_y)
                        / (previous_y - current_y)
                        + current_x
                    )
                    if x <= crossing_x:
                        inside = not inside
                previous_x, previous_y = current_x, current_y
        return inside

    def x_intervals(self, y: float) -> tuple[tuple[float, float], ...]:
        """Return even-odd polygon interiors along one horizontal scan line."""

        crossings: list[float] = []
        for ring in self.rings:
            previous_x, previous_y = ring[-1]
            for current_x, current_y in ring:
                if (current_y > y) != (previous_y > y):
                    crossings.append(
                        (previous_x - current_x)
                        * (y - current_y)
                        / (previous_y - current_y)
                        + current_x
                    )
                previous_x, previous_y = current_x, current_y
        crossings.sort()
        return tuple(
            (crossings[index], crossings[index + 1])
            for index in range(0, len(crossings) - 1, 2)
        )


class DbfReader:
    def __init__(self, path: Path) -> None:
        self.path = path
        with path.open("rb") as stream:
            header = stream.read(32)
            if len(header) != 32:
                raise ValueError(f"DBF 標頭損壞：{path}")
            self.record_count = struct.unpack_from("<I", header, 4)[0]
            self.header_length = struct.unpack_from("<H", header, 8)[0]
            self.record_length = struct.unpack_from("<H", header, 10)[0]
            field_bytes = stream.read(self.header_length - 32)
        self.fields: list[tuple[str, str, int, int]] = []
        position = 0
        offset = 1
        while position + 32 <= len(field_bytes) and field_bytes[position] != 0x0D:
            descriptor = field_bytes[position : position + 32]
            name = descriptor[:11].split(b"\0", 1)[0].decode("ascii", errors="replace")
            kind = chr(descriptor[11])
            length = descriptor[16]
            self.fields.append((name, kind, length, offset))
            offset += length
            position += 32

    def record(self, index: int) -> dict[str, str]:
        if not 0 <= index < self.record_count:
            raise IndexError(index)
        with self.path.open("rb") as stream:
            stream.seek(self.header_length + index * self.record_length)
            payload = stream.read(self.record_length)
        if len(payload) != self.record_length or payload[:1] == b"*":
            return {}
        result: dict[str, str] = {}
        for name, _kind, length, offset in self.fields:
            value = payload[offset : offset + length].decode("latin-1", errors="replace").strip()
            if value:
                result[name] = value
        return result

    def summaries(self, kind: str) -> Iterable[tuple[str, float, float, float]]:
        id_names = (f"ID_{kind}_G0", "Unique_ID", "ID_ENTITY", "ENTITY_ID", "ID")
        field_lookup = {name: (length, offset) for name, _kind, length, offset in self.fields}

        def text(payload: bytes, name: str) -> str:
            field = field_lookup.get(name)
            if field is None:
                return ""
            length, offset = field
            return payload[offset : offset + length].decode("latin-1", errors="replace").strip()

        with self.path.open("rb") as stream:
            stream.seek(self.header_length)
            for index in range(self.record_count):
                payload = stream.read(self.record_length)
                if len(payload) != self.record_length or payload[:1] == b"*":
                    yield f"{kind}-{index + 1}", 0.0, 0.0, 0.0
                    continue
                settlement_id = next((text(payload, name) for name in id_names if text(payload, name)), "")
                try:
                    population = float(text(payload, "POP_2025") or 0.0)
                    centroid_x = float(text(payload, "PWCentroidX") or 0.0)
                    centroid_y = float(text(payload, "PWCentroidY") or 0.0)
                except ValueError:
                    population = centroid_x = centroid_y = 0.0
                yield (
                    settlement_id or f"{kind}-{index + 1}",
                    population,
                    centroid_x,
                    centroid_y,
                )


class ShapeSource:
    def __init__(self, kind: str, shp_path: Path, source_index: int) -> None:
        self.kind = kind
        self.path = shp_path
        self.source_index = source_index
        prj_path = shp_path.with_suffix(".prj")
        projection = "".join(prj_path.read_text(encoding="utf-8-sig").split())
        if projection != "".join(_GHSL_MOLLWEIDE_WKT.split()):
            raise ValueError(
                f"不支援的 GHSL 座標系統：{prj_path}；"
                "需要原始 GHSL ESRI:54009 定義（半徑 6378137 m）。"
            )
        self.dbf = DbfReader(shp_path.with_suffix(".dbf"))
        summaries = self.dbf.summaries(kind)
        self.records = [
            ShapeBounds(*bounds, *summary)
            for bounds, summary in zip(self._read_bounds(), summaries, strict=True)
        ]
        if len(self.records) != self.dbf.record_count:
            raise ValueError(
                f"{shp_path.name} 的 SHP/DBF 筆數不一致："
                f"{len(self.records)} / {self.dbf.record_count}"
            )

    def _read_bounds(self) -> Iterable[tuple[int, int, int, int, float, float, float, float]]:
        with self.path.open("rb") as stream:
            header = stream.read(100)
            if len(header) != 100:
                raise ValueError(f"SHP 標頭損壞：{self.path}")
            record_index = 0
            while True:
                record_header = stream.read(8)
                if not record_header:
                    break
                if len(record_header) != 8:
                    raise ValueError(f"SHP 記錄標頭損壞：{self.path}")
                content_length = struct.unpack(">I", record_header[4:])[0] * 2
                content_offset = stream.tell()
                prefix = stream.read(min(36, content_length))
                if len(prefix) < min(36, content_length):
                    raise ValueError(f"SHP 記錄內容損壞：{self.path}")
                shape_type = struct.unpack_from("<I", prefix, 0)[0] if len(prefix) >= 4 else 0
                if shape_type not in {5, 15, 25} or len(prefix) < 36:
                    raise ValueError(
                        f"{self.path.name} 第 {record_index + 1} 筆不是 Polygon：{shape_type}"
                    )
                min_x, min_y, max_x, max_y = struct.unpack_from("<4d", prefix, 4)
                yield (
                    self.source_index,
                    record_index,
                    content_offset,
                    content_length,
                    min_x,
                    min_y,
                    max_x,
                    max_y,
                )
                stream.seek(content_offset + content_length)
                record_index += 1

    def read_geometry(self, record: ShapeBounds) -> bytes:
        with self.path.open("rb") as stream:
            stream.seek(record.content_offset)
            return stream.read(record.content_length)

    def settlement_id(self, record_index: int) -> str:
        return self.records[record_index].settlement_id


def _point_in_polygon(payload: bytes, x: float, y: float) -> bool:
    return PolygonGeometry.from_payload(payload).contains(x, y)


def _bbox_distance(record: ShapeBounds, x: float, y: float) -> float:
    delta_x = max(record.min_x - x, 0.0, x - record.max_x)
    delta_y = max(record.min_y - y, 0.0, y - record.max_y)
    return math.hypot(delta_x, delta_y)


class SettlementIndex:
    def __init__(self, directory: Path) -> None:
        paths: list[tuple[str, Path]] = []
        for kind in ("UC", "DUC", "SDUC", "RC"):
            matches = sorted(directory.glob(f"*_{kind}_V1_0.shp"))
            if len(matches) != 1:
                raise FileNotFoundError(f"找不到唯一的 GHSL {kind} 邊界檔：{directory}")
            paths.append((kind, matches[0]))
        self.sources = [
            ShapeSource(kind, path, source_index)
            for source_index, (kind, path) in enumerate(paths)
        ]
        self.bins: dict[tuple[int, int], list[ShapeBounds]] = defaultdict(list)
        for source in self.sources:
            for record in source.records:
                min_cell_x = math.floor(record.min_x / INDEX_CELL_M)
                max_cell_x = math.floor(record.max_x / INDEX_CELL_M)
                min_cell_y = math.floor(record.min_y / INDEX_CELL_M)
                max_cell_y = math.floor(record.max_y / INDEX_CELL_M)
                for cell_x in range(min_cell_x, max_cell_x + 1):
                    for cell_y in range(min_cell_y, max_cell_y + 1):
                        self.bins[(cell_x, cell_y)].append(record)
        self._center_cache: dict[tuple[int, int], tuple[float, float]] = {}
        self._scan_span_cache: dict[
            tuple[int, int, float, float, float, float],
            tuple[tuple[int, int, int], ...],
        ] = {}

    def match(self, lat: float, lng: float, population: int = 0) -> SettlementMatch | None:
        x, y = mollweide_xy(lat, lng)
        cell_x = math.floor(x / INDEX_CELL_M)
        cell_y = math.floor(y / INDEX_CELL_M)
        nearby: list[ShapeBounds] = []
        for delta_x in (-1, 0, 1):
            for delta_y in (-1, 0, 1):
                nearby.extend(self.bins.get((cell_x + delta_x, cell_y + delta_y), []))
        if not nearby:
            return None
        unique = {(record.source_index, record.record_index): record for record in nearby}.values()
        candidates = [
            record
            for record in unique
            if _bbox_distance(record, x, y) <= NEAREST_SETTLEMENT_LIMIT_M
        ]
        if not candidates:
            return None

        exact_keys: set[tuple[int, int]] = set()
        for record in candidates:
            if not (
                record.min_x <= x <= record.max_x
                and record.min_y <= y <= record.max_y
            ):
                continue
            source = self.sources[record.source_index]
            if _point_in_polygon(source.read_geometry(record), x, y):
                exact_keys.add((record.source_index, record.record_index))

        city_population = max(0.0, float(population))

        def population_error(record: ShapeBounds) -> float:
            if city_population <= 0 or record.population <= 0:
                return 0.0
            return abs(math.log((record.population + 1.0) / (city_population + 1.0)))

        def exact_score(record: ShapeBounds) -> tuple[float, float, float]:
            return (
                population_error(record),
                math.hypot(record.centroid_x - x, record.centroid_y - y),
                record.bbox_area,
            )

        def nearby_score(record: ShapeBounds) -> tuple[float, float, float]:
            bbox_distance = _bbox_distance(record, x, y)
            centroid_distance = math.hypot(record.centroid_x - x, record.centroid_y - y)
            # Outside all polygons, balance population compatibility with
            # distance. The selected route centre is moved into this polygon
            # and masked to its actual shape, so the gap from GeoNames is never
            # photographed; population matching also avoids treating a large
            # rural-cluster polygon as the extent of a nearby city.
            value = (
                population_error(record)
                + bbox_distance / 20_000.0
                + centroid_distance / 100_000.0
            )
            return value, bbox_distance, record.bbox_area

        exact_candidates = [
            record
            for record in candidates
            if (record.source_index, record.record_index) in exact_keys
        ]
        selected = (
            min(exact_candidates, key=exact_score)
            if exact_candidates
            else min(candidates, key=nearby_score)
        )
        distance = _bbox_distance(selected, x, y)
        method = (
            "contains"
            if (selected.source_index, selected.record_index) in exact_keys
            else "nearest"
        )
        return self._result(selected, x, y, method, distance)

    def _record_center(self, record: ShapeBounds) -> tuple[float, float]:
        key = (record.source_index, record.record_index)
        cached = self._center_cache.get(key)
        if cached is not None:
            return cached
        source = self.sources[record.source_index]
        geometry = PolygonGeometry.from_payload(source.read_geometry(record))
        candidates = (
            (record.centroid_x, record.centroid_y),
            ((record.min_x + record.max_x) / 2.0, (record.min_y + record.max_y) / 2.0),
        )
        for candidate in candidates:
            if all(math.isfinite(value) for value in candidate) and geometry.contains(*candidate):
                self._center_cache[key] = candidate
                return candidate

        # Concave or multipart polygons can have both centroids outside. Search
        # from the population centroid in 250 m increments for a guaranteed
        # interior point. GHSL settlement polygons are derived from 1 km cells.
        anchor_x = min(record.max_x, max(record.min_x, record.centroid_x))
        anchor_y = min(record.max_y, max(record.min_y, record.centroid_y))
        maximum_ring = int(
            math.ceil(max(record.max_x - record.min_x, record.max_y - record.min_y) / 250.0)
        )
        for ring in range(maximum_ring + 1):
            offsets = range(-ring, ring + 1)
            perimeter = (
                ((offset, -ring) for offset in offsets),
                ((ring, offset) for offset in range(-ring + 1, ring + 1)),
                ((offset, ring) for offset in range(ring - 1, -ring - 1, -1)),
                ((-ring, offset) for offset in range(ring - 1, -ring, -1)),
            )
            for side in perimeter:
                for offset_x, offset_y in side:
                    candidate = (anchor_x + offset_x * 250.0, anchor_y + offset_y * 250.0)
                    if geometry.contains(*candidate):
                        self._center_cache[key] = candidate
                        return candidate
        raise ValueError(f"GHSL 聚落 {record.settlement_id} 找不到內部中心點。")

    def scan_spans(
        self,
        match: SettlementMatch,
        *,
        horizontal_step_km: float,
        vertical_step_km: float,
        horizontal_half_km: float,
        vertical_half_km: float,
    ) -> tuple[tuple[int, int, int], ...]:
        """Return compact grid-row spans whose viewport intersects the polygon."""

        cache_key = (
            match.source_index,
            match.record_index,
            round(horizontal_step_km, 9),
            round(vertical_step_km, 9),
            round(horizontal_half_km, 9),
            round(vertical_half_km, 9),
        )
        cached = self._scan_span_cache.get(cache_key)
        if cached is not None:
            return cached
        record = self.sources[match.source_index].records[match.record_index]
        geometry = PolygonGeometry.from_payload(
            self.sources[match.source_index].read_geometry(record)
        )
        center_x, center_y = self._record_center(record)
        step_x = horizontal_step_km * 1_000.0
        step_y = vertical_step_km * 1_000.0
        half_x = horizontal_half_km * 1_000.0
        half_y = vertical_half_km * 1_000.0

        def radius(distance_m: float, half_m: float, step_m: float) -> int:
            return max(0, math.ceil(max(0.0, distance_m - half_m) / step_m))

        left = radius(center_x - record.min_x, half_x, step_x)
        right = radius(record.max_x - center_x, half_x, step_x)
        up = radius(record.max_y - center_y, half_y, step_y)
        down = radius(center_y - record.min_y, half_y, step_y)
        spans: list[tuple[int, int, int]] = []
        for grid_y in range(-down, up + 1):
            y = center_y + grid_y * step_y
            integer_intervals: list[tuple[int, int]] = []
            # Five horizontal samples cover the viewport's vertical band. GHSL
            # polygons come from 1 km settlement cells, so this retains edge
            # cells while avoiding a point-in-polygon call for every grid cell.
            for sample_y in (
                y - half_y,
                y - half_y / 2.0,
                y,
                y + half_y / 2.0,
                y + half_y,
            ):
                for start_x, end_x in geometry.x_intervals(sample_y):
                    start_grid = max(
                        -left,
                        math.ceil((start_x - half_x - center_x) / step_x),
                    )
                    end_grid = min(
                        right,
                        math.floor((end_x + half_x - center_x) / step_x),
                    )
                    if start_grid <= end_grid:
                        integer_intervals.append((start_grid, end_grid))
            # Preserve very small polygon fragments that fall between the five
            # scan lines by including viewports containing one of their vertices.
            for ring in geometry.rings:
                for point_x, point_y in ring:
                    if abs(point_y - y) <= half_y:
                        grid_x = round((point_x - center_x) / step_x)
                        start_grid = max(
                            -left,
                            math.ceil((point_x - half_x - center_x) / step_x),
                        )
                        end_grid = min(
                            right,
                            math.floor((point_x + half_x - center_x) / step_x),
                        )
                        if start_grid <= grid_x <= end_grid:
                            integer_intervals.append((start_grid, end_grid))
            integer_intervals.sort()
            merged: list[list[int]] = []
            for start_grid, end_grid in integer_intervals:
                if merged and start_grid <= merged[-1][1] + 1:
                    merged[-1][1] = max(merged[-1][1], end_grid)
                else:
                    merged.append([start_grid, end_grid])
            spans.extend((grid_y, start_grid, end_grid) for start_grid, end_grid in merged)
        if not any(y == 0 and start <= 0 <= end for y, start, end in spans):
            spans.append((0, 0, 0))
            spans.sort()
        result = tuple(spans)
        self._scan_span_cache[cache_key] = result
        return result

    def _result(
        self,
        record: ShapeBounds,
        x: float,
        y: float,
        method: str,
        distance_m: float,
    ) -> SettlementMatch:
        source = self.sources[record.source_index]
        center_x, center_y = self._record_center(record)
        center_lat, center_lng = mollweide_lat_lng(center_x, center_y)
        return SettlementMatch(
            settlement_class=source.kind,
            settlement_id=record.settlement_id,
            method=method,
            distance_m=distance_m,
            left_km=max(0.0, center_x - record.min_x) / 1_000.0,
            right_km=max(0.0, record.max_x - center_x) / 1_000.0,
            up_km=max(0.0, record.max_y - center_y) / 1_000.0,
            down_km=max(0.0, center_y - record.min_y) / 1_000.0,
            center_lat=center_lat,
            center_lng=center_lng,
            source_index=record.source_index,
            record_index=record.record_index,
        )
