"""Per-device Pikmin map scale calibration and marker-motion measurement."""

from __future__ import annotations

import json
import math
import os
import threading
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import cv2
import numpy as np

from .paths import DEVICE_SCALE_CALIBRATIONS_PATH


@dataclass(frozen=True)
class MarkerDisplacement:
    dx: float
    dy: float
    source_x: float
    source_y: float
    target_x: float
    target_y: float
    tracked_points: int


@dataclass(frozen=True)
class DeviceScaleCalibration:
    serial: str
    calibrated_at: str
    screen_width: int
    screen_height: int
    origin_lat: float
    origin_lng: float
    east_probe_m: float
    north_probe_m: float
    east_pixels: float
    north_pixels: float
    longitude_degrees_per_pixel: float
    latitude_degrees_per_pixel: float
    meters_per_pixel_x: float
    meters_per_pixel_y: float

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    def estimation_kwargs(self) -> dict[str, float | int]:
        return {
            "longitude_degrees_per_pixel": self.longitude_degrees_per_pixel,
            "latitude_degrees_per_pixel": self.latitude_degrees_per_pixel,
            "calibration_latitude": self.origin_lat,
            "calibration_screen_width": self.screen_width,
            "calibration_screen_height": self.screen_height,
        }

    @classmethod
    def from_dict(cls, value: object) -> DeviceScaleCalibration | None:
        if not isinstance(value, dict):
            return None
        try:
            item = cls(
                serial=str(value["serial"]).strip(),
                calibrated_at=str(value["calibrated_at"]),
                screen_width=int(value["screen_width"]),
                screen_height=int(value["screen_height"]),
                origin_lat=float(value["origin_lat"]),
                origin_lng=float(value["origin_lng"]),
                east_probe_m=float(value["east_probe_m"]),
                north_probe_m=float(value["north_probe_m"]),
                east_pixels=float(value["east_pixels"]),
                north_pixels=float(value["north_pixels"]),
                longitude_degrees_per_pixel=float(value["longitude_degrees_per_pixel"]),
                latitude_degrees_per_pixel=float(value["latitude_degrees_per_pixel"]),
                meters_per_pixel_x=float(value["meters_per_pixel_x"]),
                meters_per_pixel_y=float(value["meters_per_pixel_y"]),
            )
        except (KeyError, TypeError, ValueError, OverflowError):
            return None
        numeric = (
            item.origin_lat,
            item.origin_lng,
            item.east_probe_m,
            item.north_probe_m,
            item.east_pixels,
            item.north_pixels,
            item.longitude_degrees_per_pixel,
            item.latitude_degrees_per_pixel,
            item.meters_per_pixel_x,
            item.meters_per_pixel_y,
        )
        if (
            not item.serial
            or item.screen_width < 100
            or item.screen_height < 100
            or not all(math.isfinite(number) for number in numeric)
            or not -90 <= item.origin_lat <= 90
            or not -180 <= item.origin_lng <= 180
            or min(numeric[2:]) <= 0
        ):
            return None
        return item


class DeviceScaleStore:
    """Small atomic JSON store keyed by Android serial."""

    def __init__(self, path: Path = DEVICE_SCALE_CALIBRATIONS_PATH) -> None:
        self.path = Path(path)
        self._lock = threading.RLock()

    def _load_unlocked(self) -> dict[str, DeviceScaleCalibration]:
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except (FileNotFoundError, OSError, json.JSONDecodeError):
            return {}
        raw_items = payload.get("devices") if isinstance(payload, dict) else None
        if not isinstance(raw_items, dict):
            return {}
        result: dict[str, DeviceScaleCalibration] = {}
        for serial, raw in raw_items.items():
            item = DeviceScaleCalibration.from_dict(raw)
            if item is not None and item.serial == str(serial):
                result[item.serial] = item
        return result

    def get(self, serial: str) -> DeviceScaleCalibration | None:
        key = serial.strip()
        if not key:
            return None
        with self._lock:
            return self._load_unlocked().get(key)

    def save(self, calibration: DeviceScaleCalibration) -> DeviceScaleCalibration:
        with self._lock:
            items = self._load_unlocked()
            items[calibration.serial] = calibration
            payload = {
                "version": 1,
                "devices": {key: item.to_dict() for key, item in sorted(items.items())},
            }
            self.path.parent.mkdir(parents=True, exist_ok=True)
            temporary = self.path.with_name(f".{self.path.name}.{os.getpid()}.part")
            try:
                temporary.write_text(
                    json.dumps(payload, ensure_ascii=False, indent=2),
                    encoding="utf-8",
                )
                temporary.replace(self.path)
            finally:
                temporary.unlink(missing_ok=True)
        return calibration


def new_device_scale_calibration(
    *,
    serial: str,
    screen_width: int,
    screen_height: int,
    origin_lat: float,
    origin_lng: float,
    east_probe_m: float,
    north_probe_m: float,
    east_coordinate_delta: float,
    north_coordinate_delta: float,
    east_pixels: float,
    north_pixels: float,
) -> DeviceScaleCalibration:
    if abs(east_pixels) < 1 or abs(north_pixels) < 1:
        raise ValueError("自身位置移動像素不足，無法建立比例尺。")
    return DeviceScaleCalibration(
        serial=serial.strip(),
        calibrated_at=datetime.now(timezone.utc).isoformat(),
        screen_width=int(screen_width),
        screen_height=int(screen_height),
        origin_lat=float(origin_lat),
        origin_lng=float(origin_lng),
        east_probe_m=abs(float(east_probe_m)),
        north_probe_m=abs(float(north_probe_m)),
        east_pixels=abs(float(east_pixels)),
        north_pixels=abs(float(north_pixels)),
        longitude_degrees_per_pixel=abs(float(east_coordinate_delta) / float(east_pixels)),
        latitude_degrees_per_pixel=abs(float(north_coordinate_delta) / float(north_pixels)),
        meters_per_pixel_x=abs(float(east_probe_m) / float(east_pixels)),
        meters_per_pixel_y=abs(float(north_probe_m) / float(north_pixels)),
    )


def _decode_frame(payload: bytes) -> np.ndarray:
    image = cv2.imdecode(np.frombuffer(payload or b"", dtype=np.uint8), cv2.IMREAD_COLOR)
    if image is None:
        raise ValueError("無法解碼比例尺校正畫面。")
    return image


def _player_marker_candidates(image: np.ndarray) -> list[tuple[float, float, float, float]]:
    """Find white-ringed player-avatar circles away from fixed map controls.

    The avatar artwork animates, which makes point optical flow unreliable on
    real Pikmin Bloom frames.  Its thick white circular border is stable across
    animations and players, so use that border as the primary position signal.
    Each tuple is ``(x, y, radius, white_ring_score)``.
    """
    height, width = image.shape[:2]
    left = round(width * 0.10)
    right = round(width * 0.90)
    top = round(height * 0.10)
    bottom = round(height * 0.84)
    roi = image[top:bottom, left:right]
    if roi.size == 0:
        return []
    gray = cv2.cvtColor(roi, cv2.COLOR_BGR2GRAY)
    gray = cv2.GaussianBlur(gray, (5, 5), 1.2)
    minimum_radius = max(12, round(width * 0.018))
    maximum_radius = max(minimum_radius + 6, round(width * 0.055))
    circles = cv2.HoughCircles(
        gray,
        cv2.HOUGH_GRADIENT,
        dp=1.2,
        minDist=max(24, round(width * 0.035)),
        param1=100,
        param2=30,
        minRadius=minimum_radius,
        maxRadius=maximum_radius,
    )
    if circles is None:
        return []
    hsv = cv2.cvtColor(image, cv2.COLOR_BGR2HSV)
    candidates: list[tuple[float, float, float, float]] = []
    for local_x, local_y, radius in circles[0]:
        x = float(local_x + left)
        y = float(local_y + top)
        radius = float(radius)
        extent = radius * 1.18
        x0 = max(0, math.floor(x - extent))
        x1 = min(width, math.ceil(x + extent + 1))
        y0 = max(0, math.floor(y - extent))
        y1 = min(height, math.ceil(y + extent + 1))
        sample = hsv[y0:y1, x0:x1]
        yy, xx = np.ogrid[y0:y1, x0:x1]
        distance = np.sqrt((xx - x) ** 2 + (yy - y) ** 2)
        annulus = (distance >= radius * 0.78) & (distance <= radius * 1.15)
        if not np.any(annulus):
            continue
        white = (sample[:, :, 1] < 70) & (sample[:, :, 2] > 185)
        score = float(np.mean(white[annulus]))
        # Real player-avatar borders consistently score around 0.5 on device
        # frames. Mushroom pins and road loops may also trigger Hough circles,
        # but their partial white arcs stay below 0.3. Keeping a clear margin
        # prevents those animated map objects from looking like early motion
        # while the game is still applying the injected GPS update.
        if score >= 0.40:
            candidates.append((x, y, radius, score))
    candidates.sort(key=lambda item: item[3], reverse=True)
    return candidates[:8]


def _measure_avatar_circle_displacement(
    reference: np.ndarray,
    moved: np.ndarray,
    *,
    axis: str,
    expected_sign: int,
) -> MarkerDisplacement | None:
    source_candidates = _player_marker_candidates(reference)
    target_candidates = _player_marker_candidates(moved)
    if not source_candidates or not target_candidates:
        return None
    height, width = reference.shape[:2]
    axis_size = width if axis == "x" else height
    minimum = max(5.0, axis_size * 0.006)
    maximum = axis_size * 0.42
    ranked: list[tuple[float, tuple[float, float, float, float], tuple[float, float, float, float]]] = []
    for source in source_candidates:
        for target in target_candidates:
            dx = target[0] - source[0]
            dy = target[1] - source[1]
            primary = dx if axis == "x" else dy
            secondary = dy if axis == "x" else dx
            if (
                primary * expected_sign < minimum
                or abs(primary) > maximum
                or abs(secondary) > max(5.0, abs(primary) * 0.35)
                or abs(source[2] - target[2]) > max(6.0, source[2] * 0.30)
            ):
                continue
            # Prefer two strong white rings of similar size and nearby motion.
            rank = (
                source[3]
                + target[3]
                - abs(source[2] - target[2]) / max(source[2], target[2], 1.0) * 0.20
                - abs(primary) / axis_size * 0.12
            )
            ranked.append((rank, source, target))
    if not ranked:
        return None
    _rank, source, target = max(ranked, key=lambda item: item[0])
    return MarkerDisplacement(
        dx=target[0] - source[0],
        dy=target[1] - source[1],
        source_x=source[0],
        source_y=source[1],
        target_x=target[0],
        target_y=target[1],
        tracked_points=2,
    )


def measure_player_marker_displacement(
    reference_payload: bytes,
    moved_payload: bytes,
    *,
    axis: str,
    expected_sign: int,
) -> MarkerDisplacement:
    """Track the GPS-responsive marker while rejecting static map features.

    The caller verifies an inverse track after restoring the original GPS
    coordinate. That round trip makes the selected motion cluster specific to
    the player marker rather than ordinary map animation.
    """
    if axis not in {"x", "y"} or expected_sign not in {-1, 1}:
        raise ValueError("比例尺位移方向無效。")
    reference = _decode_frame(reference_payload)
    moved = _decode_frame(moved_payload)
    if reference.shape != moved.shape:
        raise ValueError("比例尺校正前後的手機畫面尺寸不同。")
    height, width = reference.shape[:2]
    avatar_displacement = _measure_avatar_circle_displacement(
        reference,
        moved,
        axis=axis,
        expected_sign=expected_sign,
    )
    if avatar_displacement is not None:
        return avatar_displacement

    reference_gray = cv2.cvtColor(reference, cv2.COLOR_BGR2GRAY)
    moved_gray = cv2.cvtColor(moved, cv2.COLOR_BGR2GRAY)
    mask = np.zeros((height, width), dtype=np.uint8)
    cv2.rectangle(
        mask,
        (round(width * 0.025), round(height * 0.065)),
        (round(width * 0.975), round(height * 0.94)),
        255,
        thickness=-1,
    )
    # Exclude the bottom-right locator itself; its animation must never become
    # the selected motion cluster.
    cv2.rectangle(
        mask,
        (round(width * 0.82), round(height * 0.82)),
        (width - 1, height - 1),
        0,
        thickness=-1,
    )
    points = cv2.goodFeaturesToTrack(
        reference_gray,
        maxCorners=1400,
        qualityLevel=0.006,
        minDistance=3,
        mask=mask,
        blockSize=5,
    )
    if points is None or len(points) < 3:
        raise ValueError("找不到可追蹤的自身位置特徵。")
    next_points, forward_status, _error = cv2.calcOpticalFlowPyrLK(
        reference_gray,
        moved_gray,
        points,
        None,
        winSize=(31, 31),
        maxLevel=4,
        criteria=(cv2.TERM_CRITERIA_EPS | cv2.TERM_CRITERIA_COUNT, 40, 0.01),
    )
    if next_points is None or forward_status is None:
        raise ValueError("無法追蹤自身位置的畫面位移。")
    back_points, back_status, _back_error = cv2.calcOpticalFlowPyrLK(
        moved_gray,
        reference_gray,
        next_points,
        None,
        winSize=(31, 31),
        maxLevel=4,
        criteria=(cv2.TERM_CRITERIA_EPS | cv2.TERM_CRITERIA_COUNT, 40, 0.01),
    )
    if back_points is None or back_status is None:
        raise ValueError("無法驗證自身位置的畫面位移。")

    original = points.reshape(-1, 2)
    target = next_points.reshape(-1, 2)
    returned = back_points.reshape(-1, 2)
    valid = (forward_status.reshape(-1) == 1) & (back_status.reshape(-1) == 1)
    valid &= np.linalg.norm(returned - original, axis=1) <= 2.0
    flow = target - original
    primary = flow[:, 0] if axis == "x" else flow[:, 1]
    secondary = flow[:, 1] if axis == "x" else flow[:, 0]
    minimum = max(5.0, (width if axis == "x" else height) * 0.006)
    maximum = (width if axis == "x" else height) * 0.42
    valid &= primary * expected_sign >= minimum
    valid &= np.abs(primary) <= maximum
    valid &= np.abs(secondary) <= np.maximum(5.0, np.abs(primary) * 0.35)
    indices = np.flatnonzero(valid)
    if indices.size < 2:
        raise ValueError("未偵測到符合注入方向的自身位置移動；請讓自身位置保持在畫面內。")

    # Optical-flow vectors belonging to the marker share nearly the same
    # displacement. Select the densest neighbouring three-pixel bins.
    bins: dict[tuple[int, int], list[int]] = {}
    for index in indices.tolist():
        key = (round(float(flow[index, 0]) / 3), round(float(flow[index, 1]) / 3))
        bins.setdefault(key, []).append(index)
    best: list[int] = []
    for key in bins:
        members: list[int] = []
        for x_offset in (-1, 0, 1):
            for y_offset in (-1, 0, 1):
                members.extend(bins.get((key[0] + x_offset, key[1] + y_offset), ()))
        if len(members) > len(best):
            best = members
    if len(best) < 2:
        raise ValueError("自身位置的移動特徵不足，無法可靠推算比例尺。")
    selected_flow = flow[best]
    selected_original = original[best]
    dx = float(np.median(selected_flow[:, 0]))
    dy = float(np.median(selected_flow[:, 1]))
    source_x = float(np.median(selected_original[:, 0]))
    source_y = float(np.median(selected_original[:, 1]))
    return MarkerDisplacement(
        dx=dx,
        dy=dy,
        source_x=source_x,
        source_y=source_y,
        target_x=source_x + dx,
        target_y=source_y + dy,
        tracked_points=len(best),
    )


def validate_round_trip(
    forward: MarkerDisplacement,
    backward: MarkerDisplacement,
    *,
    axis: str,
) -> None:
    forward_primary = forward.dx if axis == "x" else forward.dy
    backward_primary = backward.dx if axis == "x" else backward.dy
    tolerance = max(3.0, abs(forward_primary) * 0.25)
    if (
        forward_primary * backward_primary >= 0
        or abs(abs(forward_primary) - abs(backward_primary)) > tolerance
    ):
        raise ValueError("自身位置往返的像素位移不一致，未保存本次比例尺。")
