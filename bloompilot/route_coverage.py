from __future__ import annotations

import math

from .project_defaults import VIEWPORT_WIDTH_M


# The global routes were measured on the currently connected MI 9. Android input
# uses this logical coordinate space and the map is prepared at its maximum zoom.
REFERENCE_SCREEN_WIDTH_PX = 1080
REFERENCE_SCREEN_HEIGHT_PX = 2340


def _swipe_pixels(axis: str) -> int:
    if axis == "horizontal":
        return abs(
            round(REFERENCE_SCREEN_WIDTH_PX * 0.17)
            - round(REFERENCE_SCREEN_WIDTH_PX * 0.83)
        )
    if axis == "vertical":
        return abs(
            round(REFERENCE_SCREEN_HEIGHT_PX * 0.15)
            - round(REFERENCE_SCREEN_HEIGHT_PX * 0.81)
        )
    raise ValueError(f"不支援的範圍軸向：{axis}")


def viewport_half_km(axis: str, step_km: float | None = None) -> float:
    meters_per_pixel = VIEWPORT_WIDTH_M / REFERENCE_SCREEN_WIDTH_PX
    pixels = (
        REFERENCE_SCREEN_WIDTH_PX
        if axis == "horizontal"
        else REFERENCE_SCREEN_HEIGHT_PX
        if axis == "vertical"
        else 0
    )
    if not pixels:
        raise ValueError(f"不支援的範圍軸向：{axis}")
    half_km = meters_per_pixel * pixels / 2_000.0
    if step_km is not None:
        # A calibrated step covers the same reference swipe percentage.
        half_km *= grid_step_km(axis, step_km) / grid_step_km(axis)
    return half_km


def grid_step_km(axis: str, step_km: float | None = None) -> float:
    meters_per_pixel = VIEWPORT_WIDTH_M / REFERENCE_SCREEN_WIDTH_PX
    default_step_km = _swipe_pixels(axis) * meters_per_pixel / 1_000.0
    if step_km is None:
        return default_step_km
    value = float(step_km)
    if not math.isfinite(value) or value <= 0:
        raise ValueError("搜尋格距必須是大於 0 的有限 km 數值。")
    return value


def coverage_km_to_radius(
    distance_km: float, axis: str, step_km: float | None = None
) -> int:
    """Convert a centre-to-boundary distance to map swipes, always rounding up.

    The initial centred viewport already covers half a screen. Every additional
    cell moves the map by one Android swipe. ``ceil`` guarantees that the outer
    settlement bound is still visible in the last cell. A supplied step scales
    the viewport by the same ratio as the swipe distance.
    """

    value = float(distance_km)
    if not math.isfinite(value) or value < 0:
        raise ValueError("城市四向範圍必須是大於或等於 0 的 km 數值。")
    uncovered_km = max(0.0, value - viewport_half_km(axis, step_km))
    return max(0, math.ceil(uncovered_km / grid_step_km(axis, step_km)))


def radius_to_coverage_km(
    radius: int, axis: str, step_km: float | None = None
) -> float:
    value = int(radius)
    if value < 0:
        raise ValueError("搜尋格數不能小於 0。")
    return viewport_half_km(axis, step_km) + value * grid_step_km(axis, step_km)


def coverage_radii_from_km(
    left_km: float,
    right_km: float,
    up_km: float,
    down_km: float,
    horizontal_step_km: float | None = None,
    vertical_step_km: float | None = None,
) -> tuple[int, int, int, int]:
    return (
        coverage_km_to_radius(left_km, "horizontal", horizontal_step_km),
        coverage_km_to_radius(right_km, "horizontal", horizontal_step_km),
        coverage_km_to_radius(up_km, "vertical", vertical_step_km),
        coverage_km_to_radius(down_km, "vertical", vertical_step_km),
    )
