from __future__ import annotations

import math
from typing import Sequence


EARTH_RADIUS_M = 6_378_137.0


def coordinate_from_offset(
    center_lat: float,
    center_lng: float,
    *,
    east_m: float,
    north_m: float,
) -> tuple[float, float]:
    """Move a WGS84 coordinate by a small local east/north offset."""
    if not -90 <= center_lat <= 90 or not -180 <= center_lng <= 180:
        raise ValueError("中心經緯度超出有效範圍。")
    latitude_radians = math.radians(center_lat)
    longitude_scale = math.cos(latitude_radians)
    if abs(longitude_scale) < 1e-8:
        raise ValueError("極區座標無法使用畫面比例尺換算。")
    lat = center_lat + math.degrees(north_m / EARTH_RADIUS_M)
    lng = center_lng + math.degrees(east_m / (EARTH_RADIUS_M * longitude_scale))
    return lat, lng


def estimate_detection_coordinate(
    *,
    center_lat: float,
    center_lng: float,
    viewport_width_m: float | None = None,
    screen_width: int,
    screen_height: int,
    grid_x: int,
    grid_y: int,
    xyxy: Sequence[float],
    longitude_degrees_per_pixel: float | None = None,
    latitude_degrees_per_pixel: float | None = None,
    calibration_latitude: float | None = None,
    calibration_screen_width: int | None = None,
    calibration_screen_height: int | None = None,
) -> dict[str, object]:
    """Estimate a detection coordinate from map scale, viewport cell and box center.

    The map must be north-up and recentered on ``center_lat``/``center_lng`` before
    the rectangular scan begins. One map pixel is treated as a constant physical
    distance across the viewport, which is intentionally an approximate model.
    """
    if screen_width < 100 or screen_height < 100:
        raise ValueError("手機畫面尺寸太小，無法換算座標。")
    if len(xyxy) != 4 or any(not math.isfinite(float(value)) for value in xyxy):
        raise ValueError("辨識框座標無效。")

    horizontal_step_px = abs(round(screen_width * 0.17) - round(screen_width * 0.83))
    vertical_step_px = abs(round(screen_height * 0.15) - round(screen_height * 0.81))
    box_center_x = (float(xyxy[0]) + float(xyxy[2])) / 2
    box_center_y = (float(xyxy[1]) + float(xyxy[3])) / 2
    viewport_center_x = screen_width / 2
    viewport_center_y = screen_height / 2

    east_pixels = (
        grid_x * horizontal_step_px
        + box_center_x
        - viewport_center_x
    )
    north_pixels = (
        grid_y * vertical_step_px
        + viewport_center_y
        - box_center_y
    )

    calibrated = (
        longitude_degrees_per_pixel is not None
        or latitude_degrees_per_pixel is not None
    )
    if calibrated:
        values = (longitude_degrees_per_pixel, latitude_degrees_per_pixel)
        if any(value is None for value in values) or any(
            not math.isfinite(float(value)) or float(value) <= 0
            for value in values
            if value is not None
        ):
            raise ValueError("手機經緯度／像素校正值無效。")
        reference_width = calibration_screen_width or screen_width
        reference_height = calibration_screen_height or screen_height
        if reference_width < 100 or reference_height < 100:
            raise ValueError("手機比例尺的參考畫面尺寸無效。")
        calibrated_latitude = (
            center_lat if calibration_latitude is None else float(calibration_latitude)
        )
        if not math.isfinite(calibrated_latitude) or not -89.9 <= calibrated_latitude <= 89.9:
            raise ValueError("手機比例尺的校正緯度無效。")
        center_cosine = math.cos(math.radians(center_lat))
        calibration_cosine = math.cos(math.radians(calibrated_latitude))
        if abs(center_cosine) < 1e-8 or abs(calibration_cosine) < 1e-8:
            raise ValueError("極區座標無法使用手機比例尺換算。")
        longitude_per_pixel = (
            float(longitude_degrees_per_pixel) * reference_width / screen_width
        )
        # Web-map vertical degrees per pixel shrink with cos(latitude). This
        # keeps a device calibration useful when a route moves to another city.
        latitude_per_pixel = (
            float(latitude_degrees_per_pixel)
            * reference_height
            / screen_height
            * center_cosine
            / calibration_cosine
        )
        longitude_delta = east_pixels * longitude_per_pixel
        latitude_delta = north_pixels * latitude_per_pixel
        lat = center_lat + latitude_delta
        lng = ((center_lng + longitude_delta + 180.0) % 360.0) - 180.0
        if not -90 <= lat <= 90:
            raise ValueError("推估緯度超出有效範圍。")
        north_m = math.radians(latitude_delta) * EARTH_RADIUS_M
        east_m = math.radians(longitude_delta) * EARTH_RADIUS_M * center_cosine
        meters_per_pixel_x = (
            math.radians(longitude_per_pixel) * EARTH_RADIUS_M * center_cosine
        )
        meters_per_pixel_y = math.radians(latitude_per_pixel) * EARTH_RADIUS_M
        meters_per_pixel = (abs(meters_per_pixel_x) + abs(meters_per_pixel_y)) / 2
        viewport_width = abs(meters_per_pixel_x) * screen_width
        method = "device_pixel_geographic_scale"
    else:
        if (
            viewport_width_m is None
            or not math.isfinite(float(viewport_width_m))
            or not 10 <= float(viewport_width_m) <= 100_000
        ):
            raise ValueError("手機地圖畫面寬度必須介於 10 與 100000 公尺。")
        meters_per_pixel = float(viewport_width_m) / screen_width
        meters_per_pixel_x = meters_per_pixel
        meters_per_pixel_y = meters_per_pixel
        east_m = east_pixels * meters_per_pixel
        north_m = north_pixels * meters_per_pixel
        lat, lng = coordinate_from_offset(
            center_lat,
            center_lng,
            east_m=east_m,
            north_m=north_m,
        )
        longitude_per_pixel = None
        latitude_per_pixel = None
        viewport_width = float(viewport_width_m)
        method = "viewport_scale"
    distance_m = math.hypot(east_m, north_m)
    accuracy_m = max(8.0 if calibrated else 15.0, viewport_width * 0.035, distance_m * 0.08)
    result: dict[str, object] = {
        "lat": round(lat, 7),
        "lng": round(lng, 7),
        "east_m": round(east_m, 1),
        "north_m": round(north_m, 1),
        "accuracy_m": round(accuracy_m, 1),
        "meters_per_pixel": round(meters_per_pixel, 4),
        "meters_per_pixel_x": round(abs(meters_per_pixel_x), 4),
        "meters_per_pixel_y": round(abs(meters_per_pixel_y), 4),
        "method": method,
    }
    if calibrated:
        result["longitude_degrees_per_pixel"] = round(float(longitude_per_pixel), 12)
        result["latitude_degrees_per_pixel"] = round(float(latitude_per_pixel), 12)
    return result
