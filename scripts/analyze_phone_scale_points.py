from __future__ import annotations

import json
import math
from pathlib import Path

import cv2
import numpy as np


ROOT = Path(__file__).resolve().parents[1]
CAPTURE_DIR = ROOT / "outputs" / "phone_scale_calibration"
CAPTURES = {
    "origin": CAPTURE_DIR / "point_locked_origin.png",
    "east": CAPTURE_DIR / "point_locked_east_100m.png",
    "west": CAPTURE_DIR / "point_locked_west_100m.png",
    "north": CAPTURE_DIR / "point_locked_north_100m.png",
    "south": CAPTURE_DIR / "point_locked_south_100m.png",
}


def load(path: Path) -> np.ndarray:
    frame = cv2.imread(str(path), cv2.IMREAD_COLOR)
    if frame is None:
        raise RuntimeError(f"無法讀取 {path}")
    return frame


def portrait_center(frame: np.ndarray, expected: tuple[float, float]) -> tuple[float, float, float]:
    height, width = frame.shape[:2]
    gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
    circles = cv2.HoughCircles(
        cv2.GaussianBlur(gray, (7, 7), 1.4),
        cv2.HOUGH_GRADIENT,
        dp=1.0,
        minDist=45,
        param1=110,
        param2=28,
        minRadius=30,
        maxRadius=58,
    )
    if circles is None:
        raise RuntimeError("找不到角色頭像圓環。")

    yy, xx = np.ogrid[:height, :width]
    candidates: list[tuple[float, float, float, float]] = []
    for x, y, radius in circles[0]:
        if not (90 <= x <= width - 90 and 250 <= y <= height - 350):
            continue
        distance = math.hypot(float(x) - expected[0], float(y) - expected[1])
        if distance > 430:
            continue
        radial = np.sqrt((xx - x) ** 2 + (yy - y) ** 2)
        ring = (radial >= radius * 0.78) & (radial <= radius * 1.08)
        inner = radial <= radius * 0.68
        pixels = frame
        white = np.all(pixels >= 205, axis=2)
        # The avatar marker has a thick white ring, plus a skin/hair portrait
        # whose center is much less green than the map surface.
        ring_white = float(white[ring].mean())
        blue, green, red = cv2.split(pixels)
        portrait_color = ((red > green * 0.78) & (red > blue * 0.92))[inner]
        color_ratio = float(portrait_color.mean())
        proximity = max(0.0, 1.0 - distance / 430.0)
        score = ring_white * 5.0 + color_ratio * 2.0 + proximity * 0.7
        candidates.append((score, float(x), float(y), float(radius)))
    if not candidates:
        raise RuntimeError("角色頭像候選全部位於量測區域外。")
    candidates.sort(reverse=True)
    score, x, y, _radius = candidates[0]
    return x, y, score


def main() -> None:
    frames = {name: load(path) for name, path in CAPTURES.items()}
    height, width = frames["origin"].shape[:2]
    expected = (width / 2 + 60.0, height / 2)
    centers: dict[str, tuple[float, float, float]] = {}
    for name in ("origin", "east", "west", "north", "south"):
        centers[name] = portrait_center(frames[name], expected)

    origin_x, origin_y, _score = centers["origin"]
    samples: list[dict[str, object]] = []
    for name in ("east", "west", "north", "south"):
        x, y, score = centers[name]
        pixels = math.hypot(x - origin_x, y - origin_y)
        samples.append(
            {
                "direction": name,
                "origin_center_px": [origin_x, origin_y],
                "moved_center_px": [x, y],
                "dx_px": round(x - origin_x, 3),
                "dy_px": round(y - origin_y, 3),
                "distance_px": round(pixels, 3),
                "injected_distance_m": 100.0,
                "meters_per_pixel": round(100.0 / pixels, 6),
                "detector_score": round(score, 4),
            }
        )
    meters_per_pixel = float(
        np.median([float(item["meters_per_pixel"]) for item in samples])
    )
    result = {
        "schema": "bloompilot.phone-point-scale.v1",
        "screen": {"width": width, "height": height},
        "method": "locked_map_player_marker_displacement",
        "centers": {
            name: {"x": value[0], "y": value[1], "score": round(value[2], 4)}
            for name, value in centers.items()
        },
        "samples": samples,
        "meters_per_pixel": round(meters_per_pixel, 6),
        "pixels_per_meter": round(1.0 / meters_per_pixel, 6),
        "viewport_width_m": round(meters_per_pixel * width, 1),
    }
    output = CAPTURE_DIR / "point_calibration.json"
    output.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
