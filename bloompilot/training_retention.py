"""Publish completed weights before removing their disposable training inputs."""
from __future__ import annotations

import hashlib
import json
import os
import shutil
from pathlib import Path
from typing import Any

from .artifacts import ArtifactManager, TRAINING_MANIFEST, _atomic_write, _load_json, write_pointer


def file_hash(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def background_snapshot(paths: list[Path], project_root: Path) -> list[dict[str, str]]:
    root = project_root.resolve()
    return [{"path": path.resolve().relative_to(root).as_posix(), "sha256": file_hash(path)}
            for path in paths if path.resolve().is_relative_to(root)]


def publish_completed_model(config: dict[str, Any], run: Path, *, select_default: bool = True) -> Path:
    manager = ArtifactManager(config)
    run = manager._safe_run(run)
    metadata = _load_json(run / TRAINING_MANIFEST)
    source = run / "weights/best.pt"
    if (metadata.get("status") != "completed" or not source.is_file()
            or not source.resolve().is_relative_to(run) or source.stat().st_size == 0):
        raise ValueError("模型尚未完成或 best.pt 尚未保存；保留所有訓練資料。")
    # Plain Git keeps cloning independent of LFS billing/download configuration.
    if source.stat().st_size >= 100 * 1024 * 1024:
        raise ValueError("模型超過 GitHub 一般 Git 的單檔限制；請先配置 LFS，訓練資料仍保留。")
    from ultralytics import YOLO
    model = YOLO(str(source))
    names = model.names
    classes = [str(names[key]) for key in sorted(names)] if isinstance(names, dict) else list(names)
    root = manager.project_root / "models/published"
    destination = root / run.name / "weights/best.pt"
    if not root.resolve().is_relative_to(manager.project_root) or not destination.resolve().is_relative_to(root.resolve()):
        raise ValueError("發布目錄位於專案外；保留模型與訓練資料。")
    destination.parent.mkdir(parents=True, exist_ok=True)
    digest = file_hash(source)
    if destination.exists() and file_hash(destination) != digest:
        raise ValueError("已有同名發布模型且內容不同；請使用新的 run 名稱，原模型不會被覆蓋。")
    temporary = destination.with_name(f".best.{os.getpid()}.tmp")
    try:
        shutil.copyfile(source, temporary)
        if file_hash(temporary) != digest:
            raise OSError("模型複製雜湊不符；訓練資料仍保留。")
        os.replace(temporary, destination)
    finally:
        temporary.unlink(missing_ok=True)
    portable = {key: metadata[key] for key in ("status", "created_at", "completed_epochs", "total_epochs", "monthly_workflow") if key in metadata}
    portable.update({"class_names": classes, "sha256": digest, "source_run": run.name})
    _atomic_write(destination.parent.parent / TRAINING_MANIFEST, json.dumps(portable, ensure_ascii=False, indent=2) + "\n")
    if select_default:
        write_pointer(root / "DEFAULT.txt", destination, root)
    if not any(name.casefold().startswith("event") for name in classes):
        write_pointer(root / "RESIDENT.txt", destination, root)
    return destination


def remove_backgrounds(config: dict[str, Any], snapshot: list[dict[str, str]], *, excluding: set[Path]) -> dict[str, list[str]]:
    manager = ArtifactManager(config)
    root = manager.project_root
    shared: set[str] = set()
    if manager.dataset_root.is_dir():
        for dataset in manager.dataset_root.iterdir():
            if dataset.resolve() not in excluding:
                shared.update(item["path"] for item in _load_json(dataset / "metadata.json").get("background_snapshot", []) if "path" in item)
    result: dict[str, list[str]] = {"deleted": [], "retained": []}
    for item in snapshot:
        path = (root / item["path"]).resolve()
        if not path.exists():
            continue
        # Never follow a manifest out of this project's data directory, erase
        # target sprites, or remove a recaptured image with the same filename.
        allowed = path.is_relative_to(root / "data") and "backgrounds" in path.relative_to(root).parts
        if (not allowed or not path.is_file() or path.suffix.lower() not in {".png", ".jpg", ".jpeg", ".webp", ".bmp"}
                or item["path"] in shared or file_hash(path) != item.get("sha256")):
            result["retained"].append(item["path"])
            continue
        path.unlink()
        result["deleted"].append(item["path"])
    return result


def finalize_training(config: dict[str, Any], run: Path, dataset: Path) -> dict[str, Any]:
    """Called by training, never by a catalog read or an intermediate epoch."""
    metadata = _load_json(dataset / "metadata.json")
    published = publish_completed_model(config, run, select_default=not bool(metadata.get("monthly_workflow")))
    if metadata.get("monthly_workflow"):
        return {"model": str(published), "deferred": "monthly_pipeline"}
    manager = ArtifactManager(config)
    dataset = dataset.resolve()
    if (dataset == manager.dataset_root or not dataset.is_relative_to(manager.dataset_root)
            or not dataset.is_relative_to(manager.project_root)):
        raise ValueError("資料集不在專案生成資料管理目錄內，拒絕清理。")
    registered = _load_json(run / TRAINING_MANIFEST).get("dataset")
    if not registered or (manager.project_root / registered).resolve() != dataset:
        raise ValueError("資料集與完成模型的訓練紀錄不符，拒絕清理。")
    for artifact in manager.scan(reconcile=False):
        if artifact.kind == "training_run" and (manager.project_root / artifact.path).resolve() != run.resolve():
            dependency = artifact.details.get("dataset")
            if dependency and (manager.project_root / dependency).resolve() == dataset and artifact.status != "completed":
                return {"model": str(published), "retained": "dataset_needed_by_incomplete_run"}
    record = {"model": str(published), "dataset": str(dataset), "background_snapshot": metadata.get("background_snapshot", _load_json(run / "cleanup.json").get("background_snapshot", []))}
    _atomic_write(run / "cleanup.json", json.dumps(record, ensure_ascii=False, indent=2) + "\n")
    backgrounds = remove_backgrounds(config, record["background_snapshot"], excluding={dataset})
    manager._remove_pointer_to(manager.dataset_root / "LATEST.txt", manager.dataset_root, dataset)
    if dataset.exists():
        shutil.rmtree(dataset)
    # Keep the weight and tiny provenance files, not training-preview images.
    for image in run.iterdir():
        if image.is_file() and image.suffix.lower() in {".png", ".jpg", ".jpeg"}:
            image.unlink()
    record.update({"completed": True, "background_cleanup": backgrounds})
    _atomic_write(run / "cleanup.json", json.dumps(record, ensure_ascii=False, indent=2) + "\n")
    return record
