"""Read the participant number shown in Pikmin Bloom target badges.

The game renders this value as white digits to the right of a white person icon
inside a dark green pill.  Locating that fixed UI is both faster and more
reliable than running general-purpose OCR over a map screenshot.
"""

from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
import math
import os
from pathlib import Path
from typing import MutableMapping, Sequence

import cv2
import numpy as np
from PIL import Image, ImageDraw, ImageFont


@dataclass(frozen=True)
class BadgeNumber:
    """A recognized participant badge value in full-screen coordinates."""

    value: int
    text: str
    confidence: float
    bounds: tuple[int, int, int, int]


@dataclass(frozen=True)
class _WhiteComponent:
    label: int
    x: int
    y: int
    width: int
    height: int
    area: int
    background_h: float
    background_s: float
    background_v: float

    @property
    def right(self) -> int:
        return self.x + self.width

    @property
    def bottom(self) -> int:
        return self.y + self.height

    @property
    def center_y(self) -> float:
        return self.y + self.height / 2


_FONT_FILENAMES = (
    "arial.ttf",
    "arialbd.ttf",
    "ARIALNB.TTF",
    "bahnschrift.ttf",
    "segoeui.ttf",
    "segoeuib.ttf",
    "tahoma.ttf",
    "trebucbd.ttf",
    "verdanab.ttf",
)


def _normalize_glyph(mask: np.ndarray) -> np.ndarray:
    output = np.zeros((40, 28), dtype=np.uint8)
    ys, xs = np.where(mask > 0)
    if not len(xs):
        return output
    glyph = mask[ys.min() : ys.max() + 1, xs.min() : xs.max() + 1]
    scale = min(32 / glyph.shape[0], 20 / glyph.shape[1])
    glyph = cv2.resize(
        glyph,
        (
            max(1, round(glyph.shape[1] * scale)),
            max(1, round(glyph.shape[0] * scale)),
        ),
        interpolation=cv2.INTER_AREA,
    )
    y = (output.shape[0] - glyph.shape[0]) // 2
    x = (output.shape[1] - glyph.shape[1]) // 2
    output[y : y + glyph.shape[0], x : x + glyph.shape[1]] = glyph
    return output


def _glyph_feature(mask: np.ndarray) -> np.ndarray:
    normalized = _normalize_glyph(mask)
    small = cv2.resize(normalized, (14, 20), interpolation=cv2.INTER_AREA).astype(
        np.float32
    ) / 255.0
    return np.concatenate(
        (
            small.ravel(),
            small.sum(axis=0) / small.shape[0],
            small.sum(axis=1) / small.shape[1],
        )
    )


def _font_paths() -> list[Path]:
    roots: list[Path] = []
    windows = os.environ.get("WINDIR")
    if windows:
        roots.append(Path(windows) / "Fonts")
    roots.extend(
        (
            Path("C:/Windows/Fonts"),
            Path("/usr/share/fonts/truetype/dejavu"),
            Path("/usr/share/fonts/truetype/liberation2"),
        )
    )
    paths: list[Path] = []
    seen: set[str] = set()
    for root in roots:
        for name in _FONT_FILENAMES:
            path = root / name
            key = str(path).casefold()
            if key not in seen and path.is_file():
                seen.add(key)
                paths.append(path)
    return paths


def _pil_digit_mask(font: ImageFont.FreeTypeFont, digit: int, stroke: int) -> np.ndarray:
    canvas = Image.new("L", (88, 88), 0)
    draw = ImageDraw.Draw(canvas)
    draw.text(
        (12, -2),
        str(digit),
        font=font,
        fill=255,
        stroke_width=stroke,
        stroke_fill=255,
    )
    return np.asarray(canvas)


def _opencv_digit_mask(font: int, digit: int, thickness: int) -> np.ndarray:
    canvas = np.zeros((88, 88), dtype=np.uint8)
    cv2.putText(
        canvas,
        str(digit),
        (10, 65),
        font,
        1.8,
        255,
        thickness,
        cv2.LINE_AA,
    )
    return canvas


@lru_cache(maxsize=1)
def _template_bank() -> tuple[np.ndarray, np.ndarray]:
    """Build a small, cached digit bank; roughly 3 MiB on a normal Windows host."""
    features: list[np.ndarray] = []
    labels: list[int] = []
    for path in _font_paths():
        for size in (30, 36, 42, 48):
            try:
                font = ImageFont.truetype(str(path), size)
            except (OSError, ValueError):
                continue
            for digit in range(10):
                for stroke in (0, 1):
                    source = _pil_digit_mask(font, digit, stroke)
                    ys, xs = np.where(source > 0)
                    if not len(xs):
                        continue
                    glyph = source[ys.min() : ys.max() + 1, xs.min() : xs.max() + 1]
                    for horizontal_scale in (0.84, 1.0, 1.16):
                        variant = cv2.resize(
                            glyph,
                            (
                                max(1, round(glyph.shape[1] * horizontal_scale)),
                                glyph.shape[0],
                            ),
                            interpolation=cv2.INTER_AREA,
                        )
                        features.append(_glyph_feature(variant))
                        labels.append(digit)

    # These OpenCV faces are also the dependency-free fallback used by tests and
    # non-Windows development machines.
    for face in (
        cv2.FONT_HERSHEY_SIMPLEX,
        cv2.FONT_HERSHEY_DUPLEX,
        cv2.FONT_HERSHEY_COMPLEX,
    ):
        for digit in range(10):
            for thickness in (2, 3, 4):
                features.append(_glyph_feature(_opencv_digit_mask(face, digit, thickness)))
                labels.append(digit)

    return np.asarray(features, dtype=np.float32), np.asarray(labels, dtype=np.int16)


def _classify_glyph(mask: np.ndarray) -> tuple[str, float] | None:
    features, labels = _template_bank()
    if not len(labels):
        return None
    feature = _glyph_feature(mask)
    distances = np.mean(np.square(features - feature), axis=1)
    nearest = np.argsort(distances)[: min(15, len(distances))]
    scores = np.zeros(10, dtype=np.float64)
    for index in nearest:
        scores[int(labels[index])] += 1.0 / (float(distances[index]) + 1e-6)
    winner = int(np.argmax(scores))
    ordered_scores = np.sort(scores)[::-1]
    vote_purity = float(ordered_scores[0] / max(1e-6, ordered_scores[:2].sum()))
    winner_distance = float(np.min(distances[labels == winner]))
    shape_fit = float(np.clip((0.17 - winner_distance) / 0.14, 0.0, 1.0))
    confidence = 0.58 * vote_purity + 0.42 * shape_fit
    if confidence < 0.54:
        return None
    return str(winner), confidence


def _bounded_box(
    xyxy: Sequence[object], width: int, height: int
) -> tuple[int, int, int, int] | None:
    if len(xyxy) != 4:
        return None
    try:
        values = [float(value) for value in xyxy]
    except (TypeError, ValueError, OverflowError):
        return None
    if any(not math.isfinite(value) for value in values):
        return None
    x1 = max(0, min(width, math.floor(values[0])))
    y1 = max(0, min(height, math.floor(values[1])))
    x2 = max(0, min(width, math.ceil(values[2])))
    y2 = max(0, min(height, math.ceil(values[3])))
    if x2 <= x1 or y2 <= y1:
        return None
    return x1, y1, x2, y2


def _white_components(crop: np.ndarray) -> tuple[np.ndarray, list[_WhiteComponent]]:
    hsv = cv2.cvtColor(crop, cv2.COLOR_BGR2HSV)
    mask = cv2.inRange(hsv, (0, 0, 195), (179, 82, 255))
    count, labels, stats, _ = cv2.connectedComponentsWithStats(mask, 8)
    crop_height, crop_width = crop.shape[:2]
    minimum_height = max(7, round(min(crop_height, crop_width) * 0.035))
    maximum_height = max(18, min(60, round(min(crop_height, crop_width) * 0.30)))
    components: list[_WhiteComponent] = []
    for label in range(1, count):
        x, y, width, height, area = (int(value) for value in stats[label])
        if not (
            minimum_height <= height <= maximum_height
            and 2 <= width <= math.ceil(height * 1.5)
            and area >= max(12, round(height * height * 0.07))
            and y < crop_height * 0.68
        ):
            continue
        padding = max(4, round(height * 0.25))
        left = max(0, x - padding)
        top = max(0, y - padding)
        right = min(crop_width, x + width + padding)
        bottom = min(crop_height, y + height + padding)
        region = hsv[top:bottom, left:right]
        foreground = labels[top:bottom, left:right] == label
        background = ~foreground
        if not background.any():
            continue
        components.append(
            _WhiteComponent(
                label=label,
                x=x,
                y=y,
                width=width,
                height=height,
                area=area,
                background_h=float(np.median(region[:, :, 0][background])),
                background_s=float(np.median(region[:, :, 1][background])),
                background_v=float(np.median(region[:, :, 2][background])),
            )
        )
    return labels, components


def _on_badge(component: _WhiteComponent) -> bool:
    return (
        35 <= component.background_h <= 105
        and 85 <= component.background_s <= 195
        and 75 <= component.background_v <= 165
    )


def _digit_group(components: Sequence[_WhiteComponent]) -> list[_WhiteComponent]:
    badge = sorted((component for component in components if _on_badge(component)), key=lambda c: c.x)
    best: list[_WhiteComponent] = []
    best_score = -1.0
    for icon in badge:
        aspect = icon.width / icon.height
        density = icon.area / (icon.width * icon.height)
        complete_icon = 0.72 <= aspect <= 1.35 and 0.32 <= density <= 0.82
        clipped_icon = icon.x <= 1 and 0.30 <= aspect < 0.72
        if not (complete_icon or clipped_icon):
            continue
        possible = [
            component
            for component in badge
            if component.x >= icon.right - 1
            and component.x - icon.right <= icon.height * 3.4
            and abs(component.center_y - icon.center_y) <= icon.height * 0.42
            and icon.height * 0.52 <= component.height <= icon.height * 1.35
            and component.width / component.height <= 1.05
        ]
        if not possible:
            continue
        possible.sort(key=lambda component: component.x)
        digits = [possible[0]]
        for component in possible[1:]:
            if component.x - digits[-1].right > icon.height * 0.75:
                break
            digits.append(component)
        score = (2.0 if complete_icon else 1.0) + sum(
            component.area / max(1, component.width * component.height)
            for component in digits
        )
        if score > best_score:
            best = digits
            best_score = score
    return best


def recognize_badge_number(
    source: np.ndarray,
    xyxy: Sequence[object],
) -> BadgeNumber | None:
    """Recognize the person-count badge inside one detector bounding box."""
    if source.ndim != 3 or source.shape[2] < 3:
        return None
    bounded = _bounded_box(xyxy, source.shape[1], source.shape[0])
    if bounded is None:
        return None
    x1, y1, x2, y2 = bounded
    crop = source[y1:y2, x1:x2]
    labels, components = _white_components(crop)
    digits = _digit_group(components)
    if not digits:
        return None

    text_parts: list[str] = []
    confidences: list[float] = []
    for component in digits:
        mask = (labels[
            component.y : component.bottom,
            component.x : component.right,
        ] == component.label).astype(np.uint8) * 255
        classified = _classify_glyph(mask)
        if classified is None:
            return None
        digit, confidence = classified
        text_parts.append(digit)
        confidences.append(confidence)
    text = "".join(text_parts)
    if not text:
        return None
    left = min(component.x for component in digits) + x1
    top = min(component.y for component in digits) + y1
    right = max(component.right for component in digits) + x1
    bottom = max(component.bottom for component in digits) + y1
    return BadgeNumber(
        value=int(text),
        text=text,
        confidence=round(float(min(confidences)), 4),
        bounds=(left, top, right, bottom),
    )


def enrich_detections_with_badge_numbers(
    source: np.ndarray,
    detections: Sequence[MutableMapping[str, object]],
) -> Sequence[MutableMapping[str, object]]:
    """Attach a count to every detection; an absent badge number means zero."""
    for detection in detections:
        xyxy = detection.get("xyxy")
        if not isinstance(xyxy, Sequence) or isinstance(xyxy, (str, bytes)):
            continue
        result = recognize_badge_number(source, xyxy)
        if result is None:
            detection["participant_count"] = 0
            detection["participant_count_confidence"] = 1.0
            detection["participant_count_method"] = "number_absent"
            detection.pop("participant_count_bounds", None)
            continue
        detection["participant_count"] = result.value
        detection["participant_count_confidence"] = result.confidence
        detection["participant_count_method"] = "badge_digit"
        detection["participant_count_bounds"] = list(result.bounds)
    return detections


def normalize_participant_count_filter(value: object) -> int | None:
    """Normalize an optional exact participant-count filter from an API payload."""
    if value is None or value == "":
        return None
    if isinstance(value, bool):
        raise ValueError("人數篩選必須是 0 以上的整數。")
    try:
        count = int(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError("人數篩選必須是 0 以上的整數。") from exc
    if isinstance(value, float) and not value.is_integer():
        raise ValueError("人數篩選必須是 0 以上的整數。")
    if isinstance(value, str) and value.strip() not in {str(count), f"+{count}"}:
        raise ValueError("人數篩選必須是 0 以上的整數。")
    if count < 0:
        raise ValueError("人數篩選必須是 0 以上的整數。")
    return count


def filter_detections_by_participant_count(
    detections: Sequence[MutableMapping[str, object]],
    participant_count: object,
) -> list[MutableMapping[str, object]]:
    """Return only detections whose recognized count exactly matches the filter."""
    required = normalize_participant_count_filter(participant_count)
    if required is None:
        return list(detections)
    return [
        detection
        for detection in detections
        if detection.get("participant_count") == required
    ]
