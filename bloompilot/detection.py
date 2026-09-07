"""Shared model selection and detection drawing for recognition workflows."""

from __future__ import annotations

import base64
import math
from pathlib import Path
from typing import Sequence

import cv2
import numpy as np

from .paths import TRAINING_RUNS_DIR


def newest_trained_model(root: Path = TRAINING_RUNS_DIR) -> Path | None:
    candidates = list(root.glob("*/weights/best.pt")) if root.is_dir() else []
    if not candidates:
        return None
    return max(candidates, key=lambda path: path.stat().st_mtime)


def draw_detections(result: object, source: np.ndarray) -> tuple[np.ndarray, int]:
    output = source.copy()
    boxes = getattr(result, "boxes", None)
    if boxes is None or len(boxes) == 0:
        return output, 0

    names = getattr(result, "names", {})
    coordinates = boxes.xyxy.detach().cpu().numpy()
    confidences = boxes.conf.detach().cpu().numpy()
    classes = boxes.cls.detach().cpu().numpy().astype(int)
    line_width = max(2, round(min(output.shape[:2]) / 360))
    font_scale = max(0.5, min(output.shape[:2]) / 1100)

    for xyxy, confidence, class_id in zip(coordinates, confidences, classes):
        x1, y1, x2, y2 = (int(round(value)) for value in xyxy)
        color = (42, 230, 120)
        cv2.rectangle(output, (x1, y1), (x2, y2), color, line_width, cv2.LINE_AA)
        if isinstance(names, dict):
            class_name = str(names.get(class_id, class_id))
        else:
            class_name = str(names[class_id])
        label = f"{class_name} {confidence:.0%}"
        (text_width, text_height), baseline = cv2.getTextSize(
            label, cv2.FONT_HERSHEY_SIMPLEX, font_scale, line_width
        )
        label_top = max(0, y1 - text_height - baseline - 8)
        cv2.rectangle(
            output,
            (x1, label_top),
            (x1 + text_width + 10, label_top + text_height + baseline + 8),
            color,
            -1,
        )
        cv2.putText(
            output,
            label,
            (x1 + 5, label_top + text_height + 3),
            cv2.FONT_HERSHEY_SIMPLEX,
            font_scale,
            (8, 24, 14),
            line_width,
            cv2.LINE_AA,
        )
    return output, len(coordinates)


def draw_detection_records(
    detections: Sequence[dict[str, object]], source: np.ndarray
) -> tuple[np.ndarray, int]:
    """Draw already-filtered detection dictionaries on a copy of the source."""
    output = source.copy()
    line_width = max(2, round(min(output.shape[:2]) / 360))
    font_scale = max(0.5, min(output.shape[:2]) / 1100)
    drawn = 0
    for detection in detections:
        xyxy = detection.get("xyxy")
        if not isinstance(xyxy, Sequence) or isinstance(xyxy, (str, bytes)) or len(xyxy) != 4:
            continue
        try:
            x1, y1, x2, y2 = (int(round(float(value))) for value in xyxy)
            confidence = float(detection.get("confidence", 0.0))
        except (TypeError, ValueError, OverflowError):
            continue
        color = (42, 230, 120)
        cv2.rectangle(output, (x1, y1), (x2, y2), color, line_width, cv2.LINE_AA)
        label = f"{detection.get('class_name', 'target')} {confidence:.0%}"
        participant_count = detection.get("participant_count")
        if isinstance(participant_count, int) and not isinstance(participant_count, bool):
            label += f" n={participant_count}"
        (text_width, text_height), baseline = cv2.getTextSize(
            label, cv2.FONT_HERSHEY_SIMPLEX, font_scale, line_width
        )
        label_top = max(0, y1 - text_height - baseline - 8)
        cv2.rectangle(
            output,
            (x1, label_top),
            (x1 + text_width + 10, label_top + text_height + baseline + 8),
            color,
            -1,
        )
        cv2.putText(
            output,
            label,
            (x1 + 5, label_top + text_height + 3),
            cv2.FONT_HERSHEY_SIMPLEX,
            font_scale,
            (8, 24, 14),
            line_width,
            cv2.LINE_AA,
        )
        drawn += 1
    return output, drawn


def detections_with_crops(
    source: np.ndarray,
    detections: Sequence[dict[str, object]],
    *,
    maximum_dimension: int = 320,
    jpeg_quality: int = 84,
) -> list[dict[str, object]]:
    """Build live-hit payloads containing only each bounded target crop."""
    if source.ndim < 2 or maximum_dimension < 1:
        return []
    height, width = source.shape[:2]
    enriched: list[dict[str, object]] = []
    for detection in detections:
        xyxy = detection.get("xyxy")
        if (
            not isinstance(xyxy, Sequence)
            or isinstance(xyxy, (str, bytes))
            or len(xyxy) != 4
        ):
            continue
        try:
            coordinates = [float(value) for value in xyxy]
        except (TypeError, ValueError, OverflowError):
            continue
        if any(not math.isfinite(value) for value in coordinates):
            continue
        x1 = max(0, min(width, math.floor(coordinates[0])))
        y1 = max(0, min(height, math.floor(coordinates[1])))
        x2 = max(0, min(width, math.ceil(coordinates[2])))
        y2 = max(0, min(height, math.ceil(coordinates[3])))
        if x2 <= x1 or y2 <= y1:
            continue
        crop = source[y1:y2, x1:x2]
        crop_height, crop_width = crop.shape[:2]
        largest = max(crop_width, crop_height)
        if largest > maximum_dimension:
            scale = maximum_dimension / largest
            crop = cv2.resize(
                crop,
                (max(1, round(crop_width * scale)), max(1, round(crop_height * scale))),
                interpolation=cv2.INTER_AREA,
            )
        ok, encoded = cv2.imencode(
            ".jpg",
            crop,
            [cv2.IMWRITE_JPEG_QUALITY, max(40, min(100, int(jpeg_quality)))],
        )
        if not ok:
            continue
        item = dict(detection)
        item["crop_data_url"] = "data:image/jpeg;base64," + base64.b64encode(encoded).decode("ascii")
        item["crop_bounds"] = [x1, y1, x2, y2]
        item["crop_width"] = int(crop.shape[1])
        item["crop_height"] = int(crop.shape[0])
        enriched.append(item)
    return enriched
