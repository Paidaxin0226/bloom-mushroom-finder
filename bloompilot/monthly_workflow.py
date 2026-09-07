"""Monthly activity-mushroom capture, review, dataset, and model state."""

from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import shutil
import threading
from copy import deepcopy
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Iterable

import numpy as np
from PIL import Image

from .dataset_builder import IMAGE_EXTENSIONS, discover_targets
from .device_control import (
    DeviceControlError,
    UsbDeviceController,
    reconnect_usb,
    restart_locwarp,
)
from .memory_watchdog import PikminMemoryWatchdog
from .paths import (
    MONTHLY_ACTIVITY_DIR,
    MONTHLY_ACTIVITY_STATE_PATH,
    PROJECT_ROOT,
    RESIDENT_MODELS_DIR,
)
from .phone_automation import (
    AdbClient,
    CaptureOutcome,
    ScanPoint,
    atomic_write_bytes,
    run_rectangular_scan,
    slugify_city,
)
from .route_library import RouteLibrary


_CYCLE_RE = re.compile(r"^\d{4}-(?:0[1-9]|1[0-2])$")
_SAFE_NAME_RE = re.compile(r"[^A-Za-z0-9._-]+")
_MAX_TARGET_BYTES = 20 * 1024 * 1024
_STATE_VERSION = 1


def _now() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def validate_cycle(value: str) -> str:
    cycle = str(value).strip()
    if not _CYCLE_RE.fullmatch(cycle):
        raise ValueError("月份必須使用 YYYY-MM 格式。")
    return cycle


def _atomic_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        temporary.write_text(
            json.dumps(value, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _read_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def _safe_relative(root: Path, value: str) -> Path:
    relative = Path(str(value).replace("\\", "/"))
    if not str(value).strip() or relative.is_absolute() or ".." in relative.parts:
        raise ValueError("檔案路徑無效。")
    candidate = (root / relative).resolve()
    if not candidate.is_relative_to(root.resolve()):
        raise ValueError("檔案路徑超出每月活動資料目錄。")
    return candidate


def _safe_upload_name(value: str) -> str:
    stem = _SAFE_NAME_RE.sub("_", Path(value).stem).strip("._")[:80]
    if not stem:
        stem = "activity_mushroom"
    return f"{stem}.png"


class MonthlyWorkflowStore:
    def __init__(
        self,
        *,
        project_root: Path = PROJECT_ROOT,
        workspace_root: Path = MONTHLY_ACTIVITY_DIR,
        state_path: Path = MONTHLY_ACTIVITY_STATE_PATH,
        resident_root: Path = RESIDENT_MODELS_DIR,
    ) -> None:
        self.project_root = project_root.resolve()
        self.workspace_root = workspace_root.resolve()
        self.state_path = state_path.resolve()
        self.resident_root = resident_root.resolve()
        self._lock = threading.RLock()

    def _state(self) -> dict[str, Any]:
        value = _read_json(self.state_path)
        value.setdefault("version", _STATE_VERSION)
        value.setdefault("fixed_routes", [])
        return value

    def _save_state(self, value: dict[str, Any]) -> None:
        value["version"] = _STATE_VERSION
        value["updated_at"] = _now()
        _atomic_json(self.state_path, value)

    def cycle_dir(self, cycle: str) -> Path:
        return self.workspace_root / validate_cycle(cycle)

    def backgrounds_dir(self, cycle: str) -> Path:
        return self.cycle_dir(cycle) / "backgrounds"

    def targets_dir(self, cycle: str) -> Path:
        return self.cycle_dir(cycle) / "activity_targets"

    def _manifest_path(self, cycle: str) -> Path:
        return self.cycle_dir(cycle) / "manifest.json"

    def manifest(self, cycle: str) -> dict[str, Any]:
        cycle = validate_cycle(cycle)
        value = _read_json(self._manifest_path(cycle))
        value.setdefault("version", _STATE_VERSION)
        value.setdefault("cycle", cycle)
        return value

    def update_manifest(self, cycle: str, **changes: Any) -> dict[str, Any]:
        cycle = validate_cycle(cycle)
        with self._lock:
            value = self.manifest(cycle)
            value.update(changes)
            value["updated_at"] = _now()
            _atomic_json(self._manifest_path(cycle), value)
            return value

    def fixed_routes(self) -> list[str]:
        return [str(item) for item in self._state().get("fixed_routes", []) if str(item).strip()]

    def set_fixed_routes(self, routes: Iterable[str]) -> list[str]:
        normalized = list(dict.fromkeys(str(item).strip() for item in routes if str(item).strip()))
        with self._lock:
            state = self._state()
            state["fixed_routes"] = normalized
            self._save_state(state)
        return normalized

    def resident_model(self) -> Path | None:
        raw = self._state().get("resident_model")
        if not isinstance(raw, str) or not raw.strip():
            return None
        path = Path(raw)
        path = path if path.is_absolute() else self.project_root / path
        path = path.resolve()
        return path if path.is_file() and path.suffix.casefold() == ".pt" else None

    def resident_metadata(self) -> dict[str, Any]:
        return _read_json(self.resident_root / "metadata.json")

    def set_resident_model(
        self,
        source: Path,
        *,
        source_label: str = "",
        class_names: Iterable[str] | None = None,
        cycle: str | None = None,
    ) -> Path:
        source = source.resolve()
        if not source.is_file() or source.suffix.casefold() != ".pt":
            raise FileNotFoundError(f"找不到可用的常駐模型權重：{source}")
        if not source.is_relative_to(self.project_root):
            raise ValueError("常駐模型必須來自 BloomPilot 專案。")
        self.resident_root.mkdir(parents=True, exist_ok=True)
        destination = self.resident_root / "current.pt"
        temporary = self.resident_root / f".current.{os.getpid()}.tmp"
        with self._lock:
            try:
                shutil.copyfile(source, temporary)
                os.replace(temporary, destination)
            finally:
                temporary.unlink(missing_ok=True)
            metadata = {
                "version": _STATE_VERSION,
                "role": "resident",
                "updated_at": _now(),
                "source": source.relative_to(self.project_root).as_posix(),
                "source_label": source_label,
                "cycle": validate_cycle(cycle) if cycle else None,
                "class_names": [str(name) for name in (class_names or [])],
            }
            _atomic_json(self.resident_root / "metadata.json", metadata)
            state = self._state()
            state["resident_model"] = destination.relative_to(self.project_root).as_posix()
            state["resident_metadata"] = metadata
            self._save_state(state)
        return destination

    def delete_superseded_resident_source(
        self,
        source_value: object,
        *,
        keep: Iterable[Path] = (),
    ) -> Path | None:
        """Delete an old training weight after its replacement is fully usable."""
        if not isinstance(source_value, str) or not source_value.strip():
            return None
        candidate = Path(source_value)
        candidate = candidate if candidate.is_absolute() else self.project_root / candidate
        candidate = candidate.resolve()
        training_root = (self.project_root / "outputs" / "training").resolve()
        kept = {path.resolve() for path in keep}
        if (
            candidate in kept
            or candidate.suffix.casefold() != ".pt"
            or not candidate.is_relative_to(training_root)
        ):
            return None
        if not candidate.is_file():
            return None
        candidate.unlink()
        return candidate

    def delete_superseded_monthly_model(
        self,
        source_value: object,
        *,
        keep: Iterable[Path] = (),
    ) -> Path | None:
        """Delete a previous monthly event weight after the new default is active."""
        if not isinstance(source_value, str) or not source_value.strip():
            return None
        candidate = Path(source_value)
        candidate = candidate if candidate.is_absolute() else self.project_root / candidate
        candidate = candidate.resolve()
        training_root = (self.project_root / "outputs" / "training").resolve()
        kept = {path.resolve() for path in keep}
        if (
            candidate in kept
            or candidate.suffix.casefold() != ".pt"
            or not candidate.is_relative_to(training_root)
            or not candidate.is_file()
        ):
            return None
        metadata = _read_json(candidate.parent.parent / ".artifact.json").get("monthly_workflow")
        if not isinstance(metadata, dict) or metadata.get("role") != "event":
            return None
        candidate.unlink()
        return candidate

    def _configured_path(
        self,
        config: dict[str, Any],
        key: str,
        default: str,
    ) -> Path:
        root = Path(str(config.get("_project_root") or self.project_root)).resolve()
        value = Path(str(config.get("paths", {}).get(key, default)))
        return (value if value.is_absolute() else root / value).resolve()

    @staticmethod
    def _clean_background_files(root: Path) -> list[Path]:
        if not root.is_dir():
            return []
        return [
            path
            for path in sorted(root.rglob("*"))
            if path.is_file()
            and path.suffix.casefold() in IMAGE_EXTENSIONS
            and "_target_" not in path.name.casefold()
        ]

    @staticmethod
    def _background_bundle_path(prefix: str, root: Path, source: Path) -> Path:
        relative = source.relative_to(root)
        group = relative.parts[0] if len(relative.parts) > 1 else "root"
        group = _SAFE_NAME_RE.sub("_", group).strip("._") or "root"
        tail = Path(*relative.parts[1:]) if len(relative.parts) > 1 else Path(source.name)
        return Path(f"{prefix}_{group}") / tail

    def prepare_training_backgrounds(self, config: dict[str, Any], cycle: str) -> Path:
        """Build a disposable view of clean baseline plus this month's approved frames."""
        cycle = validate_cycle(cycle)
        if not self.review_is_current(cycle):
            raise ValueError("當月背景尚未通過人工審核。")
        baseline_root = self._configured_path(config, "backgrounds", "data/source/backgrounds")
        monthly_root = self.backgrounds_dir(cycle)
        baseline = self._clean_background_files(baseline_root)
        monthly = self._clean_background_files(monthly_root)
        if not monthly:
            raise ValueError("當月沒有可用的已核准背景。")

        bundle = self.cycle_dir(cycle) / "training_backgrounds"
        temporary = self.cycle_dir(cycle) / f".training_backgrounds.{os.getpid()}.building"
        with self._lock:
            if temporary.exists():
                shutil.rmtree(temporary)
            temporary.mkdir(parents=True)
            try:
                for prefix, root, sources in (
                    ("base", baseline_root, baseline),
                    ("month", monthly_root, monthly),
                ):
                    for source in sources:
                        destination = temporary / self._background_bundle_path(prefix, root, source)
                        destination.parent.mkdir(parents=True, exist_ok=True)
                        try:
                            os.link(source, destination)
                        except OSError:
                            shutil.copy2(source, destination)
                if bundle.exists():
                    shutil.rmtree(bundle)
                os.replace(temporary, bundle)
            finally:
                if temporary.exists():
                    shutil.rmtree(temporary)
        self.update_manifest(
            cycle,
            background_policy="clean_baseline_plus_current_month",
            baseline_background_count=len(baseline),
            monthly_background_count=len(monthly),
            excluded_marked_background_count=(
                len([
                    path for path in baseline_root.rglob("*")
                    if path.is_file() and path.suffix.casefold() in IMAGE_EXTENSIONS
                ]) - len(baseline)
            ) if baseline_root.is_dir() else 0,
        )
        return bundle

    def cleanup_completed_cycle(
        self,
        config: dict[str, Any],
        cycle: str,
        datasets: Iterable[Path],
    ) -> dict[str, Any]:
        """Remove rebuildable monthly data and previous monthly workspaces."""
        cycle = validate_cycle(cycle)
        generated_root = self._configured_path(config, "generated_root", "data/generated")
        removed_datasets: list[str] = []
        for value in datasets:
            candidate = value.resolve()
            if (
                candidate != generated_root
                and candidate.is_relative_to(generated_root)
                and candidate.is_dir()
            ):
                shutil.rmtree(candidate)
                removed_datasets.append(str(candidate))
        latest_pointer = generated_root / "LATEST.txt"
        if latest_pointer.is_file():
            try:
                raw = latest_pointer.read_text(encoding="utf-8").strip()
                selected = Path(raw)
                selected = selected if selected.is_absolute() else generated_root / selected
                if not selected.resolve().exists():
                    latest_pointer.unlink()
            except OSError:
                pass

        removed_intermediates: list[str] = []
        for name in ("training_backgrounds", "mixed_targets"):
            candidate = self.cycle_dir(cycle) / name
            if candidate.is_dir():
                shutil.rmtree(candidate)
                removed_intermediates.append(str(candidate))

        removed_cycles: list[str] = []
        if self.workspace_root.is_dir():
            for candidate in self.workspace_root.iterdir():
                if (
                    candidate.is_dir()
                    and candidate.name != cycle
                    and _CYCLE_RE.fullmatch(candidate.name)
                ):
                    shutil.rmtree(candidate)
                    removed_cycles.append(candidate.name)

        self.update_manifest(
            cycle,
            resident_dataset=None,
            event_dataset=None,
            intermediates_cleaned_at=_now(),
            removed_previous_cycles=removed_cycles,
        )
        return {
            "datasets": removed_datasets,
            "intermediates": removed_intermediates,
            "cycles": removed_cycles,
        }

    def _background_signature(self, cycle: str) -> list[list[Any]]:
        root = self.backgrounds_dir(cycle)
        return [
            [item["path"], item["size_bytes"], item["modified_ns"]]
            for item in self.list_backgrounds(cycle)
        ]

    def list_backgrounds(self, cycle: str) -> list[dict[str, Any]]:
        root = self.backgrounds_dir(cycle)
        if not root.is_dir():
            return []
        result: list[dict[str, Any]] = []
        for path in sorted(root.rglob("*")):
            if not path.is_file() or path.suffix.casefold() not in IMAGE_EXTENSIONS:
                continue
            stat = path.stat()
            relative = path.relative_to(root).as_posix()
            result.append({
                "path": relative,
                "name": path.name,
                "route": relative.split("/", 1)[0],
                "size_bytes": stat.st_size,
                "modified_ns": stat.st_mtime_ns,
            })
        return result

    def background_file(self, cycle: str, relative_path: str) -> Path:
        path = _safe_relative(self.backgrounds_dir(cycle), relative_path)
        if not path.is_file() or path.suffix.casefold() not in IMAGE_EXTENSIONS:
            raise FileNotFoundError("找不到背景圖片。")
        return path

    def delete_background(self, cycle: str, relative_path: str) -> dict[str, Any]:
        path = self.background_file(cycle, relative_path)
        with self._lock:
            path.unlink()
            manifest = self.manifest(cycle)
            manifest.pop("reviewed_at", None)
            manifest.pop("review_signature", None)
            manifest["updated_at"] = _now()
            _atomic_json(self._manifest_path(cycle), manifest)
        return {"deleted": relative_path, "background_count": len(self.list_backgrounds(cycle))}

    def mark_collection_started(self, cycle: str, routes: list[str]) -> None:
        manifest = self.manifest(cycle)
        manifest.pop("reviewed_at", None)
        manifest.pop("review_signature", None)
        manifest.update({
            "route_paths": list(routes),
            "collection_started_at": _now(),
            "collection_completed_at": None,
        })
        _atomic_json(self._manifest_path(cycle), manifest)

    def mark_collection_complete(self, cycle: str, *, captured: int, routes: list[str]) -> None:
        self.update_manifest(
            cycle,
            route_paths=list(routes),
            background_count=int(captured),
            collection_completed_at=_now(),
        )

    def approve_backgrounds(self, cycle: str) -> dict[str, Any]:
        signature = self._background_signature(cycle)
        if not signature:
            raise ValueError("沒有可核准的背景圖片。")
        return self.update_manifest(cycle, reviewed_at=_now(), review_signature=signature)

    def review_is_current(self, cycle: str) -> bool:
        manifest = self.manifest(cycle)
        return bool(manifest.get("reviewed_at")) and manifest.get("review_signature") == self._background_signature(cycle)

    def save_activity_target(self, cycle: str, filename: str, encoded: str) -> dict[str, Any]:
        cycle = validate_cycle(cycle)
        payload = str(encoded).split(",", 1)[-1]
        try:
            raw = base64.b64decode(payload, validate=True)
        except (ValueError, TypeError) as exc:
            raise ValueError("活動菇圖片不是有效的 Base64 資料。") from exc
        if not raw or len(raw) > _MAX_TARGET_BYTES:
            raise ValueError("活動菇 PNG 必須介於 1 byte 與 20 MiB。")
        name = _safe_upload_name(filename)
        root = self.targets_dir(cycle)
        root.mkdir(parents=True, exist_ok=True)
        temporary = root / f".{name}.{os.getpid()}.tmp"
        destination = root / name
        try:
            temporary.write_bytes(raw)
            with Image.open(temporary) as image:
                image.load()
                if image.format != "PNG":
                    raise ValueError("活動菇素材必須是 PNG。")
                rgba = image.convert("RGBA")
                alpha = rgba.getchannel("A")
                if alpha.getextrema()[0] == 255 or alpha.getbbox() is None:
                    raise ValueError("活動菇 PNG 必須包含透明背景與可見圖像。")
            os.replace(temporary, destination)
        finally:
            temporary.unlink(missing_ok=True)
        manifest = self.manifest(cycle)
        manifest.pop("event_dataset", None)
        manifest.pop("event_model", None)
        manifest["updated_at"] = _now()
        _atomic_json(self._manifest_path(cycle), manifest)
        return {"path": name, "name": destination.stem, "size_bytes": destination.stat().st_size}

    def list_activity_targets(self, cycle: str) -> list[dict[str, Any]]:
        root = self.targets_dir(cycle)
        if not root.is_dir():
            return []
        return [
            {"path": path.name, "name": path.stem, "size_bytes": path.stat().st_size}
            for path in sorted(root.glob("*.png"))
            if path.is_file()
        ]

    def prepare_mixed_targets(self, config: dict[str, Any], cycle: str) -> Path:
        cycle = validate_cycle(cycle)
        permanent, _ = discover_targets(config, callback=lambda _message: None)
        activities = self.list_activity_targets(cycle)
        if not activities:
            raise ValueError("請先加入本月活動菇透明 PNG。")
        permanent_names = {asset.relative_path.stem.casefold() for asset in permanent}
        duplicates = [item["name"] for item in activities if str(item["name"]).casefold() in permanent_names]
        if duplicates:
            raise ValueError("活動菇類別名稱與常駐類別重複：" + "、".join(duplicates))
        bundle = self.cycle_dir(cycle) / "mixed_targets"
        temporary = self.cycle_dir(cycle) / f".mixed_targets.{os.getpid()}.building"
        if temporary.exists():
            shutil.rmtree(temporary)
        temporary.mkdir(parents=True)
        try:
            for asset in permanent:
                destination = temporary / asset.relative_path
                destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(asset.path, destination)
            for item in activities:
                shutil.copy2(self.targets_dir(cycle) / str(item["path"]), temporary / str(item["path"]))
            if bundle.exists():
                shutil.rmtree(bundle)
            os.replace(temporary, bundle)
        finally:
            if temporary.exists():
                shutil.rmtree(temporary)
        return bundle

    def dataset_config(
        self,
        config: dict[str, Any],
        cycle: str,
        *,
        mixed_targets: bool,
    ) -> dict[str, Any]:
        active = deepcopy(config)
        active["paths"] = dict(active["paths"])
        active["paths"]["backgrounds"] = str(self.prepare_training_backgrounds(config, cycle))
        if mixed_targets:
            active["paths"]["targets"] = str(self.prepare_mixed_targets(config, cycle))
            active["generation"] = dict(active["generation"])
            active["generation"]["excluded_target_directories"] = []
        return active

    def annotate_dataset(self, dataset: Path, *, cycle: str, role: str) -> None:
        path = dataset / "metadata.json"
        value = _read_json(path)
        value["monthly_workflow"] = {"cycle": validate_cycle(cycle), "role": role}
        _atomic_json(path, value)

    def status(self, cycle: str) -> dict[str, Any]:
        cycle = validate_cycle(cycle)
        manifest = self.manifest(cycle)
        resident = self.resident_model()
        metadata = self.resident_metadata()
        backgrounds = self.list_backgrounds(cycle)
        return {
            "cycle": cycle,
            "fixed_routes": self.fixed_routes(),
            "resident_model": str(resident) if resident else None,
            "resident_updated_at": metadata.get("updated_at"),
            "resident_source": metadata.get("source"),
            "resident_class_names": metadata.get("class_names", []),
            "backgrounds": backgrounds,
            "background_count": len(backgrounds),
            "background_policy": manifest.get("background_policy"),
            "baseline_background_count": int(manifest.get("baseline_background_count") or 0),
            "monthly_background_count": int(manifest.get("monthly_background_count") or 0),
            "intermediates_cleaned_at": manifest.get("intermediates_cleaned_at"),
            "reviewed": self.review_is_current(cycle),
            "reviewed_at": manifest.get("reviewed_at"),
            "activity_targets": self.list_activity_targets(cycle),
            "route_paths": manifest.get("route_paths", []),
            "collection_completed_at": manifest.get("collection_completed_at"),
            "resident_dataset": manifest.get("resident_dataset"),
            "resident_training_run": manifest.get("resident_training_run"),
            "event_dataset": manifest.get("event_dataset"),
            "event_model": manifest.get("event_model"),
            "completed_at": manifest.get("completed_at"),
        }


def _route_key(relative_path: str) -> str:
    stem = slugify_city(Path(relative_path).stem)[:48]
    digest = hashlib.sha1(relative_path.encode("utf-8")).hexdigest()[:8]
    return f"{stem}_{digest}"


def collect_fixed_route_backgrounds(
    *,
    store: MonthlyWorkflowStore,
    route_library: RouteLibrary,
    controller: UsbDeviceController,
    adb: Path,
    serial: str,
    cycle: str,
    route_paths: list[str],
    settle_seconds: float,
    swipe_duration_ms: int,
    stop_event: threading.Event,
    status: Callable[[str], None],
    progress: Callable[[int, int, dict[str, Any]], None],
    preview: Callable[[np.ndarray], None] | None = None,
) -> dict[str, Any]:
    cycle = validate_cycle(cycle)
    routes = [(path, route_library.waypoints(path)) for path in route_paths]
    if not routes:
        raise ValueError("至少要選擇一條固定背景路線。")
    if settle_seconds < 0:
        raise ValueError("背景等待時間不能小於 0。")
    total = sum(waypoint.point_count for _, waypoints in routes for waypoint in waypoints)
    if total < 1:
        raise ValueError("固定背景路線沒有可蒐集的搜尋格。")

    store.set_fixed_routes(route_paths)
    store.mark_collection_started(cycle, route_paths)
    background_root = store.backgrounds_dir(cycle)
    background_root.mkdir(parents=True, exist_ok=True)
    for relative_path, _ in routes:
        route_dir = background_root / _route_key(relative_path)
        if route_dir.exists():
            shutil.rmtree(route_dir)

    client = AdbClient(adb, serial)
    client.ensure_ready()
    captured = 0
    connection_generation = getattr(controller, "connection_generation", 0)
    keepalive_stop = threading.Event()

    def report(message: str) -> None:
        status(message)

    def command(name: str, payload: dict[str, object] | None = None) -> None:
        try:
            controller.command(name, payload)
        except DeviceControlError:
            reconnect_usb(controller, stop_event=stop_event, status=report)
            if stop_event.is_set():
                return
            controller.command(name, payload)

    def keepalive() -> None:
        while not keepalive_stop.wait(15.0):
            try:
                controller.keepalive(idle_seconds=15.0, timeout=3.0)
            except Exception as exc:
                if not stop_event.is_set():
                    report(f"背景路線 USB 心跳暫時失敗，下一個座標會自動重連：{exc}")

    heartbeat = threading.Thread(target=keepalive, name="monthly-background-usb", daemon=True)
    heartbeat.start()
    first = routes[0][1][0]
    command("teleport", {"lat": first.lat, "lng": first.lng})
    watchdog = PikminMemoryWatchdog(
        client,
        stop_event=stop_event,
        status=report,
        memory_pressure_recovery=lambda: restart_locwarp(
            controller, stop_event=stop_event, status=report
        ).ok,
    )
    if not watchdog.prepare_for_scan():
        keepalive_stop.set()
        raise RuntimeError("無法將 Pikmin Bloom 準備為背景蒐集所需的 2D 地圖。")

    try:
        for route_index, (relative_path, waypoints) in enumerate(routes, start=1):
            route_dir = background_root / _route_key(relative_path)
            for waypoint_index, waypoint in enumerate(waypoints, start=1):
                if stop_event.is_set():
                    break
                label = f"路線 {route_index}/{len(routes)} · {waypoint.name}"
                report(f"{label}：正在前往固定背景座標…")
                command("teleport", {"lat": waypoint.lat, "lng": waypoint.lng})
                if stop_event.wait(settle_seconds):
                    break
                waypoint_dir = route_dir / f"wp_{waypoint_index:05d}_{slugify_city(waypoint.id)[:48]}"
                waypoint_dir.mkdir(parents=True, exist_ok=True)

                def save(point: ScanPoint, payload: bytes) -> CaptureOutcome:
                    path = waypoint_dir / f"{point.stem}.png"
                    atomic_write_bytes(path, payload)
                    if preview is not None:
                        import cv2

                        image = cv2.imdecode(np.frombuffer(payload, dtype=np.uint8), cv2.IMREAD_COLOR)
                        if image is not None:
                            preview(image)
                    return CaptureOutcome()

                def before_capture(point: ScanPoint) -> bool:
                    nonlocal connection_generation
                    reset = watchdog.before_capture(point)
                    if stop_event.is_set():
                        return reset
                    if not controller.connected:
                        command("status")
                    generation = getattr(controller, "connection_generation", 0)
                    if generation != connection_generation:
                        command("teleport", {"lat": waypoint.lat, "lng": waypoint.lng})
                        if not watchdog.recover_after_usb_service_restart():
                            raise RuntimeError("LocWarp 重啟後無法恢復背景蒐集地圖。")
                        connection_generation = getattr(controller, "connection_generation", 0)
                        return True
                    return reset

                offset = captured

                def update(done: int, _total: int, point: ScanPoint) -> None:
                    progress(offset + done, total, {
                        "route": relative_path,
                        "waypoint": waypoint.name,
                        "grid_x": point.grid_x,
                        "grid_y": point.grid_y,
                    })

                left, right, up, down = waypoint.resolved_radii
                summary = run_rectangular_scan(
                    client,
                    horizontal_radius=waypoint.horizontal_radius,
                    vertical_radius=waypoint.vertical_radius,
                    left_radius=left,
                    right_radius=right,
                    up_radius=up,
                    down_radius=down,
                    settle_seconds=settle_seconds,
                    swipe_duration_ms=swipe_duration_ms,
                    stop_event=stop_event,
                    on_capture=save,
                    before_capture=before_capture,
                    recover_foreground=watchdog.restart_game,
                    status=lambda message: report(f"{label}：{message}"),
                    progress=update,
                    scan_spans=waypoint.scan_spans,
                )
                captured += summary.captured
                if not summary.completed:
                    break
            if stop_event.is_set():
                break
        if not stop_event.is_set():
            if captured != total:
                raise RuntimeError(f"背景路線只完成 {captured}/{total} 張，未標記為可審核。")
            store.mark_collection_complete(cycle, captured=captured, routes=route_paths)
    finally:
        keepalive_stop.set()
        heartbeat.join(timeout=2.0)
    return {"cycle": cycle, "captured": captured, "total": total, "routes": route_paths}


def annotate_training_run(run_dir: Path, *, cycle: str, role: str) -> None:
    manifest_path = run_dir / ".artifact.json"
    value = _read_json(manifest_path)
    value["monthly_workflow"] = {"cycle": validate_cycle(cycle), "role": role}
    _atomic_json(manifest_path, value)
