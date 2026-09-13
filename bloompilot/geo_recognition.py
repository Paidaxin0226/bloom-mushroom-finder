from __future__ import annotations

import json
import math
import threading
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Sequence

import numpy as np

from .device_control import (
    DeviceControlError,
    DeviceResponse,
    UsbDeviceController,
    reconnect_usb,
    restart_locwarp,
)
from .geo_scan import (
    GeoScanPoint,
    GeoScanSummary,
    offset_coordinate,
    wait_for_arrival,
)
from .paths import RECOGNITION_RUNS_DIR
from .project_defaults import (
    MAX_GEO_SEQUENCE_SEARCH_POINTS,
    MAX_GEO_SEQUENCE_RADIUS,
    MAX_GEO_SEQUENCE_WAYPOINTS,
    VIEWPORT_WIDTH_M,
)
from .viewport_geo import estimate_detection_coordinate
from .route_coverage import coverage_radii_from_km
from .memory_watchdog import PikminMemoryWatchdog
from .detection import filter_terrain_false_positives
from .phone_automation import (
    AdbClient,
    AutomationError,
    CaptureOutcome,
    SCREEN_STABLE_SECONDS,
    ScanPoint,
    ScanSummary,
    _decode_png,
    _encode_png,
    _unique_run_directory,
    atomic_write_bytes,
    atomic_write_json,
    detections_from_result,
    normalize_model_class_names,
    resolve_target_selection,
    run_rectangular_scan,
    restore_recognition_map,
)

StatusCallback = Callable[[str], None]
ProgressCallback = Callable[[int, int, ScanPoint], None]
SequenceProgressCallback = Callable[[int, int, dict[str, object]], None]
PreviewCallback = Callable[[np.ndarray], None]
DetectionCallback = Callable[[object, list[dict[str, object]]], None]
MemoryUpdateCallback = Callable[[dict[str, object]], None]
PauseCheckpoint = Callable[[], bool]
USB_CONTROL_KEEPALIVE_SECONDS = 15.0
USB_CONTROL_KEEPALIVE_TIMEOUT_SECONDS = 3.0
USB_CONTROL_FINISH_TIMEOUT_SECONDS = 8.0


def _is_usb_disconnect(error: DeviceControlError) -> bool:
    message = str(error)
    return any(
        marker in message
        for marker in (
            "USB 控制連線中斷",
            "USB 控制連線已關閉",
            "Android 已關閉 USB 控制連線",
            "尚未連接 Android USB 控制",
        )
    )


def _command_with_reconnect(
    controller: UsbDeviceController,
    command: str,
    payload: dict[str, object] | None = None,
    *,
    require_ok: bool = True,
    status: StatusCallback | None = None,
    stop_event: threading.Event | None = None,
) -> DeviceResponse:
    try:
        return controller.command(command, payload, require_ok=require_ok)
    except DeviceControlError as exc:
        if not _is_usb_disconnect(exc):
            raise
        reconnect_usb(controller, stop_event=stop_event, status=status)
        if stop_event is not None and stop_event.is_set():
            raise DeviceControlError("USB 控制恢復已取消。")
        response = controller.command(command, payload, require_ok=require_ok)
        if status is not None:
            status("USB 控制已重新連接，繼續目前序列。")
        return response


def _keep_usb_control_alive(
    controller: UsbDeviceController,
    stop_event: threading.Event,
    report: StatusCallback,
) -> None:
    failure_reported = False
    while not stop_event.wait(USB_CONTROL_KEEPALIVE_SECONDS):
        try:
            # Heartbeats must not start a long reconnect during shutdown.
            # Foreground sequence commands still reconnect when needed.
            if controller.keepalive(
                idle_seconds=USB_CONTROL_KEEPALIVE_SECONDS,
                timeout=USB_CONTROL_KEEPALIVE_TIMEOUT_SECONDS,
            ):
                failure_reported = False
        except Exception as exc:
            if not stop_event.is_set() and not failure_reported:
                report(f"USB 控制心跳暫時失敗，稍後會再試：{exc}")
                failure_reported = True


def _finish_sequence_movement(
    controller: UsbDeviceController,
    keepalive_stop: threading.Event,
    keepalive_thread: threading.Thread,
    *,
    origin: tuple[float, float] | None,
    return_to_origin: bool,
    speed_kmh: float,
    report: StatusCallback,
) -> dict[str, object]:
    deadline = time.monotonic() + USB_CONTROL_FINISH_TIMEOUT_SECONDS
    keepalive_stop.set()
    returning = return_to_origin and origin is not None
    command = "teleport" if returning else "stop"
    action = "回到最初座標" if returning else "停止定位移動"
    report(f"序列結束，正在{action}（最多等候 {USB_CONTROL_FINISH_TIMEOUT_SECONDS:g} 秒）…")
    # One shared deadline covers joining the heartbeat, waiting for either USB
    # lock, sending and receiving. Do not leave a teleport running in a worker
    # after the job is finished, or start an unbounded reconnect here.
    keepalive_thread.join(timeout=min(2.0, max(0.0, deadline - time.monotonic())))
    payload = (
        {"lat": origin[0], "lng": origin[1], "speedKmh": speed_kmh}
        if returning else None
    )
    try:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise DeviceControlError("等候 USB 控制通道逾時。")
        response = controller.command(command, payload, timeout=remaining)
        if not response.ok:
            raise DeviceControlError(response.message or "Android 拒絕了控制指令。")
    except Exception as exc:
        message = f"無法確認已{action}：{exc}；將繼續儲存辨識結果。"
        report(message)
        return {"command": command, "ok": False, "message": message}
    message = f"已{action}。"
    report(message)
    return {"command": command, "ok": True, "message": message}


@dataclass(frozen=True)
class GeoRecognitionWaypoint:
    id: str
    name: str
    lat: float
    lng: float
    wait_seconds: float = 10.0
    horizontal_radius: int = 3
    vertical_radius: int = 4
    spacing_m: float = 100.0
    left_radius: int | None = None
    right_radius: int | None = None
    up_radius: int | None = None
    down_radius: int | None = None
    range_unit: str = "km"
    left_km: float | None = None
    right_km: float | None = None
    up_km: float | None = None
    down_km: float | None = None
    horizontal_step_km: float | None = None
    vertical_step_km: float | None = None
    coverage_source: str | None = None
    settlement_class: str | None = None
    settlement_id: str | None = None
    settlement_match: str | None = None
    settlement_distance_km: float | None = None
    scan_spans: tuple[tuple[int, int, int], ...] | None = None

    @property
    def resolved_radii(self) -> tuple[int, int, int, int]:
        distances = (self.left_km, self.right_km, self.up_km, self.down_km)
        if any(value is not None for value in distances):
            if any(value is None for value in distances):
                raise ValueError("使用 km 範圍時必須同時提供左、右、上、下四個方向。")
            return coverage_radii_from_km(
                float(self.left_km),
                float(self.right_km),
                float(self.up_km),
                float(self.down_km),
                horizontal_step_km=self.horizontal_step_km,
                vertical_step_km=self.vertical_step_km,
            )
        return (
            self.horizontal_radius if self.left_radius is None else self.left_radius,
            self.horizontal_radius if self.right_radius is None else self.right_radius,
            self.vertical_radius if self.up_radius is None else self.up_radius,
            self.vertical_radius if self.down_radius is None else self.down_radius,
        )

    @property
    def point_count(self) -> int:
        if self.scan_spans is not None:
            return sum(int(end_x) - int(start_x) + 1 for _y, start_x, end_x in self.scan_spans)
        left, right, up, down = self.resolved_radii
        return (left + right + 1) * (up + down + 1)

    @property
    def horizontal_spacing_m(self) -> float:
        return (
            float(self.horizontal_step_km) * 1_000.0
            if self.horizontal_step_km is not None
            else self.spacing_m
        )

    @property
    def vertical_spacing_m(self) -> float:
        return (
            float(self.vertical_step_km) * 1_000.0
            if self.vertical_step_km is not None
            else self.spacing_m
        )


@dataclass(frozen=True)
class GeoSequenceSummary:
    waypoint_count: int
    completed_waypoints: int
    total_points: int
    visited: int
    detection_count: int
    capture_failures: int
    completed: bool
    stop_reason: str
    skipped_waypoints: int = 0
    skipped_points: int = 0
    coordinate_recovery_count: int = 0
    coordinate_recovery_seconds: float = 0.0
    final_movement_message: str = ""


def _current_coordinate(status: dict[str, object]) -> tuple[float, float] | None:
    current = status.get("current")
    if not isinstance(current, dict):
        return None
    try:
        lat = float(current["lat"])
        lng = float(current["lng"])
    except (KeyError, TypeError, ValueError):
        return None
    return (lat, lng) if -90 <= lat <= 90 and -180 <= lng <= 180 else None


def _safe_stem(value: str) -> str:
    normalized = "".join(character if character.isalnum() else "_" for character in value)
    return normalized.strip("_")[:40] or "waypoint"


def validate_geo_waypoints(
    waypoints: Sequence[GeoRecognitionWaypoint],
    *,
    maximum_points: int = MAX_GEO_SEQUENCE_SEARCH_POINTS,
) -> int:
    if not waypoints:
        raise ValueError("座標序列至少需要一個節點。")
    if len(waypoints) > MAX_GEO_SEQUENCE_WAYPOINTS:
        raise ValueError(f"座標序列最多只能有 {MAX_GEO_SEQUENCE_WAYPOINTS} 個節點。")
    total_points = 0
    seen_ids: set[str] = set()
    for index, waypoint in enumerate(waypoints, start=1):
        if not waypoint.id.strip() or len(waypoint.id) > 100:
            raise ValueError(f"第 {index} 個節點的 ID 無效。")
        if waypoint.id in seen_ids:
            raise ValueError(f"節點 ID 重複：{waypoint.id}")
        seen_ids.add(waypoint.id)
        if not -90 <= waypoint.lat <= 90 or not -180 <= waypoint.lng <= 180:
            raise ValueError(f"第 {index} 個節點的經緯度超出範圍。")
        if not 0 <= waypoint.wait_seconds <= 3600:
            raise ValueError(f"第 {index} 個節點的等待秒數必須介於 0 到 3600。")
        if any(not 0 <= radius <= MAX_GEO_SEQUENCE_RADIUS for radius in waypoint.resolved_radii):
            raise ValueError(
                f"第 {index} 個節點換算後的左、右、上、下搜尋格數必須介於 "
                f"0 到 {MAX_GEO_SEQUENCE_RADIUS}。"
            )
        distances = (waypoint.left_km, waypoint.right_km, waypoint.up_km, waypoint.down_km)
        if any(value is not None for value in distances) and any(
            value is None or not math.isfinite(float(value)) or not 0 <= float(value) <= 10_000
            for value in distances
        ):
            raise ValueError(f"第 {index} 個節點的四向 km 範圍必須介於 0 到 10000。")
        if not 0 < waypoint.horizontal_spacing_m <= 100_000:
            raise ValueError(f"第 {index} 個節點的水平格距無效。")
        if not 0 < waypoint.vertical_spacing_m <= 100_000:
            raise ValueError(f"第 {index} 個節點的垂直格距無效。")
        if waypoint.scan_spans is not None:
            left, right, up, down = waypoint.resolved_radii
            if not waypoint.scan_spans:
                raise ValueError(f"第 {index} 個節點的有效格帶不能是空的。")
            intervals_by_row: dict[int, list[tuple[int, int]]] = {}
            includes_center = False
            for span_index, span in enumerate(waypoint.scan_spans, start=1):
                if not isinstance(span, (list, tuple)) or len(span) != 3:
                    raise ValueError(
                        f"第 {index} 個節點的第 {span_index} 個有效格帶格式錯誤。"
                    )
                if any(isinstance(value, bool) or not isinstance(value, int) for value in span):
                    raise ValueError(
                        f"第 {index} 個節點的第 {span_index} 個有效格帶必須使用整數。"
                    )
                row_y, start_x, end_x = span
                if not -down <= row_y <= up or not -left <= start_x <= end_x <= right:
                    raise ValueError(
                        f"第 {index} 個節點的第 {span_index} 個有效格帶超出四向範圍。"
                    )
                intervals_by_row.setdefault(row_y, []).append((start_x, end_x))
                includes_center = includes_center or (
                    row_y == 0 and start_x <= 0 <= end_x
                )
            for intervals in intervals_by_row.values():
                intervals.sort()
                for previous, current in zip(intervals, intervals[1:]):
                    if current[0] <= previous[1]:
                        raise ValueError(f"第 {index} 個節點的有效格帶彼此重疊。")
            if not includes_center:
                raise ValueError(f"第 {index} 個節點的有效格帶必須包含中心格。")
        total_points += waypoint.point_count
    if total_points > maximum_points:
        raise ValueError(f"序列總搜尋點數 {total_points} 超過上限 {maximum_points}。")
    return total_points


def run_geographic_recognition_sequence(
    *,
    controller: UsbDeviceController,
    adb: Path,
    serial: str,
    model_path: Path,
    waypoints: Sequence[GeoRecognitionWaypoint],
    waypoint_started: Callable[[dict[str, object]], threading.Event] | None = None,
    waypoint_finished: Callable[[], bool] | None = None,
    sequence_name: str = "座標序列",
    settle_seconds: float = 4.0,
    confidence: float,
    image_size: int,
    swipe_duration_ms: int = 800,
    movement_mode: str = "teleport",
    speed_kmh: float = 10.8,
    use_path: bool = False,
    navigation_timeout_seconds: float = 300.0,
    return_to_origin: bool = True,
    save_all_frames: bool = False,
    bookmark_detection: bool = True,
    target_names: Sequence[str] | None = None,
    participant_count: int | None = None,
    filter_provider: Callable[[], dict[str, object] | None] | None = None,
    output_root: Path = RECOGNITION_RUNS_DIR,
    output_prefix: str = "geo_sequence",
    stop_event: threading.Event | None = None,
    status: StatusCallback | None = None,
    progress: SequenceProgressCallback | None = None,
    preview: PreviewCallback | None = None,
    detection_update: DetectionCallback | None = None,
    memory_update: MemoryUpdateCallback | None = None,
    pause_checkpoint: PauseCheckpoint | None = None,
    coordinate_scale: dict[str, float | int] | None = None,
    capture_png: Callable[[], bytes] | None = None,
    capture_image: Callable[[], np.ndarray] | None = None,
    precise_coordinates: bool = False,
) -> tuple[Path, GeoSequenceSummary]:
    if not controller.connected:
        raise AutomationError("尚未連接 Android USB 控制。")
    if not model_path.is_file():
        raise AutomationError(f"找不到模型：{model_path}")
    if not 0 < confidence < 1:
        raise AutomationError("信心門檻必須介於 0 與 1。")
    if image_size < 32:
        raise AutomationError("YOLO 尺寸太小。")
    if settle_seconds < 0:
        raise AutomationError("格點等待時間不能小於 0。")
    if movement_mode not in {"teleport", "navigate"}:
        raise AutomationError("移動模式必須是 teleport 或 navigate。")
    if (waypoint_started is None) != (waypoint_finished is None):
        raise ValueError("節點控制必須同時提供開始與結束回呼。")
    total_points = validate_geo_waypoints(waypoints)

    import torch
    from ultralytics import YOLO
    from ultralytics.cfg import DEFAULT_CFG_DICT

    from .badge_number import (
        enrich_detections_with_badge_numbers,
        filter_detections_by_participant_count,
        normalize_participant_count_filter,
    )
    from .detection import detections_with_crops, draw_detection_records

    def report(message: str) -> None:
        if status is not None:
            status(message)

    event = stop_event or threading.Event()
    adb_client = AdbClient(adb, serial)
    adb_client.ensure_ready()
    coordinate_resolver = None
    if precise_coordinates:
        from .mushroom_coordinate import MushroomCoordinateResolver

        coordinate_resolver = MushroomCoordinateResolver(
            adb_client,
            controller,
            stop_event=event,
            capture_image=capture_image,
            status=report,
        )
    origin_response = _command_with_reconnect(
        controller,
        "status",
        status=report,
        stop_event=event,
    )
    origin = _current_coordinate(origin_response.status)
    first_waypoint = waypoints[0]
    first_label = first_waypoint.name.strip() or "節點 1"
    report(f"正在先注入第一個目標位置 {first_label}，再重新啟動 Pikmin Bloom…")
    _command_with_reconnect(
        controller,
        "teleport",
        {
            "lat": first_waypoint.lat,
            "lng": first_waypoint.lng,
            "speedKmh": speed_kmh,
        },
        status=report,
        stop_event=event,
    )
    initial_position_generation = getattr(controller, "connection_generation", 0)
    memory_watchdog = PikminMemoryWatchdog(
        adb_client,
        stop_event=event,
        status=report,
        update=memory_update,
        capture_image=capture_image,
        memory_pressure_recovery=lambda: restart_locwarp(
            controller, stop_event=event, status=report,
        ).ok,
    )
    foreground_recovery = getattr(
        memory_watchdog, "recover_after_foreground_loss", None
    )
    if not callable(foreground_recovery):
        foreground_recovery = lambda: memory_watchdog.prepare_for_scan(
            force_restart=True
        )
    if not memory_watchdog.prepare_for_scan(force_restart=True):
        raise AutomationError("序列辨識開始前無法完成遊戲重啟與最大範圍地圖準備。")
    run_dir = _unique_run_directory(output_root, output_prefix)
    matches_original = run_dir / "matches" / "original"
    matches_annotated = run_dir / "matches" / "annotated"
    frames_dir = run_dir / "frames"
    records_path = run_dir / "detections.jsonl"
    started_at = datetime.now(timezone.utc)
    matched_frames = 0

    device: int | str = 0 if torch.cuda.is_available() else "cpu"
    device_name = torch.cuda.get_device_name(0) if device == 0 else "CPU"
    if status is not None:
        status(f"正在載入序列搜尋模型（{device_name}）…")
    model = YOLO(str(model_path))
    available_classes = normalize_model_class_names(model.names)
    resolved_target_names, selected_class_ids = resolve_target_selection(
        available_classes,
        target_names,
    )
    required_participant_count = normalize_participant_count_filter(participant_count)
    target_label = ", ".join(resolved_target_names) if resolved_target_names else "全部目標"
    count_label = "不限" if required_participant_count is None else str(required_participant_count)
    if status is not None:
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

    visited = 0
    detection_count = 0
    capture_failures = 0
    consecutive_capture_failures = 0
    completed_waypoints = 0
    skipped_waypoints = 0
    skipped_points = 0
    skipped_records: list[dict[str, object]] = []
    stop_reason = "cancelled"
    keepalive_stop = threading.Event()

    keepalive_thread = threading.Thread(
        target=_keep_usb_control_alive,
        args=(controller, keepalive_stop, report),
        name="BloomPilotUsbKeepAlive",
        daemon=True,
    )
    keepalive_thread.start()

    try:
        for waypoint_index, waypoint in enumerate(waypoints):
            if event.is_set():
                break
            if pause_checkpoint is not None and pause_checkpoint():
                report("已返回暫停前座標，繼續原本序列。")
            if event.is_set():
                break
            label = waypoint.name.strip() or f"節點 {waypoint_index + 1}"
            waypoint_detail = {
                "sequence_index": waypoint_index,
                "waypoint_id": waypoint.id,
                "waypoint_name": label,
                "total_points": total_points,
            }
            waypoint_event = waypoint_started(waypoint_detail) if waypoint_started else event

            def scan_waypoint() -> ScanSummary | None:
                connection_generation = initial_position_generation
                if waypoint_event.is_set():
                    return None
                report(f"序列 {waypoint_index + 1}/{len(waypoints)}：正在前往 {label}…")
                center_point = GeoScanPoint(
                    ordinal=0,
                    ring=0,
                    grid_x=0,
                    grid_y=0,
                    lat=waypoint.lat,
                    lng=waypoint.lng,
                )
                if waypoint_index > 0:
                    _command_with_reconnect(
                        controller,
                        movement_mode,
                        {
                            "lat": waypoint.lat,
                            "lng": waypoint.lng,
                            "speedKmh": speed_kmh,
                            "usePath": use_path,
                        },
                        status=report,
                        stop_event=waypoint_event,
                    )
                    connection_generation = getattr(controller, "connection_generation", 0)
                if waypoint_index > 0 and movement_mode == "navigate" and not wait_for_arrival(
                    controller,
                    center_point,
                    waypoint_event,
                    timeout_seconds=navigation_timeout_seconds,
                    tolerance_m=4.0,
                ):
                    return None
                report(
                    f"已到達 {label}；等待 {waypoint.wait_seconds:g} 秒後開始附近搜尋…"
                )
                if waypoint_event.wait(waypoint.wait_seconds):
                    return None

                waypoint_stem = f"wp_{waypoint_index + 1:03d}_{_safe_stem(waypoint.id)}"
                active_point_ordinal = -1
                point_frame_index = 0
                precise_cache = None
                if coordinate_resolver is not None:
                    from .mushroom_coordinate import PreciseCoordinateCache

                    precise_cache = PreciseCoordinateCache()

                def capture_current_frame() -> bytes:
                    nonlocal capture_failures, consecutive_capture_failures
                    try:
                        payload = capture_png() if capture_png is not None else adb_client.capture_png()
                    except AutomationError as exc:
                        capture_failures += 1
                        consecutive_capture_failures += 1
                        report(
                            f"序列 {waypoint_index + 1}/{len(waypoints)} · {label}："
                            "完整畫面取樣重試後仍失敗，已略過本次取樣"
                            f"（連續 {consecutive_capture_failures}/3）：{exc}"
                        )
                        if consecutive_capture_failures >= 3:
                            raise AutomationError(
                                "ADB 已連續 3 個搜尋格無法擷取完整畫面，為避免無效地跑完整條序列，已停止辨識。"
                            ) from exc
                        return b""
                    consecutive_capture_failures = 0
                    return payload

                def recognize(point: ScanPoint, payload: bytes) -> CaptureOutcome:
                    nonlocal matched_frames, active_point_ordinal, point_frame_index, detection_count
                    if not payload:
                        return CaptureOutcome()
                    if point.ordinal != active_point_ordinal:
                        active_point_ordinal = point.ordinal
                        point_frame_index = 0
                        if precise_cache is not None:
                            precise_cache.clear()
                    point_frame_index += 1
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
                    height, width = frame.shape[:2]
                    if coordinate_resolver is not None:
                        from .mushroom_coordinate import enrich_detections_with_precise_coordinates

                        assert precise_cache is not None
                        unresolved = precise_cache.pending(detections, frame)
                        recheck_args = dict(frame_predict_args)
                        recheck_args.pop("classes", None)
                        for detection in unresolved:
                            if waypoint_event.is_set():
                                detection["coordinate_error"] = "已略過此節點剩餘的精確座標擷取。"
                                continue
                            # Finish returning from the current target page,
                            # then skip opening any remaining target pages.
                            enrich_detections_with_precise_coordinates(
                                [detection], coordinate_resolver,
                                reference_image=frame,
                                detect_targets=lambda image: filter_terrain_false_positives(
                                    image, detections_from_result(model.predict(image, **recheck_args)[0]),
                                ),
                                recover_map=lambda: restore_recognition_map(
                                    adb_client, memory_watchdog, point, stop_event=event,
                                    swipe_duration_ms=swipe_duration_ms, status=report,
                                ),
                                failure_dir=run_dir / "coordinate_failures",
                            )
                        precise_cache.remember(unresolved, frame)
                    else:
                        for detection in detections:
                            xyxy = detection.get("xyxy")
                            if isinstance(xyxy, (list, tuple)):
                                try:
                                    detection["estimated_coordinate"] = estimate_detection_coordinate(
                                        center_lat=waypoint.lat,
                                        center_lng=waypoint.lng,
                                        viewport_width_m=(
                                            None if coordinate_scale is not None else VIEWPORT_WIDTH_M
                                        ),
                                        screen_width=width,
                                        screen_height=height,
                                        grid_x=point.grid_x,
                                        grid_y=point.grid_y,
                                        xyxy=xyxy,
                                        **(coordinate_scale or {}),
                                    )
                                except (TypeError, ValueError, OverflowError):
                                    pass
                    annotated, _ = draw_detection_records(detections, frame)
                    point_lat, point_lng = offset_coordinate(
                        waypoint.lat,
                        waypoint.lng,
                        point.grid_x * waypoint.horizontal_spacing_m,
                        point.grid_y * waypoint.vertical_spacing_m,
                    )
                    point_detail = {
                        **asdict(point),
                        "lat": point_lat,
                        "lng": point_lng,
                    }
                    location = {
                        "sequence_index": waypoint_index,
                        "waypoint_id": waypoint.id,
                        "waypoint_name": label,
                        "center": {"lat": waypoint.lat, "lng": waypoint.lng},
                        "point": point_detail,
                    }
                    event_detections = detections_with_crops(frame, detections)
                    if detection_update is not None and event_detections:
                        detection_update(location, event_detections)
                    if preview is not None:
                        preview(annotated)

                    file_stem = (
                        f"{waypoint_stem}_{point.stem}_change_{point_frame_index:03d}"
                    )
                    if save_all_frames:
                        atomic_write_bytes(frames_dir / f"{file_stem}.png", payload)
                    if detections:
                        original_path = matches_original / f"{file_stem}.png"
                        annotated_path = matches_annotated / f"{file_stem}.png"
                        atomic_write_bytes(original_path, payload)
                        atomic_write_bytes(annotated_path, _encode_png(annotated))
                        record = {
                            "captured_at": datetime.now(timezone.utc).isoformat(),
                            "sequence_index": waypoint_index,
                            "waypoint_id": waypoint.id,
                            "waypoint_name": label,
                            "center_lat": waypoint.lat,
                            "center_lng": waypoint.lng,
                            "ordinal": point.ordinal,
                            "ring": point.step,
                            "grid_x": point.grid_x,
                            "grid_y": point.grid_y,
                            "change_frame": point_frame_index,
                            "lat": point_lat,
                            "lng": point_lng,
                            "original": original_path.relative_to(run_dir).as_posix(),
                            "annotated": annotated_path.relative_to(run_dir).as_posix(),
                            "detections": detections,
                        }
                        matched_frames += 1
                        records_path.parent.mkdir(parents=True, exist_ok=True)
                        with records_path.open("a", encoding="utf-8") as stream:
                            stream.write(json.dumps(record, ensure_ascii=False) + "\n")
                        if bookmark_detection:
                            if coordinate_resolver is not None:
                                for detection in detections:
                                    coordinate = detection.get("precise_coordinate")
                                    if not isinstance(coordinate, dict):
                                        continue
                                    _command_with_reconnect(
                                        controller,
                                        "addBookmark",
                                        {
                                            "name": (
                                                f"{label} · YOLO · "
                                                f"{str(detection.get('class_name', 'target'))}"
                                            ),
                                            "lat": coordinate.get("lat"),
                                            "lng": coordinate.get("lng"),
                                        },
                                        require_ok=False,
                                        status=report,
                                    )
                            else:
                                labels = sorted(
                                    {str(item.get("class_name", "target")) for item in detections}
                                )
                                _command_with_reconnect(
                                    controller,
                                    "addBookmark",
                                    {
                                        "name": f"{label} · YOLO · " + ", ".join(labels),
                                        "lat": point_lat,
                                        "lng": point_lng,
                                    },
                                    require_ok=False,
                                    status=report,
                                )
                    detection_count += len(detections)
                    return CaptureOutcome(
                        detection_count=len(detections),
                    )

                completed_before_waypoint = visited + skipped_points

                def before_capture(point: ScanPoint) -> bool:
                    nonlocal connection_generation
                    reset = memory_watchdog.before_capture(point)
                    if waypoint_event.is_set():
                        return reset
                    if not controller.connected:
                        _command_with_reconnect(
                            controller, "status", status=report, stop_event=waypoint_event,
                        )
                    generation = getattr(controller, "connection_generation", 0)
                    if generation != connection_generation:
                        # Recreating LocWarp's process can lose mock-location
                        # state. Recover the socket, location and map as one
                        # transaction. Android can reclaim LocWarp again while
                        # Pikmin is loading, so retry the whole transaction.
                        for recovery_round in range(1, 4):
                            if waypoint_event.is_set():
                                return reset
                            if recovery_round == 1:
                                report("USB 控制已恢復，正在重建目前節點定位與搜尋地圖…")
                            else:
                                report(
                                    "USB 在地圖恢復期間再次中斷，正在重試完整恢復"
                                    f"（{recovery_round}/3）…"
                                )
                            try:
                                if not controller.connected:
                                    reconnect_usb(
                                        controller, stop_event=waypoint_event, status=report,
                                    )
                                _command_with_reconnect(
                                    controller, "teleport",
                                    {"lat": waypoint.lat, "lng": waypoint.lng, "speedKmh": speed_kmh},
                                    status=report, stop_event=waypoint_event,
                                )
                                position_generation = getattr(controller, "connection_generation", 0)
                                usb_map_recovery = getattr(
                                    memory_watchdog, "recover_after_usb_service_restart", None
                                )
                                recovered = (
                                    usb_map_recovery()
                                    if callable(usb_map_recovery)
                                    else memory_watchdog.prepare_for_scan()
                                )
                            except DeviceControlError:
                                recovered = False
                                position_generation = -1
                            if (
                                recovered
                                and controller.connected
                                and getattr(controller, "connection_generation", 0)
                                == position_generation
                            ):
                                connection_generation = position_generation
                                return True
                        raise AutomationError(
                            "USB 控制、目前節點定位與搜尋地圖連續 3 次無法同時恢復，已停止辨識。"
                        )
                    return reset

                def swipe_progress(done: int, _total: int, point: ScanPoint) -> None:
                    if progress is not None:
                        progress(
                            completed_before_waypoint + done,
                            total_points,
                            {
                                "sequence_index": waypoint_index,
                                "waypoint_id": waypoint.id,
                                "waypoint_name": label,
                                "point": asdict(point),
                            },
                        )

                area_label = "有效聚落格" if waypoint.scan_spans is not None else "長方形範圍"
                report(
                    f"序列 {waypoint_index + 1}/{len(waypoints)}："
                    f"正在以手機地圖滑動搜尋 {label} 的{area_label}…"
                )
                left_radius, right_radius, up_radius, down_radius = waypoint.resolved_radii
                return run_rectangular_scan(
                    adb_client,
                    ensure_map=getattr(memory_watchdog, "ensure_scan_map", None),
                    horizontal_radius=waypoint.horizontal_radius,
                    vertical_radius=waypoint.vertical_radius,
                    left_radius=left_radius,
                    right_radius=right_radius,
                    up_radius=up_radius,
                    down_radius=down_radius,
                    settle_seconds=settle_seconds,
                    swipe_duration_ms=swipe_duration_ms,
                    stop_event=waypoint_event,
                    on_capture=recognize,
                    capture_png=capture_current_frame,
                    detect_screen_changes=True,
                    before_capture=before_capture,
                    recover_foreground=lambda: (
                        not waypoint_event.is_set()
                        and foreground_recovery()
                    ),
                    pause_checkpoint=pause_checkpoint,
                    status=lambda message: report(
                        f"序列 {waypoint_index + 1}/{len(waypoints)} · {label}：{message}"
                    ),
                    progress=swipe_progress,
                    scan_spans=waypoint.scan_spans,
                )
            try:
                local_summary = scan_waypoint()
            except DeviceControlError:
                if not waypoint_event.is_set():
                    raise
                local_summary = None
            finally:
                skipped = waypoint_finished() if waypoint_finished else False
            if local_summary is not None:
                visited += local_summary.captured
            if event.is_set():
                break
            if skipped:
                skipped_waypoints += 1
                remaining_points = waypoint.point_count - (local_summary.captured if local_summary else 0)
                skipped_points += remaining_points
                skipped_records.append({**waypoint_detail, "skipped_points": remaining_points})
                next_action = (
                    "正在前往下一個節點。"
                    if waypoint_index + 1 < len(waypoints)
                    else "已是最後一個節點，序列結束。"
                )
                report(f"已跳過 {label} 的剩餘辨識；{next_action}")
                if progress is not None:
                    progress(visited + skipped_points, total_points, {**waypoint_detail, "skipped": True})
                continue
            if local_summary is None or not local_summary.completed:
                break
            completed_waypoints += 1
        else:
            stop_reason = "cancelled" if event.is_set() else "completed"
    finally:
        final_movement = _finish_sequence_movement(
            controller, keepalive_stop, keepalive_thread,
            origin=origin, return_to_origin=return_to_origin,
            speed_kmh=speed_kmh, report=report,
        )

    report("正在儲存辨識結果與序列摘要…")
    sequence_summary = GeoSequenceSummary(
        waypoint_count=len(waypoints),
        completed_waypoints=completed_waypoints,
        total_points=total_points,
        visited=visited,
        detection_count=detection_count,
        capture_failures=capture_failures,
        completed=stop_reason == "completed",
        stop_reason=stop_reason,
        skipped_waypoints=skipped_waypoints,
        skipped_points=skipped_points,
        coordinate_recovery_count=coordinate_resolver.recovery_count if coordinate_resolver else 0,
        coordinate_recovery_seconds=round(coordinate_resolver.recovery_seconds, 2) if coordinate_resolver else 0.0,
        final_movement_message=str(final_movement.get("message", "")),
    )
    finished_at = datetime.now(timezone.utc)
    if not records_path.exists():
        records_path.parent.mkdir(parents=True, exist_ok=True)
        records_path.write_text("", encoding="utf-8")
    atomic_write_json(
        run_dir / "summary.json",
        {
            "started_at": started_at.isoformat(),
            "finished_at": finished_at.isoformat(),
            "model": str(model_path.resolve()),
            "serial": serial,
            "device": device_name,
            "sequence": {
                "name": sequence_name,
                "waypoints": [asdict(waypoint) for waypoint in waypoints],
            },
            "settings": {
                "settle_seconds": settle_seconds,
                "confidence": confidence,
                "image_size": image_size,
                "scan_mode": (
                    "screen_swipe_polygon_mask"
                    if any(waypoint.scan_spans is not None for waypoint in waypoints)
                    else "screen_swipe_rectangle"
                ),
                "capture_mode": "screen_change",
                "screen_stable_seconds": SCREEN_STABLE_SECONDS,
                "swipe_duration_ms": swipe_duration_ms,
                "movement_mode": movement_mode,
                "speed_kmh": speed_kmh,
                "use_path": use_path,
                "return_to_origin": return_to_origin,
                "save_all_frames": save_all_frames,
                "bookmark_detection": bookmark_detection,
                "target_names": resolved_target_names or "all",
                "participant_count": (
                    "any" if required_participant_count is None else required_participant_count
                ),
            },
            "scan": asdict(sequence_summary),
            "final_movement": final_movement,
            "skipped_waypoints": skipped_records,
            "matched_frames": matched_frames,
            "coordinate_recovery": {
                "count": coordinate_resolver.recovery_count if coordinate_resolver else 0,
                "seconds": round(coordinate_resolver.recovery_seconds, 2) if coordinate_resolver else 0,
            },
            "memory_watchdog": {
                "enabled": True,
                "restart_count": memory_watchdog.restart_count,
                "poll_seconds": memory_watchdog.poll_seconds,
                "pressure_samples": memory_watchdog.pressure_samples,
            },
        },
    )
    return run_dir, sequence_summary


def run_geographic_recognition_scan(
    *,
    controller: UsbDeviceController,
    adb: Path,
    serial: str,
    model_path: Path,
    center_lat: float,
    center_lng: float,
    horizontal_radius: int,
    vertical_radius: int,
    spacing_m: float,
    settle_seconds: float,
    confidence: float,
    image_size: int,
    left_radius: int | None = None,
    right_radius: int | None = None,
    up_radius: int | None = None,
    down_radius: int | None = None,
    movement_mode: str = "teleport",
    speed_kmh: float = 10.8,
    use_path: bool = False,
    navigation_timeout_seconds: float = 300.0,
    return_to_origin: bool = True,
    save_all_frames: bool = False,
    bookmark_detection: bool = True,
    target_names: Sequence[str] | None = None,
    participant_count: int | None = None,
    output_root: Path = RECOGNITION_RUNS_DIR,
    stop_event: threading.Event | None = None,
    status: StatusCallback | None = None,
    progress: ProgressCallback | None = None,
    preview: PreviewCallback | None = None,
    detection_update: DetectionCallback | None = None,
    memory_update: MemoryUpdateCallback | None = None,
    pause_checkpoint: PauseCheckpoint | None = None,
) -> tuple[Path, GeoScanSummary]:
    waypoint = GeoRecognitionWaypoint(
        id="center",
        name="單一中心",
        lat=center_lat,
        lng=center_lng,
        wait_seconds=settle_seconds,
        horizontal_radius=horizontal_radius,
        vertical_radius=vertical_radius,
        spacing_m=spacing_m,
        left_radius=left_radius,
        right_radius=right_radius,
        up_radius=up_radius,
        down_radius=down_radius,
    )

    def adapt_progress(done: int, total: int, detail: dict[str, object]) -> None:
        if progress is None:
            return
        point_value = detail.get("point")
        if isinstance(point_value, dict):
            filtered = {
                key: point_value[key]
                for key in ("ordinal", "direction", "step", "grid_x", "grid_y")
                if key in point_value
            }
            progress(done, total, ScanPoint(**filtered))

    run_dir, sequence_summary = run_geographic_recognition_sequence(
        controller=controller,
        adb=adb,
        serial=serial,
        model_path=model_path,
        waypoints=[waypoint],
        sequence_name="單一中心",
        settle_seconds=settle_seconds,
        confidence=confidence,
        image_size=image_size,
        swipe_duration_ms=800,
        movement_mode=movement_mode,
        speed_kmh=speed_kmh,
        use_path=use_path,
        navigation_timeout_seconds=navigation_timeout_seconds,
        return_to_origin=return_to_origin,
        save_all_frames=save_all_frames,
        bookmark_detection=bookmark_detection,
        target_names=target_names,
        participant_count=participant_count,
        output_root=output_root,
        output_prefix="geo_recognition",
        stop_event=stop_event,
        status=status,
        progress=adapt_progress,
        preview=preview,
        detection_update=detection_update,
        memory_update=memory_update,
        pause_checkpoint=pause_checkpoint,
    )
    return run_dir, GeoScanSummary(
        visited=sequence_summary.visited,
        detection_count=sequence_summary.detection_count,
        completed=sequence_summary.completed,
        stop_reason=sequence_summary.stop_reason,
    )
