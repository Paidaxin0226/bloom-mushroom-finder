"""Image-based map alignment and conservative target selection for UI clicks."""

from __future__ import annotations

from collections.abc import Mapping, Sequence

import cv2
import numpy as np

from .project_defaults import MAP_GESTURE_BOTTOM_FRACTION


class TargetAlignmentError(RuntimeError):
    pass


class MapDragCalibration:
    """Learn screen motion per touch pixel, including the game's drag threshold."""

    def __init__(self) -> None:
        self.gain = 1.0
        self.slop = 0.0
        self.samples: list[tuple[float, float]] = []

    def observe(self, gesture: tuple[int, int, int, int], movement: tuple[float, float]) -> None:
        touch = np.array([gesture[2] - gesture[0], gesture[3] - gesture[1]], dtype=float)
        distance = float(np.linalg.norm(touch))
        if distance < 1.0:
            return
        direction = touch / distance
        motion = float(np.dot(movement, direction))
        sideways = float(np.linalg.norm(np.array(movement) - motion * direction))
        if motion <= 2.0 or sideways > max(4.0, motion * 0.10):
            return
        self.samples.append((distance, motion))
        if 0.25 <= motion / distance <= 4.0:
            self.gain = motion / distance
        shortest = min(self.samples)
        longest = max(self.samples)
        if longest[0] - shortest[0] >= 12.0:
            gain = (longest[1] - shortest[1]) / (longest[0] - shortest[0])
            if 0.25 <= gain <= 4.0:
                slop = shortest[0] - shortest[1] / gain
                if 0.0 <= slop <= 32.0:
                    self.gain, self.slop = gain, slop

    def correction(
        self, delta: tuple[float, float], *, minimum_distance: float = 0.0,
    ) -> tuple[float, float]:
        distance = float(np.linalg.norm(delta))
        if distance < 1.0:
            return delta
        factor = 1.0 / self.gain + self.slop / distance
        if distance * factor < minimum_distance:
            # Android/game touch slop can ignore a tiny swipe altogether.
            # Move away once so the next verified correction is a real drag.
            # Increasing the tiny swipe toward the target can just oscillate
            # across it forever when both residuals are below the threshold.
            factor = -2.0 * minimum_distance / distance
        return delta[0] * factor, delta[1] * factor


def target_box(detection: Mapping[str, object]) -> tuple[float, float, float, float]:
    value = detection.get("xyxy")
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)) or len(value) != 4:
        raise TargetAlignmentError("目標框沒有可點擊的位置。")
    try:
        x1, y1, x2, y2 = (float(item) for item in value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise TargetAlignmentError("目標框座標無效。") from exc
    if not all(np.isfinite(item) for item in (x1, y1, x2, y2)) or x2 <= x1 or y2 <= y1:
        raise TargetAlignmentError("目標框座標無效。")
    return x1, y1, x2, y2


def target_is_clipped(detection: Mapping[str, object], shape: tuple[int, ...]) -> bool:
    """Treat boxes touching the image border as potentially cut-off targets."""
    height, width = shape[:2]
    x1, y1, x2, y2 = target_box(detection)
    # Allow only detector rounding at the edge (2 px on a 1080 px screen),
    # rather than reserving a percentage of the map for a future name card.
    edge_tolerance = max(1.0, min(height, width) / 540.0)
    return (
        x1 <= edge_tolerance or y1 <= edge_tolerance
        or x2 >= width - edge_tolerance or y2 >= height - edge_tolerance
    )


def target_is_safe(detection: Mapping[str, object], shape: tuple[int, ...]) -> bool:
    """Click fully visible targets unless their tap point lies on a control."""
    if target_is_clipped(detection, shape):
        return False
    height, width = shape[:2]
    x1, y1, x2, y2 = target_box(detection)
    cx, cy = (x1 + x2) / 2, (y1 + y2) / 2
    # These are local tap blockers, not clipping margins. A complete mushroom
    # next to a control can be opened without moving the map first.
    control_regions = (
        (0.0, 0.0, 1.0, 0.035),      # Android status bar
        (0.0, 0.97, 1.0, 1.0),       # Android navigation bar
        (0.84, 0.035, 1.0, 0.10),    # Compass / upper-right back control
        (0.92, 0.33, 1.0, 0.40),     # Right-side map tab
        (0.03, 0.89, 0.17, 0.955),   # Back
        (0.37, 0.89, 0.63, 0.955),   # 2D / 3D switch
        (0.83, 0.89, 0.97, 0.955),   # Locator
    )
    return not any(
        left * width <= cx <= right * width and top * height <= cy <= bottom * height
        for left, top, right, bottom in control_regions
    )


def _feature_translation(
    points_a: Sequence[cv2.KeyPoint],
    descriptors_a: np.ndarray | None,
    points_b: Sequence[cv2.KeyPoint],
    descriptors_b: np.ndarray | None,
    *,
    norm: int,
    ratio: float,
    minimum_matches: int,
    minimum_inliers: int,
    minimum_inlier_ratio: float,
    minimum_span: tuple[float, float],
    image_size: tuple[int, int],
    scale: float,
) -> tuple[float, float] | None:
    if descriptors_a is None or descriptors_b is None:
        return None
    pairs = cv2.BFMatcher(norm).knnMatch(descriptors_a, descriptors_b, k=2)
    matches = [
        pair[0]
        for pair in pairs
        if len(pair) == 2 and pair[0].distance < ratio * pair[1].distance
    ]
    if len(matches) < minimum_matches:
        return None
    source = np.float32([points_a[match.queryIdx].pt for match in matches])
    dest = np.float32([points_b[match.trainIdx].pt for match in matches])
    matrix, inliers = cv2.estimateAffinePartial2D(
        source,
        dest,
        method=cv2.RANSAC,
        ransacReprojThreshold=2.5,
    )
    if matrix is None or inliers is None:
        return None
    accepted = inliers.ravel().astype(bool)
    if (
        int(accepted.sum()) < minimum_inliers
        or float(accepted.mean()) < minimum_inlier_ratio
    ):
        return None
    width, height = image_size
    span = np.ptp(source[accepted], axis=0)
    if span[0] < width * minimum_span[0] or span[1] < height * minimum_span[1]:
        return None
    zoom = float(np.hypot(matrix[0, 0], matrix[1, 0]))
    angle = abs(float(np.degrees(np.arctan2(matrix[1, 0], matrix[0, 0]))))
    if not 0.97 <= zoom <= 1.03 or angle > 1.5:
        return None
    # A median translation is less sensitive to small affine fitting errors.
    delta = dest[accepted] - source[accepted]
    shift = np.median(delta, axis=0)
    residual = np.linalg.norm(delta - shift, axis=1)
    if float(np.percentile(residual, 80)) > 3.0:
        return None
    return float(shift[0] / scale), float(shift[1] / scale)


def map_translation(before: np.ndarray, after: np.ndarray) -> tuple[float, float] | None:
    """Measure map movement, including large low-overlap recovery offsets.

    ORB remains the fast primary matcher. A conservative SIFT fallback handles
    rebuilt grid views whose displacement leaves too little overlap for ORB.
    Fixed controls are masked and both paths reject rotation, zoom, clustered
    matches, and inconsistent motion before any corrective drag is permitted.
    """
    if before.shape != after.shape:
        return None
    height, width = before.shape[:2]
    scale = min(1.0, 720.0 / max(height, width))
    size = (round(width * scale), round(height * scale))
    images = [
        cv2.resize(cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY), size)
        for frame in (before, after)
    ]
    mask = np.zeros(images[0].shape, dtype=np.uint8)
    h, w = mask.shape
    mask[round(h * 0.13):round(h * 0.83), round(w * 0.05):round(w * 0.95)] = 255

    orb = cv2.ORB_create(nfeatures=1600, fastThreshold=12)
    points_a, descriptors_a = orb.detectAndCompute(images[0], mask)
    points_b, descriptors_b = orb.detectAndCompute(images[1], mask)
    measured = _feature_translation(
        points_a,
        descriptors_a,
        points_b,
        descriptors_b,
        norm=cv2.NORM_HAMMING,
        ratio=0.72,
        minimum_matches=12,
        minimum_inliers=10,
        minimum_inlier_ratio=0.50,
        minimum_span=(0.18, 0.18),
        image_size=(w, h),
        scale=scale,
    )
    if measured is not None:
        return measured

    sift_create = getattr(cv2, "SIFT_create", None)
    if not callable(sift_create):
        return None
    sift = sift_create(nfeatures=2500, contrastThreshold=0.015)
    points_a, descriptors_a = sift.detectAndCompute(images[0], mask)
    points_b, descriptors_b = sift.detectAndCompute(images[1], mask)
    return _feature_translation(
        points_a,
        descriptors_a,
        points_b,
        descriptors_b,
        norm=cv2.NORM_L2,
        ratio=0.72,
        minimum_matches=10,
        minimum_inliers=8,
        minimum_inlier_ratio=0.60,
        minimum_span=(0.14, 0.18),
        image_size=(w, h),
        scale=scale,
    )


def map_camera_is_stable(
    before: np.ndarray,
    after: np.ndarray,
    *,
    tolerance: float = 2.5,
) -> bool:
    """Ignore animated map objects while checking whether the camera stopped."""
    if before.shape == after.shape and np.array_equal(before, after):
        return True
    shift = map_translation(before, after)
    return shift is not None and max(abs(shift[0]), abs(shift[1])) <= tolerance


def target_crop(image: np.ndarray, detection: Mapping[str, object]) -> np.ndarray:
    x1, y1, x2, y2 = target_box(detection)
    height, width = image.shape[:2]
    return image[max(0, round(y1)):min(height, round(y2)), max(0, round(x1)):min(width, round(x2))]


def _appearance_matches(first: np.ndarray, second: np.ndarray) -> bool:
    if not first.size or not second.size:
        return False
    histograms = []
    for crop in (first, second):
        hsv = cv2.cvtColor(crop, cv2.COLOR_BGR2HSV)
        hist = cv2.calcHist([hsv], [0, 1], None, [24, 16], [0, 180, 0, 256])
        histograms.append(cv2.normalize(hist, None, norm_type=cv2.NORM_L1))
    return cv2.compareHist(*histograms, cv2.HISTCMP_BHATTACHARYYA) <= 0.45


def match_target(
    original: Mapping[str, object],
    before: np.ndarray,
    after: np.ndarray,
    candidates: Sequence[Mapping[str, object]],
    shift: tuple[float, float],
    *,
    reference_crop: np.ndarray | None = None,
) -> dict[str, object]:
    x1, y1, x2, y2 = target_box(original)
    expected = ((x1 + x2) / 2 + shift[0], (y1 + y2) / 2 + shift[1])
    # A clipped edge box can grow when the full mushroom enters the viewport.
    tolerance = max(14.0, before.shape[1] * 0.035, max(x2 - x1, y2 - y1) * 0.65)
    original_crop = target_crop(before, original) if reference_crop is None else reference_crop
    matches = []
    for candidate in candidates:
        if candidate.get("class_name", candidate.get("class_id")) != original.get("class_name", original.get("class_id")):
            continue
        a, b, c, d = target_box(candidate)
        distance = np.hypot((a + c) / 2 - expected[0], (b + d) / 2 - expected[1])
        if distance <= tolerance and _appearance_matches(original_crop, target_crop(after, candidate)):
            matches.append(candidate)
    if len(matches) != 1:
        raise TargetAlignmentError("無法唯一確認原本目標，已保留命中並略過點擊。")
    return dict(matches[0])


def plan_map_drag(
    shape: tuple[int, ...],
    delta: tuple[float, float],
    detections: Sequence[Mapping[str, object]],
) -> tuple[int, int, int, int]:
    """Choose a bounded gesture whose start is clear of detected targets/UI."""
    height, width = shape[:2]
    dx = float(np.clip(delta[0], -width * 0.32, width * 0.32))
    dy = float(np.clip(delta[1], -height * 0.28, height * 0.28))
    boxes = [target_box(detection) for detection in detections]
    padding = max(12, width * 0.025)
    for fy in (0.48, 0.38, 0.60, 0.28, 0.70):
        for fx in (0.50, 0.35, 0.65, 0.22, 0.78):
            x, y = width * fx, height * fy
            end_x, end_y = x + dx, y + dy
            if not (
                width * 0.12 <= end_x <= width * 0.88
                and height * 0.20 <= end_y <= height * MAP_GESTURE_BOTTOM_FRACTION
            ):
                continue
            if any(a - padding <= x <= c + padding and b - padding <= y <= d + padding for a, b, c, d in boxes):
                continue
            return round(x), round(y), round(end_x), round(end_y)
    raise TargetAlignmentError("找不到可安全拖曳的地圖空白位置。")
