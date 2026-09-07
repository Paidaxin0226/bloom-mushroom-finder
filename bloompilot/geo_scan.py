from __future__ import annotations

import math
import threading
import time
from dataclasses import dataclass
from typing import Callable, Protocol, Sequence

from .device_control import DeviceResponse
from .project_defaults import MAX_GEO_SEQUENCE_RADIUS
class CommandController(Protocol):
    def command(
        self,
        command: str,
        payload: dict[str, object] | None = None,
        *,
        require_ok: bool = True,
    ) -> DeviceResponse: ...

    def status(self) -> dict[str, object]: ...


@dataclass(frozen=True)
class GeoScanPoint:
    ordinal: int
    ring: int
    grid_x: int
    grid_y: int
    lat: float
    lng: float

    @property
    def location_label(self) -> str:
        if self.ring == 0:
            return "中心"
        return f"x={self.grid_x:+d}, y={self.grid_y:+d}｜第 {self.ring} 圈"

    @property
    def stem(self) -> str:
        return f"geo_{self.ordinal:03d}_x{self.grid_x:+d}_y{self.grid_y:+d}".replace(
            "+", "p"
        ).replace("-", "m")


@dataclass(frozen=True)
class GeoScanSummary:
    visited: int
    detection_count: int
    completed: bool
    stop_reason: str


@dataclass(frozen=True)
class GeoCaptureOutcome:
    detection_count: int = 0


GeoCaptureCallback = Callable[[GeoScanPoint, bytes], GeoCaptureOutcome | None]
StatusCallback = Callable[[str], None]
ProgressCallback = Callable[[int, int, GeoScanPoint], None]


def offset_coordinate(lat: float, lng: float, east_m: float, north_m: float) -> tuple[float, float]:
    if not -90 <= lat <= 90 or not -180 <= lng <= 180:
        raise ValueError("中心座標超出有效範圍。")
    next_lat = lat + north_m / 111_320.0
    if not -89.999 <= next_lat <= 89.999:
        raise ValueError("掃描範圍太接近極區。")
    longitude_scale = 111_320.0 * math.cos(math.radians(lat))
    if abs(longitude_scale) < 1e-6:
        raise ValueError("中心座標無法換算東西向距離。")
    next_lng = ((lng + east_m / longitude_scale + 180.0) % 360.0) - 180.0
    return next_lat, next_lng


def build_geo_scan_points(
    center_lat: float,
    center_lng: float,
    horizontal_radius: int,
    vertical_radius: int,
    spacing_m: float,
    *,
    left_radius: int | None = None,
    right_radius: int | None = None,
    up_radius: int | None = None,
    down_radius: int | None = None,
) -> list[GeoScanPoint]:
    if spacing_m <= 0:
        raise ValueError("掃描點間距必須大於 0 公尺。")
    left = horizontal_radius if left_radius is None else left_radius
    right = horizontal_radius if right_radius is None else right_radius
    up = vertical_radius if up_radius is None else up_radius
    down = vertical_radius if down_radius is None else down_radius
    radii = (left, right, up, down)
    if any(value < 0 for value in radii):
        raise ValueError("左、右、上、下搜尋範圍都不能小於 0。")
    if any(value > MAX_GEO_SEQUENCE_RADIUS for value in radii):
        raise ValueError(
            f"左、右、上、下搜尋範圍都不能超過 {MAX_GEO_SEQUENCE_RADIUS} 格。"
        )
    coordinates = [(0, 0, 0)]
    maximum_ring = max(radii)
    for ring in range(1, maximum_ring + 1):
        perimeter: list[tuple[int, int]] = []
        perimeter.extend((ring, y) for y in range(-(ring - 1), ring + 1))
        perimeter.extend((x, ring) for x in range(ring - 1, -ring - 1, -1))
        perimeter.extend((-ring, y) for y in range(ring - 1, -ring - 1, -1))
        perimeter.extend((x, -ring) for x in range(-ring + 1, ring + 1))
        coordinates.extend(
            (x, y, ring)
            for x, y in perimeter
            if -left <= x <= right and -down <= y <= up
        )
    points: list[GeoScanPoint] = []
    for ordinal, (grid_x, grid_y, ring) in enumerate(coordinates):
        lat, lng = offset_coordinate(
            center_lat,
            center_lng,
            grid_x * spacing_m,
            grid_y * spacing_m,
        )
        points.append(
            GeoScanPoint(
                ordinal=ordinal,
                ring=ring,
                grid_x=grid_x,
                grid_y=grid_y,
                lat=lat,
                lng=lng,
            )
        )
    return points


def distance_m(a_lat: float, a_lng: float, b_lat: float, b_lng: float) -> float:
    radius = 6_371_000.0
    lat1 = math.radians(a_lat)
    lat2 = math.radians(b_lat)
    d_lat = lat2 - lat1
    d_lng = math.radians(b_lng - a_lng)
    value = math.sin(d_lat / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin(
        d_lng / 2
    ) ** 2
    return 2 * radius * math.asin(min(1.0, math.sqrt(value)))


def _current_coordinate(status: dict[str, object]) -> tuple[float, float] | None:
    current = status.get("current")
    if not isinstance(current, dict):
        return None
    try:
        lat = float(current["lat"])
        lng = float(current["lng"])
    except (KeyError, TypeError, ValueError):
        return None
    if not -90 <= lat <= 90 or not -180 <= lng <= 180:
        return None
    return lat, lng


def wait_for_arrival(
    controller: CommandController,
    point: GeoScanPoint,
    stop_event: threading.Event,
    *,
    timeout_seconds: float,
    tolerance_m: float,
    poll_seconds: float = 0.5,
) -> bool:
    deadline = time.monotonic() + max(1.0, timeout_seconds)
    while not stop_event.is_set() and time.monotonic() < deadline:
        status = controller.status()
        current = _current_coordinate(status)
        running = bool(status.get("running", False))
        if current is not None:
            remaining = distance_m(current[0], current[1], point.lat, point.lng)
            if remaining <= tolerance_m and not running:
                return True
        if stop_event.wait(max(0.1, poll_seconds)):
            return False
    if stop_event.is_set():
        return False
    raise TimeoutError(f"前往 {point.location_label} 超過 {timeout_seconds:.0f} 秒。")


def run_geographic_scan(
    controller: CommandController,
    *,
    points: Sequence[GeoScanPoint],
    capture_png: Callable[[], bytes],
    stop_event: threading.Event,
    on_capture: GeoCaptureCallback,
    movement_mode: str = "teleport",
    speed_kmh: float = 10.8,
    use_path: bool = False,
    settle_seconds: float = 4.0,
    navigation_timeout_seconds: float = 300.0,
    arrival_tolerance_m: float = 4.0,
    return_to_origin: bool = True,
    status: StatusCallback | None = None,
    progress: ProgressCallback | None = None,
) -> GeoScanSummary:
    if movement_mode not in {"teleport", "navigate"}:
        raise ValueError("地理掃描移動模式必須是 teleport 或 navigate。")
    if not points:
        raise ValueError("地理掃描至少需要一個座標點。")
    if settle_seconds < 0:
        raise ValueError("畫面等待時間不能小於 0。")

    initial_status = controller.status()
    origin = _current_coordinate(initial_status)
    visited = 0
    detection_count = 0
    stop_reason = "cancelled"

    def report(message: str) -> None:
        if status is not None:
            status(message)

    try:
        for point in points:
            if stop_event.is_set():
                break
            report(f"正在前往 {point.location_label}…")
            controller.command(
                movement_mode,
                {
                    "lat": point.lat,
                    "lng": point.lng,
                    "speedKmh": speed_kmh,
                    "usePath": use_path,
                },
            )
            if movement_mode == "navigate":
                if not wait_for_arrival(
                    controller,
                    point,
                    stop_event,
                    timeout_seconds=navigation_timeout_seconds,
                    tolerance_m=arrival_tolerance_m,
                ):
                    break
            if stop_event.wait(max(0.0, settle_seconds)):
                break
            report(f"正在辨識 {point.location_label}…")
            outcome = on_capture(point, capture_png()) or GeoCaptureOutcome()
            visited += 1
            detection_count += outcome.detection_count
            if progress is not None:
                progress(visited, len(points), point)
        else:
            stop_reason = "completed"
    finally:
        try:
            controller.command("stop", require_ok=False)
        except Exception:
            pass
        if return_to_origin and origin is not None:
            report("正在回到掃描起點…")
            try:
                controller.command(
                    "teleport",
                    {"lat": origin[0], "lng": origin[1], "speedKmh": speed_kmh},
                    require_ok=False,
                )
            except Exception as exc:
                report(f"回到掃描起點失敗：{exc}")

    return GeoScanSummary(
        visited=visited,
        detection_count=detection_count,
        completed=stop_reason == "completed",
        stop_reason=stop_reason,
    )
