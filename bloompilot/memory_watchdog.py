"""ADB-backed Android RAM watchdog for long-running Pikmin scans."""

from __future__ import annotations

import re
import threading
import time
from dataclasses import asdict, dataclass, replace
from typing import Any, Callable

import cv2
import numpy as np

from .device_control import (
    DeviceControlError,
    DeviceResponse,
    UsbDeviceController,
    reconnect_usb,
    restart_locwarp,
)
from .map_targeting import map_camera_is_stable
from .daily_review import daily_review_summary_visible, daily_review_mood_visible


PIKMIN_PACKAGE = "com.nianticlabs.pikmin"
KIB_PER_MIB = 1024
# Leave time for the scan's next safe checkpoint before Android kills the
# foreground USB service. This is a preventive margin, not an Android LMK limit.
MIN_AVAILABLE_MIB = 256
MAX_AVAILABLE_RESERVE_MIB = 384
AVAILABLE_RESERVE_RATIO = 0.05
MIN_GAME_PSS_MIB = 2048
MAX_GAME_PSS_MIB = 3072
GAME_PSS_TOTAL_RATIO = 0.30
RESTART_SCREEN_LOADING = "loading"
RESTART_SCREEN_SPLASH = "splash"
RESTART_SCREEN_LOGO = "logo"
RESTART_SCREEN_LOGIN = "login"
RESTART_SCREEN_MAP = "map"
RESTART_SCREEN_MAP_3D = "map_3d"
# Compatibility name for callers/tests created before the intermediate screen
# was correctly identified as Pikmin's 3D map.
RESTART_SCREEN_GARDEN = RESTART_SCREEN_MAP_3D
RESTART_SCREEN_HOME = "home"
RESTART_SCREEN_CLOSABLE = "closable"
RESTART_SCREEN_SAFETY = "safety"
RESTART_SCREEN_DAILY_REVIEW = "daily_review"
RESTART_SCREEN_UNKNOWN = "unknown"
MAX_MAP_ZOOM_OUT_PINCHES = 8
# The connected MI 9 repeatedly records Unity/Adreno native crashes after
# roughly 37–45 minutes. Restart with margin before that known failure window.
DEFAULT_PREVENTIVE_RESTART_SECONDS = 30 * 60


@dataclass(frozen=True)
class AndroidMemorySnapshot:
    total_mib: float
    available_mib: float
    available_percent: float
    game_pss_mib: float | None
    available_threshold_mib: float
    game_pss_threshold_mib: float
    pressure: bool
    pressure_reason: str
    consecutive_pressure: int = 0
    restart_count: int = 0
    restarting: bool = False

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


def parse_proc_meminfo(value: str) -> tuple[int, int]:
    fields: dict[str, int] = {}
    for match in re.finditer(r"(?m)^([A-Za-z_()]+):\s*([0-9]+)\s*kB\s*$", value or ""):
        fields[match.group(1)] = int(match.group(2))
    total = fields.get("MemTotal", 0)
    available = fields.get("MemAvailable", 0)
    if available <= 0:
        available = sum(fields.get(name, 0) for name in ("MemFree", "Buffers", "Cached"))
    if total <= 0 or available < 0 or available > total:
        raise ValueError("無法解析手機 /proc/meminfo。")
    return total, available


def parse_total_pss_kib(value: str) -> int | None:
    patterns = (
        r"(?mi)^\s*TOTAL PSS:\s*([0-9,]+)",
        r"(?mi)^\s*TOTAL\s+([0-9,]+)\s+",
    )
    for pattern in patterns:
        match = re.search(pattern, value or "")
        if match is not None:
            return int(match.group(1).replace(",", ""))
    return None


def memory_snapshot(total_kib: int, available_kib: int, game_pss_kib: int | None) -> AndroidMemorySnapshot:
    total_mib = total_kib / KIB_PER_MIB
    available_mib = available_kib / KIB_PER_MIB
    game_pss_mib = None if game_pss_kib is None else game_pss_kib / KIB_PER_MIB
    available_threshold = min(
        float(MAX_AVAILABLE_RESERVE_MIB),
        max(float(MIN_AVAILABLE_MIB), total_mib * AVAILABLE_RESERVE_RATIO),
    )
    game_threshold = min(
        float(MAX_GAME_PSS_MIB),
        max(float(MIN_GAME_PSS_MIB), total_mib * GAME_PSS_TOTAL_RATIO),
    )
    reasons: list[str] = []
    if available_mib < available_threshold:
        reasons.append("system_available")
    return AndroidMemorySnapshot(
        total_mib=round(total_mib, 1),
        available_mib=round(available_mib, 1),
        available_percent=round(available_kib / total_kib * 100, 1),
        game_pss_mib=None if game_pss_mib is None else round(game_pss_mib, 1),
        available_threshold_mib=round(available_threshold, 1),
        game_pss_threshold_mib=round(game_threshold, 1),
        pressure=bool(reasons),
        pressure_reason="+".join(reasons),
    )


def _restart_icon_mask(
    image: np.ndarray,
    x_ratio: float,
    y_ratio: float,
    *,
    side_ratio: float = 0.11,
    white_value_min: int = 155,
    white_saturation_max: int = 115,
) -> np.ndarray:
    """Return a normalized mask for one of Pikmin's fixed bottom controls."""
    height, width = image.shape[:2]
    side = max(32, round(width * side_ratio))
    center_x = round(width * x_ratio)
    center_y = round(height * y_ratio)
    left = max(0, min(width - side, center_x - side // 2))
    top = max(0, min(height - side, center_y - side // 2))
    crop = image[top : top + side, left : left + side]
    if crop.size == 0:
        return np.zeros((96, 96), dtype=np.uint8)
    hsv = cv2.cvtColor(crop, cv2.COLOR_BGR2HSV)
    # The map, close, and menu glyphs are neutral white/grey over a saturated
    # green button. Keeping the threshold broad tolerates the button glow and
    # different map brightness without treating the green background as an icon.
    white = cv2.inRange(hsv, (0, 0, white_value_min), (179, white_saturation_max, 255))
    return cv2.resize(white, (96, 96), interpolation=cv2.INTER_NEAREST)


def _restart_icon_corner_densities(mask: np.ndarray) -> tuple[float, float, float, float] | None:
    count, labels, stats, centroids = cv2.connectedComponentsWithStats(mask, 8)
    candidates = [
        index
        for index in range(1, count)
        if int(stats[index, cv2.CC_STAT_AREA]) >= 20
    ]
    if not candidates:
        return None
    center = mask.shape[0] / 2.0
    component = min(
        candidates,
        key=lambda index: (
            float(centroids[index, 0]) - center
        ) ** 2 + (float(centroids[index, 1]) - center) ** 2,
    )
    x, y, width, height, _area = (int(value) for value in stats[component])
    if width < 12 or height < 12:
        return None
    shape = labels[y : y + height, x : x + width] == component
    x_low = max(1, round(width * 0.35))
    x_high = min(width - 1, round(width * 0.65))
    y_low = max(1, round(height * 0.35))
    y_high = min(height - 1, round(height * 0.65))
    corners = (
        shape[:y_low, :x_low],
        shape[:y_low, x_high:],
        shape[y_high:, :x_low],
        shape[y_high:, x_high:],
    )
    if any(not corner.size for corner in corners):
        return None
    return tuple(float(corner.mean()) for corner in corners)


def _restart_icon_is_x(mask: np.ndarray) -> bool:
    """Distinguish the centred X from the map's left-facing back chevron."""
    corners = _restart_icon_corner_densities(mask)
    # An X has strokes in all four bounding-box corners. A back chevron only
    # occupies both right corners and converges near the middle of its left edge.
    return corners is not None and all(density >= 0.20 for density in corners)


def _restart_icon_is_back(mask: np.ndarray) -> bool:
    corners = _restart_icon_corner_densities(mask)
    if corners is None:
        return False
    top_left, top_right, bottom_left, bottom_right = corners
    # Downsampling/compression can move one sparse corner exactly onto 0.20.
    # Keep the other sparse corner below it so an X still cannot qualify.
    return (
        top_left <= 0.20
        and bottom_left <= 0.20
        and min(top_left, bottom_left) < 0.20
        and top_right >= 0.20
        and bottom_right >= 0.20
    )


def _restart_icon_is_hamburger(mask: np.ndarray) -> bool:
    count, _labels, stats, centroids = cv2.connectedComponentsWithStats(mask, 8)
    strokes: list[float] = []
    for index in range(1, count):
        x, y, width, height, area = (int(value) for value in stats[index])
        center_x, center_y = (float(value) for value in centroids[index])
        if (
            area >= 80
            and width >= 28
            and height <= 12
            and 18 <= center_x <= 78
            and 24 <= center_y <= 80
            and width >= height * 3
        ):
            strokes.append(center_y)
    strokes.sort()
    return len(strokes) >= 3 and strokes[-1] - strokes[0] >= 16


def _pikmin_map_tab_state(image: np.ndarray) -> str:
    # Only the selected view has a bright white outline. The broad glyph mask
    # also includes both pale green tile backgrounds, hiding that difference.
    left_tab = _restart_icon_mask(
        image, 0.45, 0.923, side_ratio=0.078,
        white_value_min=225, white_saturation_max=55,
    )
    right_tab = _restart_icon_mask(
        image, 0.55, 0.923, side_ratio=0.078,
        white_value_min=225, white_saturation_max=55,
    )
    left_white = float(np.count_nonzero(left_tab)) / float(left_tab.size)
    right_white = float(np.count_nonzero(right_tab)) / float(right_tab.size)
    if right_white - left_white >= 0.08:
        return RESTART_SCREEN_MAP
    if left_white - right_white >= 0.08:
        return RESTART_SCREEN_MAP_3D
    return RESTART_SCREEN_UNKNOWN


def _pikmin_map_locator_visible(image: np.ndarray) -> bool:
    right = _restart_icon_mask(image, 0.898, 0.923)
    count, _labels, stats, centroids = cv2.connectedComponentsWithStats(right, 8)
    for index in range(1, count):
        _x, _y, width, height, area = (int(value) for value in stats[index])
        center_x, center_y = (float(value) for value in centroids[index])
        aspect = width / max(1.0, float(height))
        fill = area / max(1.0, float(width * height))
        if (
            20 <= width <= 60
            and 20 <= height <= 60
            and 0.70 <= aspect <= 1.30
            and 0.15 <= fill <= 0.65
            and (center_x - 48.0) ** 2 + (center_y - 48.0) ** 2 <= 24.0 ** 2
        ):
            return True
    return False


def _pikmin_safety_warning_visible(
    image: np.ndarray,
    *,
    hsv: np.ndarray | None = None,
    gray: np.ndarray | None = None,
) -> bool:
    """Detect Pikmin's optional don't-drive warning by its confirm button.

    The warning is localized, so text is intentionally ignored. Its large
    teal outlined pill on a white modal is stable and does not occur on the
    normal HOME/loading screens that were previously misclassified as it.
    """
    height, width = image.shape[:2]
    if hsv is None:
        hsv = cv2.cvtColor(image, cv2.COLOR_BGR2HSV)
    teal = cv2.inRange(hsv, (65, 70, 80), (105, 255, 255))
    count, _labels, stats, centroids = cv2.connectedComponentsWithStats(teal, 8)
    if gray is None:
        gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
    for index in range(1, count):
        x, y, component_width, component_height, area = (
            int(value) for value in stats[index]
        )
        center_x, center_y = (float(value) for value in centroids[index])
        aspect = component_width / max(1.0, float(component_height))
        fill = area / max(1.0, float(component_width * component_height))
        if not (
            width * 0.22 <= component_width <= width * 0.45
            and height * 0.035 <= component_height <= height * 0.09
            and 2.0 <= aspect <= 4.5
            and 0.02 <= fill <= 0.16
            and abs(center_x / width - 0.50) <= 0.09
            and 0.48 <= center_y / height <= 0.59
        ):
            continue
        inner = gray[
            y + round(component_height * 0.18) : y + round(component_height * 0.82),
            x + round(component_width * 0.12) : x + round(component_width * 0.88),
        ]
        if inner.size and float(np.mean(inner >= 225)) >= 0.65:
            return True
    return False


def _pikmin_launch_splash_visible(
    image: np.ndarray,
    *,
    hsv: np.ndarray | None = None,
    gray: np.ndarray | None = None,
) -> bool:
    """Detect Android's black Pikmin app-icon launch splash."""
    height, width = image.shape[:2]
    if hsv is None:
        hsv = cv2.cvtColor(image, cv2.COLOR_BGR2HSV)
    if gray is None:
        gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
    center = hsv[
        round(height * 0.32) : round(height * 0.68),
        round(width * 0.22) : round(width * 0.78),
    ]
    if not center.size:
        return False
    dark_background = float(np.mean(gray < 35)) >= 0.72
    colorful_center = float(
        np.mean((center[:, :, 1] >= 90) & (center[:, :, 2] >= 100))
    )
    bright_center = float(np.mean(center[:, :, 2] >= 210))
    return dark_background and colorful_center >= 0.035 and bright_center >= 0.045


def _pikmin_publisher_logo_visible(
    image: np.ndarray,
    *,
    hsv: np.ndarray | None = None,
) -> bool:
    """Detect the white Scopely/Nintendo publisher screen by its red plate."""
    height, width = image.shape[:2]
    if hsv is None:
        hsv = cv2.cvtColor(image, cv2.COLOR_BGR2HSV)
    white_ratio = float(
        np.mean((hsv[:, :, 1] <= 35) & (hsv[:, :, 2] >= 215))
    )
    if white_ratio < 0.68:
        return False
    red = cv2.bitwise_or(
        cv2.inRange(hsv, (0, 150, 130), (12, 255, 255)),
        cv2.inRange(hsv, (168, 150, 130), (179, 255, 255)),
    )
    count, _labels, stats, centroids = cv2.connectedComponentsWithStats(red, 8)
    for index in range(1, count):
        _x, _y, component_width, component_height, area = (
            int(value) for value in stats[index]
        )
        center_x, center_y = (float(value) for value in centroids[index])
        fill = area / max(1.0, float(component_width * component_height))
        if (
            width * 0.35 <= component_width <= width * 0.75
            and height * 0.05 <= component_height <= height * 0.16
            and fill >= 0.55
            and abs(center_x / width - 0.50) <= 0.08
            and 0.52 <= center_y / height <= 0.74
        ):
            return True
    return False


def _pikmin_loading_progress_visible(
    image: np.ndarray,
    *,
    hsv: np.ndarray | None = None,
) -> bool:
    """Detect the thin white/teal progress track on Pikmin's loading artwork."""
    height, width = image.shape[:2]
    if hsv is None:
        hsv = cv2.cvtColor(image, cv2.COLOR_BGR2HSV)
    y1, y2 = round(height * 0.82), round(height * 0.90)
    x1, x2 = round(width * 0.07), round(width * 0.93)
    roi = hsv[y1:y2, x1:x2]
    if not roi.size:
        return False
    neutral_bright = (roi[:, :, 1] <= 55) & (roi[:, :, 2] >= 225)
    teal = (
        (roi[:, :, 0] >= 70)
        & (roi[:, :, 0] <= 105)
        & (roi[:, :, 1] >= 70)
        & (roi[:, :, 2] >= 120)
    )
    row_coverage = np.mean(neutral_bright | teal, axis=1)
    if row_coverage.size < 5:
        return False
    peak = float(np.max(row_coverage))
    baseline = float(np.percentile(row_coverage, 35))
    return peak >= 0.62 and peak - baseline >= 0.28


def _pikmin_login_visible(
    image: np.ndarray,
    *,
    hsv: np.ndarray | None = None,
) -> bool:
    """Conservatively detect a logged-out account screen by a wide teal button."""
    height, width = image.shape[:2]
    if hsv is None:
        hsv = cv2.cvtColor(image, cv2.COLOR_BGR2HSV)
    teal = cv2.inRange(hsv, (65, 85, 85), (105, 255, 255))
    count, _labels, stats, centroids = cv2.connectedComponentsWithStats(teal, 8)
    for index in range(1, count):
        _x, _y, component_width, component_height, area = (
            int(value) for value in stats[index]
        )
        center_x, center_y = (float(value) for value in centroids[index])
        aspect = component_width / max(1.0, float(component_height))
        fill = area / max(1.0, float(component_width * component_height))
        if (
            width * 0.38 <= component_width <= width * 0.88
            and height * 0.025 <= component_height <= height * 0.10
            and 4.0 <= aspect <= 18.0
            and fill >= 0.30
            and abs(center_x / width - 0.50) <= 0.10
            and 0.58 <= center_y / height <= 0.90
        ):
            return True
    return False


def pikmin_map_locator_visible(payload: bytes) -> bool:
    image = cv2.imdecode(np.frombuffer(payload or b"", dtype=np.uint8), cv2.IMREAD_COLOR)
    return image is not None and pikmin_map_locator_visible_image(image)


def pikmin_map_locator_visible_image(image: np.ndarray) -> bool:
    """Return whether the map locator is visible in an already-decoded frame."""
    return bool(image is not None and image.size and _pikmin_map_locator_visible(image))


def classify_pikmin_restart_screen(payload: bytes) -> str:
    """Classify the explicit pages used by Pikmin's launch-to-map workflow."""
    image = cv2.imdecode(np.frombuffer(payload or b"", dtype=np.uint8), cv2.IMREAD_COLOR)
    return classify_pikmin_restart_image(image)


def classify_pikmin_restart_image(image: np.ndarray | None) -> str:
    """Classify a decoded scrcpy/ADB frame without a PNG encode/decode cycle."""
    if image is None or not image.size:
        return RESTART_SCREEN_UNKNOWN
    if daily_review_summary_visible(image) or daily_review_mood_visible(image):
        return RESTART_SCREEN_DAILY_REVIEW
    height, width = image.shape[:2]
    if width > 360:
        analysis = cv2.resize(
            image,
            (360, max(1, round(height * 360 / width))),
            interpolation=cv2.INTER_AREA,
        )
    else:
        analysis = image
    analysis_hsv = cv2.cvtColor(analysis, cv2.COLOR_BGR2HSV)
    analysis_gray = cv2.cvtColor(analysis, cv2.COLOR_BGR2GRAY)
    if _pikmin_launch_splash_visible(analysis, hsv=analysis_hsv, gray=analysis_gray):
        return RESTART_SCREEN_SPLASH
    if _pikmin_publisher_logo_visible(analysis, hsv=analysis_hsv):
        return RESTART_SCREEN_LOGO
    # Pikmin renders the confirm control as a very thin anti-aliased outline.
    # At 1080 px it is one connected pill, but resizing to 360 px can erase the
    # vertical sides and split it into two components.  Keep the full-resolution
    # geometry for this detector; the other launch screens have broad shapes
    # that remain stable in the cheaper analysis image.
    if width > 360:
        safety_visible = _pikmin_safety_warning_visible(image)
    else:
        safety_visible = _pikmin_safety_warning_visible(
            analysis,
            hsv=analysis_hsv,
            gray=analysis_gray,
        )
    if safety_visible:
        return RESTART_SCREEN_SAFETY
    if _pikmin_login_visible(analysis, hsv=analysis_hsv):
        return RESTART_SCREEN_LOGIN
    left = _restart_icon_mask(image, 0.098, 0.923)
    left_white_ratio = float(np.count_nonzero(left)) / float(left.size)
    # Popups, the main menu, and garden subviews all expose the same lower-left X.
    if (
        0.025 <= left_white_ratio <= 0.16
        and _restart_icon_is_x(left)
    ):
        return RESTART_SCREEN_CLOSABLE
    # The normal home screen exposes a three-line menu button at lower left.
    if (
        0.07 <= left_white_ratio <= 0.30
        and _restart_icon_is_hamburger(left)
    ):
        return RESTART_SCREEN_HOME

    # The 3D and 2D maps have the two-icon view switcher at bottom centre and a
    # back arrow at bottom left. Its separate bottom-right locator disappears
    # when the map is already centred, so it cannot be the sole map signal.
    if _restart_icon_is_back(left):
        tab_state = _pikmin_map_tab_state(image)
        if tab_state != RESTART_SCREEN_UNKNOWN:
            return tab_state

    # The recap/share button can resemble the locator. A locator by itself
    # cannot prove either that this is a map or that the selected view is 2D.
    # Roads and flower animation can form a bright horizontal band in the same
    # area as the loading track. Confirmed map controls take priority over this
    # broad artwork heuristic, while safety/login overlays still take priority.
    if _pikmin_loading_progress_visible(analysis, hsv=analysis_hsv):
        return RESTART_SCREEN_LOADING
    if float(analysis_gray.mean()) < 10.0:
        # A completely black Unity frame is a transition between visible pages.
        return RESTART_SCREEN_LOADING
    return RESTART_SCREEN_UNKNOWN


class PikminMemoryWatchdog:
    """Poll memory at scan-safe points and restart Pikmin after sustained pressure."""

    def __init__(
        self,
        adb_client: Any,
        *,
        stop_event: threading.Event,
        status: Callable[[str], None] | None = None,
        update: Callable[[dict[str, object]], None] | None = None,
        poll_seconds: float = 10.0,
        pressure_samples: int = 2,
        cooldown_seconds: float = 300.0,
        preventive_restart_seconds: float = DEFAULT_PREVENTIVE_RESTART_SECONDS,
        launch_wait_seconds: float = 45.0,
        transition_wait_seconds: float = 0.75,
        capture_image: Callable[[], np.ndarray] | None = None,
        clock: Callable[[], float] = time.monotonic,
        waiter: Callable[[float], bool] | None = None,
        memory_pressure_recovery: Callable[[], bool] | None = None,
    ) -> None:
        self.adb = adb_client
        self.stop_event = stop_event
        self.status = status
        self.update = update
        self.poll_seconds = max(1.0, float(poll_seconds))
        self.pressure_samples = max(1, int(pressure_samples))
        self.cooldown_seconds = max(0.0, float(cooldown_seconds))
        self.preventive_restart_seconds = max(0.0, float(preventive_restart_seconds))
        self.launch_wait_seconds = max(0.0, float(launch_wait_seconds))
        self.transition_wait_seconds = max(0.0, float(transition_wait_seconds))
        self.capture_image = capture_image
        self.clock = clock
        self.waiter = waiter or self.stop_event.wait
        self.memory_pressure_recovery = memory_pressure_recovery
        self.next_poll_at = 0.0
        self.last_restart_at = float("-inf")
        self.last_memory_recovery_at = float("-inf")
        self.consecutive_pressure = 0
        self.restart_count = 0
        self.last_error = ""

    def _stdout(self, *arguments: str, timeout: float) -> str:
        result = self.adb.run(*arguments, timeout=timeout)
        return str(result.stdout or "")

    def sample(self) -> AndroidMemorySnapshot:
        total_kib, available_kib = parse_proc_meminfo(
            self._stdout("shell", "cat", "/proc/meminfo", timeout=8)
        )
        game_pss_kib = parse_total_pss_kib(
            self._stdout("shell", "dumpsys", "meminfo", PIKMIN_PACKAGE, timeout=15)
        )
        return memory_snapshot(total_kib, available_kib, game_pss_kib)

    def _publish(self, snapshot: AndroidMemorySnapshot) -> None:
        if self.update is not None:
            self.update(snapshot.to_dict())

    def _report(self, message: str) -> None:
        if self.status is not None:
            self.status(message)

    def _wait(self, seconds: float) -> bool:
        return not self.waiter(max(0.0, seconds))

    def _wake_display(self) -> bool:
        # USB-connected phones may report AC charging (MI 9 plug type 1).
        # A USB-only mask leaves mStayOn=false, so cover all charging sources.
        for arguments in (
            ("shell", "svc", "power", "stayon", "true"),
            ("shell", "input", "keyevent", "KEYCODE_WAKEUP"),
            ("shell", "wm", "dismiss-keyguard"),
        ):
            if self.stop_event.is_set():
                return False
            self.adb.run(*arguments, timeout=8)
        return self._wait(min(0.25, self.transition_wait_seconds))

    def _tap(self, x: int, y: int) -> None:
        self._ensure_foreground()
        self.adb.run("shell", "input", "tap", str(x), str(y), timeout=8)

    def _ensure_foreground(self) -> None:
        ensure = getattr(self.adb, "ensure_pikmin_foreground", None)
        if callable(ensure):
            ensure()

    def _restart_screen_state(self) -> tuple[str, bool]:
        if self.capture_image is not None:
            image = self.capture_image()
        else:
            payload = self.adb.capture_png(attempts=2, timeout=20)
            image = cv2.imdecode(np.frombuffer(payload, dtype=np.uint8), cv2.IMREAD_COLOR)
        state = classify_pikmin_restart_image(image)
        if state in (RESTART_SCREEN_UNKNOWN, RESTART_SCREEN_DAILY_REVIEW):
            from .daily_review import read_daily_review

            self._review_action = None
            visible, action = read_daily_review(
                image, continuing=getattr(self, "_review_continuing", False),
            )
            if visible:
                self._review_continuing = True
                self._review_action = action
                state = RESTART_SCREEN_DAILY_REVIEW
        elif state in (RESTART_SCREEN_MAP, RESTART_SCREEN_MAP_3D, RESTART_SCREEN_HOME):
            self._review_continuing = False
        return state, pikmin_map_locator_visible_image(image)

    def ensure_scan_map(self) -> bool:
        """Check before gestures/recognition; True means the grid was reset."""
        if self.stop_event.is_set():
            return False
        # Allow short transitions; no scan gesture is sent while unverified.
        for attempt in range(3):
            state, _ = self._restart_screen_state()
            if state == RESTART_SCREEN_MAP:
                return False
            if state != RESTART_SCREEN_UNKNOWN or attempt == 2:
                break
            if not self._wait(0.15):
                return False
        self._report("搜尋畫面已離開 2D 地圖，暫停滑動並處理今日回顧或其他頁面…")
        if self._open_map_from_current(startup=False):
            return True
        if self.stop_event.is_set():
            return False
        # Import at call time to avoid the existing watchdog/ADB module cycle.
        from .phone_automation import MapRecoveryError

        raise MapRecoveryError("無法關閉目前頁面並確認 2D 地圖，已停止搜尋，避免繼續誤操作。")

    def _wait_for_2d_map(self) -> tuple[bool, bool]:
        """Allow gesture animation and a stale streamed frame before rejecting 2D."""
        attempts = 12 if self.capture_image is not None else 3
        locator_visible = False
        for attempt in range(attempts):
            if self.stop_event.is_set():
                return False, locator_visible
            self._ensure_foreground()
            state, locator_visible = self._restart_screen_state()
            if state == RESTART_SCREEN_MAP:
                return True, locator_visible
            if attempt + 1 < attempts and not self._wait(
                min(0.25, self.transition_wait_seconds)
            ):
                return False, locator_visible
        return False, locator_visible

    def _wait_for_map_camera(self) -> bool:
        """Wait until recenter/zoom/north animations stop accepting map motion."""
        if self.capture_image is None:
            return self._wait(max(0.6, self.transition_wait_seconds))
        previous: np.ndarray | None = None
        stable_samples = 0
        for _attempt in range(20):
            image = self.capture_image()
            state = classify_pikmin_restart_image(image)
            if state == RESTART_SCREEN_MAP and previous is not None:
                stable_samples = stable_samples + 1 if map_camera_is_stable(previous, image) else 0
                if stable_samples >= 3:
                    return True
            else:
                stable_samples = 0
            previous = image if state == RESTART_SCREEN_MAP else None
            if not self._wait(min(0.12, self.transition_wait_seconds)):
                return False
        return False

    def _prepare_map_view(self, size: Any, locator_visible: bool, *, startup: bool) -> bool:
        """Normalize an already-open 2D map for recognition."""
        self._report("已進入 2D 地圖，正在以雙指收合取得最大地圖範圍…")
        try:
            self.adb.pinch_zoom_out(repetitions=MAX_MAP_ZOOM_OUT_PINCHES)
        except Exception as exc:
            detail = str(exc).splitlines()[0]
            self._report(f"地圖雙指縮小失敗：{detail}")
            return False
        if not self._wait(self.transition_wait_seconds):
            return False
        verified, _ = self._wait_for_2d_map()
        if not verified:
            self._report("最大化後無法確認仍在 2D 地圖；本次不會開始辨識。")
            return False

        # The locator is hidden when already centred. Use its state from the
        # frame before zooming so an empty coordinate is never tapped.
        if locator_visible:
            self._report("最大地圖範圍已完成，正在按右下角定位按鈕回到自身位置…")
            self.adb.recenter(size)
            if not self._wait(self.transition_wait_seconds):
                return False
            verified, _ = self._wait_for_2d_map()
            if not verified:
                self._report("定位後無法確認 2D 地圖仍然開啟。")
                return False

        self._report("正在按右上角指南針，讓地圖北方向上…")
        self._tap(round(size.width * 0.898), round(size.height * 0.063))
        if not self._wait(self.transition_wait_seconds):
            return False
        verified, _ = self._wait_for_2d_map()
        if not verified:
            self._report("調整北向後無法確認 2D 地圖仍然開啟。")
            return False
        if not self._wait_for_map_camera():
            self._report("地圖定位或北向動畫沒有停止；本次不會開始辨識。")
            return False

        if not startup:
            self.restart_count += 1
        self.last_restart_at = self.clock()
        if startup:
            self._report("Pikmin Bloom 已準備完成：2D、最大範圍、定位完成且北方向上。")
        else:
            self._report("Pikmin Bloom 已重新準備為 2D 最大範圍並完成定位與北向，正在恢復目前搜尋格。")
        return True

    def restart_game(self, *, startup: bool = False, reason: str = "memory") -> bool:
        """Restart Pikmin and prepare a north-up, maximum-range 2D map."""
        if startup:
            self._report("手機辨識即將開始，正在先重新啟動 Pikmin Bloom…")
        elif reason == "foreground":
            self._report("Pikmin Bloom 已意外退出，正在重新啟動遊戲並準備 2D 地圖…")
        elif reason == "preventive":
            minutes = self.preventive_restart_seconds / 60.0
            self._report(
                f"Pikmin Bloom 已連續運行 {minutes:g} 分鐘，"
                "正在預防性重啟以避開 Unity／GPU 當機…"
            )
        else:
            self._report("手機記憶體接近上限，正在重新啟動 Pikmin Bloom…")
        self.adb.run("shell", "am", "force-stop", PIKMIN_PACKAGE, timeout=12)
        if not self._wait(min(0.5, self.transition_wait_seconds)):
            return False
        # A long scan may let Android enter Doze while the USB connection stays
        # alive. Wake and keep the display on over USB before launching Unity;
        # otherwise screencap returns valid but completely black frames.
        if not self._wake_display():
            return False
        self.adb.run(
            "shell",
            "monkey",
            "-p",
            PIKMIN_PACKAGE,
            "-c",
            "android.intent.category.LAUNCHER",
            "1",
            timeout=15,
        )
        return self._open_map_from_current(startup=startup)

    def _open_map_from_current(self, *, startup: bool) -> bool:
        """Advance only recognized navigation controls, including night recap."""
        size = self.adb.screen_size()
        phase = "wait_startup"
        safety_tap_attempts = 0
        home_tap_attempts = 0
        map_switch_attempts = 0
        review_tap_attempts = 0
        last_state = ""
        # scrcpy can sample quickly enough to react within a frame or two. ADB
        # screenshot fallback uses fewer attempts because each screencap itself
        # is slow. launch_wait_seconds is now a timeout allowance, not a fixed
        # sleep paid even when the screen is already ready.
        maximum_attempts = 80 if self.capture_image is not None else 24
        restart_deadline = self.clock() + max(12.0, self.launch_wait_seconds + 20.0)
        for _attempt in range(maximum_attempts):
            state, locator_visible = self._restart_screen_state()
            if state != last_state:
                state_messages = {
                    RESTART_SCREEN_SPLASH: "偵測到 Pikmin Bloom 啟動畫面，正在等待商標頁…",
                    RESTART_SCREEN_LOGO: "偵測到 Scopely／Nintendo 商標，正在等待加載頁…",
                    RESTART_SCREEN_LOADING: "偵測到 Pikmin Bloom 加載畫面，正在等待完成…",
                    RESTART_SCREEN_CLOSABLE: "偵測到重啟後的彈窗或子畫面，正在關閉…",
                    RESTART_SCREEN_SAFETY: "偵測到可選的安全警示，正在按下確認…",
                    RESTART_SCREEN_DAILY_REVIEW: "偵測到今日回顧，正在辨識跳過／下一步／完成按鈕…",
                    RESTART_SCREEN_HOME: "已確認 Pikmin Bloom 主畫面。",
                    RESTART_SCREEN_MAP_3D: "已進入 3D 地圖，正在準備切換為 2D 地圖…",
                    RESTART_SCREEN_MAP: "已確認 Pikmin Bloom 2D 地圖。",
                }
                if state == RESTART_SCREEN_UNKNOWN:
                    if phase == "wait_startup":
                        message = "啟動畫面正在轉場，等待下一個可確認的頁面…"
                    elif phase == "await_3d":
                        message = "指南針已按下，正在等待可確認的 3D 地圖…"
                    else:
                        message = "3D／2D 切換中，正在等待可確認的 2D 地圖…"
                else:
                    message = state_messages.get(state)
                if message:
                    self._report(message)
                last_state = state

            if state == RESTART_SCREEN_DAILY_REVIEW:
                action = getattr(self, "_review_action", None)
                if review_tap_attempts >= 8:
                    self._report("今日回顧已達操作上限，停止搜尋並保留目前畫面。")
                    return False
                if action is not None:
                    if self.stop_event.is_set():
                        return False
                    self._tap(*action)
                    review_tap_attempts += 1
                phase = "wait_startup"
                if self.clock() >= restart_deadline or not self._wait(self.transition_wait_seconds):
                    return False
                continue

            if state == RESTART_SCREEN_SAFETY:
                if safety_tap_attempts >= 3:
                    self._report("安全警示已嘗試確認 3 次，但畫面仍未關閉。")
                    return False
                self._tap(round(size.width * 0.50), round(size.height * 0.534))
                safety_tap_attempts += 1
                phase = "wait_startup"
                if not self._wait(self.transition_wait_seconds):
                    return False
                continue

            if state == RESTART_SCREEN_LOGIN:
                self._report("偵測到 Pikmin Bloom 登入頁；請先在手機完成登入，再開始搜尋。")
                return False

            if phase == "wait_startup":
                if state == RESTART_SCREEN_CLOSABLE:
                    self._tap(round(size.width * 0.098), round(size.height * 0.923))
                elif state == RESTART_SCREEN_HOME:
                    self._report("正在按主畫面中間底部的指南針，進入 3D 地圖…")
                    self._tap(round(size.width * 0.50), round(size.height * 0.923))
                    home_tap_attempts = 1
                    phase = "await_3d"
                elif state == RESTART_SCREEN_MAP_3D:
                    # Some Android builds restore directly to the last opened
                    # 3D view after launch. Continue from the same explicit step.
                    phase = "await_3d"
                    last_state = ""
                    continue
                elif state == RESTART_SCREEN_MAP:
                    # Resume is already in 2D; do not toggle it back to 3D.
                    phase = "map_2d"

            elif phase == "await_3d":
                if state == RESTART_SCREEN_HOME:
                    if home_tap_attempts >= 3:
                        self._report("已多次按下主畫面指南針，但仍未進入 3D 地圖。")
                        return False
                    self._report("主畫面仍未切換，正在再次按下底部指南針…")
                    self._tap(round(size.width * 0.50), round(size.height * 0.923))
                    home_tap_attempts += 1
                elif state == RESTART_SCREEN_CLOSABLE:
                    self._tap(round(size.width * 0.098), round(size.height * 0.923))
                elif state == RESTART_SCREEN_MAP:
                    phase = "map_2d"
                elif state == RESTART_SCREEN_MAP_3D:
                    self._report("正在按中間底部的 3D／2D 切換，進入 2D 地圖…")
                    self._tap(round(size.width * 0.551), round(size.height * 0.923))
                    map_switch_attempts = 1
                    phase = "await_2d"

            elif phase == "await_2d":
                if state == RESTART_SCREEN_MAP:
                    phase = "map_2d"
                if state == RESTART_SCREEN_HOME:
                    phase = "wait_startup"
                    last_state = ""
                    continue
                if state == RESTART_SCREEN_CLOSABLE:
                    self._tap(round(size.width * 0.098), round(size.height * 0.923))
                elif state == RESTART_SCREEN_MAP_3D and map_switch_attempts < 3:
                    self._report("尚未確認 2D 地圖，正在再次按下 3D／2D 切換…")
                    self._tap(round(size.width * 0.551), round(size.height * 0.923))
                    map_switch_attempts += 1

            if phase == "map_2d":
                return self._prepare_map_view(size, locator_visible, startup=startup)

            if not self._wait(self.transition_wait_seconds):
                return False
            if self.clock() >= restart_deadline:
                break

        self._report("Pikmin Bloom 已重啟，但無法確認完整地圖已開啟；本次不會恢復辨識。")
        return False

    def recover_after_foreground_loss(self) -> bool:
        """Recover a crashed/backgrounded game to a known centered map."""
        if self.stop_event.is_set():
            return False
        restarted = self.restart_game(reason="foreground")
        if restarted:
            self.consecutive_pressure = 0
            self.next_poll_at = self.clock() + self.poll_seconds
        return restarted

    def recover_usb_connection(self, controller: UsbDeviceController) -> DeviceResponse:
        """At a scan checkpoint, free game RAM if ordinary reconnects fail.

        Leave Pikmin stopped until the caller reapplies the waypoint and calls
        recover_after_usb_service_restart(), which rebuilds the map. Relaunching
        it here could reclaim LocWarp again before location has been restored.
        """
        try:
            return reconnect_usb(controller, stop_event=self.stop_event, status=self.status)
        except DeviceControlError:
            if self.stop_event.is_set():
                raise
        self._report("USB 三次重連仍失敗，正在暫停 Pikmin Bloom 釋放記憶體，再重建控制服務…")
        try:
            self.adb.run("shell", "am", "force-stop", PIKMIN_PACKAGE, timeout=12)
        except Exception as exc:
            raise DeviceControlError(f"USB 完整恢復無法釋放遊戲記憶體：{exc}") from exc
        if self.stop_event.is_set():
            raise DeviceControlError("USB 控制恢復已取消。")
        # One escalation only. restart_locwarp has its own finite reconnect
        # budget and does not call this recovery method recursively.
        return restart_locwarp(
            controller, stop_event=self.stop_event, status=self.status, reason="usb",
        )

    def recover_after_usb_service_restart(self) -> bool:
        """Restore the map after Android restarts LocWarp but leaves Pikmin alive.

        The game map was already normalized before the service loss, so another
        force-stop and full zoom sequence adds risk without improving recovery.
        If Pikmin was killed in the same low-memory event, fall back to the
        existing full foreground recovery.
        """
        if self.stop_event.is_set():
            return False
        try:
            if self._restore_live_map_after_usb_restart():
                return True
        except Exception as exc:
            self._report(f"USB 地圖快速恢復失敗：{str(exc).splitlines()[0]}")
        if self.stop_event.is_set():
            return False
        self._report("USB 恢復途中無法確認地圖，改用完整遊戲恢復…")
        return self.recover_after_foreground_loss()

    def _restore_live_map_after_usb_restart(self) -> bool:
        self._report("USB 控制服務已重啟，正在確認 Pikmin 地圖狀態…")
        if not self._wake_display():
            return False
        verified, locator_visible = self._wait_for_2d_map()
        if not verified:
            return False

        size = self.adb.screen_size()
        if locator_visible:
            self._report("USB 已恢復，正在回到目前節點的角色定位中心…")
            self.adb.recenter(size)
            if not self._wait(self.transition_wait_seconds):
                return False
            verified, _ = self._wait_for_2d_map()
            if not verified:
                self._report("USB 恢復後定位時無法確認 2D 地圖仍然開啟。")
                return False

        self._report("USB 已恢復，正在重新確認地圖北方向上…")
        self._tap(round(size.width * 0.898), round(size.height * 0.063))
        if not self._wait(self.transition_wait_seconds):
            return False
        verified, _ = self._wait_for_2d_map()
        if not verified:
            self._report("USB 恢復後調整北向時無法確認 2D 地圖仍然開啟。")
            return False
        if not self._wait_for_map_camera():
            self._report("USB 恢復後地圖動畫沒有停止，無法安全接續辨識。")
            return False
        self._ensure_foreground()
        self.consecutive_pressure = 0
        self.next_poll_at = self.clock() + self.poll_seconds
        self._report("USB 控制與 2D 地圖均已恢復，正在接續目前搜尋格。")
        return True

    def prepare_for_scan(self, *, force_restart: bool = False) -> bool:
        """Build a known full-map viewport before the first recognition frame."""
        if self.stop_event.is_set():
            return False
        ready = False
        if not force_restart:
            # The fast path must also wake and keep the display on. Otherwise
            # a dark screenshot unnecessarily triggers a full game restart.
            if not self._wake_display():
                return False
            try:
                state, locator_visible = self._restart_screen_state()
            except Exception as exc:
                self._report(f"快速檢查目前畫面失敗，改用完整重啟：{str(exc).splitlines()[0]}")
            else:
                if state == RESTART_SCREEN_MAP:
                    self._report("Pikmin Bloom 已在 2D 地圖，略過完整重啟並直接準備搜尋畫面。")
                    ready = self._prepare_map_view(
                        self.adb.screen_size(), locator_visible, startup=True
                    )
        if not ready:
            ready = self.restart_game(startup=True)
        if ready:
            self.consecutive_pressure = 0
            self.next_poll_at = self.clock() + self.poll_seconds
        return ready

    def before_capture(self, _point: object | None = None) -> bool:
        """Return True when a restart reset the map to its center."""
        now = self.clock()
        if now < self.next_poll_at or self.stop_event.is_set():
            return False
        self.next_poll_at = now + self.poll_seconds
        if (
            self.preventive_restart_seconds > 0
            and self.last_restart_at != float("-inf")
            and now - self.last_restart_at >= self.preventive_restart_seconds
        ):
            restarted = self.restart_game(reason="preventive")
            self.consecutive_pressure = 0
            self.next_poll_at = self.clock() + self.poll_seconds
            return restarted
        try:
            snapshot = self.sample()
            self.last_error = ""
        except Exception as exc:
            message = str(exc).splitlines()[0]
            if message != self.last_error:
                self._report(f"手機記憶體監控暫時無法取樣：{message}")
                self.last_error = message
            return False

        self.consecutive_pressure = self.consecutive_pressure + 1 if snapshot.pressure else 0
        snapshot = replace(
            snapshot,
            consecutive_pressure=self.consecutive_pressure,
            restart_count=self.restart_count,
        )
        self._publish(snapshot)
        if not snapshot.pressure:
            return False

        game_pressure = (
            snapshot.game_pss_mib is not None
            and snapshot.game_pss_mib >= snapshot.game_pss_threshold_mib
        )
        critical = game_pressure and snapshot.available_mib < MIN_AVAILABLE_MIB
        last_recovery = self.last_restart_at if game_pressure else max(
            self.last_restart_at, self.last_memory_recovery_at,
        )
        cooldown_remaining = max(0.0, self.cooldown_seconds - (now - last_recovery))
        game_text = "未取得" if snapshot.game_pss_mib is None else f"{snapshot.game_pss_mib:.0f} MB"
        cooldown_text = f"（恢復冷卻剩餘 {cooldown_remaining:.0f} 秒）" if cooldown_remaining else ""
        self._report(
            f"手機記憶體壓力 {min(self.consecutive_pressure, self.pressure_samples)}/{self.pressure_samples}："
            f"可用 {snapshot.available_mib:.0f}/{snapshot.total_mib:.0f} MB，"
            f"Pikmin {game_text}。{cooldown_text}"
        )
        if self.consecutive_pressure < self.pressure_samples and not critical:
            return False
        if cooldown_remaining:
            return False

        if critical:
            self._report("可用記憶體已低於 256 MB 且 Pikmin 占用偏高，立即重啟遊戲釋放記憶體…")
        self._publish(replace(snapshot, restarting=True))
        if self.memory_pressure_recovery is not None and not game_pressure:
            restarted = self.memory_pressure_recovery()
            if restarted:
                # Restarting LocWarp does not reset Pikmin's age. Otherwise
                # repeated USB recoveries postpone the preventive game restart
                # forever while the game's memory keeps growing.
                self.last_memory_recovery_at = self.clock()
        else:
            restarted = self.restart_game()
        self.consecutive_pressure = 0
        self.next_poll_at = self.clock() + self.poll_seconds
        self._publish(replace(
            snapshot,
            pressure=False,
            pressure_reason="",
            consecutive_pressure=0,
            restart_count=self.restart_count,
            restarting=False,
        ))
        return restarted
