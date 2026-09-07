"""Project route discovery and validated JSON imports.

Route listing reads only a bounded header from every JSON file.  Full waypoint
arrays are therefore loaded only when a route is selected or imported.
"""

from __future__ import annotations

import json
import os
import re
import threading
import unicodedata
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .geo_recognition import GeoRecognitionWaypoint, validate_geo_waypoints
from .project_defaults import MAX_GEO_SEQUENCE_FILE_BYTES


_HEADER_BYTES = 512 * 1024
_JSON_STRING = r'"(?:\\.|[^"\\])*"'
_INVALID_FILENAME = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
_WINDOWS_RESERVED = {
    "CON", "PRN", "AUX", "NUL",
    *(f"COM{index}" for index in range(1, 10)),
    *(f"LPT{index}" for index in range(1, 10)),
}


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _string_field(text: str, field: str) -> str | None:
    match = re.search(rf'"{re.escape(field)}"\s*:\s*({_JSON_STRING})', text)
    if match is None:
        return None
    try:
        value = json.loads(match.group(1))
    except json.JSONDecodeError:
        return None
    return value if isinstance(value, str) else None


def _integer_field(text: str, *fields: str) -> int | None:
    for field in fields:
        match = re.search(rf'"{re.escape(field)}"\s*:\s*(\d+)', text)
        if match is not None:
            return int(match.group(1))
    return None


def _safe_filename(source_name: str, fallback: str) -> str:
    source_stem = Path(source_name).stem if source_name.strip() else fallback
    stem = unicodedata.normalize("NFKC", source_stem)
    stem = _INVALID_FILENAME.sub("_", stem).strip(" ._")[:100]
    if not stem:
        stem = "imported-route"
    if stem.upper() in _WINDOWS_RESERVED:
        stem = f"route-{stem}"
    return f"{stem}.json"


def _candidate_sequence(value: Any) -> dict[str, Any]:
    if isinstance(value, list):
        return {"name": "匯入的座標序列", "waypoints": value}
    if not isinstance(value, dict):
        raise ValueError("序列檔案必須是 JSON 物件或座標陣列。")
    candidate = value.get("sequence", value)
    if not isinstance(candidate, dict):
        raise ValueError("序列檔案的 sequence 格式錯誤。")
    return candidate


def _waypoints_from_candidate(candidate: dict[str, Any]) -> tuple[list[GeoRecognitionWaypoint], int]:
    raw_waypoints = candidate.get("waypoints")
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
        except (KeyError, TypeError, ValueError, OverflowError) as exc:
            raise ValueError(f"第 {index} 個座標節點格式錯誤。") from exc
        waypoints.append(waypoint)
    return waypoints, validate_geo_waypoints(waypoints)


def _canonical_document(value: Any) -> dict[str, Any]:
    candidate = _candidate_sequence(value)
    waypoints, total_points = _waypoints_from_candidate(candidate)
    normalized_waypoints: list[dict[str, Any]] = []
    for waypoint in waypoints:
        item = asdict(waypoint)
        left, right, up, down = waypoint.resolved_radii
        item.update({
            "left_radius": left,
            "right_radius": right,
            "up_radius": up,
            "down_radius": down,
        })
        normalized_waypoints.append(item)
    sequence_id = str(candidate.get("id") or "imported-route").strip()[:120] or "imported-route"
    sequence_name = str(candidate.get("name") or "匯入的座標序列").strip()[:120] or "匯入的座標序列"
    return {
        "schema": "bloompilot.geo-sequence.v1",
        "imported_at": _utc_now(),
        "metadata": {
            "waypoint_count": len(waypoints),
            "total_search_points": total_points,
        },
        "sequence": {
            "version": 1,
            "id": sequence_id,
            "name": sequence_name,
            "updated_at": str(candidate.get("updated_at") or _utc_now()),
            "waypoints": normalized_waypoints,
        },
    }


class RouteLibrary:
    def __init__(self, directory: Path) -> None:
        self.directory = directory
        self._lock = threading.RLock()

    @property
    def imported_directory(self) -> Path:
        return self.directory / "imported"

    def _resolve(self, relative_path: str) -> Path:
        if not isinstance(relative_path, str) or not relative_path.strip():
            raise ValueError("必須指定專案路線。")
        relative = Path(relative_path.replace("\\", "/"))
        if relative.is_absolute() or relative.suffix.lower() != ".json":
            raise ValueError("專案路線路徑無效。")
        root = self.directory.resolve()
        path = (root / relative).resolve()
        if not path.is_relative_to(root):
            raise ValueError("專案路線必須位於 routes 目錄內。")
        if not path.is_file():
            raise FileNotFoundError(f"找不到專案路線：{relative_path}")
        if path.stat().st_size > MAX_GEO_SEQUENCE_FILE_BYTES:
            raise ValueError("座標序列檔案不能超過 32 MB。")
        return path

    def route_path(self, relative_path: str) -> Path:
        path = self._resolve(relative_path)
        if self._summary(path) is None:
            raise ValueError("指定的 JSON 不是 BloomPilot 座標序列。")
        return path

    def waypoints(self, relative_path: str) -> list[GeoRecognitionWaypoint]:
        """Load and validate one route for a worker that needs its coordinates."""
        path = self.route_path(relative_path)
        try:
            document = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ValueError(f"無法讀取專案路線：{relative_path}") from exc
        candidate = _candidate_sequence(document)
        waypoints, _ = _waypoints_from_candidate(candidate)
        return waypoints

    def _summary(self, path: Path) -> dict[str, Any] | None:
        try:
            size = path.stat().st_size
            if size <= 0 or size > MAX_GEO_SEQUENCE_FILE_BYTES:
                return None
            with path.open("r", encoding="utf-8", errors="replace") as handle:
                header = handle.read(_HEADER_BYTES)
            sequence_match = re.search(r'"sequence"\s*:\s*\{', header)
            if sequence_match is None:
                return None
            waypoints_match = re.search(r'"waypoints"\s*:\s*\[', header[sequence_match.end():])
            if waypoints_match is None:
                return None
            sequence_header = header[sequence_match.end():sequence_match.end() + waypoints_match.start()]
            name = _string_field(sequence_header, "name")
            if not name:
                return None
            relative = path.resolve().relative_to(self.directory.resolve()).as_posix()
            stat = path.stat()
            return {
                "path": relative,
                "name": name,
                "size_bytes": size,
                "waypoint_count": _integer_field(header[:sequence_match.start()], "waypoint_count", "city_count"),
                "search_point_count": _integer_field(header[:sequence_match.start()], "total_search_points"),
                "updated_at": _string_field(sequence_header, "updated_at")
                or datetime.fromtimestamp(stat.st_mtime, timezone.utc).isoformat(),
                "imported": path.resolve().is_relative_to(self.imported_directory.resolve()),
            }
        except (OSError, UnicodeError, ValueError):
            return None

    def list(self) -> list[dict[str, Any]]:
        if not self.directory.is_dir():
            return []
        routes: list[dict[str, Any]] = []
        for path in self.directory.rglob("*.json"):
            if not path.is_file():
                continue
            relative = path.resolve().relative_to(self.directory.resolve())
            if relative.parts and relative.parts[0].startswith("_"):
                continue
            summary = self._summary(path)
            if summary is not None:
                routes.append(summary)
        return sorted(routes, key=lambda item: (not item["imported"], item["name"].casefold(), item["path"]))

    def import_document(self, source_name: str, value: Any) -> dict[str, Any]:
        canonical = _canonical_document(value)
        encoded = (json.dumps(canonical, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
        if len(encoded) > MAX_GEO_SEQUENCE_FILE_BYTES:
            raise ValueError("正規化後的座標序列資料不能超過 32 MB。")
        self.imported_directory.mkdir(parents=True, exist_ok=True)
        fallback = str(canonical["sequence"]["name"])
        destination = self.imported_directory / _safe_filename(source_name, fallback)
        temporary = destination.with_suffix(".json.tmp")
        with self._lock:
            try:
                temporary.write_bytes(encoded)
                os.replace(temporary, destination)
            finally:
                temporary.unlink(missing_ok=True)
            summary = self._summary(destination)
        if summary is None:
            raise RuntimeError("路線已寫入，但無法建立路線摘要。")
        return summary

    def delete_waypoint(self, relative_path: str, waypoint_id: str) -> None:
        """Remove one node in place, including from bundled routes, then commit atomically."""
        with self._lock:
            path = self.route_path(relative_path)
            document = json.loads(path.read_text(encoding="utf-8"))
            candidate = _candidate_sequence(document)
            waypoints, total_points = _waypoints_from_candidate(candidate)
            index = next((index for index, item in enumerate(waypoints) if item.id == waypoint_id), None)
            if index is None:
                raise RuntimeError("目前節點已不在原始 route 中，未刪除任何節點。")
            removed = waypoints[index]
            # Keep the original fields and ordering of every remaining node.
            candidate["waypoints"].pop(index)
            candidate["updated_at"] = _utc_now()
            metadata = document.setdefault("metadata", {})
            if not isinstance(metadata, dict):
                raise ValueError("路線 metadata 必須是物件。")
            metadata["waypoint_count"] = len(waypoints) - 1
            if "city_count" in metadata:
                metadata["city_count"] = len(waypoints) - 1
            metadata["total_search_points"] = total_points - removed.point_count
            # Compact output keeps large bundled routes within the file size limit.
            encoded = (json.dumps(document, ensure_ascii=False, separators=(",", ":")) + "\n").encode("utf-8")
            if len(encoded) > MAX_GEO_SEQUENCE_FILE_BYTES:
                raise ValueError("更新後的座標序列資料不能超過 32 MB。")
            temporary = path.with_suffix(".json.tmp")
            try:
                temporary.write_bytes(encoded)
                os.replace(temporary, path)
            finally:
                temporary.unlink(missing_ok=True)

    def delete(self, relative_path: str) -> dict[str, Any]:
        with self._lock:
            path = self.route_path(relative_path)
            imported_root = self.imported_directory.resolve()
            if not path.resolve().is_relative_to(imported_root):
                raise ValueError("內建專案序列不能刪除；修改後請另存為自訂序列。")
            summary = self._summary(path)
            if summary is None:
                raise ValueError("指定的 JSON 不是 BloomPilot 座標序列。")
            path.unlink()
            return summary
