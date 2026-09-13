"""Resolve a detected Pikmin mushroom through its map intent.

The Android companion enters a short-lived read-only capture lease before any
UI interaction. During that lease LocWarp consumes map intents and reports the
coordinate over USB, but never changes the mock location.
"""

from __future__ import annotations

import threading
import time
import uuid
import subprocess
from collections.abc import Callable, Mapping, MutableMapping, Sequence
from pathlib import Path

import cv2
import numpy as np

from .device_control import DeviceControlError, DeviceResponse, UsbDeviceController
from .memory_watchdog import (
    RESTART_SCREEN_MAP,
    RESTART_SCREEN_UNKNOWN,
    classify_pikmin_restart_image,
)
from .phone_automation import (
    AdbClient,
    AutomationError,
    MapRecoveryError,
    MapRestoreResult,
    _encode_png,
    atomic_write_bytes,
    atomic_write_json,
)
from .map_targeting import (
    MapDragCalibration, TargetAlignmentError, map_camera_is_stable, map_translation,
    match_target, plan_map_drag,
    target_box, target_is_clipped, target_is_safe, target_crop,
)


CAPTURE_TIMEOUT_MS = 60_000
PAGE_TIMEOUT_SECONDS = 7.0
CAPTURE_POLL_SECONDS = 0.12
TARGET_TIMEOUT_SECONDS = 45.0
MAX_TARGET_ADJUSTMENTS = 2
MAX_VIEW_CORRECTIONS = 6
DetectTargets = Callable[[np.ndarray], Sequence[Mapping[str, object]]]


class PreciseCoordinateError(AutomationError):
    """A recoverable failure while opening one mushroom's map intent."""


class CoordinateRecoveryError(MapRecoveryError):
    """The map is unverified; no more detections may be clicked in this scan."""


class PreciseCoordinateCache:
    """Cache attempts within one grid, with visual identity instead of pixel bins."""

    def __init__(self) -> None:
        self.clear()

    def clear(self) -> None:
        self.reference: np.ndarray | None = None
        self.shift = (0.0, 0.0)
        self.entries: list[tuple[dict[str, object], np.ndarray, dict[str, object]]] = []

    def pending(
        self, detections: Sequence[MutableMapping[str, object]], frame: np.ndarray,
    ) -> list[MutableMapping[str, object]]:
        shift = None
        if self.reference is not None:
            shift = (0.0, 0.0) if np.array_equal(self.reference, frame) else map_translation(self.reference, frame)
        if shift is None:
            # A different/unverifiable map must never inherit old coordinates.
            self.clear()
            self.reference = frame.copy()
            shift = (0.0, 0.0)
        self.shift = shift
        pending = []
        for detection in detections:
            matches = []
            for target, crop, result in self.entries:
                try:
                    found = match_target(target, self.reference, frame, detections, shift, reference_crop=crop)
                    if target_box(found) == target_box(detection):
                        matches.append(result)
                except TargetAlignmentError:
                    continue
            if len(matches) == 1:
                detection.update(matches[0])
            else:
                pending.append(detection)
        return pending

    def remember(self, detections: Sequence[Mapping[str, object]], frame: np.ndarray) -> None:
        for detection in detections:
            result = {key: detection[key] for key in ("precise_coordinate", "coordinate_error") if key in detection}
            if not result:
                continue
            try:
                x1, y1, x2, y2 = target_box(detection)
                crop = target_crop(frame, detection)
            except TargetAlignmentError:
                continue
            if not crop.size:
                continue
            target = {
                "class_name": detection.get("class_name", detection.get("class_id")),
                "xyxy": [x1 - self.shift[0], y1 - self.shift[1], x2 - self.shift[0], y2 - self.shift[1]],
            }
            # Keep only small target patches plus one reference frame per grid.
            crop = cv2.resize(crop, (48, 48), interpolation=cv2.INTER_AREA)
            self.entries.append((target, crop, result))


def _valid_coordinate(lat: object, lng: object) -> tuple[float, float] | None:
    try:
        parsed_lat = float(lat)  # type: ignore[arg-type]
        parsed_lng = float(lng)  # type: ignore[arg-type]
    except (TypeError, ValueError, OverflowError):
        return None
    if not np.isfinite(parsed_lat) or not np.isfinite(parsed_lng):
        return None
    if not -90.0 <= parsed_lat <= 90.0 or not -180.0 <= parsed_lng <= 180.0:
        return None
    return parsed_lat, parsed_lng


def mushroom_selection_card_center(image: np.ndarray) -> tuple[int, int] | None:
    """Locate the dark name card shown after tapping a mushroom on the 2D map."""
    if image is None or not image.size:
        return None
    height, width = image.shape[:2]
    gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
    dark = cv2.inRange(gray, 0, 72)
    kernel = cv2.getStructuringElement(
        cv2.MORPH_RECT,
        (max(7, round(width * 0.025)), max(5, round(height * 0.006))),
    )
    joined = cv2.morphologyEx(dark, cv2.MORPH_CLOSE, kernel)
    count, _labels, stats, _centroids = cv2.connectedComponentsWithStats(joined, 8)
    candidates: list[tuple[int, int, int, int, float]] = []
    for index in range(1, count):
        x, y, component_width, component_height, _area = (
            int(value) for value in stats[index]
        )
        center_x = x + component_width / 2.0
        center_y = y + component_height / 2.0
        roi = dark[y : y + component_height, x : x + component_width]
        fill = float(np.count_nonzero(roi)) / max(1.0, float(roi.size))
        if (
            width * 0.25 <= component_width <= width * 0.78
            and height * 0.035 <= component_height <= height * 0.16
            and 0.10 <= center_x / width <= 0.90
            and 0.13 <= center_y / height <= 0.78
            and fill >= 0.35
        ):
            candidates.append((x, y, component_width, component_height, fill))
    if not candidates:
        return None
    x, y, component_width, component_height, _fill = max(
        candidates,
        key=lambda item: item[2] * item[3],
    )
    # The whole card is clickable; bias slightly right to avoid overlapping
    # mushroom art beneath translucent parts of unusually short labels.
    return round(x + component_width * 0.72), round(y + component_height * 0.50)


def mushroom_detail_visible(image: np.ndarray) -> bool:
    """Recognize the mushroom detail sheet without relying on localized OCR."""
    if image is None or not image.size:
        return False
    height, width = image.shape[:2]
    hsv = cv2.cvtColor(image, cv2.COLOR_BGR2HSV)
    lower_sheet = hsv[
        round(height * 0.48) : round(height * 0.86),
        round(width * 0.04) : round(width * 0.96),
    ]
    if not lower_sheet.size:
        return False
    neutral_bright = (lower_sheet[:, :, 1] <= 52) & (lower_sheet[:, :, 2] >= 205)
    return float(np.mean(neutral_bright)) >= 0.52


def mushroom_destination_menu_visible(image: np.ndarray) -> bool:
    """Recognize Pikmin's destination action sheet by its modal geometry."""
    if image is None or not image.size:
        return False
    height, width = image.shape[:2]
    gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
    center = gray[
        round(height * 0.65) : round(height * 0.83),
        round(width * 0.12) : round(width * 0.88),
    ]
    side = gray[
        round(height * 0.65) : round(height * 0.83),
        0 : round(width * 0.04),
    ]
    if not center.size or not side.size:
        return False
    bright_fraction = float(np.mean(center >= 205))
    return bright_fraction >= 0.32 and float(np.mean(side)) <= 145.0


def mushroom_route_cancel_confirmation_visible(image: np.ndarray) -> bool:
    """Detect the confirmation shown when this mushroom is already a route target."""
    if image is None or not image.size:
        return False
    height, width = image.shape[:2]
    gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
    center = gray[
        round(height * 0.41) : round(height * 0.60),
        round(width * 0.10) : round(width * 0.90),
    ]
    side = gray[
        round(height * 0.41) : round(height * 0.60),
        0 : round(width * 0.04),
    ]
    if not center.size or not side.size:
        return False
    return float(np.mean(center >= 205)) >= 0.62 and float(np.mean(side)) <= 150.0


def mushroom_route_active(image: np.ndarray) -> bool:
    """Detect the cyan active-destination pill on the mushroom detail page."""
    if image is None or not image.size or not mushroom_detail_visible(image):
        return False
    height, width = image.shape[:2]
    roi = image[
        round(height * 0.004) : round(height * 0.072),
        round(width * 0.80) : round(width * 0.94),
    ]
    if not roi.size:
        return False
    hsv = cv2.cvtColor(roi, cv2.COLOR_BGR2HSV)
    cyan = (
        (hsv[:, :, 0] >= 90)
        & (hsv[:, :, 0] <= 115)
        & (hsv[:, :, 1] >= 130)
        & (hsv[:, :, 2] >= 130)
    )
    gray = cv2.cvtColor(roi, cv2.COLOR_BGR2GRAY)
    return float(np.mean(cyan)) >= 0.72 and float(np.mean(gray < 80)) >= 0.18


class MushroomCoordinateResolver:
    """Drive one map target into LocWarp's read-only coordinate capture path."""

    def __init__(
        self,
        adb: AdbClient,
        controller: UsbDeviceController,
        *,
        stop_event: threading.Event,
        capture_image: Callable[[], np.ndarray] | None = None,
        status: Callable[[str], None] | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.adb = adb
        self.controller = controller
        self.stop_event = stop_event
        self.capture_image = capture_image
        self.status = status
        self.clock = clock
        self._deadline: float | None = None
        self._last_image: np.ndarray | None = None
        self._return_frames: list[np.ndarray] = []
        self._recovery_directory: Path | None = None
        self.recovery_count = 0
        self.recovery_seconds = 0.0

    def resolve(
        self,
        detection: Mapping[str, object],
        *,
        reference_image: np.ndarray | None = None,
        detect_targets: DetectTargets | None = None,
        recover_map: Callable[[], bool | MapRestoreResult] | None = None,
        failure_dir: Path | None = None,
    ) -> dict[str, object]:
        try:
            target_box(detection)
        except TargetAlignmentError as exc:
            raise PreciseCoordinateError(str(exc)) from exc
        session = uuid.uuid4().hex
        self._return_frames = []
        self._recovery_directory = failure_dir / f"{session}_return" if failure_dir else None
        self._deadline = self.clock() + TARGET_TIMEOUT_SECONDS
        self._last_image = None
        lease_requested = False
        interacted = False
        moved = False
        original = reference_image
        try:
            before = self._current_coordinate()
            initial = self._capture()
            if original is None:
                original = initial.copy()
            current = dict(detection)
            candidates: Sequence[Mapping[str, object]] = [current]
            if detect_targets is not None:
                interacted = True
                # The triggering frame can be taken during swipe inertia. Wait
                # before inference/alignment, then retain the settled grid as
                # the restoration baseline instead of undoing that inertia.
                initial = self._settled_map()
                candidates = detect_targets(initial)
                cleared = self._clear_selection(initial, candidates)
                if cleared is not initial:
                    candidates = detect_targets(cleared)
                initial = cleared
                shift = self._alignment(original, initial)
                current = match_target(detection, original, initial, candidates, shift)
                original = initial.copy()
            for attempt in range(MAX_TARGET_ADJUSTMENTS):
                if target_is_safe(current, initial.shape):
                    break
                adjustment_reason = (
                    "目標被畫面邊緣切到" if target_is_clipped(current, initial.shape)
                    else "目標點擊位置被介面按鈕遮住"
                )
                if detect_targets is None:
                    raise PreciseCoordinateError(f"{adjustment_reason}，需要重新辨識才能安全平移。")
                x1, y1, x2, y2 = target_box(current)
                height, width = initial.shape[:2]
                gesture = plan_map_drag(
                    initial.shape,
                    # At maximum zoom the map moves farther than the finger.
                    # Keep overlapping landmarks for the next alignment check.
                    (
                        float(np.clip(width * 0.50 - (x1 + x2) / 2, -width * 0.12, width * 0.12)),
                        float(np.clip(height * 0.52 - (y1 + y2) / 2, -height * 0.08, height * 0.08)),
                    ),
                    candidates,
                )
                self._report(f"{adjustment_reason}，正在移到中央（{attempt + 1}/{MAX_TARGET_ADJUSTMENTS}）…")
                # Mark before dispatch: an ADB timeout can still deliver input.
                moved = interacted = True
                self._drag(gesture)
                updated = self._settled_map()
                shift = self._alignment(initial, updated)
                candidates = detect_targets(updated)
                current = match_target(current, initial, updated, candidates, shift)
                initial = updated
            if not target_is_safe(current, initial.shape):
                raise PreciseCoordinateError("調整兩次後目標仍被畫面邊緣切到或點擊位置被介面遮住，精確座標未取得。")
            if detect_targets is not None:
                # Inference itself takes time. Never tap its coordinates unless
                # a fresh frame still shows the same camera position/target.
                verified = self._settled_map()
                shift = self._alignment(initial, verified)
                if max(abs(shift[0]), abs(shift[1])) > 2.5:
                    candidates = detect_targets(verified)
                    current = match_target(current, initial, verified, candidates, shift)
                    if not target_is_safe(current, verified.shape):
                        raise PreciseCoordinateError("地圖仍在移動，目標已離開可安全點擊的位置。")
                initial = verified
            self._check_active()
            lease_requested = True
            self._command("beginCoordinateCapture", {"session": session, "timeoutMs": CAPTURE_TIMEOUT_MS})
            self._report("正在開啟命中蘑菇並讀取精確座標（辨識模式不會瞬移）…")
            x1, y1, x2, y2 = target_box(current)
            target_x = round((x1 + x2) / 2.0)
            target_y = round((y1 + y2) / 2.0)
            initial_card = mushroom_selection_card_center(initial)
            initial_height, initial_width = initial.shape[:2]
            card_matches_target = (
                detect_targets is None
                and initial_card is not None
                and abs(initial_card[0] - target_x) <= initial_width * 0.36
                and initial_card[1] < target_y <= initial_card[1] + initial_height * 0.20
            )
            interacted = True
            if card_matches_target:
                assert initial_card is not None
                self._tap(*initial_card)
                selected: object = ("detail", None) if self._wait_for(
                    mushroom_detail_visible,
                    PAGE_TIMEOUT_SECONDS,
                ) else False
            else:
                self._tap(target_x, target_y)
                selected = self._wait_for(self._selection_or_detail, PAGE_TIMEOUT_SECONDS)
            if not isinstance(selected, tuple) or len(selected) != 2:
                raise PreciseCoordinateError("點擊目標後沒有出現蘑菇名稱卡片。")
            page, card = selected
            if page == "selection":
                if not isinstance(card, tuple):
                    raise PreciseCoordinateError("蘑菇名稱卡片位置無效。")
                self._tap(*card)
                if not self._wait_for(mushroom_detail_visible, PAGE_TIMEOUT_SECONDS):
                    raise PreciseCoordinateError("沒有進入蘑菇詳細頁。")

            image = self._capture()
            height, width = image.shape[:2]
            if mushroom_route_active(image):
                raise PreciseCoordinateError(
                    "此蘑菇目前是遊戲內目的地；為避免修改既有路線，已略過精確座標擷取。"
                )
            self._tap(round(width * 0.81), round(height * 0.915))
            action = self._wait_for(self._destination_action_state, 3.0)
            if action == "route_confirmation":
                image = self._capture()
                height, width = image.shape[:2]
                # Dismiss with the left "cancel" action. Never confirm, because
                # coordinate recognition must not alter an existing game route.
                self._tap(round(width * 0.32), round(height * 0.545))
                raise PreciseCoordinateError(
                    "此蘑菇目前是遊戲內目的地；既有路線未被修改。"
                )
            if action != "menu":
                raise PreciseCoordinateError("沒有出現前往目標選單。")

            image = self._capture()
            height, width = image.shape[:2]
            self._tap(round(width * 0.50), round(height * 0.845))
            coordinate = self._wait_for_coordinate(session, PAGE_TIMEOUT_SECONDS)
            after = self._current_coordinate()
            if before is not None and after is not None and not self._same_coordinate(before, after):
                raise PreciseCoordinateError("安全檢查失敗：取得座標期間手機位置發生變化。")
            lat, lng = coordinate
            return {
                "lat": lat,
                "lng": lng,
                "method": "pikmin_map_intent",
                "exact": True,
                "captured_without_movement": True,
                "target_reframed": moved,
            }
        except (TargetAlignmentError, AutomationError, DeviceControlError, subprocess.TimeoutExpired, OSError, cv2.error) as exc:
            self._save_failure(failure_dir, session, detection, str(exc), original)
            raise PreciseCoordinateError(str(exc)) from exc
        finally:
            # Cleanup has its own bounded budget; a cancelled scan sends no more gestures.
            self._deadline = None
            try:
                if lease_requested:
                    self._command("endCoordinateCapture", {"session": session}, require_ok=True)
            except (DeviceControlError, AutomationError, OSError, subprocess.TimeoutExpired) as exc:
                self._save_failure(failure_dir, session, detection, str(exc), original)
                raise CoordinateRecoveryError("無法解除座標擷取狀態，已停止辨識。") from exc
            finally:
                if interacted and not self.stop_event.is_set():
                    try:
                        map_ready = self._return_to_map()
                        if detect_targets is not None and original is not None:
                            self._restore_view(original, detect_targets, recover_map, map_ready)
                        elif not map_ready:
                            raise CoordinateRecoveryError("無法回到 2D 地圖，已停止辨識。")
                    except (TargetAlignmentError, AutomationError, DeviceControlError, subprocess.TimeoutExpired, OSError, cv2.error) as exc:
                        self._save_failure(failure_dir, session + "_recovery", detection, str(exc), original)
                        raise CoordinateRecoveryError(f"無法恢復原搜尋位置，已停止辨識：{exc}") from exc
                    finally:
                        self._deadline = None

    def _check_active(self) -> None:
        if self.stop_event.is_set():
            raise PreciseCoordinateError("辨識已停止。")
        if self._deadline is not None and self.clock() >= self._deadline:
            raise PreciseCoordinateError("單一目標座標擷取逾時，已停止重試。")

    @staticmethod
    def _alignment(before: np.ndarray, after: np.ndarray) -> tuple[float, float]:
        if before.shape == after.shape and np.array_equal(before, after):
            return (0.0, 0.0)
        shift = map_translation(before, after)
        if shift is None:
            raise TargetAlignmentError("無法確認地圖位移或縮放已改變。")
        return shift

    def _drag(self, gesture: tuple[int, int, int, int]) -> None:
        self._check_active()
        self.adb.run("shell", "input", "swipe", *(str(value) for value in gesture), "650", timeout=8)

    def _settled_map(self) -> np.ndarray:
        deadline = self.clock() + 5.0
        previous: np.ndarray | None = None
        stable_since = self.clock()
        while self.clock() < deadline:
            image = self._capture()
            state = classify_pikmin_restart_image(image)
            if state == RESTART_SCREEN_UNKNOWN:
                # A fresh scrcpy frame can catch map controls between animation
                # states immediately after a drag. Wait for a classifiable frame
                # instead of treating one transient sample as leaving the map.
                previous = None
                stable_since = self.clock()
                self.stop_event.wait(CAPTURE_POLL_SECONDS)
                continue
            if state != RESTART_SCREEN_MAP:
                raise PreciseCoordinateError("畫面已離開 2D 地圖。")
            if previous is None or not map_camera_is_stable(previous, image):
                stable_since = self.clock()
            elif self.clock() - stable_since >= 0.6:
                return image
            previous = image
            self.stop_event.wait(CAPTURE_POLL_SECONDS)
        raise PreciseCoordinateError("地圖在等待時間內沒有穩定，已略過點擊。")

    def _clear_selection(
        self, image: np.ndarray, candidates: Sequence[Mapping[str, object]],
    ) -> np.ndarray:
        card = mushroom_selection_card_center(image)
        if card is None:
            return image
        height, width = image.shape[:2]
        blockers = list(candidates) + [{"xyxy": [
            card[0] - width * 0.60, card[1] - height * 0.10,
            card[0] + width * 0.35, card[1] + height * 0.10,
        ]}]
        x, y, _, _ = plan_map_drag(image.shape, (0.0, 0.0), blockers)
        self._tap(x, y)
        image = self._settled_map()
        if mushroom_selection_card_center(image) is not None:
            raise PreciseCoordinateError("無法關閉上一個目標的名稱卡片。")
        return image

    def _restore_view(
        self,
        original: np.ndarray,
        detect_targets: DetectTargets,
        recover_map: Callable[[], bool | MapRestoreResult] | None,
        map_ready: bool,
    ) -> None:
        # A rebuilt grid gets a fresh correction budget, even if recovery was
        # requested on the last local attempt. Every gesture uses measured map
        # displacement: touch pixels are not map pixels at maximum zoom-out.
        rebuilt_grid = False
        for recovery in range(2):
            self._deadline = self.clock() + 20.0
            calibration = MapDragCalibration()
            previous_shift: tuple[float, float] | None = None
            previous_gesture: tuple[int, int, int, int] | None = None
            if map_ready:
                try:
                    for attempt in range(MAX_VIEW_CORRECTIONS + 1):
                        image = self._settled_map()
                        if mushroom_selection_card_center(image) is not None:
                            image = self._clear_selection(image, detect_targets(image))
                        shift = self._alignment(original, image)
                        tolerance = max(3.0, original.shape[1] * 0.006)
                        if max(abs(shift[0]), abs(shift[1])) <= tolerance:
                            return
                        if attempt == MAX_VIEW_CORRECTIONS:
                            break
                        if previous_shift is not None and previous_gesture is not None:
                            calibration.observe(previous_gesture, (
                                shift[0] - previous_shift[0], shift[1] - previous_shift[1],
                            ))
                        gesture = plan_map_drag(
                            image.shape, calibration.correction(
                                (-shift[0], -shift[1]),
                                minimum_distance=max(24.0, image.shape[1] * 0.025),
                            ),
                            detect_targets(image),
                        )
                        self._report("正在恢復並核對原本搜尋位置…")
                        previous_shift, previous_gesture = shift, gesture
                        self._drag(gesture)
                except (TargetAlignmentError, AutomationError, OSError, subprocess.TimeoutExpired, cv2.error) as exc:
                    if self.stop_event.is_set():
                        raise
                    self._report(f"搜尋位置局部校正未完成：{exc}")
            if recover_map is None or recovery == 1:
                break
            self._report("搜尋畫面無法確認，正在重新準備遊戲與目前搜尋格…")
            self._save_return_diagnostics()
            self.recovery_count += 1
            recovery_started = self.clock()
            # The existing game recovery owns its startup timeout and stop checks.
            self._deadline = None
            recovery_result = recover_map()
            self.recovery_seconds += self.clock() - recovery_started
            if not recovery_result:
                break
            rebuilt_grid = (
                isinstance(recovery_result, MapRestoreResult)
                and recovery_result.grid_replayed
            )
            map_ready = True
        if rebuilt_grid:
            # The recovery callback has rebuilt a known north-up map from GPS
            # center and replayed the scanner's exact x/y grid. Sparse rural
            # maps can still lack enough overlapping features for pixel-level
            # registration. That uncertainty should skip fine correction, not
            # abort a multi-day geographic sequence after a successful replay.
            self._report(
                "完整地圖與搜尋格已重建，但像素核對缺少足夠重疊；"
                "已略過局部微調並繼續序列。"
            )
            return
        raise CoordinateRecoveryError("畫面位置核對失敗。")

    def _save_failure(
        self, directory: Path | None, session: str, detection: Mapping[str, object],
        error: str, original: np.ndarray | None,
    ) -> None:
        if directory is None:
            return
        try:
            atomic_write_json(directory / f"{session}.json", {
                "target": {key: detection.get(key) for key in ("xyxy", "class_name", "confidence")},
                "coordinate_error": error,
            })
            if original is not None:
                atomic_write_bytes(directory / f"{session}_original.png", _encode_png(original))
            if self._last_image is not None:
                atomic_write_bytes(directory / f"{session}_last.png", _encode_png(self._last_image))
        except (OSError, cv2.error) as exc:
            self._report(f"無法保存座標擷取錯誤紀錄：{exc}")

    def _capture(self) -> np.ndarray:
        self._check_active()
        if self.capture_image is not None:
            image = self.capture_image()
            if image is not None and image.size:
                self._last_image = image.copy()
                return image
        payload = self.adb.capture_png(attempts=1, timeout=8)
        image = cv2.imdecode(np.frombuffer(payload, dtype=np.uint8), cv2.IMREAD_COLOR)
        if image is None:
            raise PreciseCoordinateError("無法解碼手機畫面。")
        self._last_image = image.copy()
        return image

    @staticmethod
    def _selection_or_detail(image: np.ndarray) -> object:
        if mushroom_detail_visible(image):
            return ("detail", None)
        card = mushroom_selection_card_center(image)
        return ("selection", card) if card is not None else False

    @staticmethod
    def _destination_action_state(image: np.ndarray) -> object:
        if mushroom_destination_menu_visible(image):
            return "menu"
        if mushroom_route_cancel_confirmation_visible(image):
            return "route_confirmation"
        return False

    def _wait_for(
        self,
        detector: Callable[[np.ndarray], object],
        timeout: float,
    ) -> object:
        deadline = self.clock() + timeout
        while self.clock() < deadline:
            if self.stop_event.is_set():
                raise PreciseCoordinateError("辨識已停止。")
            result = detector(self._capture())
            if result:
                return result
            self.stop_event.wait(CAPTURE_POLL_SECONDS)
        return False

    def _wait_for_coordinate(self, session: str, timeout: float) -> tuple[float, float]:
        deadline = self.clock() + timeout
        while self.clock() < deadline:
            self._check_active()
            response = self._command(
                "coordinateCaptureStatus",
                {"session": session},
            )
            capture = response.raw.get("capture")
            if isinstance(capture, Mapping) and capture.get("captured") is True:
                coordinate = _valid_coordinate(capture.get("lat"), capture.get("lng"))
                if coordinate is not None:
                    return coordinate
            self.stop_event.wait(CAPTURE_POLL_SECONDS)
        raise PreciseCoordinateError("預設地圖應用程式沒有回傳座標。")

    def _current_coordinate(self) -> tuple[float, float] | None:
        response = self._command("status", require_ok=False)
        current = response.status.get("current")
        if not isinstance(current, Mapping):
            return None
        return _valid_coordinate(current.get("lat"), current.get("lng"))

    def _command(
        self,
        command: str,
        payload: dict[str, object] | None = None,
        *,
        require_ok: bool = True,
    ) -> DeviceResponse:
        try:
            return self.controller.command(command, payload, require_ok=require_ok)
        except DeviceControlError as exc:
            message = str(exc)
            if not any(
                marker in message
                for marker in (
                    "USB 控制連線中斷",
                    "USB 控制連線已關閉",
                    "Android 已關閉 USB 控制連線",
                    "尚未連接 Android USB 控制",
                )
            ):
                raise
            self.controller.connect()
            return self.controller.command(command, payload, require_ok=require_ok)

    def _tap(self, x: int, y: int) -> None:
        self._check_active()
        self.adb.run("shell", "input", "tap", str(x), str(y), timeout=8)

    def _save_return_diagnostics(self) -> None:
        if self._recovery_directory is None:
            return
        try:
            frames = self._return_frames + ([self._last_image] if self._last_image is not None else [])
            for index, frame in enumerate(frames):
                atomic_write_bytes(self._recovery_directory / f"{index:02d}.png", _encode_png(frame))
        except (OSError, cv2.error) as exc:
            self._report(f"無法保存返回地圖診斷影格：{exc}")

    def _return_to_map(self) -> bool:
        self._deadline = self.clock() + 12.0
        try:
            for _ in range(4):
                image = self._capture()
                self._return_frames.append(image.copy())
                if classify_pikmin_restart_image(image) == RESTART_SCREEN_MAP:
                    try:
                        # A single map frame can be the outgoing page underneath
                        # the intent/detail transition. Verify it has settled
                        # before handing it to the scanner's restore routine.
                        self._settled_map()
                        return True
                    except PreciseCoordinateError:
                        self._check_active()
                        continue
                if mushroom_destination_menu_visible(image):
                    # Opening a map intent can leave this sheet over the detail
                    # page. Recheck the exposed page before choosing its close
                    # control; two blind Android Back presses can leave 2D.
                    self.adb.run("shell", "input", "keyevent", "4", timeout=8)
                    self._wait_for(lambda frame: mushroom_detail_visible(frame) or
                                   classify_pikmin_restart_image(frame) == RESTART_SCREEN_MAP, 2.0)
                    continue
                if mushroom_detail_visible(image):
                    height, width = image.shape[:2]
                    self._tap(round(width * 0.10), round(height * 0.915))
                else:
                    # Unknown/animated pages are not permission to press Back:
                    # the delayed gesture can land on the map and leave 2D.
                    self._wait_for(lambda frame: mushroom_destination_menu_visible(frame)
                                   or mushroom_detail_visible(frame)
                                   or classify_pikmin_restart_image(frame) == RESTART_SCREEN_MAP, 2.0)
                    continue
                self._wait_for(lambda frame: classify_pikmin_restart_image(frame) == RESTART_SCREEN_MAP, 2.0)
        except (AutomationError, OSError, subprocess.TimeoutExpired):
            pass
        return False

    @staticmethod
    def _same_coordinate(
        first: tuple[float, float],
        second: tuple[float, float],
    ) -> bool:
        return abs(first[0] - second[0]) <= 1e-7 and abs(first[1] - second[1]) <= 1e-7

    def _report(self, message: str) -> None:
        if self.status is not None:
            self.status(message)


def enrich_detections_with_precise_coordinates(
    detections: Sequence[MutableMapping[str, object]],
    resolver: MushroomCoordinateResolver,
    *,
    reference_image: np.ndarray | None = None,
    detect_targets: DetectTargets | None = None,
    recover_map: Callable[[], bool | MapRestoreResult] | None = None,
    failure_dir: Path | None = None,
) -> None:
    """Resolve each detection independently and keep a failed hit visible."""
    for detection in detections:
        try:
            if resolver.stop_event.is_set():
                detection["coordinate_error"] = "辨識已停止。"
                continue
            detection["precise_coordinate"] = resolver.resolve(
                detection, reference_image=reference_image, detect_targets=detect_targets,
                recover_map=recover_map, failure_dir=failure_dir,
            )
            detection.pop("coordinate_error", None)
        except CoordinateRecoveryError:
            raise
        except (AutomationError, DeviceControlError) as exc:
            detection.pop("precise_coordinate", None)
            detection["coordinate_error"] = str(exc)
