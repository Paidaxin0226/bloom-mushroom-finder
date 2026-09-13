from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import threading
import time
import unicodedata
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Iterator, Sequence

import cv2
import numpy as np

from .paths import BACKGROUND_SOURCE_DIR, PROJECT_ROOT, RECOGNITION_RUNS_DIR
from .viewport_geo import estimate_detection_coordinate
from .memory_watchdog import (
    PIKMIN_PACKAGE, PikminMemoryWatchdog, RESTART_SCREEN_MAP,
    classify_pikmin_restart_screen,
)
from .project_defaults import (
    MAP_GESTURE_BOTTOM_FRACTION,
    MAP_GESTURE_TOP_FRACTION,
    MAX_GEO_SEQUENCE_RADIUS,
)
from .device_control import UsbDeviceController
from .detection import filter_terrain_false_positives


DIRECTION_ORDER = ("north", "south", "east", "west")
PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
SCREEN_STABLE_SECONDS = 1.0
SCREEN_CHANGE_POLL_SECONDS = 0.15
SCREEN_CHANGE_THRESHOLD = 0.01
SCREEN_CHANGE_MAX_SECONDS = 10.0
GESTURE_ACCESSIBILITY_COMPONENT = (
    "com.locwarp.mock/com.locwarp.mock.PikminGestureAccessibilityService"
)
GESTURE_BROADCAST_COMPONENT = "com.locwarp.mock/.GestureCommandReceiver"
GESTURE_BROADCAST_ACTION = "com.locwarp.mock.PINCH_ZOOM_OUT"


class AutomationError(RuntimeError):
    """An ADB or scan error that can be displayed directly to the user."""


class MapRecoveryError(AutomationError):
    """Map state is unknown, including for the scanner's final recenter tap."""


class ForegroundAppError(MapRecoveryError):
    """Pikmin is no longer foreground, commonly because Unity crashed."""


@dataclass(frozen=True)
class ScreenSize:
    width: int
    height: int


@dataclass(frozen=True)
class SwipeGesture:
    x1: int
    y1: int
    x2: int
    y2: int


@dataclass(frozen=True)
class ScanPoint:
    ordinal: int
    direction: str
    step: int
    grid_x: int = 0
    grid_y: int = 0

    @staticmethod
    def _axis_token(value: int) -> str:
        if value > 0:
            return f"p{value:02d}"
        if value < 0:
            return f"m{abs(value):02d}"
        return "z00"

    @property
    def stem(self) -> str:
        if self.direction == "center":
            return f"scan_{self.ordinal:03d}_center"
        if self.direction == "grid":
            return (
                f"scan_{self.ordinal:03d}_"
                f"x{self._axis_token(self.grid_x)}_y{self._axis_token(self.grid_y)}"
            )
        return f"scan_{self.ordinal:03d}_{self.direction}_{self.step:02d}"

    @property
    def location_label(self) -> str:
        if self.direction == "center":
            return "中心 (x=0, y=0)"
        if self.direction == "grid":
            return f"x={self.grid_x:+d}, y={self.grid_y:+d}｜第 {self.step} 圈"
        return f"{self.direction} {self.step}"


@dataclass(frozen=True)
class CaptureOutcome:
    detection_count: int = 0


@dataclass(frozen=True)
class ScanSummary:
    captured: int
    detection_count: int
    completed: bool
    stop_reason: str


@dataclass(frozen=True)
class MapRestoreResult:
    """Result of rebuilding a known map center and replaying an x/y grid."""

    ready: bool
    grid_replayed: bool = False

    def __bool__(self) -> bool:
        return self.ready


@dataclass(frozen=True)
class ScreenObservation:
    payload: bytes | None
    signature: np.ndarray | None
    changed: bool
    change_count: int
    sampled: bool
    stopped: bool
    timed_out: bool


StatusCallback = Callable[[str], None]
ProgressCallback = Callable[[int, int, ScanPoint], None]
CaptureCallback = Callable[[ScanPoint, bytes], CaptureOutcome | None]
BeforeCaptureCallback = Callable[[ScanPoint], bool]
ForegroundRecoveryCallback = Callable[[], bool]


def _process_kwargs() -> dict[str, int]:
    return {"creationflags": CREATE_NO_WINDOW} if os.name == "nt" else {}


def slugify_city(value: str) -> str:
    normalized = unicodedata.normalize("NFKC", value).strip().casefold()
    normalized = re.sub(r"[^\w-]+", "_", normalized, flags=re.UNICODE)
    normalized = re.sub(r"_+", "_", normalized).strip("_-")
    if not normalized:
        raise AutomationError("城市名稱不能是空白。")
    if normalized in {".", ".."}:
        raise AutomationError("城市名稱無效。")
    return normalized


def parse_screen_size(output: str) -> ScreenSize:
    override = re.search(r"Override size:\s*(\d+)x(\d+)", output, re.IGNORECASE)
    physical = re.search(r"Physical size:\s*(\d+)x(\d+)", output, re.IGNORECASE)
    # A wm-size override is the logical coordinate space used by Android input.
    match = override or physical or re.search(r"(\d+)x(\d+)", output)
    if match is None:
        raise AutomationError(f"無法解析手機螢幕尺寸：{output.strip() or '沒有輸出'}")
    width, height = int(match.group(1)), int(match.group(2))
    if width < 100 or height < 100:
        raise AutomationError(f"手機螢幕尺寸不合理：{width}x{height}")
    return ScreenSize(width, height)


def gesture_for_direction(direction: str, size: ScreenSize) -> SwipeGesture:
    direction = direction.casefold()
    center_x = round(size.width * 0.50)
    center_y = round(size.height * 0.43)
    left = round(size.width * 0.17)
    right = round(size.width * 0.83)
    # Preserve the calibrated 66%-of-screen travel while moving the entire
    # vertical stroke away from Android/launcher swipe-up recognition.
    top = round(size.height * MAP_GESTURE_TOP_FRACTION)
    bottom = round(size.height * MAP_GESTURE_BOTTOM_FRACTION)
    gestures = {
        # Dragging the map down reveals the area north of the current viewport.
        "north": SwipeGesture(center_x, top, center_x, bottom),
        "south": SwipeGesture(center_x, bottom, center_x, top),
        "east": SwipeGesture(right, center_y, left, center_y),
        "west": SwipeGesture(left, center_y, right, center_y),
    }
    try:
        return gestures[direction]
    except KeyError as exc:
        raise AutomationError(f"不支援的掃描方向：{direction}") from exc


def focused_packages_from_activity_dump(output: str) -> set[str]:
    """Extract only resumed/focused packages from ``dumpsys activity``."""
    packages: set[str] = set()
    for line in str(output or "").splitlines():
        if not any(marker in line for marker in (
            "topResumedActivity=", "ResumedActivity:", "mFocusedApp=",
        )):
            continue
        packages.update(re.findall(r"\bu\d+\s+([A-Za-z][\w.]*)/", line))
    return packages


def build_scan_points(
    steps_per_direction: int,
    directions: Sequence[str] = DIRECTION_ORDER,
    *,
    include_center: bool = True,
) -> list[ScanPoint]:
    if steps_per_direction < 1:
        raise AutomationError("每個方向的步數必須大於 0。")
    points: list[ScanPoint] = []
    ordinal = 0
    if include_center:
        points.append(ScanPoint(ordinal, "center", 0))
        ordinal += 1
    for direction in directions:
        normalized = direction.casefold()
        if normalized not in DIRECTION_ORDER:
            raise AutomationError(f"不支援的掃描方向：{direction}")
        for step in range(1, steps_per_direction + 1):
            points.append(ScanPoint(ordinal, normalized, step))
            ordinal += 1
    return points


def build_rectangular_scan_points(
    horizontal_radius: int,
    vertical_radius: int,
    *,
    left_radius: int | None = None,
    right_radius: int | None = None,
    up_radius: int | None = None,
    down_radius: int | None = None,
) -> list[ScanPoint]:
    """Build a center-out spiral clipped to an optionally asymmetric rectangle."""
    return list(
        iter_rectangular_scan_points(
            horizontal_radius,
            vertical_radius,
            left_radius=left_radius,
            right_radius=right_radius,
            up_radius=up_radius,
            down_radius=down_radius,
        )
    )


def iter_rectangular_scan_points(
    horizontal_radius: int,
    vertical_radius: int,
    *,
    left_radius: int | None = None,
    right_radius: int | None = None,
    up_radius: int | None = None,
    down_radius: int | None = None,
) -> Iterator[ScanPoint]:
    """Yield a centre-out rectangle without materialising a large city grid."""

    left = horizontal_radius if left_radius is None else left_radius
    right = horizontal_radius if right_radius is None else right_radius
    up = vertical_radius if up_radius is None else up_radius
    down = vertical_radius if down_radius is None else down_radius
    radii = (left, right, up, down)
    if any(value < 0 for value in radii):
        raise AutomationError("左、右、上、下搜尋範圍都不能小於 0。")
    if any(value > MAX_GEO_SEQUENCE_RADIUS for value in radii):
        raise AutomationError(
            f"左、右、上、下搜尋範圍都不能超過 {MAX_GEO_SEQUENCE_RADIUS} 格。"
        )

    ordinal = 0
    yield ScanPoint(ordinal=ordinal, direction="center", step=0, grid_x=0, grid_y=0)
    maximum_ring = max(radii)
    for ring in range(1, maximum_ring + 1):
        # Continue from the previous ring's south-east corner, then move
        # clockwise around the new perimeter. Adjacent square cells differ by 1.
        coordinates = (
            ((ring, y) for y in range(-(ring - 1), ring + 1)),
            ((x, ring) for x in range(ring - 1, -ring - 1, -1)),
            ((-ring, y) for y in range(ring - 1, -ring - 1, -1)),
            ((x, -ring) for x in range(-ring + 1, ring + 1)),
        )
        for side in coordinates:
            for x, y in side:
                if not (-left <= x <= right and -down <= y <= up):
                    continue
                ordinal += 1
                yield ScanPoint(
                    ordinal=ordinal,
                    direction="grid",
                    step=ring,
                    grid_x=x,
                    grid_y=y,
                )


def next_capture_index(directory: Path) -> int:
    highest = -1
    if not directory.is_dir():
        return 0
    for path in directory.glob("*.png"):
        match = re.search(r"_bg_(\d{3,})_", path.name)
        if match:
            highest = max(highest, int(match.group(1)))
    return highest + 1


def background_filename(city: str, index: int, point: ScanPoint) -> str:
    if point.direction == "center":
        return f"{city}_bg_{index:03d}_center.png"
    if point.direction == "grid":
        return (
            f"{city}_bg_{index:03d}_"
            f"x{point._axis_token(point.grid_x)}_y{point._axis_token(point.grid_y)}.png"
        )
    return f"{city}_bg_{index:03d}_{point.direction}_{point.step:02d}.png"


def atomic_write_bytes(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.part")
    try:
        temporary.write_bytes(data)
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def atomic_write_json(path: Path, value: object) -> None:
    payload = json.dumps(value, ensure_ascii=False, indent=2).encode("utf-8")
    atomic_write_bytes(path, payload)


class AdbClient:
    def __init__(self, adb: Path, serial: str) -> None:
        self.adb = Path(adb)
        self.serial = serial.strip()
        if not self.adb.is_file():
            raise AutomationError(f"找不到 ADB：{self.adb}")
        if not self.serial:
            raise AutomationError("沒有選擇 Android 手機。")

    def _command(self, *arguments: str) -> list[str]:
        return [str(self.adb), "-s", self.serial, *arguments]

    def run(
        self,
        *arguments: str,
        timeout: float = 20,
        binary: bool = False,
    ) -> subprocess.CompletedProcess[bytes] | subprocess.CompletedProcess[str]:
        try:
            result = subprocess.run(
                self._command(*arguments),
                capture_output=True,
                text=not binary,
                encoding=None if binary else "utf-8",
                errors=None if binary else "replace",
                timeout=timeout,
                **_process_kwargs(),
            )
        except (subprocess.TimeoutExpired, OSError) as exc:
            raise AutomationError(f"ADB 指令逾時或無法執行：{' '.join(arguments[:3])}") from exc
        if result.returncode != 0:
            if binary:
                detail = bytes(result.stderr).decode("utf-8", errors="replace").strip()
            else:
                detail = str(result.stderr or result.stdout).strip()
            raise AutomationError(f"ADB 執行失敗：{detail or '未知錯誤'}")
        return result

    def ensure_ready(self) -> None:
        state = str(self.run("get-state", timeout=8).stdout).strip()
        if state != "device":
            raise AutomationError(f"手機狀態不是 device：{state or '沒有回應'}")

    def screen_size(self) -> ScreenSize:
        result = self.run("shell", "wm", "size", timeout=8)
        return parse_screen_size(str(result.stdout))

    def ensure_pikmin_foreground(self) -> None:
        """Refuse to inject a gesture into Home, Google Discover, or another app."""
        observed: set[str] = set()
        for attempt in range(3):
            # ``dumpsys window windows`` omits mCurrentFocus on some recent
            # Android builds (including the connected MI 9 ROM). Activity's
            # resumed record is present and identifies the actual input owner.
            result = self.run("shell", "dumpsys", "activity", "activities", timeout=8)
            observed = focused_packages_from_activity_dump(str(result.stdout or ""))
            if PIKMIN_PACKAGE in observed:
                return
            # Focus can be temporarily null for a frame during a Unity surface
            # transition.  Retry briefly, but never guess and inject blindly.
            if not observed and attempt < 2:
                time.sleep(0.08)
                continue
            break
        current = "無法確認" if not observed else ", ".join(sorted(observed))
        raise ForegroundAppError(
            "自動手勢前偵測到前景已離開 Pikmin Bloom"
            f"（目前：{current}）；已停止輸入，不會繼續滑動首頁或 Google 頁面。"
        )

    def recenter(self, size: ScreenSize) -> None:
        self.ensure_pikmin_foreground()
        x = round(size.width * 0.898)
        y = round(size.height * 0.923)
        self.run("shell", "input", "tap", str(x), str(y), timeout=8)

    def swipe(self, direction: str, size: ScreenSize, duration_ms: int) -> None:
        self.ensure_pikmin_foreground()
        gesture = gesture_for_direction(direction, size)
        self.run(
            "shell",
            "input",
            "swipe",
            str(gesture.x1),
            str(gesture.y1),
            str(gesture.x2),
            str(gesture.y2),
            str(duration_ms),
            timeout=10,
        )

    def pinch_zoom_out(self, *, repetitions: int = 8) -> None:
        """Inject a real two-pointer gesture on Android without opening scrcpy."""
        repetitions = max(1, min(8, int(repetitions)))
        enabled_result = self.run(
            "shell", "settings", "get", "secure", "enabled_accessibility_services",
            timeout=8,
        )
        enabled_text = str(enabled_result.stdout or "").strip()
        services = [] if enabled_text in {"", "null"} else enabled_text.split(":")
        normalized = {
            service.replace("/.", "/com.locwarp.mock.")
            for service in services
            if service
        }
        if GESTURE_ACCESSIBILITY_COMPONENT not in normalized:
            services.append(GESTURE_ACCESSIBILITY_COMPONENT)
            self.run(
                "shell", "settings", "put", "secure", "enabled_accessibility_services",
                ":".join(services),
                timeout=8,
            )
        self.run(
            "shell", "settings", "put", "secure", "accessibility_enabled", "1",
            timeout=8,
        )

        last_detail = "Android 手勢服務尚未連線"
        for _attempt in range(4):
            result = self.run(
                "shell", "am", "broadcast", "--receiver-foreground",
                "-a", GESTURE_BROADCAST_ACTION,
                "-n", GESTURE_BROADCAST_COMPONENT,
                "--ei", "repetitions", str(repetitions),
                timeout=20,
            )
            detail = str(result.stdout or result.stderr or "").strip()
            if "result=-1" in detail and "completed" in detail.lower():
                return
            last_detail = detail or last_detail
            time.sleep(0.5)
        raise AutomationError(
            "ADB 雙指縮小失敗；請安裝最新版 BloomPilot Android app。"
            f" Android 回應：{last_detail}"
        )

    def capture_png(self, *, attempts: int = 3, timeout: float = 30) -> bytes:
        """Capture a full device frame, tolerating transient USB/ADB stalls."""
        attempts = max(1, int(attempts))
        last_error: Exception | None = None
        for attempt in range(1, attempts + 1):
            try:
                result = self.run(
                    "exec-out",
                    "screencap",
                    "-p",
                    timeout=max(1.0, float(timeout)),
                    binary=True,
                )
                payload = bytes(result.stdout)
                if payload.startswith(PNG_SIGNATURE):
                    return payload
                last_error = AutomationError("ADB 截圖不是有效的 PNG。")
            except (AutomationError, subprocess.TimeoutExpired) as exc:
                last_error = exc
            if attempt < attempts:
                try:
                    self.ensure_ready()
                except (AutomationError, subprocess.TimeoutExpired):
                    pass
        if isinstance(last_error, subprocess.TimeoutExpired) or isinstance(
            getattr(last_error, "__cause__", None), subprocess.TimeoutExpired,
        ):
            raise AutomationError(
                f"ADB 完整畫面截圖連續 {attempts} 次逾時；請確認 USB 線與偵錯連線。"
            ) from last_error
        raise AutomationError(
            f"ADB 完整畫面截圖連續 {attempts} 次失敗：{last_error or '未知錯誤'}"
        ) from last_error


def _wait(stop_event: threading.Event, seconds: float) -> bool:
    return not stop_event.wait(max(0.0, seconds))


def _screen_signature(payload: bytes) -> np.ndarray:
    image = cv2.imdecode(np.frombuffer(payload, dtype=np.uint8), cv2.IMREAD_GRAYSCALE)
    if image is None:
        raise AutomationError("無法解碼用於畫面變化偵測的手機 PNG 截圖。")
    height, width = image.shape[:2]
    # Ignore the Android/game status area and bottom controls. The middle map
    # area is enough to detect a swipe or newly loaded map content, while
    # avoiding clocks and character animations that otherwise never settle.
    top = max(0, round(height * 0.08))
    bottom = min(height, max(top + 1, round(height * 0.82)))
    left = max(0, round(width * 0.04))
    right = min(width, max(left + 1, round(width * 0.96)))
    map_area = image[top:bottom, left:right]
    signature = cv2.resize(map_area, (96, 128), interpolation=cv2.INTER_AREA)
    return cv2.GaussianBlur(signature, (5, 5), 0)


def _screen_change_score(previous: np.ndarray, current: np.ndarray) -> float:
    if previous.shape != current.shape:
        return 1.0
    difference = cv2.absdiff(previous, current)
    return float(np.mean(difference)) / 255.0


def observe_screen_until_stable(
    capture_png: Callable[[], bytes],
    *,
    stop_event: threading.Event,
    reference_signature: np.ndarray | None,
    stable_seconds: float = SCREEN_STABLE_SECONDS,
    poll_seconds: float = SCREEN_CHANGE_POLL_SECONDS,
    change_threshold: float = SCREEN_CHANGE_THRESHOLD,
    maximum_seconds: float = SCREEN_CHANGE_MAX_SECONDS,
    on_change: Callable[[bytes], None] | None = None,
) -> ScreenObservation:
    """Return the latest frame after the map has stopped changing.

    A frame is returned only when it differs meaningfully from the supplied
    reference. With no reference (the initial center), the first valid frame is
    treated as changed so the center is always recognized.
    """
    if stable_seconds < 0:
        raise AutomationError("畫面穩定秒數不能小於 0。")
    if poll_seconds < 0:
        raise AutomationError("畫面偵測間隔不能小於 0。")
    if not 0 <= change_threshold <= 1:
        raise AutomationError("畫面變化門檻必須介於 0 與 1。")
    if maximum_seconds < stable_seconds:
        raise AutomationError("畫面偵測最長時間不能小於穩定秒數。")

    started_at = time.monotonic()
    stable_since = started_at
    previous = reference_signature
    latest_payload: bytes | None = None
    saw_change = reference_signature is None
    change_count = 0
    sampled = False

    while True:
        if stop_event.is_set():
            return ScreenObservation(
                None, previous, saw_change, change_count, sampled, True, False
            )
        payload = capture_png()
        if not payload:
            return ScreenObservation(
                None, previous, saw_change, change_count, sampled, False, False
            )
        sampled = True
        signature = _screen_signature(payload)
        observed_at = time.monotonic()
        latest_payload = payload
        changed_now = False
        if previous is None:
            saw_change = True
            changed_now = True
            stable_since = observed_at
        elif _screen_change_score(previous, signature) >= change_threshold:
            saw_change = True
            changed_now = True
            stable_since = observed_at
        previous = signature
        if changed_now:
            change_count += 1
            if on_change is not None:
                on_change(payload)

        if observed_at - stable_since >= stable_seconds:
            return ScreenObservation(
                latest_payload if saw_change else None,
                previous,
                saw_change,
                change_count,
                sampled,
                False,
                False,
            )
        if observed_at - started_at >= maximum_seconds:
            return ScreenObservation(
                latest_payload if saw_change else None,
                previous,
                saw_change,
                change_count,
                sampled,
                False,
                True,
            )
        if stop_event.wait(min(poll_seconds, stable_seconds)):
            return ScreenObservation(
                None, previous, saw_change, change_count, sampled, True, False
            )


def run_directional_scan(
    client: AdbClient,
    *,
    steps_per_direction: int,
    settle_seconds: float,
    swipe_duration_ms: int,
    stop_event: threading.Event,
    on_capture: CaptureCallback,
    directions: Sequence[str] = DIRECTION_ORDER,
    include_center: bool = True,
    status: StatusCallback | None = None,
    progress: ProgressCallback | None = None,
) -> ScanSummary:
    if settle_seconds < 0:
        raise AutomationError("區域載入等待秒數不能小於 0。")
    if swipe_duration_ms < 1:
        raise AutomationError("滑動時間必須大於 0 毫秒。")
    points = build_scan_points(steps_per_direction, directions, include_center=include_center)
    point_lookup = {(point.direction, point.step): point for point in points}
    size = client.screen_size()
    captured = 0
    detection_count = 0
    completed = False
    stop_reason = "cancelled"

    def report(text: str) -> None:
        if status is not None:
            status(text)

    def capture(point: ScanPoint) -> bool:
        nonlocal captured, detection_count
        if stop_event.is_set():
            return False
        outcome = on_capture(point, client.capture_png()) or CaptureOutcome()
        captured += 1
        detection_count += outcome.detection_count
        if progress is not None:
            progress(captured, len(points), point)
        return True

    client.ensure_ready()
    try:
        report("正在回到角色定位中心…")
        client.recenter(size)
        if not _wait(stop_event, settle_seconds):
            return ScanSummary(captured, detection_count, False, stop_reason)

        if include_center:
            report("正在擷取中心畫面…")
            if not capture(point_lookup[("center", 0)]):
                return ScanSummary(captured, detection_count, False, stop_reason)

        for direction in directions:
            if stop_event.is_set():
                break
            report(f"正在回中心，準備掃描 {direction}…")
            client.recenter(size)
            if not _wait(stop_event, settle_seconds):
                break
            for step in range(1, steps_per_direction + 1):
                if stop_event.is_set():
                    break
                client.swipe(direction, size, swipe_duration_ms)
                if not _wait(stop_event, settle_seconds):
                    break
                point = point_lookup[(direction, step)]
                report(f"正在擷取 {direction} {step}/{steps_per_direction}…")
                if not capture(point):
                    break
        else:
            completed = True
            stop_reason = "completed"
    finally:
        report("正在回到角色定位中心…")
        try:
            client.recenter(size)
            # Always allow the final recenter animation to finish, even after a stop request.
            threading.Event().wait(max(0.0, settle_seconds))
        except Exception as exc:
            report(f"回中心失敗：{exc}")

    return ScanSummary(captured, detection_count, completed, stop_reason)


def restore_recognition_map(
    client: AdbClient,
    watchdog: PikminMemoryWatchdog,
    point: ScanPoint,
    *,
    stop_event: threading.Event,
    swipe_duration_ms: int,
    status: StatusCallback | None = None,
) -> MapRestoreResult:
    """Recover the game, then replay the current grid without changing GPS.

    The coordinate resolver must visually verify/correct the resulting viewport
    before the scanner may resume; replayed gestures alone are not proof.
    """
    if stop_event.is_set() or not watchdog.prepare_for_scan():
        return MapRestoreResult(False)
    deadline = time.monotonic() + 90.0
    size = client.screen_size()
    ensure_map = getattr(watchdog, "ensure_scan_map", None)
    # A recap can interrupt coordinate recovery too. Rebuild from the new
    # center instead of finishing the remaining gestures on the wrong page.
    for _attempt in range(3):
        reset = False
        for direction, count in (
            ("east" if point.grid_x > 0 else "west", abs(point.grid_x)),
            ("north" if point.grid_y > 0 else "south", abs(point.grid_y)),
        ):
            for index in range(count):
                if stop_event.is_set() or time.monotonic() >= deadline:
                    return MapRestoreResult(False)
                if ensure_map is not None and ensure_map():
                    reset = True
                    break
                if stop_event.is_set():
                    return MapRestoreResult(False)
                client.swipe(direction, size, swipe_duration_ms)
                if stop_event.wait(SCREEN_STABLE_SECONDS):
                    return MapRestoreResult(False)
                if status is not None and index % 10 == 0:
                    status(f"正在恢復搜尋格：{direction} {index + 1}/{count}…")
            if reset:
                break
        if not reset and ensure_map is not None:
            reset = ensure_map()
        if not reset:
            ready = not stop_event.is_set()
            return MapRestoreResult(ready, grid_replayed=ready)
    return MapRestoreResult(False)


def run_rectangular_scan(
    client: AdbClient,
    *,
    horizontal_radius: int,
    vertical_radius: int,
    settle_seconds: float,
    swipe_duration_ms: int,
    stop_event: threading.Event,
    on_capture: CaptureCallback,
    left_radius: int | None = None,
    right_radius: int | None = None,
    up_radius: int | None = None,
    down_radius: int | None = None,
    capture_png: Callable[[], bytes] | None = None,
    detect_screen_changes: bool = False,
    screen_stable_seconds: float | None = None,
    before_capture: BeforeCaptureCallback | None = None,
    recover_foreground: ForegroundRecoveryCallback | None = None,
    pause_checkpoint: Callable[[], bool] | None = None,
    status: StatusCallback | None = None,
    progress: ProgressCallback | None = None,
    scan_spans: Sequence[Sequence[int]] | None = None,
    ensure_map: Callable[[], bool] | None = None,
) -> ScanSummary:
    """Scan an asymmetric rectangle or its compressed effective-cell mask."""
    if settle_seconds < 0:
        raise AutomationError("區域載入等待秒數不能小於 0。")
    if swipe_duration_ms < 1:
        raise AutomationError("滑動時間必須大於 0 毫秒。")
    all_points = iter_rectangular_scan_points(
        horizontal_radius,
        vertical_radius,
        left_radius=left_radius,
        right_radius=right_radius,
        up_radius=up_radius,
        down_radius=down_radius,
    )
    left = horizontal_radius if left_radius is None else left_radius
    right = horizontal_radius if right_radius is None else right_radius
    up = vertical_radius if up_radius is None else up_radius
    down = vertical_radius if down_radius is None else down_radius
    if scan_spans is None:
        points = all_points
        total_points = (left + right + 1) * (up + down + 1)
    else:
        rows: dict[int, list[tuple[int, int]]] = {}
        total_points = 0
        for span in scan_spans:
            if len(span) != 3:
                raise AutomationError("有效格帶必須是 [y, 起始 x, 結束 x]。")
            row_y, start_x, end_x = (int(value) for value in span)
            if not -down <= row_y <= up or not -left <= start_x <= end_x <= right:
                raise AutomationError("有效格帶超出矩形搜尋範圍。")
            rows.setdefault(row_y, []).append((start_x, end_x))
            total_points += end_x - start_x + 1
        if total_points < 1 or not any(
            start_x <= 0 <= end_x for start_x, end_x in rows.get(0, [])
        ):
            raise AutomationError("有效格帶必須包含中心格。")

        def included(point: ScanPoint) -> bool:
            return any(
                start_x <= point.grid_x <= end_x
                for start_x, end_x in rows.get(point.grid_y, ())
            )

        points = (point for point in all_points if included(point))
    size = client.screen_size()
    captured = 0
    detection_count = 0
    stop_reason = "cancelled"
    current_x = 0
    current_y = 0
    current_ring = -1
    recenter_on_exit = True
    raw_capture_source = capture_png or client.capture_png
    reference_signature: np.ndarray | None = None
    consecutive_foreground_recoveries = 0
    foreground_recovery_generation = 0
    page_recoveries = 0
    stable_seconds = (
        SCREEN_STABLE_SECONDS if screen_stable_seconds is None else screen_stable_seconds
    )

    def report(text: str) -> None:
        if status is not None:
            status(text)

    def ensure_map_ready() -> bool:
        nonlocal current_x, current_y, reference_signature, page_recoveries
        if stop_event.is_set():
            return False
        if ensure_map is None:
            return True
        try:
            reset = ensure_map()
        except Exception as exc:
            raise MapRecoveryError(f"地圖狀態恢復失敗：{exc}") from exc
        if reset:
            page_recoveries += 1
            if page_recoveries > 3:
                raise MapRecoveryError("同一搜尋格反覆離開地圖，已停止搜尋。")
            current_x = current_y = 0
            reference_signature = None
        return not stop_event.is_set()

    def capture_source() -> bytes:
        # A recap can appear between the pre-gesture check and a screenshot.
        # Discard that frame before YOLO, saving, notifications or coordinates.
        target_x, target_y = current_x, current_y
        for _ in range(3):
            payload = raw_capture_source()
            if ensure_map is None or (payload and classify_pikmin_restart_screen(payload) == RESTART_SCREEN_MAP):
                return payload
            if not ensure_map_ready() or not move_to(target_x, target_y, allow_pause=False):
                return b""
        raise MapRecoveryError("連續截圖無法確認為 2D 地圖，已停止搜尋。")

    def recover_from_foreground_loss(error: ForegroundAppError) -> bool:
        """Restart at map center; the caller then replays its target grid."""
        nonlocal consecutive_foreground_recoveries, foreground_recovery_generation
        nonlocal current_x, current_y, reference_signature
        if recover_foreground is None:
            raise error
        if stop_event.is_set():
            return False
        consecutive_foreground_recoveries += 1
        if consecutive_foreground_recoveries > 3:
            raise MapRecoveryError(
                "Pikmin Bloom 在同一搜尋格連續退出前景，已停止自動恢復，"
                "以免手機反覆重啟遊戲。"
            ) from error
        report(
            "偵測到 Pikmin Bloom 已退出（手機紀錄顯示可能為 Unity／GPU 當機），"
            "正在自動重啟並恢復目前搜尋格…"
        )
        try:
            recovered = bool(recover_foreground())
        except Exception as exc:
            raise MapRecoveryError(
                f"Pikmin Bloom 退出後自動重啟失敗：{str(exc).splitlines()[0]}"
            ) from exc
        if not recovered:
            if stop_event.is_set():
                return False
            raise MapRecoveryError(
                "Pikmin Bloom 退出後已嘗試自動重啟，但無法確認 2D 地圖。"
            ) from error
        current_x = 0
        current_y = 0
        reference_signature = None
        foreground_recovery_generation += 1
        return True

    def ensure_foreground_at(target_x: int, target_y: int) -> bool:
        """Check focus before capture and replay the grid after a recovery."""
        ensure_foreground = getattr(client, "ensure_pikmin_foreground", None)
        if not callable(ensure_foreground):
            return True
        while not stop_event.is_set():
            try:
                ensure_foreground()
                return True
            except ForegroundAppError as exc:
                if not recover_from_foreground_loss(exc):
                    return False
                if not move_to(target_x, target_y, allow_pause=False):
                    return False
                report(
                    f"已恢復滑動格 x={target_x:+d}, y={target_y:+d}，繼續辨識。"
                )
        return False

    def recenter_safely() -> bool:
        while not stop_event.is_set():
            try:
                if not ensure_map_ready():
                    return False
                client.recenter(size)
                return True
            except ForegroundAppError as exc:
                if not recover_from_foreground_loss(exc):
                    return False
                # Recovery already prepares the map at its known center.
                return True
        return False

    def restore_after_excursion(target_x: int, target_y: int) -> bool:
        nonlocal reference_signature, current_x, current_y
        if pause_checkpoint is None or not pause_checkpoint():
            return not stop_event.is_set()
        reference_signature = None
        current_x = 0
        current_y = 0
        report("已返回暫停前座標，正在恢復原本搜尋格…")
        if not move_to(target_x, target_y, allow_pause=False):
            return False
        report(f"已恢復滑動格 x={target_x:+d}, y={target_y:+d}，繼續辨識。")
        return True

    def capture(point: ScanPoint) -> bool:
        nonlocal captured, detection_count, reference_signature, current_x, current_y
        nonlocal consecutive_foreground_recoveries
        nonlocal page_recoveries
        if stop_event.is_set() or not restore_after_excursion(point.grid_x, point.grid_y):
            return False
        if not ensure_foreground_at(point.grid_x, point.grid_y):
            return False
        if not ensure_map_ready() or not move_to(point.grid_x, point.grid_y, allow_pause=False):
            return False
        if before_capture is not None and before_capture(point):
            reference_signature = None
            current_x = 0
            current_y = 0
            if stop_event.is_set() or not move_to(point.grid_x, point.grid_y):
                return False
            report(f"遊戲重啟後已恢復 {point.location_label}，繼續辨識。")
        # A preventive/memory restart can fail or the game can crash while it
        # is starting. Never let the following screenshot observe Launcher.
        if not ensure_foreground_at(point.grid_x, point.grid_y):
            return False
        capture_generation = foreground_recovery_generation
        if detect_screen_changes:
            report(f"正在監看 {point.location_label} 的畫面變化…")
            point_detection_count = 0
            point_photo_count = 0

            def photograph(payload: bytes) -> None:
                nonlocal point_detection_count, point_photo_count
                point_photo_count += 1
                report(f"偵測到畫面變化，正在拍照辨識第 {point_photo_count} 張…")
                outcome = on_capture(point, payload) or CaptureOutcome()
                point_detection_count += outcome.detection_count

            observation = observe_screen_until_stable(
                capture_source,
                stop_event=stop_event,
                reference_signature=reference_signature,
                stable_seconds=stable_seconds,
                on_change=photograph,
            )
            if not ensure_foreground_at(point.grid_x, point.grid_y):
                return False
            if foreground_recovery_generation != capture_generation:
                report(f"當機發生於拍攝期間，正在重新辨識 {point.location_label}…")
                return capture(point)
            reference_signature = observation.signature
            if observation.stopped or not restore_after_excursion(point.grid_x, point.grid_y):
                return False
            if observation.timed_out:
                report("畫面持續變化超過安全等待時間，已拍攝最新變化並前往下一格。")
            if point_photo_count:
                report(
                    f"已連續 {stable_seconds:g} 秒沒有新變化；這一格共拍攝 "
                    f"{point_photo_count} 張，正在前往下一格。"
                )
            elif observation.sampled:
                report(
                    f"連續 {stable_seconds:g} 秒沒有畫面變化，略過拍照並前往下一格。"
                )
            else:
                report("無法取得這一格的畫面，已略過並前往下一格。")
            detection_count += point_detection_count
        else:
            payload = capture_source()
            if not ensure_foreground_at(point.grid_x, point.grid_y):
                return False
            if foreground_recovery_generation != capture_generation:
                report(f"當機發生於拍攝期間，正在重新辨識 {point.location_label}…")
                return capture(point)
            outcome = on_capture(point, payload) or CaptureOutcome()
            detection_count += outcome.detection_count
        captured += 1
        consecutive_foreground_recoveries = 0
        page_recoveries = 0
        if progress is not None:
            progress(captured, total_points, point)
        return True

    def move_to(target_x: int, target_y: int, *, allow_pause: bool = True) -> bool:
        nonlocal current_x, current_y
        if allow_pause and not restore_after_excursion(target_x, target_y):
            return False
        while current_x != target_x:
            if stop_event.is_set():
                return False
            if not ensure_map_ready():
                return False
            if current_x == target_x:
                break
            if allow_pause and not restore_after_excursion(target_x, target_y):
                return False
            direction = "east" if target_x > current_x else "west"
            try:
                client.swipe(direction, size, swipe_duration_ms)
            except ForegroundAppError as exc:
                if not recover_from_foreground_loss(exc):
                    return False
                continue
            current_x += 1 if direction == "east" else -1
            if not detect_screen_changes and not _wait(stop_event, settle_seconds):
                return False
        while current_y != target_y:
            if stop_event.is_set():
                return False
            if not ensure_map_ready():
                return False
            # Recovery resets both axes, including an already completed x leg.
            if current_x != target_x:
                return move_to(target_x, target_y, allow_pause=allow_pause)
            if current_y == target_y:
                break
            if allow_pause and not restore_after_excursion(target_x, target_y):
                return False
            direction = "north" if target_y > current_y else "south"
            try:
                client.swipe(direction, size, swipe_duration_ms)
            except ForegroundAppError as exc:
                if not recover_from_foreground_loss(exc):
                    return False
                continue
            current_y += 1 if direction == "north" else -1
            if not detect_screen_changes and not _wait(stop_event, settle_seconds):
                return False
        return True

    client.ensure_ready()
    try:
        report("正在回到角色定位中心…")
        if not recenter_safely():
            return ScanSummary(captured, detection_count, False, stop_reason)
        if not detect_screen_changes and not _wait(stop_event, settle_seconds):
            return ScanSummary(captured, detection_count, False, stop_reason)

        center = next(points)
        report("正在擷取區域中心…")
        if not capture(center):
            return ScanSummary(captured, detection_count, False, stop_reason)
        current_ring = 0

        for point in points:
            if stop_event.is_set():
                break
            if point.step != current_ring:
                current_ring = point.step
                report(f"正在定位並展開第 {current_ring} 圈…")
                if not recenter_safely():
                    break
                current_x = 0
                current_y = 0
                if detect_screen_changes:
                    observation = observe_screen_until_stable(
                        capture_source,
                        stop_event=stop_event,
                        reference_signature=reference_signature,
                        stable_seconds=stable_seconds,
                    )
                    reference_signature = observation.signature
                    if observation.stopped:
                        break
                elif not _wait(stop_event, settle_seconds):
                    break
            report(f"正在前往 {point.location_label}…")
            if not move_to(point.grid_x, point.grid_y):
                break
            report(f"正在辨識 {point.location_label}…")
            if not capture(point):
                break

        completed = captured == total_points
        if completed:
            stop_reason = "completed"
    except MapRecoveryError:
        recenter_on_exit = False
        raise
    finally:
        if recenter_on_exit and not stop_event.is_set():
            report("正在回到角色定位中心…")
            try:
                if ensure_map_ready():
                    client.recenter(size)
                if not detect_screen_changes:
                    threading.Event().wait(max(0.0, settle_seconds))
            except Exception as exc:
                report(f"回中心失敗：{exc}")

    return ScanSummary(captured, detection_count, completed, stop_reason)


def collect_backgrounds(
    *,
    adb: Path,
    serial: str,
    city: str,
    horizontal_radius: int,
    vertical_radius: int,
    settle_seconds: float,
    swipe_duration_ms: int = 800,
    backgrounds_root: Path = BACKGROUND_SOURCE_DIR,
    stop_event: threading.Event | None = None,
    status: StatusCallback | None = None,
    progress: ProgressCallback | None = None,
) -> tuple[Path, ScanSummary]:
    city_slug = slugify_city(city)
    output = backgrounds_root / city_slug
    output.mkdir(parents=True, exist_ok=True)
    start_index = next_capture_index(output)
    event = stop_event or threading.Event()

    def save(point: ScanPoint, payload: bytes) -> CaptureOutcome:
        path = output / background_filename(city_slug, start_index + point.ordinal, point)
        if path.exists():
            raise AutomationError(f"拒絕覆寫既有背景：{path.name}")
        atomic_write_bytes(path, payload)
        return CaptureOutcome()

    summary = run_rectangular_scan(
        AdbClient(adb, serial),
        horizontal_radius=horizontal_radius,
        vertical_radius=vertical_radius,
        settle_seconds=settle_seconds,
        swipe_duration_ms=swipe_duration_ms,
        stop_event=event,
        on_capture=save,
        status=status,
        progress=progress,
    )
    return output, summary


def _decode_png(payload: bytes) -> np.ndarray:
    image = cv2.imdecode(np.frombuffer(payload, dtype=np.uint8), cv2.IMREAD_COLOR)
    if image is None:
        raise AutomationError("無法解碼手機 PNG 截圖。")
    return image


def _encode_png(image: np.ndarray) -> bytes:
    ok, encoded = cv2.imencode(".png", image)
    if not ok:
        raise AutomationError("無法編碼辨識結果 PNG。")
    return encoded.tobytes()


def detections_from_result(result: object) -> list[dict[str, object]]:
    boxes = getattr(result, "boxes", None)
    if boxes is None or len(boxes) == 0:
        return []
    names = getattr(result, "names", {})
    coordinates = boxes.xyxy.detach().cpu().numpy()
    confidences = boxes.conf.detach().cpu().numpy()
    classes = boxes.cls.detach().cpu().numpy().astype(int)
    detections: list[dict[str, object]] = []
    for xyxy, confidence, class_id in zip(coordinates, confidences, classes):
        if isinstance(names, dict):
            class_name = str(names.get(int(class_id), int(class_id)))
        else:
            class_name = str(names[int(class_id)])
        detections.append(
            {
                "class_id": int(class_id),
                "class_name": class_name,
                "confidence": float(confidence),
                "xyxy": [float(value) for value in xyxy],
            }
        )
    return detections


def normalize_model_class_names(names: object) -> dict[int, str]:
    if isinstance(names, dict):
        return {int(class_id): str(name) for class_id, name in names.items()}
    if isinstance(names, Sequence) and not isinstance(names, (str, bytes)):
        return {class_id: str(name) for class_id, name in enumerate(names)}
    raise AutomationError("模型沒有可讀取的 target 類別名稱。")


def resolve_target_selection(
    available_classes: dict[int, str],
    target_names: Sequence[str] | None,
) -> tuple[list[str] | None, list[int] | None]:
    if target_names is None:
        return None, None
    requested = list(dict.fromkeys(str(name) for name in target_names))
    if not requested:
        raise AutomationError("至少要選擇一個辨識 target。")
    name_to_id = {name: class_id for class_id, name in available_classes.items()}
    unknown = [name for name in requested if name not in name_to_id]
    if unknown:
        raise AutomationError(f"模型內找不到所選 target：{', '.join(unknown)}")
    return requested, [name_to_id[name] for name in requested]


def _unique_run_directory(root: Path, prefix: str) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    candidate = root / f"{prefix}_{timestamp}"
    suffix = 2
    while candidate.exists():
        candidate = root / f"{prefix}_{timestamp}_{suffix:02d}"
        suffix += 1
    candidate.mkdir(parents=True)
    return candidate


def run_recognition_scan(
    *,
    adb: Path,
    serial: str,
    model_path: Path,
    horizontal_radius: int,
    vertical_radius: int,
    settle_seconds: float,
    confidence: float,
    image_size: int,
    left_radius: int | None = None,
    right_radius: int | None = None,
    up_radius: int | None = None,
    down_radius: int | None = None,
    swipe_duration_ms: int = 800,
    save_all_frames: bool = False,
    target_names: Sequence[str] | None = None,
    participant_count: int | None = None,
    filter_provider: Callable[[], dict[str, object] | None] | None = None,
    output_root: Path = RECOGNITION_RUNS_DIR,
    stop_event: threading.Event | None = None,
    status: StatusCallback | None = None,
    progress: ProgressCallback | None = None,
    preview: Callable[[np.ndarray], None] | None = None,
    detection_update: Callable[[ScanPoint, list[dict[str, object]]], None] | None = None,
    center_lat: float | None = None,
    center_lng: float | None = None,
    viewport_width_m: float | None = None,
    coordinate_scale: dict[str, float | int] | None = None,
    memory_update: Callable[[dict[str, object]], None] | None = None,
    pause_checkpoint: Callable[[], bool] | None = None,
    capture_png: Callable[[], bytes] | None = None,
    capture_image: Callable[[], np.ndarray] | None = None,
    precise_coordinates: bool = False,
    coordinate_controller: UsbDeviceController | None = None,
) -> tuple[Path, ScanSummary]:
    if not model_path.is_file():
        raise AutomationError(f"找不到模型：{model_path}")
    if not 0 < confidence < 1:
        raise AutomationError("信心門檻必須介於 0 與 1。")
    if image_size < 32:
        raise AutomationError("YOLO 尺寸太小。")
    estimate_coordinates = (
        not precise_coordinates
        and (viewport_width_m is not None or coordinate_scale is not None)
    )
    if precise_coordinates and coordinate_controller is None:
        raise AutomationError("精確座標辨識需要已連接的 Android USB 控制。")
    if estimate_coordinates:
        if center_lat is None or center_lng is None:
            raise AutomationError("啟用比例尺座標換算時必須提供中心經緯度。")
        try:
            # Validate the shared calibration values before loading the model.
            estimate_detection_coordinate(
                center_lat=float(center_lat),
                center_lng=float(center_lng),
                viewport_width_m=(
                    float(viewport_width_m) if viewport_width_m is not None else None
                ),
                screen_width=100,
                screen_height=100,
                grid_x=0,
                grid_y=0,
                xyxy=(50, 50, 50, 50),
                **(coordinate_scale or {}),
            )
        except ValueError as exc:
            raise AutomationError(str(exc)) from exc

    import torch
    from ultralytics import YOLO
    from ultralytics.cfg import DEFAULT_CFG_DICT

    from .badge_number import (
        enrich_detections_with_badge_numbers,
        filter_detections_by_participant_count,
        normalize_participant_count_filter,
    )
    from .detection import detections_with_crops, draw_detection_records

    event = stop_event or threading.Event()
    adb_client = AdbClient(adb, serial)
    adb_client.ensure_ready()
    memory_watchdog = PikminMemoryWatchdog(
        adb_client,
        stop_event=event,
        status=status,
        update=memory_update,
        capture_image=capture_image,
    )
    foreground_recovery = getattr(
        memory_watchdog, "recover_after_foreground_loss", None
    )
    if not callable(foreground_recovery):
        foreground_recovery = lambda: memory_watchdog.prepare_for_scan(
            force_restart=True
        )
    if not memory_watchdog.prepare_for_scan():
        raise AutomationError("辨識開始前無法完成遊戲重啟與最大範圍地圖準備。")
    coordinate_resolver = None
    if precise_coordinates:
        from .mushroom_coordinate import MushroomCoordinateResolver

        assert coordinate_controller is not None
        coordinate_resolver = MushroomCoordinateResolver(
            adb_client,
            coordinate_controller,
            stop_event=event,
            capture_image=capture_image,
            status=status,
        )
    run_dir = _unique_run_directory(output_root, "recognition")
    matches_original = run_dir / "matches" / "original"
    matches_annotated = run_dir / "matches" / "annotated"
    frames_dir = run_dir / "frames"
    records_path = run_dir / "detections.jsonl"
    started_at = datetime.now(timezone.utc)
    matched_frames = 0

    device: int | str = 0 if torch.cuda.is_available() else "cpu"
    device_name = torch.cuda.get_device_name(0) if device == 0 else "CPU"
    if status is not None:
        status(f"正在載入模型（{device_name}）…")
    model = YOLO(str(model_path))
    available_classes = normalize_model_class_names(model.names)
    resolved_target_names, selected_class_ids = resolve_target_selection(
        available_classes,
        target_names,
    )
    required_participant_count = normalize_participant_count_filter(participant_count)
    if status is not None:
        target_label = ", ".join(resolved_target_names) if resolved_target_names else "全部目標"
        count_label = "不限" if required_participant_count is None else str(required_participant_count)
        status(f"命中篩選：{target_label}；人數 {count_label}。")
    predict_args: dict[str, object] = {
        "imgsz": image_size,
        "conf": confidence,
        "device": device,
        "verbose": False,
    }
    if selected_class_ids is not None:
        predict_args["classes"] = selected_class_ids
    if device == 0:
        if "quantize" in DEFAULT_CFG_DICT:
            predict_args["quantize"] = 16
        else:
            predict_args["half"] = True
    model.predict(np.zeros((image_size, image_size, 3), dtype=np.uint8), **predict_args)

    active_point_ordinal = -1
    point_frame_index = 0
    precise_cache = None
    if coordinate_resolver is not None:
        from .mushroom_coordinate import PreciseCoordinateCache

        precise_cache = PreciseCoordinateCache()

    def recognize(point: ScanPoint, payload: bytes) -> CaptureOutcome:
        nonlocal active_point_ordinal, point_frame_index, matched_frames
        if point.ordinal != active_point_ordinal:
            active_point_ordinal = point.ordinal
            point_frame_index = 0
            if precise_cache is not None:
                precise_cache.clear()
        point_frame_index += 1
        file_stem = f"{point.stem}_change_{point_frame_index:03d}"
        frame = _decode_png(payload)
        frame_predict_args = dict(predict_args)
        frame_participant_count = required_participant_count
        if filter_provider is not None:
            live_filter = filter_provider()
            if isinstance(live_filter, dict):
                _, live_class_ids = resolve_target_selection(
                    available_classes,
                    live_filter.get("target_names"),
                )
                if live_class_ids is None:
                    frame_predict_args.pop("classes", None)
                else:
                    frame_predict_args["classes"] = live_class_ids
                frame_participant_count = normalize_participant_count_filter(
                    live_filter.get("participant_count")
                )
        result = model.predict(frame, **frame_predict_args)[0]
        detections = filter_terrain_false_positives(frame, detections_from_result(result))
        enrich_detections_with_badge_numbers(frame, detections)
        detections = filter_detections_by_participant_count(
            detections, frame_participant_count
        )
        if coordinate_resolver is not None:
            from .mushroom_coordinate import enrich_detections_with_precise_coordinates

            assert precise_cache is not None
            unresolved = precise_cache.pending(detections, frame)
            recheck_args = dict(frame_predict_args)
            recheck_args.pop("classes", None)
            enrich_detections_with_precise_coordinates(
                unresolved, coordinate_resolver,
                reference_image=frame,
                detect_targets=lambda image: filter_terrain_false_positives(
                    image, detections_from_result(model.predict(image, **recheck_args)[0]),
                ),
                recover_map=lambda: restore_recognition_map(
                    adb_client, memory_watchdog, point, stop_event=event,
                    swipe_duration_ms=swipe_duration_ms, status=status,
                ),
                failure_dir=run_dir / "coordinate_failures",
            )
            precise_cache.remember(unresolved, frame)
        elif estimate_coordinates:
            height, width = frame.shape[:2]
            for detection in detections:
                xyxy = detection.get("xyxy")
                if isinstance(xyxy, Sequence) and not isinstance(xyxy, (str, bytes)):
                    detection["estimated_coordinate"] = estimate_detection_coordinate(
                        center_lat=float(center_lat),
                        center_lng=float(center_lng),
                        viewport_width_m=(
                            float(viewport_width_m) if viewport_width_m is not None else None
                        ),
                        screen_width=width,
                        screen_height=height,
                        grid_x=point.grid_x,
                        grid_y=point.grid_y,
                        xyxy=xyxy,
                        **(coordinate_scale or {}),
                    )
        annotated, _ = draw_detection_records(detections, frame)
        event_detections = detections_with_crops(frame, detections)
        if detection_update is not None and event_detections:
            detection_update(point, event_detections)
        if preview is not None:
            preview(annotated)

        if save_all_frames:
            atomic_write_bytes(frames_dir / f"{file_stem}.png", payload)
        if detections:
            original_path = matches_original / f"{file_stem}.png"
            annotated_path = matches_annotated / f"{file_stem}.png"
            atomic_write_bytes(original_path, payload)
            atomic_write_bytes(annotated_path, _encode_png(annotated))
            record = {
                "captured_at": datetime.now(timezone.utc).isoformat(),
                "ordinal": point.ordinal,
                "direction": point.direction,
                "step": point.step,
                "grid_x": point.grid_x,
                "grid_y": point.grid_y,
                "change_frame": point_frame_index,
                "original": original_path.relative_to(run_dir).as_posix(),
                "annotated": annotated_path.relative_to(run_dir).as_posix(),
                "detections": detections,
            }
            matched_frames += 1
            with records_path.open("a", encoding="utf-8") as stream:
                stream.write(json.dumps(record, ensure_ascii=False) + "\n")
        return CaptureOutcome(
            detection_count=len(detections),
        )

    summary = run_rectangular_scan(
        adb_client,
        horizontal_radius=horizontal_radius,
        vertical_radius=vertical_radius,
        left_radius=left_radius,
        right_radius=right_radius,
        up_radius=up_radius,
        down_radius=down_radius,
        settle_seconds=settle_seconds,
        swipe_duration_ms=swipe_duration_ms,
        stop_event=event,
        on_capture=recognize,
        capture_png=capture_png,
        detect_screen_changes=True,
        before_capture=memory_watchdog.before_capture,
        ensure_map=getattr(memory_watchdog, "ensure_scan_map", None),
        recover_foreground=foreground_recovery,
        pause_checkpoint=pause_checkpoint,
        status=status,
        progress=progress,
    )
    finished_at = datetime.now(timezone.utc)
    if not records_path.exists():
        records_path.write_text("", encoding="utf-8")
    atomic_write_json(
        run_dir / "summary.json",
        {
            "started_at": started_at.isoformat(),
            "finished_at": finished_at.isoformat(),
            "model": str(model_path.resolve()),
            "serial": serial,
            "device": device_name,
            "settings": {
                "horizontal_radius": horizontal_radius,
                "vertical_radius": vertical_radius,
                "left_radius": horizontal_radius if left_radius is None else left_radius,
                "right_radius": horizontal_radius if right_radius is None else right_radius,
                "up_radius": vertical_radius if up_radius is None else up_radius,
                "down_radius": vertical_radius if down_radius is None else down_radius,
                "settle_seconds": settle_seconds,
                "capture_mode": "screen_change",
                "screen_stable_seconds": SCREEN_STABLE_SECONDS,
                "swipe_duration_ms": swipe_duration_ms,
                "confidence": confidence,
                "image_size": image_size,
                "save_all_frames": save_all_frames,
                "target_names": resolved_target_names or "all",
                "participant_count": (
                    "any" if required_participant_count is None else required_participant_count
                ),
                "coordinate_estimation": {
                    "enabled": estimate_coordinates,
                    "center_lat": center_lat,
                    "center_lng": center_lng,
                    "viewport_width_m": viewport_width_m,
                    "device_pixel_scale": coordinate_scale,
                    "assumption": "north_up_map",
                },
                "precise_coordinate_capture": {
                    "enabled": precise_coordinates,
                    "method": "pikmin_map_intent",
                    "movement_disabled": precise_coordinates,
                },
            },
            "scan": asdict(summary),
            "matched_frames": matched_frames,
            "memory_watchdog": {
                "enabled": True,
                "restart_count": memory_watchdog.restart_count,
                "poll_seconds": memory_watchdog.poll_seconds,
                "pressure_samples": memory_watchdog.pressure_samples,
            },
        },
    )
    return run_dir, summary


def _resolve_adb(value: str | None) -> Path:
    if value:
        return Path(value)
    from .phone_tools import find_toolchain

    return find_toolchain().adb


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Android 矩形背景蒐集與 YOLO 自動搜尋")
    parser.add_argument("--adb", help="ADB 執行檔；省略時自動尋找專案 scrcpy")
    subparsers = parser.add_subparsers(dest="command", required=True)

    collect = subparsers.add_parser("collect", help="自動滑動並蒐集背景")
    collect.add_argument("--serial", required=True)
    collect.add_argument("--city", required=True)
    collect.add_argument("--horizontal-radius", type=int, default=3)
    collect.add_argument("--vertical-radius", type=int, default=4)
    collect.add_argument("--settle", type=float, default=4.0)

    recognize = subparsers.add_parser("recognize", help="使用模型進行矩形自動搜尋")
    recognize.add_argument("--serial", required=True)
    recognize.add_argument("--model", type=Path, required=True)
    recognize.add_argument("--horizontal-radius", type=int, default=3)
    recognize.add_argument("--vertical-radius", type=int, default=4)
    recognize.add_argument("--left-radius", type=int)
    recognize.add_argument("--right-radius", type=int)
    recognize.add_argument("--up-radius", type=int)
    recognize.add_argument("--down-radius", type=int)
    recognize.add_argument("--settle", type=float, default=4.0)
    recognize.add_argument("--confidence", type=float, default=0.45)
    recognize.add_argument("--image-size", type=int, default=1024)
    recognize.add_argument(
        "--target",
        action="append",
        help="只辨識指定的模型 target 名稱；可重複使用",
    )
    recognize.add_argument(
        "--participant-count",
        type=int,
        help="只保留指定人數；0 代表畫面沒有數字",
    )
    recognize.add_argument("--save-all-frames", action="store_true")
    return parser


def main() -> int:
    args = build_parser().parse_args()
    adb = _resolve_adb(args.adb)

    def log(text: str) -> None:
        print(text, flush=True)

    def show_progress(done: int, total: int, point: ScanPoint) -> None:
        print(f"[{done}/{total}] {point.stem}", flush=True)

    if args.command == "collect":
        output, summary = collect_backgrounds(
            adb=adb,
            serial=args.serial,
            city=args.city,
            horizontal_radius=args.horizontal_radius,
            vertical_radius=args.vertical_radius,
            left_radius=args.left_radius,
            right_radius=args.right_radius,
            up_radius=args.up_radius,
            down_radius=args.down_radius,
            settle_seconds=args.settle,
            status=log,
            progress=show_progress,
        )
    else:
        output, summary = run_recognition_scan(
            adb=adb,
            serial=args.serial,
            model_path=args.model,
            horizontal_radius=args.horizontal_radius,
            vertical_radius=args.vertical_radius,
            settle_seconds=args.settle,
            confidence=args.confidence,
            image_size=args.image_size,
            target_names=args.target,
            participant_count=args.participant_count,
            save_all_frames=args.save_all_frames,
            status=log,
            progress=show_progress,
        )
    print(json.dumps({"output": str(output), "summary": asdict(summary)}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
