from __future__ import annotations

import base64
import json
import logging
import math
import os
import queue
import random
import subprocess
import sys
import threading
import time
import traceback
import uuid
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field, is_dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterator

import cv2
import numpy as np
import yaml

from ..artifacts import ArtifactManager
from ..dataset_builder import generate_dataset, load_config
from ..device_control import DeviceControlError, UsbDeviceController
from ..discord_webhook import (
    DiscordWebhookNotifier,
    build_discord_test_payload,
    normalize_discord_webhook_settings,
    post_discord_webhook,
)
from ..discord_bot import discover_bot_channels, post_discord_bot, resolve_bot_channel
from ..device_scale import (
    DeviceScaleCalibration,
    DeviceScaleStore,
    MarkerDisplacement,
    measure_player_marker_displacement,
    new_device_scale_calibration,
    validate_round_trip,
)
from ..geo_recognition import (
    GeoRecognitionWaypoint,
    run_geographic_recognition_sequence,
    validate_geo_waypoints,
)
from ..paths import BACKGROUND_SOURCE_DIR, CONFIG_PATH, PROJECT_ROOT, ROUTES_DIR, TRAINING_CONTROL_DIR
from ..phone_automation import (
    AdbClient,
    _decode_png,
    collect_backgrounds,
    detections_from_result,
    normalize_model_class_names,
    resolve_target_selection,
    run_recognition_scan,
)
from ..detection import detections_with_crops, draw_detection_records
from ..badge_number import (
    enrich_detections_with_badge_numbers,
    filter_detections_by_participant_count,
    normalize_participant_count_filter,
)
from ..project_defaults import INFERENCE_IMAGE_SIZE, VIEWPORT_WIDTH_M, fixed_studio_settings
from ..route_library import RouteLibrary
from ..memory_watchdog import PikminMemoryWatchdog, pikmin_map_locator_visible
from ..monthly_workflow import (
    MonthlyWorkflowStore,
    annotate_training_run,
    collect_fixed_route_backgrounds,
    validate_cycle,
)
from ..monthly_route import ensure_monthly_background_route
from ..train import list_generated_datasets, resolve_dataset
from ..phone_tools import ScrcpyControlWindow, find_toolchain, list_adb_devices
from ..phone_stream import ScrcpyFrameSource
from ..settings_store import load_studio_settings, save_studio_settings
from ..training_control import TrainingControl
from ..training_output import sanitize_training_output
from ..viewport_geo import coordinate_from_offset
from .events import EventHub

LOGGER = logging.getLogger(__name__)


# The player marker only needs to move far enough for optical flow to separate
# it from map animation.  A large probe is actively harmful: the map is fixed
# during calibration, so an inaccurate initial scale estimate can send the
# marker outside the captured frame.  Keep the first-time probe deliberately
# short and use the vertical axis, where a portrait screen has the most room.
SCALE_PROBE_TARGET_PIXELS = 72.0
SCALE_PROBE_MIN_METERS = 24.0
SCALE_PROBE_MAX_METERS = 65.0
SCALE_PROBE_TIMEOUT_SECONDS = 15.0
SCALE_PROBE_POLL_SECONDS = 0.75


@contextmanager
def _recognition_frame_source(
    toolchain: Any,
    serial: str,
    stop_event: threading.Event,
    status: Callable[[str], None],
) -> Iterator[ScrcpyFrameSource | None]:
    """Prefer scrcpy video frames, with transparent ADB screenshot fallback."""
    source = _open_recognition_frame_source(toolchain, serial, stop_event, status)
    try:
        yield source
    finally:
        if source is not None:
            source.close()


def _open_recognition_frame_source(
    toolchain: Any,
    serial: str,
    stop_event: threading.Event,
    status: Callable[[str], None],
) -> ScrcpyFrameSource | None:
    source: ScrcpyFrameSource | None = None
    if all(getattr(toolchain, name, None) for name in ("adb", "server", "version")):
        client = AdbClient(toolchain.adb, serial)
        try:
            source = ScrcpyFrameSource(
                toolchain,
                serial,
                status=status,
                cancel_event=stop_event,
                fallback_capture_image=lambda: _decode_png(client.capture_png()),
            )
            source.start()
            status("低延遲 scrcpy 畫面已就緒；辨識與畫面狀態檢查將共用串流影格。")
        except Exception as exc:
            if source is not None:
                source.close()
            source = None
            status(f"scrcpy 串流無法啟動，已改用 ADB 截圖備援：{str(exc).splitlines()[0]}")
    return source


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _class_names_from_dataset(dataset_dir: Path) -> list[str]:
    metadata_path = dataset_dir / "metadata.json"
    try:
        metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
        names = metadata.get("class_names") if isinstance(metadata, dict) else None
        if isinstance(names, list) and all(isinstance(name, str) for name in names):
            return names
    except (OSError, TypeError, ValueError):
        pass
    try:
        data = yaml.safe_load((dataset_dir / "data.yaml").read_text(encoding="utf-8")) or {}
        names = data.get("names") if isinstance(data, dict) else None
        if isinstance(names, dict):
            return [str(names[key]) for key in sorted(names, key=lambda value: int(value))]
        if isinstance(names, list):
            return [str(name) for name in names]
    except (OSError, TypeError, ValueError, yaml.YAMLError):
        pass
    return []


def _model_class_names(model_path: Path, project_root: Path) -> list[str]:
    run_dir = model_path.parent.parent
    candidates: list[Path] = []
    try:
        manifest = json.loads((run_dir / ".artifact.json").read_text(encoding="utf-8"))
        dataset = manifest.get("dataset") if isinstance(manifest, dict) else None
        if isinstance(dataset, str) and dataset.strip():
            candidate = Path(dataset)
            candidates.append(candidate if candidate.is_absolute() else project_root / candidate)
    except (OSError, TypeError, ValueError):
        pass
    try:
        args = yaml.safe_load((run_dir / "args.yaml").read_text(encoding="utf-8")) or {}
        data_path = args.get("data") if isinstance(args, dict) else None
        if isinstance(data_path, str) and data_path.strip():
            candidate = Path(data_path)
            candidate = candidate if candidate.is_absolute() else project_root / candidate
            candidates.append(candidate.parent if candidate.suffix else candidate)
    except (OSError, TypeError, ValueError, yaml.YAMLError):
        pass
    for candidate in candidates:
        try:
            dataset_dir = candidate.resolve()
            if dataset_dir.is_relative_to(project_root.resolve()):
                names = _class_names_from_dataset(dataset_dir)
                if names:
                    return names
        except (OSError, ValueError):
            continue
    return []


def json_value(value: Any) -> Any:
    if is_dataclass(value):
        return json_value(asdict(value))
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, dict):
        return {str(key): json_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [json_value(item) for item in value]
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return str(value)


def geo_waypoints_from_payload(payload: dict[str, Any]) -> list[GeoRecognitionWaypoint]:
    raw_waypoints = payload.get("waypoints")
    if not isinstance(raw_waypoints, list):
        raise ValueError("waypoints 必須是座標節點陣列。")
    waypoints: list[GeoRecognitionWaypoint] = []
    for index, value in enumerate(raw_waypoints, start=1):
        if not isinstance(value, dict):
            raise ValueError(f"第 {index} 個座標節點必須是物件。")
        try:
            horizontal_radius = int(value.get("horizontal_radius", 3))
            vertical_radius = int(value.get("vertical_radius", 4))
            waypoint = GeoRecognitionWaypoint(
                id=str(value.get("id") or f"waypoint-{index}"),
                name=str(value.get("name") or f"節點 {index}")[:120],
                lat=float(value["lat"]),
                lng=float(value["lng"]),
                wait_seconds=float(value.get("wait_seconds", 10.0)),
                horizontal_radius=horizontal_radius,
                vertical_radius=vertical_radius,
                spacing_m=float(value.get("spacing_m", 100.0)),
                left_radius=int(value.get("left_radius", horizontal_radius)),
                right_radius=int(value.get("right_radius", horizontal_radius)),
                up_radius=int(value.get("up_radius", vertical_radius)),
                down_radius=int(value.get("down_radius", vertical_radius)),
                range_unit=str(value.get("range_unit", "km")),
                left_km=(float(value["left_km"]) if value.get("left_km") is not None else None),
                right_km=(float(value["right_km"]) if value.get("right_km") is not None else None),
                up_km=(float(value["up_km"]) if value.get("up_km") is not None else None),
                down_km=(float(value["down_km"]) if value.get("down_km") is not None else None),
                horizontal_step_km=(
                    float(value["horizontal_step_km"])
                    if value.get("horizontal_step_km") is not None
                    else None
                ),
                vertical_step_km=(
                    float(value["vertical_step_km"])
                    if value.get("vertical_step_km") is not None
                    else None
                ),
                coverage_source=(str(value["coverage_source"]) if value.get("coverage_source") else None),
                settlement_class=(str(value["settlement_class"]) if value.get("settlement_class") else None),
                settlement_id=(str(value["settlement_id"]) if value.get("settlement_id") else None),
                settlement_match=(str(value["settlement_match"]) if value.get("settlement_match") else None),
                settlement_distance_km=(
                    float(value["settlement_distance_km"])
                    if value.get("settlement_distance_km") is not None
                    else None
                ),
                scan_spans=(
                    tuple(tuple(int(part) for part in span) for span in value["scan_spans"])
                    if value.get("scan_spans") is not None
                    else None
                ),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError(f"第 {index} 個座標節點格式錯誤。") from exc
        waypoints.append(waypoint)
    validate_geo_waypoints(waypoints)
    return waypoints


def recognition_filter_from_payload(payload: dict[str, Any]) -> dict[str, Any]:
    raw_names = payload.get("target_names")
    if raw_names is None:
        target_names: list[str] | None = None
    elif isinstance(raw_names, (list, tuple)):
        target_names = []
        for value in raw_names:
            name = str(value).strip()
            if name and name not in target_names:
                target_names.append(name)
        if not target_names:
            target_names = None
    else:
        raise ValueError("target_names 必須是目標名稱陣列。")
    return {
        "target_names": target_names,
        "participant_count": normalize_participant_count_filter(
            payload.get("participant_count")
        ),
    }


class JobCancelled(RuntimeError):
    pass


@dataclass
class JobRecord:
    id: str
    kind: str
    state: str = "queued"
    message: str = ""
    completed: int = 0
    total: int = 0
    started_at: str = field(default_factory=utc_now)
    finished_at: str | None = None
    result: Any = None
    error: str | None = None
    paused: bool = False
    resuming: bool = False
    excursion: dict[str, Any] | None = None
    resume_coordinate: dict[str, float] | None = None
    resume_serial: str | None = None
    recognition_filter: dict[str, Any] | None = None
    current_waypoint: dict[str, Any] | None = None
    skipping_waypoint: bool = False
    deleting_waypoint: bool = False
    route_path: str | None = None

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        # All REST/WebSocket messages are plain text, including exception tails
        # that may not have passed through the training subprocess reader.
        for key in ("message", "error"):
            if isinstance(payload[key], str):
                payload[key] = sanitize_training_output(payload[key])
        return json_value(payload)


class JobPauseGate:
    """Pause a recognition worker at a cooperative checkpoint and resume it safely."""

    def __init__(self) -> None:
        self._condition = threading.Condition()
        self._requested = False
        self._paused = False
        self._restored = False

    def checkpoint(self, stop_event: threading.Event) -> bool:
        """Block while paused; return True when an excursion reset the map center."""
        with self._condition:
            if not self._requested:
                return False
            self._paused = True
            self._condition.notify_all()
            while self._requested and not stop_event.is_set():
                self._condition.wait(0.25)
            restored = self._restored and not stop_event.is_set()
            self._restored = False
            self._paused = False
            return restored

    def request(self, timeout: float = 15.0) -> None:
        deadline = time.monotonic() + max(0.1, float(timeout))
        with self._condition:
            if self._requested:
                raise RuntimeError("辨識工作已經暫停。")
            self._requested = True
            self._paused = False
            self._restored = False
            self._condition.notify_all()
            while not self._paused:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    self._requested = False
                    self._condition.notify_all()
                    raise RuntimeError("等待辨識工作進入安全暫停點逾時，請稍後再試。")
                self._condition.wait(min(0.25, remaining))

    def resume(self, *, restored: bool) -> None:
        with self._condition:
            if not self._requested:
                raise RuntimeError("辨識工作目前沒有暫停。")
            self._restored = bool(restored)
            self._requested = False
            self._condition.notify_all()

    def cancel(self) -> None:
        with self._condition:
            self._restored = False
            self._requested = False
            self._condition.notify_all()


class JobContext:
    def __init__(
        self,
        manager: "JobManager",
        job_id: str,
        stop_event: threading.Event,
        pause_gate: JobPauseGate,
    ) -> None:
        self.manager = manager
        self.job_id = job_id
        self.stop_event = stop_event
        self.pause_gate = pause_gate

    def check_cancelled(self) -> None:
        if self.stop_event.is_set():
            raise JobCancelled("工作已由使用者停止。")

    def pause_checkpoint(self) -> bool:
        return self.pause_gate.checkpoint(self.stop_event)

    def begin_waypoint(self, detail: dict[str, object]) -> threading.Event:
        with self.manager._lock:
            event = threading.Event()
            if self.stop_event.is_set():
                event.set()
            self.manager._waypoint_stops[self.job_id] = event
            self.manager.update(
                self.job_id,
                current_waypoint=dict(detail),
                skipping_waypoint=False,
                deleting_waypoint=False,
                total=int(detail["total_points"]),
            )
            return event

    def finish_waypoint(self) -> bool:
        with self.manager._lock:
            skipped = self.manager.get(self.job_id).skipping_waypoint
            self.manager._waypoint_stops.pop(self.job_id, None)
            self.manager.update(self.job_id, current_waypoint=None, skipping_waypoint=False, deleting_waypoint=False)
            return skipped

    def recognition_filter(self) -> dict[str, Any] | None:
        with self.manager._lock:
            value = self.manager.get(self.job_id).recognition_filter
            return dict(value) if isinstance(value, dict) else None

    def message(self, value: str) -> None:
        self.manager.update(self.job_id, message=str(value))

    def progress(self, completed: int, total: int, detail: Any = None) -> None:
        self.manager.update(
            self.job_id,
            completed=int(completed),
            total=max(0, int(total)),
            detail=json_value(detail),
        )

    def preview(self, image: np.ndarray, *, max_width: int = 960) -> None:
        height, width = image.shape[:2]
        if width > max_width:
            scale = max_width / width
            image = cv2.resize(
                image,
                (max_width, max(1, int(height * scale))),
                interpolation=cv2.INTER_AREA,
            )
        ok, encoded = cv2.imencode(".jpg", image, [cv2.IMWRITE_JPEG_QUALITY, 82])
        if not ok:
            return
        self.manager.hub.publish(
            {
                "type": "preview",
                "job_id": self.job_id,
                "data_url": "data:image/jpeg;base64," + base64.b64encode(encoded).decode("ascii"),
            }
        )

    def detections(self, location: Any, detections: list[dict[str, object]]) -> None:
        if not detections:
            return
        location_payload = json_value(location)
        detection_payload = json_value(detections)
        self.manager.hub.publish(
            {
                "type": "detections",
                "job_id": self.job_id,
                "location": location_payload,
                "detections": detection_payload,
            }
        )
        listener = self.manager.detection_listener
        if listener is not None:
            try:
                listener(self.manager.get(self.job_id).kind, location_payload, detection_payload)
            except Exception as exc:
                self.manager.hub.publish({
                    "type": "notification_error",
                    "job_id": self.job_id,
                    "message": f"Discord 命中播報失敗：{exc}",
                })

    def memory(self, snapshot: dict[str, object]) -> None:
        LOGGER.info("job=%s memory=%s", self.job_id, json.dumps(snapshot, ensure_ascii=False))
        self.manager.hub.publish(
            {
                "type": "memory",
                "job_id": self.job_id,
                "memory": json_value(snapshot),
            }
        )


JobRunner = Callable[[JobContext], Any]


class JobManager:
    def __init__(
        self,
        hub: EventHub,
        detection_listener: Callable[[str, Any, list[dict[str, object]]], Any] | None = None,
    ) -> None:
        self.hub = hub
        self.detection_listener = detection_listener
        self._lock = threading.RLock()
        self._jobs: dict[str, JobRecord] = {}
        self._stops: dict[str, threading.Event] = {}
        self._pause_gates: dict[str, JobPauseGate] = {}
        self._waypoint_stops: dict[str, threading.Event] = {}
        self._pausing: set[str] = set()
        self._active_id: str | None = None

    def active(self) -> JobRecord | None:
        with self._lock:
            if self._active_id is None:
                return None
            return self._jobs.get(self._active_id)

    def list(self) -> list[dict[str, Any]]:
        with self._lock:
            records = sorted(self._jobs.values(), key=lambda item: item.started_at, reverse=True)
        return [record.to_dict() for record in records]

    def get(self, job_id: str) -> JobRecord:
        with self._lock:
            try:
                return self._jobs[job_id]
            except KeyError as exc:
                raise KeyError(f"找不到工作：{job_id}") from exc

    def stop_event(self, job_id: str) -> threading.Event:
        with self._lock:
            self.get(job_id)
            return self._stops[job_id]

    def start(
        self,
        kind: str,
        runner: JobRunner,
        *,
        recognition_filter: dict[str, Any] | None = None,
        route_path: str | None = None,
    ) -> JobRecord:
        with self._lock:
            active = self.active()
            if active is not None and active.state in {"queued", "running", "stopping"}:
                raise RuntimeError(f"{active.kind} 工作正在執行；請先停止或等待完成。")
            job_id = uuid.uuid4().hex
            record = JobRecord(
                id=job_id,
                kind=kind,
                route_path=route_path,
                recognition_filter=(
                    dict(recognition_filter) if isinstance(recognition_filter, dict) else None
                ),
            )
            stop_event = threading.Event()
            pause_gate = JobPauseGate()
            self._jobs[job_id] = record
            self._stops[job_id] = stop_event
            self._pause_gates[job_id] = pause_gate
            self._active_id = job_id
        thread = threading.Thread(
            target=self._run,
            args=(record, stop_event, pause_gate, runner),
            name=f"bloompilot-{kind}-{job_id[:8]}",
            daemon=True,
        )
        thread.start()
        self._publish(record)
        return record

    def _run(
        self,
        record: JobRecord,
        stop_event: threading.Event,
        pause_gate: JobPauseGate,
        runner: JobRunner,
    ) -> None:
        with self._lock:
            if not stop_event.is_set():
                self.update(record.id, state="running", message="工作已開始。")
        context = JobContext(self, record.id, stop_event, pause_gate)
        try:
            result = runner(context)
            with self._lock:
                state = "cancelled" if stop_event.is_set() else "completed"
                self.update(record.id, state=state, result=json_value(result), finished_at=utc_now())
        except JobCancelled as exc:
            self.update(
                record.id,
                state="cancelled",
                message=str(exc),
                finished_at=utc_now(),
            )
        except Exception as exc:
            LOGGER.exception("Job failed: id=%s kind=%s", record.id, record.kind)
            self.update(
                record.id,
                state="error",
                message=str(exc),
                error=traceback.format_exc(),
                finished_at=utc_now(),
            )
        finally:
            # Release scrcpy streams and any other worker tied to job lifetime,
            # including when the runner exits through an exception.
            stop_event.set()
            pause_gate.cancel()
            with self._lock:
                self._waypoint_stops.pop(record.id, None)
                if self._active_id == record.id:
                    self._active_id = None

    def update(self, job_id: str, **changes: Any) -> JobRecord:
        detail = changes.pop("detail", None)
        with self._lock:
            record = self.get(job_id)
            if changes.get("message") and changes["message"] != record.message:
                LOGGER.info("job=%s state=%s %s", job_id, changes.get("state", record.state), changes["message"])
            for key, value in changes.items():
                if hasattr(record, key):
                    setattr(record, key, value)
        event: dict[str, Any] = {"type": "job_update", "job": record.to_dict()}
        if detail is not None:
            event["detail"] = detail
        self.hub.publish(event)
        return record

    def stop(self, job_id: str) -> JobRecord:
        with self._lock:
            record = self.get(job_id)
            stop_event = self._stops.get(job_id)
            if stop_event is not None:
                stop_event.set()
            waypoint_stop = self._waypoint_stops.get(job_id)
            if waypoint_stop is not None:
                waypoint_stop.set()
            pause_gate = self._pause_gates.get(job_id)
            if pause_gate is not None:
                pause_gate.cancel()
            if record.state in {"queued", "running"}:
                record.state = "stopping"
                record.message = "正在安全停止…"
        self._publish(record)
        return record

    def skip_geo_waypoint(
        self,
        job_id: str,
        waypoint_id: str,
        *,
        remove_from_route: Callable[[str, str], None] | None = None,
    ) -> JobRecord:
        with self._lock:
            record = self.get(job_id)
            if (
                record.id != self._active_id
                or record.kind != "geo_sequence_recognition"
                or record.state != "running"
            ):
                raise RuntimeError("目前沒有可跳過節點的座標序列辨識工作。")
            if record.paused or record.resuming or job_id in self._pausing:
                raise RuntimeError("請先繼續辨識，再跳過目前節點。")
            current = record.current_waypoint
            event = self._waypoint_stops.get(job_id)
            if not current or current["waypoint_id"] != waypoint_id or event is None:
                raise RuntimeError("目前節點已變更或尚未開始，請稍後再試。")
            if record.skipping_waypoint:
                if remove_from_route is not None and not record.deleting_waypoint:
                    raise RuntimeError("正在跳過目前節點，請稍後再刪除。")
                return record
            if remove_from_route is not None:
                if not record.route_path:
                    raise RuntimeError("此工作尚未綁定已保存的 route，請保存序列後重新執行。")
                # Hold the job lock until persistence succeeds so the worker cannot
                # finish this node and begin another while we are deleting it.
                remove_from_route(record.route_path, waypoint_id)
            event.set()
            return self.update(
                job_id,
                skipping_waypoint=True,
                deleting_waypoint=remove_from_route is not None,
                message=(
                    f"已從 route 永久刪除 {current['waypoint_name']}，完成目前操作後前往下一個節點…"
                    if remove_from_route is not None
                    else f"正在跳過 {current['waypoint_name']}，完成目前操作後前往下一個節點…"
                ),
            )

    def pause_recognition(self, job_id: str, *, timeout: float = 15.0) -> JobRecord:
        with self._lock:
            record = self.get(job_id)
            if (
                record.id != self._active_id
                or record.kind not in {"screen_recognition", "geo_sequence_recognition", "live_recognition"}
                or record.state != "running"
            ):
                raise RuntimeError("目前工作不是可暫停的手機辨識工作。")
            if record.paused:
                raise RuntimeError("辨識工作已經暫停。")
            if record.skipping_waypoint:
                raise RuntimeError("正在跳過目前節點，請稍後再暫停。")
            if job_id in self._pausing:
                raise RuntimeError("辨識工作正在安全暫停。")
            gate = self._pause_gates[job_id]
            self._pausing.add(job_id)
        try:
            gate.request(timeout)
            return self.update(job_id, paused=True, message="辨識已安全暫停。")
        finally:
            with self._lock:
                self._pausing.discard(job_id)

    def set_recognition_filter(
        self,
        job_id: str,
        recognition_filter: dict[str, Any],
    ) -> JobRecord:
        with self._lock:
            record = self.get(job_id)
            if not record.paused or record.resuming:
                raise RuntimeError("只有辨識暫停時才能更改命中篩選。")
            if record.kind not in {
                "screen_recognition",
                "geo_sequence_recognition",
                "live_recognition",
            }:
                raise RuntimeError("目前工作不是手機辨識工作。")
        return self.update(job_id, recognition_filter=dict(recognition_filter))

    def resume_recognition(self, job_id: str) -> JobRecord:
        with self._lock:
            record = self.get(job_id)
            if not record.paused:
                raise RuntimeError("辨識工作目前沒有暫停。")
            gate = self._pause_gates[job_id]
        gate.resume(restored=True)
        return self.update(
            job_id,
            paused=False,
            resuming=False,
            excursion=None,
            resume_coordinate=None,
            resume_serial=None,
            message="已重新注入搜尋位置、重啟遊戲並等待 10 秒，繼續辨識。",
        )

    def cancel_recognition_pause(self, job_id: str) -> JobRecord:
        with self._lock:
            record = self.get(job_id)
            gate = self._pause_gates[job_id]
        gate.resume(restored=False)
        return self.update(
            job_id,
            paused=False,
            resuming=False,
            excursion=None,
            resume_coordinate=None,
            resume_serial=None,
        )

    def _publish(self, record: JobRecord) -> None:
        self.hub.publish({"type": "job_update", "job": record.to_dict()})

    def sync_training_stop(self, job_id: str, control: TrainingControl) -> None:
        # Serialize event inspection/request creation with cancellation so a stale
        # event read can never recreate a successfully cancelled stop request.
        with self._lock:
            if self._stops[job_id].is_set() and not control.request_accepted:
                if control.requested_action() != "stop":
                    control.request("stop")

    def cancel_training_stop(self, job_id: str, control: TrainingControl) -> JobRecord:
        with self._lock:
            record = self.get(job_id)
            if record.kind != "training" or record.id != self._active_id or record.state != "stopping":
                raise RuntimeError("目前沒有可取消的訓練安全停止要求。")
            control.cancel_stop()
            self._stops[job_id].clear()
            return self.update(job_id, state="running", message="已取消安全停止，繼續目前訓練。")


class DeviceSession:
    def __init__(self, hub: EventHub) -> None:
        self.hub = hub
        self._lock = threading.RLock()
        self._controller: UsbDeviceController | None = None
        self._serial = ""

    @property
    def connected(self) -> bool:
        with self._lock:
            return bool(self._controller and self._controller.connected)

    @property
    def serial(self) -> str:
        with self._lock:
            return self._serial

    def scan(self) -> list[dict[str, Any]]:
        toolchain = find_toolchain()
        return [json_value(device) for device in list_adb_devices(toolchain)]

    def connect(self, serial: str) -> dict[str, Any]:
        toolchain = find_toolchain()
        controller = UsbDeviceController(toolchain.adb, serial)
        response = controller.connect()
        with self._lock:
            previous = self._controller
            self._controller = controller
            self._serial = serial
        if previous is not None:
            previous.close()
        payload = {"connected": True, "serial": serial, **response.status}
        self.hub.publish({"type": "device_update", "device": json_value(payload)})
        return payload

    def disconnect(self) -> dict[str, Any]:
        with self._lock:
            controller = self._controller
            serial = self._serial
            self._controller = None
            self._serial = ""
        if controller is not None:
            controller.close()
        payload = {"connected": False, "serial": serial}
        self.hub.publish({"type": "device_update", "device": payload})
        return payload

    def controller(self) -> UsbDeviceController:
        with self._lock:
            controller = self._controller
        if controller is None or not controller.connected:
            raise DeviceControlError("尚未連接 Android USB 控制。")
        return controller

    def status(self) -> dict[str, Any]:
        # Retain the same controller: recognition workers hold this object.
        # An explicit disconnect clears it, so polling cannot reconnect a
        # device the user intentionally disconnected.
        with self._lock:
            controller = self._controller
            if controller is None:
                return {"connected": False, "serial": self._serial}
            if not controller.connected:
                response = controller.connect(timeout=8.0, only_if_disconnected=True)
                status = response.status
            else:
                status = controller.status()
            return {"connected": True, "serial": self._serial, **status}

    def command(self, command: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        response = self.controller().command(command, payload)
        # Preserve command-specific response data (search results, diagnostics,
        # backup payloads, and inventories) instead of reducing every USB
        # response to only ok/message/status.
        result = {
            key: value
            for key, value in response.raw.items()
            if key not in {"v", "type", "id", "ok", "message", "status"}
        }
        result.update({
            "ok": response.ok,
            "message": response.message,
            "status": response.status,
        })
        self.hub.publish({
            "type": "device_update",
            "device": {"connected": True, "serial": self.serial, **response.status},
        })
        return result

    def close(self) -> None:
        self.disconnect()


class AppRuntime:
    def __init__(self, *, device_scale_store: DeviceScaleStore | None = None) -> None:
        self.hub = EventHub()
        self.discord_notifier = DiscordWebhookNotifier(
            lambda: load_studio_settings(),
            error_callback=self._discord_notification_error,
        )
        self.jobs = JobManager(self.hub, detection_listener=self.discord_notifier.notify)
        self.device = DeviceSession(self.hub)
        self.screen_control = ScrcpyControlWindow()
        self.route_library = RouteLibrary(ROUTES_DIR)
        self.monthly_workflow = MonthlyWorkflowStore()
        self.device_scales = device_scale_store or DeviceScaleStore()
        self._viewport_calibration_lock = threading.Lock()

    def _discord_notification_error(self, job_kind: str, message: str) -> None:
        active = self.jobs.active()
        self.hub.publish({
            "type": "notification_error",
            "job_id": active.id if active is not None and active.kind == job_kind else None,
            "message": f"Discord 命中播報失敗：{message}",
        })

    def _device_calibration_status(self, status: dict[str, Any]) -> dict[str, Any]:
        serial = str(status.get("serial") or "").strip()
        return {
            **status,
            "scale_calibration_required": bool(
                status.get("connected")
                and serial
                and self.device_scales.get(serial) is None
            ),
        }

    def connect_device(self, serial: str) -> dict[str, Any]:
        self.screen_control.close()
        return self._device_calibration_status(self.device.connect(serial))

    def disconnect_device(self) -> dict[str, Any]:
        self.screen_control.close()
        return self.device.disconnect()

    def device_status(self) -> dict[str, Any]:
        return self._device_calibration_status(self.device.status())

    def device_scale(self, serial: str) -> dict[str, Any]:
        calibration = self.device_scales.get(serial)
        return {
            "calibration": calibration.to_dict() if calibration is not None else None,
            "required": calibration is None,
        }

    def ensure_device_scale_for_search(
        self,
        serial: str,
        *,
        ctx: JobContext,
        toolchain: Any,
    ) -> DeviceScaleCalibration:
        """Return a registered scale, measuring and registering a new phone once."""
        calibration = self.device_scales.get(serial)
        if calibration is not None:
            ctx.message(f"裝置 {serial} 已有比例尺註冊資料，直接開始搜尋準備。")
            return calibration

        ctx.message(f"裝置 {serial} 尚未註冊比例尺，正在自動量測；此動作只需執行一次。")
        with _recognition_frame_source(
            toolchain, serial, ctx.stop_event, ctx.message
        ) as frame_source:
            client = AdbClient(toolchain.adb, serial)
            client.ensure_ready()
            watchdog = PikminMemoryWatchdog(
                client,
                stop_event=ctx.stop_event,
                status=ctx.message,
                capture_image=(frame_source.capture_image if frame_source else None),
            )
            if not watchdog.prepare_for_scan():
                raise RuntimeError("自動量測前無法將 Pikmin Bloom 準備為 2D 最大範圍地圖。")

            # Calibration observes the player marker moving while the map stays
            # fixed. A short downward pan disables follow mode and leaves extra
            # room above the marker for the northward calibration probe.
            size = client.screen_size()
            center_x = round(size.width * 0.50)
            center_y = round(size.height * 0.43)
            client.run(
                "shell",
                "input",
                "swipe",
                str(center_x),
                str(center_y),
                str(center_x),
                str(round(size.height * 0.55)),
                "350",
                timeout=8,
            )
            if ctx.stop_event.wait(0.75):
                raise JobCancelled("搜尋已取消，不再執行裝置比例尺量測。")
            capture_png = frame_source.capture_png if frame_source else client.capture_png
            reference_payload = capture_png()
            if not pikmin_map_locator_visible(reference_payload):
                raise RuntimeError("自動量測無法解除地圖跟隨模式；請確認目前停留在 Pikmin 2D 地圖後重試。")
            self.calibrate_device_scale(
                serial=serial,
                settle_seconds=2.5,
                allow_active_job=True,
                capture_png=capture_png,
                reference_payload=reference_payload,
                stop_event=ctx.stop_event,
            )

        calibration = self.device_scales.get(serial)
        if calibration is None:
            raise RuntimeError(f"裝置 {serial} 的比例尺量測完成，但註冊資料沒有成功保存。")
        ctx.message(
            f"裝置 {serial} 比例尺已自動量測並註冊："
            f"水平 {calibration.meters_per_pixel_x:.4f} m/px，"
            f"垂直 {calibration.meters_per_pixel_y:.4f} m/px。"
        )
        return calibration

    @staticmethod
    def _scale_preview(
        payload: bytes,
        movements: list[tuple[MarkerDisplacement, tuple[int, int, int]]],
    ) -> str:
        image = cv2.imdecode(np.frombuffer(payload, dtype=np.uint8), cv2.IMREAD_COLOR)
        if image is None:
            raise RuntimeError("無法解碼比例尺校正預覽。")
        for movement, color in movements:
            source = (round(movement.source_x), round(movement.source_y))
            target = (round(movement.target_x), round(movement.target_y))
            cv2.circle(image, source, 8, color, 3, cv2.LINE_AA)
            cv2.arrowedLine(image, source, target, color, 4, cv2.LINE_AA, tipLength=0.16)
        height, width = image.shape[:2]
        if width > 960:
            scale = 960 / width
            image = cv2.resize(
                image,
                (960, max(1, round(height * scale))),
                interpolation=cv2.INTER_AREA,
            )
        ok, encoded = cv2.imencode(".jpg", image, [cv2.IMWRITE_JPEG_QUALITY, 86])
        if not ok:
            raise RuntimeError("無法產生比例尺校正預覽。")
        return "data:image/jpeg;base64," + base64.b64encode(encoded.tobytes()).decode("ascii")

    def calibrate_device_scale(
        self,
        *,
        serial: str,
        settle_seconds: float = 2.5,
        allow_active_job: bool = False,
        capture_png: Callable[[], bytes] | None = None,
        reference_payload: bytes | None = None,
        stop_event: threading.Event | None = None,
    ) -> dict[str, Any]:
        """Measure longitude/latitude per screen pixel for one Android device."""
        active = self.jobs.active()
        if active is not None and not allow_active_job:
            raise RuntimeError("目前有工作正在使用手機，請先停止後再校正比例尺。")
        serial = serial.strip()
        if not serial or not self.device.connected or self.device.serial != serial:
            raise RuntimeError("請先連接要校正的 Android 手機。")
        if not self._viewport_calibration_lock.acquire(blocking=False):
            raise RuntimeError("上一個手機畫面操作仍在處理中。")
        try:
            status = self.device.status()
            current = status.get("current")
            if not isinstance(current, dict) or "lat" not in current or "lng" not in current:
                raise RuntimeError("無法取得手機目前注入的經緯度。")
            origin_lat = float(current["lat"])
            origin_lng = float(current["lng"])
            toolchain = find_toolchain()
            client = AdbClient(toolchain.adb, serial)
            client.ensure_ready()
            capture = capture_png or client.capture_png
            reference_payload = reference_payload or capture()
            if not pikmin_map_locator_visible(reference_payload):
                raise RuntimeError(
                    "右下角定位按鈕尚未出現。請先拖動地圖，使自身位置不再固定於正中央後再校正。"
                )
            reference_image = cv2.imdecode(
                np.frombuffer(reference_payload, dtype=np.uint8), cv2.IMREAD_COLOR
            )
            if reference_image is None:
                raise RuntimeError("無法解碼手機比例尺校正畫面。")
            screen_height, screen_width = reference_image.shape[:2]
            previous = self.device_scales.get(serial)
            previous_mpp = (
                (previous.meters_per_pixel_x + previous.meters_per_pixel_y) / 2
                if previous is not None
                else VIEWPORT_WIDTH_M / screen_width
            )
            # A portrait screen offers much more safe travel vertically.  One
            # north/south round trip is enough because the north-up 2D map uses
            # the same physical metres-per-pixel scale on both screen axes.
            # Derive the horizontal longitude scale from that measured physical
            # scale instead of risking a second, horizontal probe.
            target_pixels = min(SCALE_PROBE_TARGET_PIXELS, screen_height * 0.12)
            probe_m = max(
                SCALE_PROBE_MIN_METERS,
                min(SCALE_PROBE_MAX_METERS, previous_mpp * target_pixels),
            )

            def send_location(lat: float, lng: float) -> None:
                response = self.device.command("teleport", {"lat": lat, "lng": lng})
                if response.get("ok") is False:
                    raise RuntimeError(str(response.get("message") or "手機拒絕位置注入。"))

            def wait_for_map(delay: float = settle_seconds) -> None:
                waiter = stop_event or threading.Event()
                if waiter.wait(delay):
                    raise JobCancelled("搜尋已取消，不再繼續裝置比例尺量測。")

            def require_locator(payload: bytes) -> None:
                if not pikmin_map_locator_visible(payload):
                    raise RuntimeError(
                        "校正途中右下角定位按鈕消失，地圖已重新跟隨自身位置；本次校正未保存。"
                    )

            def wait_for_displacement(
                baseline: bytes,
                *,
                axis: str,
                expected_sign: int,
            ) -> tuple[MarkerDisplacement, bytes]:
                """Poll until the GPS-responsive marker stops changing position."""
                deadline = time.monotonic() + SCALE_PROBE_TIMEOUT_SECONDS
                previous: MarkerDisplacement | None = None
                last_error: ValueError | None = None
                while True:
                    payload = capture()
                    require_locator(payload)
                    try:
                        movement = measure_player_marker_displacement(
                            baseline,
                            payload,
                            axis=axis,
                            expected_sign=expected_sign,
                        )
                    except ValueError as exc:
                        last_error = exc
                        previous = None
                    else:
                        if previous is not None:
                            primary = movement.dx if axis == "x" else movement.dy
                            previous_primary = previous.dx if axis == "x" else previous.dy
                            tolerance = max(3.0, abs(primary) * 0.06)
                            target_shift = math.hypot(
                                movement.target_x - previous.target_x,
                                movement.target_y - previous.target_y,
                            )
                            if (
                                abs(primary - previous_primary) <= tolerance
                                and target_shift <= 4.0
                            ):
                                return movement, payload
                        previous = movement
                    if time.monotonic() >= deadline:
                        if last_error is not None:
                            raise last_error
                        raise ValueError("自身位置尚未停止移動，未保存本次比例尺。")
                    wait_for_map(SCALE_PROBE_POLL_SECONDS)

            def run_probe(
                baseline: bytes,
                *,
                target_lat: float,
                target_lng: float,
                axis: str,
                expected_sign: int,
            ) -> tuple[MarkerDisplacement, bytes, bytes]:
                probe_error: Exception | None = None
                moved_payload: bytes | None = None
                forward: MarkerDisplacement | None = None
                try:
                    send_location(target_lat, target_lng)
                    wait_for_map()
                    forward, moved_payload = wait_for_displacement(
                        baseline,
                        axis=axis,
                        expected_sign=expected_sign,
                    )
                except Exception as exc:
                    probe_error = exc
                restore_error: Exception | None = None
                try:
                    send_location(origin_lat, origin_lng)
                    wait_for_map()
                    if moved_payload is None or forward is None:
                        restored_payload = capture()
                        require_locator(restored_payload)
                    else:
                        backward, restored_payload = wait_for_displacement(
                            moved_payload,
                            axis=axis,
                            expected_sign=-expected_sign,
                        )
                except Exception as exc:
                    restore_error = exc
                    restored_payload = b""
                if restore_error is not None:
                    detail = f"{probe_error}；" if probe_error is not None else ""
                    raise RuntimeError(f"{detail}還原原始座標失敗：{restore_error}") from restore_error
                if probe_error is not None:
                    raise probe_error
                assert moved_payload is not None and forward is not None
                assert backward is not None
                validate_round_trip(forward, backward, axis=axis)
                return forward, moved_payload, restored_payload

            north_sign = 1 if origin_lat < 89.99 else -1
            north_target_lat, north_target_lng = coordinate_from_offset(
                origin_lat,
                origin_lng,
                east_m=0,
                north_m=probe_m * north_sign,
            )
            north_move, north_payload, _final_payload = run_probe(
                reference_payload,
                target_lat=north_target_lat,
                target_lng=north_target_lng,
                axis="y",
                expected_sign=-north_sign,
            )
            # Web Mercator is conformal: at a given point, one physical metre
            # has the same pixel scale horizontally and vertically.  Calculate
            # the longitude delta for ``probe_m`` without actually moving the
            # player east/west.
            _east_target_lat, east_target_lng = coordinate_from_offset(
                origin_lat,
                origin_lng,
                east_m=probe_m,
                north_m=0,
            )
            calibration = new_device_scale_calibration(
                serial=serial,
                screen_width=screen_width,
                screen_height=screen_height,
                origin_lat=origin_lat,
                origin_lng=origin_lng,
                east_probe_m=probe_m,
                north_probe_m=probe_m,
                east_coordinate_delta=east_target_lng - origin_lng,
                north_coordinate_delta=north_target_lat - origin_lat,
                east_pixels=abs(north_move.dy),
                north_pixels=north_move.dy,
            )
            self.device_scales.save(calibration)
            return {
                "ok": True,
                "calibration": calibration.to_dict(),
                "preview": self._scale_preview(
                    reference_payload,
                    [
                        (north_move, (255, 181, 74)),
                    ],
                ),
            }
        finally:
            self._viewport_calibration_lock.release()

    def calibrate_viewport(
        self,
        *,
        serial: str,
        action: str,
        direction: str | None = None,
        settle_seconds: float = 2.0,
        swipe_duration_ms: int = 800,
    ) -> dict[str, Any]:
        active = self.jobs.active()
        if active is not None:
            raise RuntimeError("目前有工作正在使用手機，請先停止工作再建立範圍。")
        if action not in {"capture", "step", "recenter"}:
            raise ValueError("不支援的範圍建立動作。")
        if action == "step" and direction not in {"west", "east", "north", "south"}:
            raise ValueError("單步滑動必須指定左、右、上或下。")
        acquired = (
            self._viewport_calibration_lock.acquire(timeout=40)
            if action == "recenter"
            else self._viewport_calibration_lock.acquire(blocking=False)
        )
        if not acquired:
            raise RuntimeError("上一個範圍預覽動作仍在處理中。")
        try:
            toolchain = find_toolchain()
            client = AdbClient(toolchain.adb, serial)
            client.ensure_ready()
            size = client.screen_size()
            if action == "step":
                assert direction is not None
                client.swipe(direction, size, swipe_duration_ms)
            elif action == "recenter":
                client.recenter(size)
            if settle_seconds > 0:
                threading.Event().wait(settle_seconds)
            png = client.capture_png()
            image = cv2.imdecode(np.frombuffer(png, dtype=np.uint8), cv2.IMREAD_COLOR)
            if image is None:
                raise RuntimeError("無法解碼手機預覽畫面。")
            height, width = image.shape[:2]
            screen_width = width
            screen_height = height
            if width > 960:
                scale = 960 / width
                image = cv2.resize(
                    image,
                    (960, max(1, int(round(height * scale)))),
                    interpolation=cv2.INTER_AREA,
                )
            ok, encoded = cv2.imencode(".jpg", image, [cv2.IMWRITE_JPEG_QUALITY, 84])
            if not ok:
                raise RuntimeError("無法產生手機預覽畫面。")
            return {
                "ok": True,
                "action": action,
                "direction": direction,
                "screen_width": screen_width,
                "screen_height": screen_height,
                "preview": "data:image/jpeg;base64," + base64.b64encode(encoded.tobytes()).decode("ascii"),
            }
        finally:
            self._viewport_calibration_lock.release()

    def config(self) -> dict[str, Any]:
        return load_config(CONFIG_PATH)

    def settings_payload(self) -> dict[str, Any]:
        config = self.config()
        config.pop("_config_path", None)
        config.pop("_project_root", None)
        return {"settings": fixed_studio_settings(load_studio_settings()), "effective_config": config}

    def save_settings(self, settings: dict[str, Any]) -> dict[str, Any]:
        settings = normalize_discord_webhook_settings(fixed_studio_settings(settings))
        discord = settings.get("discord_webhook", {})
        if discord.get("enabled") and discord.get("mode") == "bot":
            discord["channel_id"] = resolve_bot_channel(discord["bot_token"], discord["channel_id"])
        save_studio_settings(settings)
        self.hub.publish({"type": "settings_update", "settings": json_value(settings)})
        return self.settings_payload()

    def test_discord_webhook(self, webhook_url: str) -> dict[str, Any]:
        post_discord_webhook(webhook_url, build_discord_test_payload())
        return {"ok": True, "message": "Discord 測試播報已送出。"}

    def discord_bot_channels(self, bot_token: str) -> dict[str, Any]:
        return discover_bot_channels(bot_token)

    def test_discord_bot(self, bot_token: str, channel_id: str = "") -> dict[str, Any]:
        channel = resolve_bot_channel(bot_token, channel_id)
        post_discord_bot(bot_token, channel, build_discord_test_payload())
        return {"ok": True, "message": "Discord 機器人測試播報已送出。", "channel_id": channel}

    @staticmethod
    def _trained_model_paths(manager: ArtifactManager) -> list[Path]:
        root = manager.training_root
        return sorted(
            {
                path.resolve()
                for path in root.glob("*/weights/*.pt")
                if path.is_file() and path.resolve().is_relative_to(root)
            },
            key=lambda item: item.stat().st_mtime,
            reverse=True,
        )

    @staticmethod
    def _stored_default_model(manager: ArtifactManager, paths: list[Path]) -> Path | None:
        value = load_studio_settings().get("recognition_default_model")
        if not isinstance(value, str) or not value.strip():
            return None
        candidate = Path(value)
        candidate = candidate if candidate.is_absolute() else manager.project_root / candidate
        candidate = candidate.resolve()
        return candidate if candidate in paths else None

    def models(self) -> list[dict[str, Any]]:
        manager = ArtifactManager(self.config())
        paths = self._trained_model_paths(manager)
        default = self._stored_default_model(manager, paths)
        return [
            {
                "path": str(path),
                "name": f"{path.parent.parent.name} / {path.name}",
                "size_bytes": path.stat().st_size,
                "default": path == default,
                "class_names": _model_class_names(path, manager.project_root),
            }
            for path in paths
        ]

    def set_default_recognition_model(self, model_path: str) -> dict[str, Any]:
        manager = ArtifactManager(self.config())
        paths = self._trained_model_paths(manager)
        requested = Path(model_path)
        requested = requested if requested.is_absolute() else manager.project_root / requested
        requested = requested.resolve()
        if requested not in paths:
            raise ValueError("預設模型只能選擇專案 outputs/training 內已訓練的模型")
        settings = fixed_studio_settings(load_studio_settings())
        settings["recognition_default_model"] = requested.relative_to(manager.project_root).as_posix()
        save_studio_settings(settings)
        self.hub.publish({"type": "settings_update", "settings": json_value(settings)})
        return next(item for item in self.models() if item["path"] == str(requested))

    def datasets(self) -> list[dict[str, Any]]:
        return list_generated_datasets(self.config())

    def routes(self) -> list[dict[str, Any]]:
        routes = self.route_library.list()
        if self.route_library.directory.resolve() == ROUTES_DIR.resolve():
            return [
                route for route in routes
                if route["path"] == "pikmin_inhabited/less_known_inhabited.json"
            ]
        return routes

    def _monthly_background_route(self) -> tuple[str, dict[str, Any]]:
        route, document = ensure_monthly_background_route(BACKGROUND_SOURCE_DIR, ROUTES_DIR)
        self.route_library.route_path(route)
        if self.monthly_workflow.fixed_routes() != [route]:
            self.monthly_workflow.set_fixed_routes([route])
        return route, document

    def monthly_status(self, cycle: str) -> dict[str, Any]:
        cycle = validate_cycle(cycle)
        route, route_document = self._monthly_background_route()
        if self.monthly_workflow.resident_model() is None:
            manager = ArtifactManager(self.config())
            paths = self._trained_model_paths(manager)
            candidate = self._stored_default_model(manager, paths) or (paths[0] if paths else None)
            if candidate is not None:
                self.monthly_workflow.set_resident_model(
                    candidate,
                    source_label="首次建立時採用目前辨識模型",
                    class_names=_model_class_names(candidate, manager.project_root),
                )
        status = self.monthly_workflow.status(cycle)
        metadata = route_document["metadata"]
        status["route_paths"] = [route]
        status["background_route"] = {
            "name": route_document["sequence"]["name"],
            "towns": metadata["towns"],
            "waypoint_count": metadata["waypoint_count"],
            "search_point_count": metadata["total_search_points"],
        }
        return json_value(status)

    def configure_monthly_workflow(self, payload: dict[str, Any]) -> dict[str, Any]:
        cycle = validate_cycle(str(payload.get("cycle") or ""))
        route, _ = self._monthly_background_route()
        self.monthly_workflow.update_manifest(cycle, route_paths=[route])
        return self.monthly_status(cycle)

    def save_monthly_activity_target(self, payload: dict[str, Any]) -> dict[str, Any]:
        if self.jobs.active() is not None:
            raise RuntimeError("工作執行中，請等待完成後再更改活動菇素材。")
        cycle = validate_cycle(str(payload.get("cycle") or ""))
        self.monthly_workflow.save_activity_target(
            cycle,
            str(payload.get("filename") or "activity_mushroom.png"),
            str(payload.get("data") or ""),
        )
        return self.monthly_status(cycle)

    def monthly_background_file(self, cycle: str, relative_path: str) -> Path:
        return self.monthly_workflow.background_file(validate_cycle(cycle), relative_path)

    def delete_monthly_background(self, cycle: str, relative_path: str) -> dict[str, Any]:
        if self.jobs.active() is not None:
            raise RuntimeError("工作執行中，請等待完成後再刪除背景。")
        cycle = validate_cycle(cycle)
        self.monthly_workflow.delete_background(cycle, relative_path)
        return self.monthly_status(cycle)

    def approve_monthly_backgrounds(self, cycle: str) -> dict[str, Any]:
        if self.jobs.active() is not None:
            raise RuntimeError("工作執行中，請等待完成後再核准背景。")
        cycle = validate_cycle(cycle)
        self.monthly_workflow.approve_backgrounds(cycle)
        return self.monthly_status(cycle)

    def start_monthly_background_collection(self, payload: dict[str, Any]) -> JobRecord:
        cycle = validate_cycle(str(payload.get("cycle") or ""))
        route, _ = self._monthly_background_route()
        routes = [route]
        serial = str(payload.get("serial") or self.device.serial).strip()
        if not serial or serial != self.device.serial or not self.device.connected:
            raise RuntimeError("請先連接要蒐集背景的 Android 手機。")
        settle_seconds = float(payload.get("settle_seconds", 4.0))
        swipe_duration_ms = int(payload.get("swipe_duration_ms", 800))

        def runner(ctx: JobContext) -> dict[str, Any]:
            toolchain = find_toolchain()
            result = collect_fixed_route_backgrounds(
                store=self.monthly_workflow,
                route_library=self.route_library,
                controller=self.device.controller(),
                adb=toolchain.adb,
                serial=serial,
                cycle=cycle,
                route_paths=routes,
                settle_seconds=settle_seconds,
                swipe_duration_ms=swipe_duration_ms,
                stop_event=ctx.stop_event,
                status=ctx.message,
                progress=lambda done, total, detail: ctx.progress(done, total, detail),
                preview=ctx.preview,
            )
            self.hub.publish({"type": "monthly_workflow_update", "cycle": cycle})
            return result

        return self.jobs.start("monthly_background_collection", runner)

    def route_path(self, relative_path: str) -> Path:
        return self.route_library.route_path(relative_path)

    def import_route(self, source_name: str, data: Any) -> dict[str, Any]:
        route = self.route_library.import_document(source_name, data)
        self.hub.publish({"type": "routes_update"})
        return route

    def delete_route(self, relative_path: str) -> dict[str, Any]:
        route = self.route_library.delete(relative_path)
        self.hub.publish({"type": "routes_update"})
        return route

    def delete_geo_waypoint(self, job_id: str, waypoint_id: str) -> JobRecord:
        record = self.jobs.skip_geo_waypoint(
            job_id, waypoint_id, remove_from_route=self.route_library.delete_waypoint,
        )
        self.hub.publish({"type": "routes_update"})
        return record

    def recognition_model(self, payload: dict[str, Any]) -> Path:
        manager = ArtifactManager(self.config())
        paths = self._trained_model_paths(manager)
        requested_value = payload.get("model_path")
        if requested_value:
            requested = Path(str(requested_value))
            requested = requested if requested.is_absolute() else manager.project_root / requested
            requested = requested.resolve()
        else:
            requested = self._stored_default_model(manager, paths) or (paths[0] if paths else None)
        if requested is None or requested not in paths:
            raise ValueError("辨識只能選擇專案 outputs/training 內已訓練的模型")
        return requested

    def start_dataset(self, payload: dict[str, Any]) -> JobRecord:
        def runner(ctx: JobContext) -> dict[str, Any]:
            def report(message: str) -> None:
                ctx.check_cancelled()
                ctx.message(message)

            output = generate_dataset(
                self.config(),
                composites_override=payload.get("composites_per_background"),
                parallel_workers_override=payload.get("parallel_workers"),
                limit_backgrounds=payload.get("limit_backgrounds"),
                selected_backgrounds=payload.get("backgrounds"),
                selected_targets=payload.get("targets"),
                callback=report,
            )
            return {"output": str(output)}

        return self.jobs.start("dataset", runner)

    def start_background_collection(self, payload: dict[str, Any]) -> JobRecord:
        def runner(ctx: JobContext) -> dict[str, Any]:
            toolchain = find_toolchain()
            output, summary = collect_backgrounds(
                adb=toolchain.adb,
                serial=str(payload["serial"]),
                city=str(payload["city"]),
                horizontal_radius=int(payload.get("horizontal_radius", 3)),
                vertical_radius=int(payload.get("vertical_radius", 4)),
                settle_seconds=float(payload.get("settle_seconds", 4.0)),
                swipe_duration_ms=int(payload.get("swipe_duration_ms", 800)),
                stop_event=ctx.stop_event,
                status=ctx.message,
                progress=lambda done, total, point: ctx.progress(done, total, point),
            )
            return {"output": str(output), "summary": summary}

        return self.jobs.start("background_collection", runner)

    def start_recognition(self, payload: dict[str, Any]) -> JobRecord:
        model_path = self.recognition_model(payload)
        serial = str(payload["serial"])
        initial_filter = recognition_filter_from_payload(payload)
        current = self.device.status().get("current")
        start_coordinate = (
            self._coordinate(current.get("lat"), current.get("lng"))
            if isinstance(current, dict)
            else None
        )

        def runner(ctx: JobContext) -> dict[str, Any]:
            toolchain = find_toolchain()
            self.ensure_device_scale_for_search(
                serial, ctx=ctx, toolchain=toolchain
            )
            if start_coordinate is None:
                raise RuntimeError("無法取得第一個搜尋位置，不能在重啟前注入座標。")
            ctx.message("正在先注入第一個搜尋位置，再重新啟動 Pikmin Bloom…")
            self.device.command("teleport", start_coordinate)
            horizontal_radius = int(payload.get("horizontal_radius", 3))
            vertical_radius = int(payload.get("vertical_radius", 4))
            precise_coordinates = bool(
                payload.get(
                    "precise_coordinates",
                    payload.get("estimate_coordinates", True),
                )
            )
            if precise_coordinates:
                ctx.message(
                    "命中座標將由蘑菇的地圖連結直接取得；擷取期間 Android 會停用瞬移。"
                )
            with _recognition_frame_source(
                toolchain, serial, ctx.stop_event, ctx.message
            ) as frame_source:
                output, summary = run_recognition_scan(
                    adb=toolchain.adb,
                    serial=serial,
                    model_path=model_path,
                    horizontal_radius=horizontal_radius,
                    vertical_radius=vertical_radius,
                    left_radius=int(payload.get("left_radius", horizontal_radius)),
                    right_radius=int(payload.get("right_radius", horizontal_radius)),
                    up_radius=int(payload.get("up_radius", vertical_radius)),
                    down_radius=int(payload.get("down_radius", vertical_radius)),
                    settle_seconds=float(payload.get("settle_seconds", 4.0)),
                    confidence=float(payload.get("confidence", 0.45)),
                    image_size=INFERENCE_IMAGE_SIZE,
                    swipe_duration_ms=int(payload.get("swipe_duration_ms", 800)),
                    save_all_frames=bool(payload.get("save_all_frames", False)),
                    target_names=initial_filter["target_names"],
                    participant_count=initial_filter["participant_count"],
                    filter_provider=ctx.recognition_filter,
                    stop_event=ctx.stop_event,
                    status=ctx.message,
                    progress=lambda done, total, point: ctx.progress(done, total, point),
                    preview=ctx.preview,
                    detection_update=ctx.detections,
                    memory_update=ctx.memory,
                    pause_checkpoint=ctx.pause_checkpoint,
                    capture_png=(frame_source.capture_png if frame_source else None),
                    capture_image=(frame_source.capture_image if frame_source else None),
                    precise_coordinates=precise_coordinates,
                    coordinate_controller=self.device.controller(),
                )
            return {"output": str(output), "summary": summary}

        return self.jobs.start(
            "screen_recognition",
            runner,
            recognition_filter=initial_filter,
        )

    def start_geo_sequence_recognition(self, payload: dict[str, Any]) -> JobRecord:
        waypoints = geo_waypoints_from_payload(payload)
        route_path = str(payload.get("route_path") or "") or None
        if route_path is not None:
            route_path = self.route_library.route_path(route_path).relative_to(
                self.route_library.directory.resolve()
            ).as_posix()
        randomize_waypoints = bool(payload.get("randomize_waypoints", False))
        if randomize_waypoints:
            random.SystemRandom().shuffle(waypoints)
        model_path = self.recognition_model(payload)
        # Extract scalar options before the worker closure is created. Holding
        # the original request would keep a second large waypoint payload in
        # memory for the entire route.
        serial = str(payload.get("serial") or self.device.serial)
        sequence_name = str(payload.get("sequence_name") or "座標序列")[:120]
        settle_seconds = float(payload.get("settle_seconds", 4.0))
        confidence = float(payload.get("confidence", 0.45))
        swipe_duration_ms = int(payload.get("swipe_duration_ms", 800))
        movement_mode = str(payload.get("movement_mode", "teleport"))
        speed_kmh = float(payload.get("speed_kmh", 10.8))
        use_path = bool(payload.get("use_path", False))
        return_to_origin = bool(payload.get("return_to_origin", True))
        save_all_frames = bool(payload.get("save_all_frames", False))
        bookmark_detection = bool(payload.get("bookmark_detection", True))
        initial_filter = recognition_filter_from_payload(payload)

        def runner(ctx: JobContext) -> dict[str, Any]:
            toolchain = find_toolchain()
            self.ensure_device_scale_for_search(
                serial, ctx=ctx, toolchain=toolchain
            )
            with _recognition_frame_source(
                toolchain, serial, ctx.stop_event, ctx.message
            ) as frame_source:
                output, summary = run_geographic_recognition_sequence(
                    controller=self.device.controller(),
                    adb=toolchain.adb,
                    serial=serial,
                    model_path=model_path,
                    waypoints=waypoints,
                    waypoint_started=ctx.begin_waypoint,
                    waypoint_finished=ctx.finish_waypoint,
                    sequence_name=sequence_name,
                    settle_seconds=settle_seconds,
                    confidence=confidence,
                    image_size=INFERENCE_IMAGE_SIZE,
                    swipe_duration_ms=swipe_duration_ms,
                    movement_mode=movement_mode,
                    speed_kmh=speed_kmh,
                    use_path=use_path,
                    return_to_origin=return_to_origin,
                    save_all_frames=save_all_frames,
                    bookmark_detection=bookmark_detection,
                    target_names=initial_filter["target_names"],
                    participant_count=initial_filter["participant_count"],
                    filter_provider=ctx.recognition_filter,
                    stop_event=ctx.stop_event,
                    status=ctx.message,
                    progress=lambda done, total, detail: ctx.progress(done, total, detail),
                    preview=ctx.preview,
                    detection_update=ctx.detections,
                    memory_update=ctx.memory,
                    pause_checkpoint=ctx.pause_checkpoint,
                    capture_png=(frame_source.capture_png if frame_source else None),
                    capture_image=(frame_source.capture_image if frame_source else None),
                    precise_coordinates=True,
                )
            return {"output": str(output), "summary": summary}

        return self.jobs.start(
            "geo_sequence_recognition",
            runner,
            recognition_filter=initial_filter,
            route_path=route_path,
        )

    def start_live_recognition(self, payload: dict[str, Any]) -> JobRecord:
        model_path = self.recognition_model(payload)
        serial = str(payload["serial"])
        initial_filter = recognition_filter_from_payload(payload)
        current = self.device.status().get("current")
        start_coordinate = (
            self._coordinate(current.get("lat"), current.get("lng"))
            if isinstance(current, dict)
            else None
        )

        def runner(ctx: JobContext) -> dict[str, Any]:
            import torch
            from ultralytics import YOLO

            toolchain = find_toolchain()
            client = AdbClient(toolchain.adb, serial)
            client.ensure_ready()
            self.ensure_device_scale_for_search(
                serial, ctx=ctx, toolchain=toolchain
            )
            frame_source = _open_recognition_frame_source(
                toolchain, serial, ctx.stop_event, ctx.message
            )
            if start_coordinate is None:
                raise RuntimeError("無法取得第一個搜尋位置，不能在重啟前注入座標。")
            ctx.message("正在先注入第一個搜尋位置，再重新啟動 Pikmin Bloom…")
            self.device.command("teleport", start_coordinate)
            memory_watchdog = PikminMemoryWatchdog(
                client,
                stop_event=ctx.stop_event,
                status=ctx.message,
                update=ctx.memory,
                capture_image=(frame_source.capture_image if frame_source else None),
            )
            if not memory_watchdog.prepare_for_scan():
                raise RuntimeError("即時辨識開始前無法完成遊戲重啟與最大範圍地圖準備。")
            from ..mushroom_coordinate import (
                MushroomCoordinateResolver,
                enrich_detections_with_precise_coordinates,
            )

            coordinate_resolver = MushroomCoordinateResolver(
                client,
                self.device.controller(),
                stop_event=ctx.stop_event,
                capture_image=(frame_source.capture_image if frame_source else None),
                status=ctx.message,
            )
            if not model_path.is_file():
                raise FileNotFoundError(f"找不到模型：{model_path}")
            confidence = float(payload.get("confidence", 0.45))
            participant_count = initial_filter["participant_count"]
            interval = max(0.15, float(payload.get("interval_seconds", 0.5)))
            device: int | str = 0 if torch.cuda.is_available() else "cpu"
            ctx.message("正在載入即時辨識模型…")
            model = YOLO(str(model_path))
            classes = normalize_model_class_names(model.names)
            _, selected_ids = resolve_target_selection(
                classes,
                initial_filter["target_names"],
            )
            args: dict[str, Any] = {
                "imgsz": INFERENCE_IMAGE_SIZE,
                "conf": confidence,
                "device": device,
                "verbose": False,
            }
            if selected_ids is not None:
                args["classes"] = selected_ids
            frames = 0
            current = self.device.status().get("current")
            center = current if isinstance(current, dict) and "lat" in current and "lng" in current else None
            precise_cache: dict[tuple[str, int, int], dict[str, object]] = {}
            while not ctx.stop_event.is_set():
                if ctx.pause_checkpoint():
                    current = self.device.status().get("current")
                    center = (
                        current
                        if isinstance(current, dict) and "lat" in current and "lng" in current
                        else center
                    )
                    precise_cache.clear()
                live_filter = ctx.recognition_filter() or initial_filter
                _, selected_ids = resolve_target_selection(
                    classes,
                    live_filter.get("target_names"),
                )
                if selected_ids is None:
                    args.pop("classes", None)
                else:
                    args["classes"] = selected_ids
                participant_count = normalize_participant_count_filter(
                    live_filter.get("participant_count")
                )
                memory_watchdog.before_capture()
                if ctx.stop_event.is_set():
                    break
                if frame_source is not None:
                    frame = frame_source.capture_image()
                else:
                    raw = client.capture_png()
                    frame = cv2.imdecode(np.frombuffer(raw, dtype=np.uint8), cv2.IMREAD_COLOR)
                    if frame is None:
                        raise RuntimeError("無法解碼手機畫面。")
                result = model.predict(frame, **args)[0]
                detections = detections_from_result(result)
                enrich_detections_with_badge_numbers(frame, detections)
                detections = filter_detections_by_participant_count(
                    detections, participant_count
                )
                unresolved: list[dict[str, object]] = []
                for detection in detections:
                    xyxy = detection.get("xyxy")
                    if isinstance(xyxy, (list, tuple)) and len(xyxy) == 4:
                        try:
                            center_x = (float(xyxy[0]) + float(xyxy[2])) / 2.0
                            center_y = (float(xyxy[1]) + float(xyxy[3])) / 2.0
                        except (TypeError, ValueError, OverflowError):
                            unresolved.append(detection)
                            continue
                        key = (
                            str(detection.get("class_name", "")),
                            round(center_x / 32.0),
                            round(center_y / 32.0),
                        )
                        cached = precise_cache.get(key)
                        if cached is not None:
                            detection["precise_coordinate"] = dict(cached)
                            continue
                        detection["_precise_cache_key"] = key
                    unresolved.append(detection)
                enrich_detections_with_precise_coordinates(unresolved, coordinate_resolver)
                for detection in unresolved:
                    key = detection.pop("_precise_cache_key", None)
                    coordinate = detection.get("precise_coordinate")
                    if isinstance(key, tuple) and isinstance(coordinate, dict):
                        precise_cache[key] = dict(coordinate)
                frames += 1
                annotated, _ = draw_detection_records(detections, frame)
                ctx.preview(annotated)
                event_detections = detections_with_crops(frame, detections)
                if event_detections:
                    ctx.detections({
                        "frame": frames,
                        "center": center,
                        "point": {"grid_x": 0, "grid_y": 0},
                    }, event_detections)
                ctx.progress(frames, 0, {"detections": len(detections)})
                ctx.stop_event.wait(interval)
            return {"frames": frames, "memory_restarts": memory_watchdog.restart_count}

        return self.jobs.start(
            "live_recognition",
            runner,
            recognition_filter=initial_filter,
        )

    def _run_training_process(
        self,
        ctx: JobContext,
        dataset: Path,
        *,
        phase: str = "",
        model: Path | None = None,
        run_name: str | None = None,
        allow_resume: bool = True,
        require_completed: bool = False,
    ) -> dict[str, Any]:
        control = TrainingControl(TRAINING_CONTROL_DIR)
        control.clear_requests()
        command = [sys.executable, "-u", "-X", "utf8", "-m", "bloompilot.training_worker"]
        command.extend(["--dataset", str(dataset)])
        if model is not None:
            command.extend(["--model", str(model)])
        if run_name:
            command.extend(["--run-name", run_name])
        if not allow_resume:
            command.append("--no-resume")
        prefix = f"{phase}｜" if phase else ""
        ctx.message(f"{prefix}正在啟動訓練程序…")
        process = subprocess.Popen(
            command,
            cwd=PROJECT_ROOT,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        lines: queue.Queue[str | None] = queue.Queue()

        def read_output() -> None:
            assert process.stdout is not None
            for line in process.stdout:
                lines.put(sanitize_training_output(line))
            lines.put(None)

        threading.Thread(target=read_output, daemon=True).start()
        output_tail: list[str] = []
        while process.poll() is None or not lines.empty():
            self.jobs.sync_training_stop(ctx.job_id, control)
            try:
                line = lines.get(timeout=0.5)
            except queue.Empty:
                status = control.read_status()
                if status:
                    completed = int(status.get("completed_epoch", 0) or 0)
                    total = int(status.get("total_epochs", 0) or 0)
                    ctx.progress(completed, total, {**status, "phase": phase})
                continue
            if line is None:
                continue
            output_tail.append(line)
            output_tail = output_tail[-80:]
            if line:
                ctx.message(prefix + line)
        return_code = process.wait()
        status = control.read_status()
        if return_code != 0:
            raise RuntimeError(f"{phase or '訓練'}失敗。\n" + "\n".join(output_tail[-20:]))
        if ctx.stop_event.is_set():
            raise JobCancelled(f"{phase or '訓練'}已在 checkpoint 保存後停止。")
        if require_completed and status.get("state") != "completed":
            raise RuntimeError(f"{phase or '訓練'}沒有產生完整模型：{status.get('message') or '狀態未知'}")
        return {"status": status, "return_code": return_code}

    def start_training(self, payload: dict[str, Any]) -> JobRecord:
        requested = str(payload.get("dataset") or "").strip()
        dataset = resolve_dataset(self.config(), Path(requested) if requested else None)

        def runner(ctx: JobContext) -> dict[str, Any]:
            ctx.message("正在啟動訓練程序，直接使用專案生成資料集（略過圖片與標註檢查）…")
            return self._run_training_process(ctx, dataset)

        return self.jobs.start("training", runner)

    def start_monthly_training(self, payload: dict[str, Any]) -> JobRecord:
        cycle = validate_cycle(str(payload.get("cycle") or ""))
        if not self.monthly_workflow.review_is_current(cycle):
            raise ValueError("請先逐張檢查背景並按下「背景審核完成」。")
        if not self.monthly_workflow.list_activity_targets(cycle):
            raise ValueError("請先加入本月活動菇透明 PNG。")
        resident_model = self.monthly_workflow.resident_model()
        if resident_model is None:
            raise ValueError("請先指定目前不含活動菇的常駐模型。")
        composites = payload.get("composites_per_background")
        workers = payload.get("parallel_workers")

        def runner(ctx: JobContext) -> dict[str, Any]:
            config = self.config()
            stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            previous_resident_source = self.monthly_workflow.resident_metadata().get("source")
            previous_default_model = load_studio_settings().get("recognition_default_model")

            def generate(role: str, *, mixed: bool) -> Path:
                label = "常駐模型資料" if role == "resident" else "活動菇混合資料"
                ctx.message(f"{label}｜正在合成資料集…")

                def report(message: str) -> None:
                    ctx.check_cancelled()
                    ctx.message(f"{label}｜{message}")

                active_config = self.monthly_workflow.dataset_config(
                    config, cycle, mixed_targets=mixed
                )
                dataset = generate_dataset(
                    active_config,
                    composites_override=composites,
                    parallel_workers_override=workers,
                    callback=report,
                )
                self.monthly_workflow.annotate_dataset(dataset, cycle=cycle, role=role)
                self.monthly_workflow.update_manifest(
                    cycle, **{f"{role}_dataset": str(dataset)}
                )
                return dataset

            resident_dataset = generate("resident", mixed=False)
            resident_result = self._run_training_process(
                ctx,
                resident_dataset,
                phase="常駐模型強化",
                model=resident_model,
                run_name=f"resident_{cycle}_{stamp}",
                allow_resume=False,
                require_completed=True,
            )
            resident_best = Path(str(resident_result["status"].get("checkpoint") or "")).resolve()
            if not resident_best.is_file():
                raise RuntimeError("常駐模型強化完成，但找不到 best.pt。")
            resident_run = resident_best.parent.parent
            annotate_training_run(resident_run, cycle=cycle, role="resident")
            resident_metadata = json.loads(
                (resident_dataset / "metadata.json").read_text(encoding="utf-8")
            )
            promoted = self.monthly_workflow.set_resident_model(
                resident_best,
                source_label=f"{cycle} 每月流程：常駐模型強化",
                class_names=resident_metadata.get("class_names", []),
                cycle=cycle,
            )
            self.monthly_workflow.update_manifest(
                cycle,
                resident_training_run=str(resident_run),
                resident_promoted_at=utc_now(),
            )

            event_dataset = generate("event", mixed=True)
            event_result = self._run_training_process(
                ctx,
                event_dataset,
                phase="本月活動菇模型",
                model=promoted,
                run_name=f"activity_{cycle}_{stamp}",
                allow_resume=False,
                require_completed=True,
            )
            event_best = Path(str(event_result["status"].get("checkpoint") or "")).resolve()
            if not event_best.is_file():
                raise RuntimeError("活動菇模型訓練完成，但找不到 best.pt。")
            event_run = event_best.parent.parent
            annotate_training_run(event_run, cycle=cycle, role="event")
            self.monthly_workflow.update_manifest(
                cycle,
                event_model=str(event_best),
                event_training_run=str(event_run),
                completed_at=utc_now(),
            )
            self.set_default_recognition_model(str(event_best))
            removed_source = self.monthly_workflow.delete_superseded_resident_source(
                previous_resident_source,
                keep=(resident_best, event_best, promoted),
            )
            removed_event = self.monthly_workflow.delete_superseded_monthly_model(
                previous_default_model,
                keep=(resident_best, event_best, promoted),
            )
            if removed_source is not None:
                self.monthly_workflow.update_manifest(
                    cycle, removed_resident_source=str(removed_source)
                )
            if removed_event is not None:
                self.monthly_workflow.update_manifest(
                    cycle, removed_event_model=str(removed_event)
                )
            cleanup = self.monthly_workflow.cleanup_completed_cycle(
                config,
                cycle,
                (resident_dataset, event_dataset),
            )
            ctx.message("訓練完成；已自動清理可重建資料集、暫存素材與舊月份資料。")
            ArtifactManager(self.config()).refresh_catalog()
            self.hub.publish({"type": "monthly_workflow_update", "cycle": cycle})
            return {
                "cycle": cycle,
                "resident_dataset": None,
                "resident_model": str(promoted),
                "event_dataset": None,
                "event_model": str(event_best),
                "automatic_cleanup": cleanup,
            }

        return self.jobs.start("monthly_training", runner)

    def pause_training(self) -> dict[str, Any]:
        active = self.jobs.active()
        if active is None or active.kind != "training" or active.state not in {"queued", "running"}:
            raise RuntimeError("目前沒有可暫停的訓練工作。")
        path = TrainingControl(TRAINING_CONTROL_DIR).request("pause")
        return {"requested": "pause", "path": str(path)}

    def pause_recognition(self, job_id: str) -> JobRecord:
        self.jobs.pause_recognition(job_id)
        try:
            current = self.device.status().get("current")
            if not isinstance(current, dict):
                raise RuntimeError("無法取得目前搜尋座標，不能安全暫停。")
            resume_coordinate = self._coordinate(current.get("lat"), current.get("lng"))
            serial = self.device.serial
            if not serial:
                raise RuntimeError("手機序號遺失，不能安全暫停。")
        except Exception:
            self.jobs.cancel_recognition_pause(job_id)
            raise
        message = "辨識已安全暫停；可按「重新開啟手機控制」在電腦上操作。"
        if os.environ.get("BLOOMPILOT_DESKTOP") == "1":
            try:
                self.screen_control.open(serial)
            except Exception as exc:
                message = (
                    "辨識已安全暫停，但手機滑鼠控制未能自動開啟："
                    f"{str(exc).splitlines()[0]}。可按「重新開啟手機控制」再試。"
                )
            else:
                message = (
                    "辨識已安全暫停；手機滑鼠控制已開啟。"
                    "可在電腦上點擊、拖曳與輸入，完成後回 BloomPilot 按「繼續辨識」。"
                )
        return self.jobs.update(
            job_id,
            paused=True,
            resuming=False,
            resume_coordinate=resume_coordinate,
            resume_serial=serial,
            message=message,
        )

    def open_screen_control(self, serial: str | None = None) -> dict[str, object]:
        selected = str(serial or self.device.serial).strip()
        if not self.device.connected or not selected or self.device.serial != selected:
            raise RuntimeError("請先連接要控制的 Android 手機。")
        active = self.jobs.active()
        if active is None or active.kind not in {
            "screen_recognition",
            "geo_sequence_recognition",
            "live_recognition",
        } or not active.paused or active.resuming:
            raise RuntimeError("為避免干擾工作，請先將手機辨識安全暫停。")
        self.screen_control.close()
        return {"ok": True, **self.screen_control.open(selected)}

    def close_screen_control(self) -> dict[str, object]:
        return {"ok": True, **self.screen_control.close()}

    def stop_job(self, job_id: str) -> JobRecord:
        record = self.jobs.get(job_id)
        if record.kind in {
            "screen_recognition",
            "geo_sequence_recognition",
            "live_recognition",
        }:
            self.screen_control.close()
        return self.jobs.stop(job_id)

    @staticmethod
    def _coordinate(lat: object, lng: object) -> dict[str, float]:
        try:
            latitude = float(lat)
            longitude = float(lng)
        except (TypeError, ValueError) as exc:
            raise ValueError("命中座標格式錯誤。") from exc
        if (
            not math.isfinite(latitude)
            or not math.isfinite(longitude)
            or not -90 <= latitude <= 90
            or not -180 <= longitude <= 180
        ):
            raise ValueError("命中座標超出有效經緯度範圍。")
        return {"lat": latitude, "lng": longitude}

    def teleport_while_paused(
        self,
        job_id: str,
        *,
        lat: float,
        lng: float,
        label: str = "",
    ) -> JobRecord:
        target = self._coordinate(lat, lng)
        record = self.jobs.get(job_id)
        if (
            not record.paused
            or record.resuming
            or not isinstance(record.resume_coordinate, dict)
        ):
            raise RuntimeError("請先暫停辨識，才能瞬移到命中位置。")
        excursion = {
            "origin": dict(record.resume_coordinate),
            "target": target,
            "label": str(label).strip()[:200],
            "serial": record.resume_serial or self.device.serial,
        }

        try:
            self.device.command("teleport", target)
        except Exception:
            self.jobs.update(
                job_id,
                message="瞬移命令未完成；辨識仍保持暫停，可直接重試。",
            )
            raise
        else:
            return self.jobs.update(
                job_id,
                paused=True,
                excursion=excursion,
                message="已瞬移到命中的精確位置；按繼續會回注搜尋座標並重啟遊戲。",
            )

    def resume_recognition(
        self,
        job_id: str,
        recognition_filter: dict[str, Any] | None = None,
    ) -> JobRecord:
        record = self.jobs.get(job_id)
        if (
            not record.paused
            or record.resuming
            or not isinstance(record.resume_coordinate, dict)
        ):
            raise RuntimeError("辨識工作目前沒有暫停，或恢復座標已遺失。")
        origin = self._coordinate(
            record.resume_coordinate.get("lat"),
            record.resume_coordinate.get("lng"),
        )
        serial = str(record.resume_serial or self.device.serial)
        if not serial:
            raise RuntimeError("手機序號遺失，無法恢復原本搜尋位置。")
        if recognition_filter is not None:
            self.jobs.set_recognition_filter(job_id, recognition_filter)

        self.screen_control.close()
        self.jobs.update(
            job_id,
            resuming=True,
            message="正在重新注入暫停前搜尋座標…",
        )
        try:
            self.device.command("teleport", origin)
            toolchain = find_toolchain()
            client = AdbClient(toolchain.adb, serial)
            client.ensure_ready()
            resume_stop = self.jobs.stop_event(job_id)
            watchdog = PikminMemoryWatchdog(
                client,
                stop_event=resume_stop,
                status=lambda message: self.jobs.update(job_id, message=message),
                update=lambda snapshot: self.hub.publish(
                    {
                        "type": "memory",
                        "job_id": job_id,
                        "memory": json_value(snapshot),
                    }
                ),
            )
            if not watchdog.prepare_for_scan(force_restart=True):
                raise RuntimeError("已回注搜尋座標，但 Pikmin Bloom 未完成重啟與地圖準備；辨識仍保持暫停。")
            self.jobs.update(job_id, message="地圖已準備完成，等待 10 秒讓目標載入…")
            if resume_stop.wait(10.0):
                raise RuntimeError("辨識已停止，不再恢復暫停中的工作。")
            return self.jobs.resume_recognition(job_id)
        except Exception:
            self.jobs.update(job_id, resuming=False)
            raise

    def cancel_training_stop(self, job_id: str) -> JobRecord:
        return self.jobs.cancel_training_stop(job_id, TrainingControl(TRAINING_CONTROL_DIR))

    def close(self) -> None:
        active = self.jobs.active()
        if active is not None:
            self.jobs.stop(active.id)
        self.screen_control.close()
        self.discord_notifier.close()
        self.device.close()
