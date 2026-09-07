from __future__ import annotations

import argparse
import json
import re
import shutil
import struct
import subprocess
import sys
import time
from collections import Counter
from dataclasses import dataclass
from pathlib import Path

import cv2
import numpy as np

# Allow direct execution from scripts without depending on the caller's cwd.
PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from bloompilot.device_control import DeviceControlError, UsbDeviceController
from bloompilot.phone_automation import (
    AdbClient,
    ScanPoint,
    ScreenSize,
    SwipeGesture,
    _decode_png,
    atomic_write_bytes,
    atomic_write_json,
    detections_from_result,
    gesture_for_direction,
    next_capture_index,
    slugify_city,
)


@dataclass(frozen=True)
class CitySpec:
    name: str
    lat: float
    lng: float
    left: int
    right: int
    up: int
    down: int

    @property
    def width(self) -> int:
        return self.left + self.right + 1

    @property
    def height(self) -> int:
        return self.up + self.down + 1

    @property
    def count(self) -> int:
        return self.width * self.height


BATCH_TOKEN = "adaptive1000"
MINIMUM_PNG_BYTES = 500_000
MINIMUM_SWIPE_DIFFERENCE = 1.5
SCRCPY_PATH = PROJECT_ROOT / "vendor" / "scrcpy" / "scrcpy.exe"
MAP_ZOOM_HELPER = Path(__file__).with_name("zoom_pikmin_map_out.ps1")
PIKMIN_PACKAGE = "com.nianticlabs.pikmin"
MEMORY_TRIM_AVAILABLE = True
CITY_SPECS = (
    CitySpec("taipei", 25.0330, 121.5654, 4, 3, 3, 3),
    CitySpec("tokyo", 35.6812, 139.7671, 5, 3, 4, 3),
    CitySpec("osaka", 34.7025, 135.4960, 4, 3, 3, 3),
    CitySpec("seoul", 37.5665, 126.9780, 5, 3, 3, 3),
    CitySpec("hong_kong", 22.3193, 114.1694, 4, 3, 2, 3),
    CitySpec("singapore", 1.2903, 103.8520, 3, 4, 3, 3),
    CitySpec("bangkok", 13.7563, 100.5018, 4, 4, 4, 3),
    CitySpec("sydney", -33.8688, 151.2093, 5, 3, 3, 3),
    CitySpec("melbourne", -37.8136, 144.9631, 4, 4, 3, 3),
    CitySpec("new_york", 40.7128, -74.0060, 3, 3, 5, 3),
    CitySpec("san_francisco", 37.7749, -122.4194, 3, 3, 3, 3),
    CitySpec("los_angeles", 34.0522, -118.2437, 6, 3, 3, 3),
    CitySpec("vancouver", 49.2827, -123.1207, 4, 3, 3, 3),
    CitySpec("mexico_city", 19.4326, -99.1332, 5, 3, 4, 3),
    CitySpec("sao_paulo", -23.5505, -46.6333, 6, 3, 4, 3),
    CitySpec("buenos_aires", -34.6037, -58.3816, 5, 3, 4, 3),
    CitySpec("madrid", 40.4168, -3.7038, 4, 3, 3, 3),
    CitySpec("rome", 41.9028, 12.4964, 3, 4, 2, 4),
    CitySpec("amsterdam", 52.3676, 4.9041, 3, 3, 4, 2),
    CitySpec("prague", 50.0755, 14.4378, 3, 3, 2, 4),
    CitySpec("cairo", 30.0444, 31.2357, 5, 3, 2, 4),
    CitySpec("cape_town", -33.9249, 18.4241, 4, 3, 5, 2),
)

class RetryCity(RuntimeError):
    pass


@dataclass(frozen=True)
class RawCapture:
    point: ScanPoint
    path: Path
    signature: np.ndarray


def map_signature(payload: bytes) -> np.ndarray:
    frame = _decode_png(payload)
    height, width = frame.shape[:2]
    roi = frame[
        round(height * 0.07) : round(height * 0.86),
        round(width * 0.02) : round(width * 0.98),
    ]
    gray = cv2.cvtColor(roi, cv2.COLOR_BGR2GRAY)
    return cv2.GaussianBlur(
        cv2.resize(gray, (96, 160), interpolation=cv2.INTER_AREA),
        (11, 11),
        0,
    )


def signature_difference(first: np.ndarray, second: np.ndarray) -> float:
    return float(np.mean(cv2.absdiff(first, second).astype(np.float32)))


def valid_game_frame(payload: bytes) -> bool:
    if len(payload) < MINIMUM_PNG_BYTES:
        return False
    frame = _decode_png(payload)
    if frame.shape[:2] != (2340, 1080):
        return False
    green = frame[:, :, 1].astype(np.float32)
    # The full map is predominantly green and does not contain the large white
    # cards from the game's home screen.
    white = np.all(frame > 225, axis=2).astype(np.uint8)
    _count, _labels, stats, _centroids = cv2.connectedComponentsWithStats(white)
    largest_white_component = int(stats[1:, cv2.CC_STAT_AREA].max(initial=0))
    return float(green.mean()) > 35.0 and largest_white_component < 50_000


def full_map_tab_selected(payload: bytes) -> bool:
    """Return whether the right-hand map tab in the bottom selector is active."""
    frame = _decode_png(payload)
    height, width = frame.shape[:2]
    y1, y2 = round(height * 0.882), round(height * 0.958)
    left = frame[y1:y2, round(width * 0.39) : round(width * 0.49)]
    right = frame[y1:y2, round(width * 0.51) : round(width * 0.61)]

    def selected_score(roi: np.ndarray) -> float:
        blue, green, red = cv2.split(roi)
        pale = (
            (red > 190)
            & (green > 190)
            & (blue > 175)
            & (np.abs(red.astype(np.int16) - green.astype(np.int16)) < 45)
        )
        return float(pale.mean())

    left_score = selected_score(left)
    right_score = selected_score(right)
    return right_score >= 0.12 and right_score >= left_score + 0.025


def safety_confirmation_visible(payload: bytes) -> bool:
    """Detect the dimmed startup safety dialog before pressing its OK button."""
    frame = _decode_png(payload)
    height, width = frame.shape[:2]
    dialog = frame[
        round(height * 0.34) : round(height * 0.62),
        round(width * 0.05) : round(width * 0.95),
    ]
    white_ratio = float(np.all(dialog > 225, axis=2).mean())
    hsv = cv2.cvtColor(frame, cv2.COLOR_BGR2HSV)
    dimmed_top = hsv[
        round(height * 0.08) : round(height * 0.30),
        round(width * 0.05) : round(width * 0.95),
        2,
    ]
    return white_ratio >= 0.75 and float(dimmed_top.mean()) <= 115.0


def capture_png_fast(adb: AdbClient) -> bytes:
    """Capture raw RGBA over USB and encode PNG locally."""
    result = adb.run("exec-out", "screencap", timeout=20, binary=True)
    raw = bytes(result.stdout)
    if len(raw) < 16:
        raise RetryCity("手機原始截圖資料不完整")
    width, height, pixel_format, _dataspace = struct.unpack_from("<IIII", raw)
    expected = 16 + width * height * 4
    if pixel_format != 1 or len(raw) != expected:
        raise RetryCity(
            f"不支援的手機截圖格式：{width}x{height}, format={pixel_format}, "
            f"bytes={len(raw)}"
        )
    rgba = np.frombuffer(raw, dtype=np.uint8, offset=16).reshape(height, width, 4)
    bgr = cv2.cvtColor(rgba, cv2.COLOR_RGBA2BGR)
    encoded, png = cv2.imencode(".png", bgr, [cv2.IMWRITE_PNG_COMPRESSION, 1])
    if not encoded:
        raise RetryCity("手機截圖無法編碼為 PNG")
    return png.tobytes()


def capture_valid(adb: AdbClient) -> bytes:
    payload = capture_png_fast(adb)
    if not valid_game_frame(payload):
        raise RetryCity("手機畫面不是有效的 Pikmin 地圖")
    return payload


def wait_for_valid_game_frame(adb: AdbClient, timeout: float = 60.0) -> bytes:
    deadline = time.monotonic() + timeout
    last_error: Exception | None = None
    while time.monotonic() < deadline:
        try:
            return capture_valid(adb)
        except RetryCity as exc:
            last_error = exc
            time.sleep(3.0)
    raise RetryCity(f"等待 Pikmin 地圖逾時：{last_error}")


def run_maximum_zoom(serial: str) -> None:
    if not SCRCPY_PATH.is_file():
        raise RetryCity(f"找不到 scrcpy：{SCRCPY_PATH}")
    if not MAP_ZOOM_HELPER.is_file():
        raise RetryCity(f"找不到雙指縮小工具：{MAP_ZOOM_HELPER}")
    try:
        result = subprocess.run(
            [
                "powershell.exe",
                "-NoProfile",
                "-ExecutionPolicy",
                "Bypass",
                "-File",
                str(MAP_ZOOM_HELPER),
                "-ScrcpyPath",
                str(SCRCPY_PATH),
                "-Serial",
                serial,
                "-Repetitions",
                "4",
            ],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=45,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise RetryCity(f"無法執行雙指縮小工具：{exc}") from exc
    if result.returncode != 0:
        detail = (result.stderr or result.stdout).strip()
        raise RetryCity(f"雙指縮小失敗：{detail or '未知錯誤'}")
    print(result.stdout.strip(), flush=True)


def recover_full_map(adb: AdbClient, screen: ScreenSize, serial: str) -> bytes:
    """Follow the user-approved home -> overview -> full-map sequence."""
    try:
        current = capture_png_fast(adb)
        if valid_game_frame(current) and full_map_tab_selected(current):
            run_maximum_zoom(serial)
            return settle_map(adb, capture_valid(adb), initial_wait=2.0, timeout=8.0)
    except (RetryCity, cv2.error):
        pass

    adb.run(
        "shell",
        "monkey",
        "-p",
        PIKMIN_PACKAGE,
        "-c",
        "android.intent.category.LAUNCHER",
        "1",
        timeout=15,
    )
    time.sleep(15.0)

    startup = capture_png_fast(adb)
    if safety_confirmation_visible(startup):
        print("SAFETY_CONFIRMATION detected", flush=True)
        adb.run(
            "shell",
            "input",
            "tap",
            str(round(screen.width * 0.50)),
            str(round(screen.height * 0.549)),
            timeout=8,
        )
        time.sleep(12.0)

    # Main screen: bottom-center button opens the overhead view.
    adb.run(
        "shell",
        "input",
        "tap",
        str(round(screen.width * 0.50)),
        str(round(screen.height * 0.923)),
        timeout=8,
    )
    time.sleep(6.0)

    # Overhead view: the right icon in the bottom-center selector opens the map.
    adb.run(
        "shell",
        "input",
        "tap",
        str(round(screen.width * 0.551)),
        str(round(screen.height * 0.923)),
        timeout=8,
    )
    time.sleep(7.0)
    payload = capture_png_fast(adb)
    if not valid_game_frame(payload) or not full_map_tab_selected(payload):
        raise RetryCity("依序點擊兩次底部中央後，仍未進入完整地圖")

    run_maximum_zoom(serial)
    payload = settle_map(adb, capture_valid(adb), initial_wait=2.0, timeout=10.0)
    if not full_map_tab_selected(payload):
        raise RetryCity("雙指縮小後已離開完整地圖")
    return payload


def wait_for_usb_reconnect(
    adb_path: Path,
    serial: str,
    timeout: float = 45.0,
) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        controller = UsbDeviceController(adb_path, serial)
        try:
            controller.connect()
            controller.status()
            return True
        except DeviceControlError:
            time.sleep(3.0)
        finally:
            controller.close()
    return False


def request_game_memory_trim(adb: AdbClient) -> None:
    """Ask Pikmin to release unused map resources without changing the UI."""
    global MEMORY_TRIM_AVAILABLE
    if not MEMORY_TRIM_AVAILABLE:
        return
    try:
        adb.run(
            "shell",
            "am",
            "send-trim-memory",
            "com.nianticlabs.pikmin",
            "RUNNING_CRITICAL",
            timeout=10,
        )
    except Exception as exc:
        # Android may reject a duplicate/lower-priority trim request. Memory
        # trimming is preventative and must never invalidate captured data.
        MEMORY_TRIM_AVAILABLE = False
        detail = str(exc).splitlines()[0]
        print(f"TRIM_MEMORY_DISABLED={detail}", flush=True)
    time.sleep(2.0)


def settle_map(
    adb: AdbClient,
    first_payload: bytes,
    *,
    initial_wait: float,
    timeout: float,
    threshold: float = 3.0,
    stable_required: int = 2,
) -> bytes:
    time.sleep(initial_wait)
    previous = first_payload
    previous_signature = map_signature(previous)
    stable = 0
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        time.sleep(1.0)
        current = capture_valid(adb)
        current_signature = map_signature(current)
        score = signature_difference(previous_signature, current_signature)
        stable = stable + 1 if score <= threshold else 0
        previous = current
        previous_signature = current_signature
        if stable >= stable_required:
            return current
    return previous


def recenter_button_visible(payload: bytes) -> bool:
    frame = _decode_png(payload)
    height, width = frame.shape[:2]
    roi = frame[
        round(height * 0.88) : round(height * 0.97),
        round(width * 0.82) : round(width * 0.98),
    ]
    blue, green, red = cv2.split(roi)
    neutral_white = (
        (red > 210)
        & (green > 210)
        & (blue > 210)
        & (np.abs(red.astype(np.int16) - green.astype(np.int16)) < 20)
        & (np.abs(green.astype(np.int16) - blue.astype(np.int16)) < 20)
    )
    return int(neutral_white.sum()) >= 200


def tap_north_compass(adb: AdbClient, screen: ScreenSize) -> None:
    # The only always-visible map control the user allows us to press.
    adb.run(
        "shell",
        "input",
        "tap",
        str(round(screen.width * 0.901)),
        str(round(screen.height * 0.062)),
        timeout=8,
    )


def tap_recenter_if_visible(
    adb: AdbClient,
    screen: ScreenSize,
    payload: bytes,
) -> bool:
    if not recenter_button_visible(payload):
        return False
    # This control only exists while the player is away from the map center.
    adb.run(
        "shell",
        "input",
        "tap",
        str(round(screen.width * 0.898)),
        str(round(screen.height * 0.923)),
        timeout=8,
    )
    return True


def wait_and_recenter_city_map(
    adb: AdbClient,
    screen: ScreenSize,
    baseline: bytes,
) -> bytes:
    # Coordinate changes and map tiles both lag behind the USB command.
    time.sleep(10.0)
    baseline_signature = map_signature(baseline)
    deadline = time.monotonic() + 45.0
    changed: bytes | None = None
    while time.monotonic() < deadline:
        candidate = capture_valid(adb)
        if tap_recenter_if_visible(adb, screen, candidate):
            time.sleep(7.0)
            changed = capture_valid(adb)
            break
        if signature_difference(baseline_signature, map_signature(candidate)) >= 4.0:
            # Some map states follow the new device position automatically, so
            # the conditional recenter button never appears.
            changed = candidate
            break
        time.sleep(3.0)
    if changed is None:
        raise RetryCity("座標變更後未偵測到地圖更新或右下定位鍵")
    return settle_map(adb, changed, initial_wait=3.0, timeout=12.0)


def gestures_for_screen(size: ScreenSize) -> dict[str, SwipeGesture]:
    return {
        direction: gesture_for_direction(direction, size)
        for direction in ("north", "south", "east", "west")
    }


def build_city_walk(spec: CitySpec) -> list[tuple[int, int]]:
    """Build a continuous cardinal walk covering an asymmetric rectangle.

    The current phone position is (0, 0), which may be off-center. The walk
    reaches the south-west corner first, then fills every row in a snake. Cells
    crossed on the way to the corner may be revisited, but are captured once.
    """
    walk = [(0, 0)]
    current_x, current_y = walk[-1]
    while current_x > -spec.left:
        current_x -= 1
        walk.append((current_x, current_y))
    while current_y > -spec.down:
        current_y -= 1
        walk.append((current_x, current_y))
    for row_index, y in enumerate(range(-spec.down, spec.up + 1)):
        values = range(-spec.left, spec.right + 1)
        if row_index % 2:
            values = range(spec.right, -spec.left - 1, -1)
        for x in values:
            if (x, y) != walk[-1]:
                walk.append((x, y))
    expected = {
        (x, y)
        for x in range(-spec.left, spec.right + 1)
        for y in range(-spec.down, spec.up + 1)
    }
    if set(walk) != expected:
        raise RuntimeError(f"{spec.name} 的矩形走訪未完整覆蓋")
    if any(
        abs(first[0] - second[0]) + abs(first[1] - second[1]) != 1
        for first, second in zip(walk, walk[1:])
    ):
        raise RuntimeError(f"{spec.name} 的矩形走訪不連續")
    return walk


def unique_capture_coordinates(walk: list[tuple[int, int]]) -> list[tuple[int, int]]:
    coordinates: list[tuple[int, int]] = []
    seen: set[tuple[int, int]] = set()
    for coordinate in walk:
        if coordinate not in seen:
            coordinates.append(coordinate)
            seen.add(coordinate)
    return coordinates


def scan_point_for_coordinate(ordinal: int, x: int, y: int) -> ScanPoint:
    return ScanPoint(
        ordinal=ordinal,
        direction="center" if (x, y) == (0, 0) else "grid",
        step=max(abs(x), abs(y)),
        grid_x=x,
        grid_y=y,
    )


def swipe_direction(previous: ScanPoint, current: ScanPoint) -> str:
    dx = current.grid_x - previous.grid_x
    dy = current.grid_y - previous.grid_y
    if abs(dx) + abs(dy) != 1:
        raise RuntimeError(
            f"掃描路徑不連續：({previous.grid_x},{previous.grid_y}) -> "
            f"({current.grid_x},{current.grid_y})"
        )
    if dx == 1:
        return "east"
    if dx == -1:
        return "west"
    if dy == 1:
        return "north"
    return "south"


def swipe_and_capture(
    adb: AdbClient,
    direction: str,
    gesture: SwipeGesture,
    baseline: bytes,
) -> bytes:
    baseline_signature = map_signature(baseline)
    for _attempt in range(2):
        adb.run(
            "shell",
            "input",
            "swipe",
            str(gesture.x1),
            str(gesture.y1),
            str(gesture.x2),
            str(gesture.y2),
            "700",
            timeout=10,
        )
        time.sleep(3.0)
        candidate = capture_valid(adb)
        movement = signature_difference(baseline_signature, map_signature(candidate))
        if movement < MINIMUM_SWIPE_DIFFERENCE:
            time.sleep(2.0)
            candidate = capture_valid(adb)
            movement = signature_difference(baseline_signature, map_signature(candidate))
        if movement >= MINIMUM_SWIPE_DIFFERENCE:
            return settle_map(
                adb,
                candidate,
                initial_wait=0.5,
                timeout=5.0,
                stable_required=1,
            )
    raise RetryCity(f"{direction} 滑動後地圖沒有明顯位移")


def checkpoint_path(city_stage: Path) -> Path:
    return city_stage / "checkpoint.json"


def write_city_checkpoint(
    city_stage: Path,
    spec: CitySpec,
    captures: list[RawCapture],
) -> None:
    last = captures[-1].point if captures else None
    atomic_write_json(
        checkpoint_path(city_stage),
        {
            "city": spec.name,
            "batch": BATCH_TOKEN,
            "complete": len(captures) == spec.count,
            "captured": len(captures),
            "expected": spec.count,
            "last_grid": (
                {"x": last.grid_x, "y": last.grid_y} if last is not None else None
            ),
            "grid": f"{spec.width}x{spec.height}",
            "bounds": {
                "left": spec.left,
                "right": spec.right,
                "up": spec.up,
                "down": spec.down,
            },
            "resumable": True,
            "saved_at_unix": time.time(),
        },
    )


def load_city_checkpoint(
    city_stage: Path,
    spec: CitySpec,
    walk: list[tuple[int, int]],
) -> list[RawCapture]:
    manifest_path = checkpoint_path(city_stage)
    if not manifest_path.is_file():
        return []
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise RuntimeError(f"{spec.name} 檢查點無法讀取：{exc}") from exc
    if manifest.get("city") != spec.name or manifest.get("batch") != BATCH_TOKEN:
        raise RuntimeError(f"{spec.name} 檢查點識別資料不符")

    expected_coordinates = unique_capture_coordinates(walk)
    raw_dir = city_stage / "raw"
    raw_files = sorted(raw_dir.glob("raw_*.png")) if raw_dir.is_dir() else []
    declared_count = int(manifest.get("captured", -1))
    if declared_count != len(raw_files):
        raise RuntimeError(
            f"{spec.name} 檢查點數量不一致：manifest={declared_count}, "
            f"files={len(raw_files)}"
        )
    if not 1 <= len(raw_files) <= spec.count:
        raise RuntimeError(f"{spec.name} 檢查點張數不合理：{len(raw_files)}")

    captures: list[RawCapture] = []
    for ordinal, (path, (x, y)) in enumerate(
        zip(raw_files, expected_coordinates[: len(raw_files)], strict=True)
    ):
        point = scan_point_for_coordinate(ordinal, x, y)
        expected_name = f"raw_{point.ordinal:03d}_{point.stem}.png"
        if path.name != expected_name:
            raise RuntimeError(
                f"{spec.name} 檢查點檔名不連續：{path.name} != {expected_name}"
            )
        payload = path.read_bytes()
        if not valid_game_frame(payload):
            raise RuntimeError(f"{spec.name} 檢查點畫面無效：{path.name}")
        captures.append(RawCapture(point, path, map_signature(payload)))
    return captures


def replay_route_index_for_capture_count(
    walk: list[tuple[int, int]],
    capture_count: int,
) -> int:
    if capture_count < 1:
        return 0
    seen = {(0, 0)}
    if capture_count == 1:
        return 0
    for route_index, coordinate in enumerate(walk[1:], start=1):
        seen.add(coordinate)
        if len(seen) == capture_count:
            return route_index
    raise RuntimeError(f"找不到第 {capture_count} 張對應的矩形路徑位置")


def safe_class_token(names: list[str]) -> str:
    if not names:
        return "clean"
    normalized = [re.sub(r"[^A-Za-z0-9_-]+", "_", name).strip("_") for name in names]
    return "target_" + "-".join(value or "unknown" for value in normalized)


def classify_and_finalize(
    *,
    model: object,
    predict_args: dict[str, object],
    captures: list[RawCapture],
    city: str,
    output_root: Path,
    city_stage: Path,
) -> dict[str, object]:
    output = output_root / city
    output.mkdir(parents=True, exist_ok=True)
    for partial in output.glob(f"*_{BATCH_TOKEN}_*.png"):
        partial.unlink()
    start_index = next_capture_index(output)
    classified: list[tuple[RawCapture, list[dict[str, object]], list[str]]] = []
    class_counts: Counter[str] = Counter()
    for index, capture in enumerate(captures, start=1):
        result = model.predict(_decode_png(capture.path.read_bytes()), **predict_args)[0]
        detections = detections_from_result(result)
        names = sorted({str(item["class_name"]) for item in detections})
        class_counts.update(names)
        classified.append((capture, detections, names))
        if index % 10 == 0 or index == len(captures):
            print(f"CLASSIFY {city} {index}/{len(captures)}", flush=True)

    clean = [item for item in classified if not item[1]]
    positive = [item for item in classified if item[1]]
    ordered = clean + positive
    final_stage = city_stage / "final"
    final_stage.mkdir(parents=True, exist_ok=True)
    staged: list[Path] = []
    for offset, (capture, _detections, names) in enumerate(ordered):
        index = start_index + offset
        category = safe_class_token(names)
        name = (
            f"{city}_bg_{index:04d}_{category}_{BATCH_TOKEN}_"
            f"x{ScanPoint._axis_token(capture.point.grid_x)}_"
            f"y{ScanPoint._axis_token(capture.point.grid_y)}.png"
        )
        destination = final_stage / name
        atomic_write_bytes(destination, capture.path.read_bytes())
        staged.append(destination)
    if len(staged) != len(captures) or any(
        path.stat().st_size < MINIMUM_PNG_BYTES for path in staged
    ):
        raise RuntimeError(f"{city} 最終暫存驗證失敗")
    for path in staged:
        destination = output / path.name
        if destination.exists():
            raise RuntimeError(f"拒絕覆寫：{destination}")
        path.replace(destination)
    return {
        "city": city,
        "count": len(ordered),
        "clean_count": len(clean),
        "positive_count": len(positive),
        "classes": dict(sorted(class_counts.items())),
        "first_index": start_index,
        "last_index": start_index + len(ordered) - 1,
    }


def collect_city(
    *,
    adb: AdbClient,
    controller_adb: Path,
    serial: str,
    spec: CitySpec,
    gestures: dict[str, SwipeGesture],
    screen: ScreenSize,
    city_stage: Path,
) -> list[RawCapture]:
    manifest_path = checkpoint_path(city_stage)
    if city_stage.exists() and not manifest_path.is_file():
        shutil.rmtree(city_stage)
    raw_dir = city_stage / "raw"
    raw_dir.mkdir(parents=True, exist_ok=True)
    walk = build_city_walk(spec)
    captures = load_city_checkpoint(city_stage, spec, walk)
    if captures:
        print(
            f"CITY_RESUME {spec.name} saved={len(captures)}/{spec.count}",
            flush=True,
        )
    baseline = wait_for_valid_game_frame(adb)
    tap_north_compass(adb, screen)
    baseline = settle_map(
        adb,
        baseline,
        initial_wait=2.0,
        timeout=8.0,
        stable_required=1,
    )
    controller = UsbDeviceController(controller_adb, serial)
    try:
        controller.connect()
        controller.command(
            "teleport",
            {"lat": spec.lat, "lng": spec.lng, "speedKmh": 14.0, "usePath": False},
        )
    finally:
        controller.close()
    center_payload = wait_and_recenter_city_map(adb, screen, baseline)
    signatures = [capture.signature for capture in captures]
    captured_coordinates = {
        (capture.point.grid_x, capture.point.grid_y) for capture in captures
    }

    def save(point: ScanPoint, payload: bytes) -> None:
        signature = map_signature(payload)
        if signatures:
            nearest = min(signature_difference(signature, other) for other in signatures)
            if nearest < 0.35:
                raise RetryCity(
                    f"{spec.name} {point.location_label} 與既有畫面過度相似（{nearest:.3f}）"
                )
        path = raw_dir / f"raw_{point.ordinal:03d}_{point.stem}.png"
        atomic_write_bytes(path, payload)
        captures.append(RawCapture(point, path, signature))
        signatures.append(signature)
        captured_coordinates.add((point.grid_x, point.grid_y))
        write_city_checkpoint(city_stage, spec, captures)

    center_point = scan_point_for_coordinate(0, 0, 0)
    previous_point = center_point
    previous_payload = center_payload
    route_index = 0
    if captures:
        route_index = replay_route_index_for_capture_count(walk, len(captures))
        for replayed, (x, y) in enumerate(walk[1 : route_index + 1], start=1):
            point = scan_point_for_coordinate(0, x, y)
            direction = swipe_direction(previous_point, point)
            previous_payload = swipe_and_capture(
                adb,
                direction,
                gestures[direction],
                previous_payload,
            )
            previous_point = point
            if replayed % 10 == 0 or replayed == route_index:
                print(
                    f"CITY_REPLAY {spec.name} {replayed}/{route_index}",
                    flush=True,
                )
    else:
        save(center_point, center_payload)

    for x, y in walk[route_index + 1 :]:
        point = scan_point_for_coordinate(len(captures), x, y)
        direction = swipe_direction(previous_point, point)
        payload = swipe_and_capture(adb, direction, gestures[direction], previous_payload)
        if (x, y) not in captured_coordinates:
            save(point, payload)
            count = len(captures)
            if count % 7 == 0 or count == spec.count:
                print(f"CAPTURE {spec.name} {count}/{spec.count}", flush=True)
                request_game_memory_trim(adb)
        previous_point = point
        previous_payload = payload
    if len(captures) != spec.count:
        raise RuntimeError(f"{spec.name} 只取得 {len(captures)}/{spec.count} 個唯一格點")
    return captures


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--adb", type=Path, required=True)
    parser.add_argument("--serial", required=True)
    parser.add_argument("--model", type=Path, required=True)
    parser.add_argument("--output-root", type=Path, required=True)
    parser.add_argument("--state-root", type=Path, required=True)
    parser.add_argument("--confidence", type=float, default=0.45)
    parser.add_argument("--image-size", type=int, default=1024)
    parser.add_argument("--restore-lat", type=float)
    parser.add_argument("--restore-lng", type=float)
    args = parser.parse_args()

    from ultralytics import YOLO
    from ultralytics.cfg import DEFAULT_CFG_DICT
    import torch

    expected_total = sum(spec.count for spec in CITY_SPECS)
    if expected_total < 1000:
        raise RuntimeError("城市數不足以蒐集 1000 張")
    route_directions: Counter[str] = Counter()
    for spec in CITY_SPECS:
        walk = build_city_walk(spec)
        if len(set(walk)) != spec.count:
            raise RuntimeError(f"{spec.name} 的格點數不符合規格")
        for first, second in zip(walk, walk[1:]):
            route_directions.update(
                [
                    swipe_direction(
                        scan_point_for_coordinate(0, *first),
                        scan_point_for_coordinate(1, *second),
                    )
                ]
            )

    adb = AdbClient(args.adb, args.serial)
    adb.ensure_ready()
    screen = adb.screen_size()
    try:
        initial_payload = wait_for_valid_game_frame(adb, timeout=10.0)
        if not full_map_tab_selected(initial_payload):
            recover_full_map(adb, screen, args.serial)
    except RetryCity:
        recover_full_map(adb, screen, args.serial)
    gestures = gestures_for_screen(screen)
    print(
        "AUTO_SWIPE="
        + json.dumps(
            {
                "screen": screen.__dict__,
                "gestures": {
                    direction: gesture.__dict__
                    for direction, gesture in gestures.items()
                },
                "route_directions": dict(sorted(route_directions.items())),
                "coverage": "city-sized asymmetric rectangles",
                "cities": [
                    {
                        "name": spec.name,
                        "grid": f"{spec.width}x{spec.height}",
                        "left": spec.left,
                        "right": spec.right,
                        "up": spec.up,
                        "down": spec.down,
                        "count": spec.count,
                    }
                    for spec in CITY_SPECS
                ],
                "expected": expected_total,
            },
            ensure_ascii=False,
        ),
        flush=True,
    )

    if args.restore_lat is not None and args.restore_lng is not None:
        origin = (args.restore_lat, args.restore_lng)
    else:
        origin_controller = UsbDeviceController(args.adb, args.serial)
        origin_controller.connect()
        origin_value = origin_controller.status().get("current")
        origin_controller.close()
        if not isinstance(origin_value, dict):
            raise RuntimeError("無法取得手機原始座標")
        origin = (float(origin_value["lat"]), float(origin_value["lng"]))

    device: int | str = 0 if torch.cuda.is_available() else "cpu"
    model = YOLO(str(args.model))
    predict_args: dict[str, object] = {
        "imgsz": args.image_size,
        "conf": args.confidence,
        "device": device,
        "verbose": False,
    }
    if device == 0:
        if "quantize" in DEFAULT_CFG_DICT:
            predict_args["quantize"] = 16
        else:
            predict_args["half"] = True
    model.predict(np.zeros((args.image_size, args.image_size, 3), dtype=np.uint8), **predict_args)

    args.state_root.mkdir(parents=True, exist_ok=True)
    state_path = args.state_root / "state.json"
    results: list[dict[str, object]] = []
    try:
        for city_number, spec in enumerate(CITY_SPECS, start=1):
            city = slugify_city(spec.name)
            output = args.output_root / city
            completed_files = sorted(output.glob(f"*_{BATCH_TOKEN}_*.png")) if output.is_dir() else []
            if len(completed_files) == spec.count:
                clean_count = sum("_clean_" in path.name for path in completed_files)
                result = {
                    "city": city,
                    "count": spec.count,
                    "clean_count": clean_count,
                    "positive_count": spec.count - clean_count,
                    "classes": {},
                    "grid": f"{spec.width}x{spec.height}",
                    "bounds": {
                        "left": spec.left,
                        "right": spec.right,
                        "up": spec.up,
                        "down": spec.down,
                    },
                    "resumed": True,
                }
                results.append(result)
                print(f"SKIP {city_number}/{len(CITY_SPECS)} {city} 已完成", flush=True)
                continue
            for path in completed_files:
                path.unlink()

            print(
                f"CITY {city_number}/{len(CITY_SPECS)} {city} "
                f"({spec.lat:.4f},{spec.lng:.4f}) grid={spec.width}x{spec.height} "
                f"L{spec.left}/R{spec.right}/U{spec.up}/D{spec.down} n={spec.count}",
                flush=True,
            )
            last_error: Exception | None = None
            attempt = 1
            usb_recoveries = 0
            while attempt <= 3:
                city_stage = args.state_root / city
                try:
                    captures = collect_city(
                        adb=adb,
                        controller_adb=args.adb,
                        serial=args.serial,
                        spec=spec,
                        gestures=gestures,
                        screen=screen,
                        city_stage=city_stage,
                    )
                    result = classify_and_finalize(
                        model=model,
                        predict_args=predict_args,
                        captures=captures,
                        city=city,
                        output_root=args.output_root,
                        city_stage=city_stage,
                    )
                    result["grid"] = f"{spec.width}x{spec.height}"
                    result["bounds"] = {
                        "left": spec.left,
                        "right": spec.right,
                        "up": spec.up,
                        "down": spec.down,
                    }
                    results.append(result)
                    shutil.rmtree(city_stage, ignore_errors=True)
                    atomic_write_json(
                        state_path,
                        {
                            "completed": results,
                            "count": sum(int(item["count"]) for item in results),
                        },
                    )
                    print(
                        f"CITY_DONE {city} clean={result['clean_count']} "
                        f"target={result['positive_count']}",
                        flush=True,
                    )
                    break
                except DeviceControlError as exc:
                    usb_recoveries += 1
                    if usb_recoveries > 5:
                        raise RuntimeError(f"{city} USB 控制連續恢復失敗：{exc}") from exc
                    if wait_for_usb_reconnect(args.adb, args.serial):
                        print(f"USB_RECONNECTED {city}", flush=True)
                        continue
                    print(f"USB_RETRY {city} count={usb_recoveries} reason={exc}", flush=True)
                    time.sleep(3.0)
                except RetryCity as exc:
                    last_error = exc
                    print(f"CITY_RETRY {city} attempt={attempt} reason={exc}", flush=True)
                    try:
                        payload = capture_png_fast(adb)
                        if not valid_game_frame(payload) or not full_map_tab_selected(payload):
                            print(f"MAP_RECOVERY {city} 啟動主介面到地圖流程", flush=True)
                            recover_full_map(adb, screen, args.serial)
                    except (RetryCity, cv2.error) as recovery_exc:
                        print(f"MAP_RECOVERY_WARNING={recovery_exc}", flush=True)
                    attempt += 1
            else:
                raise RuntimeError(f"{city} 連續三次蒐集失敗：{last_error}")

        total = sum(int(item["count"]) for item in results)
        clean_total = sum(int(item["clean_count"]) for item in results)
        positive_total = sum(int(item["positive_count"]) for item in results)
        if total < 1000:
            raise RuntimeError(f"最終只有 {total} 張，未達 1000 張")
        print(
            "COLLECTION_1000_RESULT="
            + json.dumps(
                {
                    "count": total,
                    "clean_count": clean_total,
                    "positive_count": positive_total,
                    "cities": results,
                },
                ensure_ascii=False,
            ),
            flush=True,
        )
        return 0
    finally:
        restore = UsbDeviceController(args.adb, args.serial)
        restore_sent = False
        try:
            restore.connect()
            restore.command(
                "teleport",
                {"lat": origin[0], "lng": origin[1], "speedKmh": 14.0},
                require_ok=False,
            )
            restore_sent = True
        except Exception as exc:
            print(f"RESTORE_WARNING={exc}", flush=True)
        finally:
            restore.close()
        if restore_sent:
            try:
                time.sleep(10.0)
                payload = capture_valid(adb)
                tap_north_compass(adb, screen)
                time.sleep(2.0)
                payload = capture_valid(adb)
                if tap_recenter_if_visible(adb, screen, payload):
                    time.sleep(5.0)
            except Exception as exc:
                print(f"RESTORE_MAP_WARNING={exc}", flush=True)


if __name__ == "__main__":
    raise SystemExit(main())
