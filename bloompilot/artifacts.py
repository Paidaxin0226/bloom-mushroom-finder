"""Inventory, validate, select, and safely clean project artifacts."""

from __future__ import annotations

import argparse
import csv
import json
import os
import shutil
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable

import yaml

from .paths import CONFIG_PATH


SCHEMA_VERSION = 1
TRAINING_MANIFEST = ".artifact.json"
LATEST_DATASET = "LATEST.txt"
LATEST_RUN = "LATEST_RUN.txt"
LEGACY_LATEST_MODEL = "LATEST_MODEL.txt"
IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".webp", ".bmp"}


def _now() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _load_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def _atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temporary.write_text(text, encoding="utf-8")
    temporary.replace(path)


def _is_within(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
        return True
    except ValueError:
        return False


def read_pointer(pointer: Path, root: Path) -> Path | None:
    """Resolve a portable relative pointer, while accepting legacy absolute values."""
    if not pointer.is_file():
        return None
    try:
        raw = pointer.read_text(encoding="utf-8").strip()
        candidate = Path(raw)
        candidate = candidate if candidate.is_absolute() else root / candidate
        candidate = candidate.resolve()
        return candidate if _is_within(candidate, root) and candidate.exists() else None
    except OSError:
        return None


def write_pointer(pointer: Path, target: Path, root: Path) -> None:
    root = root.resolve()
    target = target.resolve()
    if not _is_within(target, root):
        raise ValueError(f"產物指標不可指向管理目錄外：{target}")
    _atomic_write(pointer, target.relative_to(root).as_posix() + "\n")


def format_bytes(size: int) -> str:
    value = float(size)
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if value < 1024 or unit == "TiB":
            return f"{value:.0f} {unit}" if unit == "B" else f"{value:.2f} {unit}"
        value /= 1024
    return f"{size} B"


def _directory_size(path: Path) -> int:
    total = 0
    try:
        for item in path.rglob("*"):
            if item.is_file():
                try:
                    total += item.stat().st_size
                except OSError:
                    pass
    except OSError:
        pass
    return total


def _last_epoch(results_path: Path) -> int:
    if not results_path.is_file():
        return 0
    try:
        with results_path.open("r", encoding="utf-8", newline="") as handle:
            rows = list(csv.DictReader(handle))
        return max((int(float(row["epoch"])) for row in rows if row.get("epoch")), default=0)
    except (OSError, ValueError, KeyError, csv.Error):
        return 0


@dataclass(frozen=True)
class Artifact:
    id: str
    kind: str
    name: str
    path: str
    status: str
    size_bytes: int
    created_at: str
    modified_at: str
    latest: bool = False
    protected: bool = False
    details: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class ArtifactManager:
    def __init__(self, config: dict[str, Any]) -> None:
        self.config = config
        self.project_root = Path(config["_project_root"]).resolve()
        paths = config.get("paths", {})
        self.dataset_root = self._path(paths.get("generated_root", "data/generated"))
        self.training_root = self._path(paths.get("training_runs", "outputs/training"))
        self.pretrained_root = self._path(paths.get("pretrained_models", "models/pretrained"))
        self.control_root = self._path(paths.get("training_control", "runtime/training_control"))
        self.catalog_path = self._path(paths.get("artifact_catalog", "runtime/artifacts/catalog.json"))

    @classmethod
    def from_config_file(cls, path: Path = CONFIG_PATH) -> "ArtifactManager":
        from .dataset_builder import load_config

        return cls(load_config(path))

    def _path(self, value: str | Path) -> Path:
        candidate = Path(value)
        return (candidate if candidate.is_absolute() else self.project_root / candidate).resolve()

    def _relative(self, path: Path) -> str:
        try:
            return path.resolve().relative_to(self.project_root).as_posix()
        except ValueError:
            return str(path.resolve())

    @staticmethod
    def _timestamp(path: Path) -> str:
        try:
            return datetime.fromtimestamp(path.stat().st_mtime).astimezone().isoformat(timespec="seconds")
        except OSError:
            return ""

    @staticmethod
    def _yaml(path: Path) -> dict[str, Any]:
        try:
            value = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
            return value if isinstance(value, dict) else {}
        except (OSError, yaml.YAMLError):
            return {}

    def _safe_run(self, run_dir: Path) -> Path:
        run_dir = run_dir.resolve()
        if not _is_within(run_dir, self.training_root) or run_dir == self.training_root:
            raise ValueError(f"訓練產物不在管理目錄內：{run_dir}")
        return run_dir

    def register_training_run(
        self,
        run_dir: Path,
        dataset_dir: Path | None,
        *,
        status: str,
        completed_epochs: int = 0,
        total_epochs: int = 0,
        class_names: Iterable[str] | None = None,
    ) -> None:
        run_dir = self._safe_run(run_dir)
        manifest_path = run_dir / TRAINING_MANIFEST
        previous = _load_json(manifest_path)
        payload = {
            "schema_version": SCHEMA_VERSION,
            "kind": "training_run",
            "id": f"run:{run_dir.name}",
            "status": status,
            "created_at": previous.get("created_at") or self._timestamp(run_dir) or _now(),
            "updated_at": _now(),
            "dataset": self._relative(dataset_dir) if dataset_dir else previous.get("dataset"),
            "completed_epochs": int(completed_epochs),
            "total_epochs": int(total_epochs),
        }
        if isinstance(previous.get("monthly_workflow"), dict):
            payload["monthly_workflow"] = previous["monthly_workflow"]
        names = list(class_names) if class_names is not None else (
            _load_json(dataset_dir / "metadata.json").get("class_names") if dataset_dir else None
        )
        if not names:
            names = previous.get("class_names")
        if isinstance(names, list) and names and all(isinstance(name, str) for name in names):
            payload["class_names"] = names
        _atomic_write(manifest_path, json.dumps(payload, ensure_ascii=False, indent=2) + "\n")
        if status == "completed":
            write_pointer(self.training_root / LATEST_RUN, run_dir, self.training_root)
            (self.training_root / LEGACY_LATEST_MODEL).unlink(missing_ok=True)
            if bool(self.config.get("artifacts", {}).get("auto_prune_completed_weights", False)):
                self.prune_model_files(apply=True)

    def reconcile_control_status(self) -> None:
        status = _load_json(self.control_root / "status.json")
        state = str(status.get("state", ""))
        if state not in {"completed", "paused", "stopped", "running"}:
            return
        checkpoint = Path(str(status.get("checkpoint", "")))
        if not checkpoint.is_absolute():
            checkpoint = self.project_root / checkpoint
        checkpoint = checkpoint.resolve()
        if not checkpoint.is_file() or not _is_within(checkpoint, self.training_root):
            return
        # The final checkpoint progress can briefly outlive train()'s completed
        # manifest. Never turn an early-stopped run back into an incomplete run.
        existing = _load_json(checkpoint.parent.parent / TRAINING_MANIFEST)
        if existing.get("status") == "completed" and state != "completed":
            return
        dataset_value = status.get("dataset")
        if dataset_value:
            dataset_candidate = Path(str(dataset_value))
            dataset = (
                dataset_candidate
                if dataset_candidate.is_absolute()
                else self.project_root / dataset_candidate
            ).resolve()
        else:
            dataset = None
        self.register_training_run(
            checkpoint.parent.parent,
            dataset,
            status=state,
            completed_epochs=int(status.get("completed_epoch", 0) or 0),
            total_epochs=int(status.get("total_epochs", 0) or 0),
        )

    def _scan_runs(self, latest_run: Path | None) -> tuple[list[Artifact], set[Path]]:
        artifacts: list[Artifact] = []
        referenced_datasets: set[Path] = set()
        if not self.training_root.is_dir():
            return artifacts, referenced_datasets
        for run_dir in sorted(self.training_root.iterdir()):
            if not run_dir.is_dir() or run_dir.name.startswith("."):
                continue
            args = self._yaml(run_dir / "args.yaml")
            manifest = _load_json(run_dir / TRAINING_MANIFEST)
            dataset_value = manifest.get("dataset") or args.get("data")
            dataset_dir: Path | None = None
            if dataset_value:
                dataset_candidate = Path(str(dataset_value))
                dataset_candidate = (
                    dataset_candidate
                    if dataset_candidate.is_absolute()
                    else self.project_root / dataset_candidate
                ).resolve()
                dataset_dir = dataset_candidate.parent if dataset_candidate.name == "data.yaml" else dataset_candidate
                referenced_datasets.add(dataset_dir)
            completed = int(manifest.get("completed_epochs") or _last_epoch(run_dir / "results.csv"))
            total = int(manifest.get("total_epochs") or args.get("epochs") or 0)
            if manifest.get("status"):
                status = str(manifest["status"])
            elif (run_dir / "weights" / "last.pt").is_file():
                status = "resumable"
            else:
                status = "incomplete"
            is_latest = latest_run == run_dir.resolve()
            protected = status in {"running", "paused", "resumable"}
            created = str(manifest.get("created_at") or self._timestamp(run_dir))
            artifacts.append(
                Artifact(
                    id=f"run:{run_dir.name}",
                    kind="training_run",
                    name=run_dir.name,
                    path=self._relative(run_dir),
                    status=status,
                    size_bytes=_directory_size(run_dir),
                    created_at=created,
                    modified_at=self._timestamp(run_dir),
                    latest=is_latest,
                    protected=protected,
                    details={
                        "dataset": self._relative(dataset_dir) if dataset_dir else None,
                        "completed_epochs": completed,
                        "total_epochs": total,
                        "has_best": (run_dir / "weights" / "best.pt").is_file(),
                        "has_last": (run_dir / "weights" / "last.pt").is_file(),
                        "monthly_workflow": manifest.get("monthly_workflow"),
                    },
                )
            )
            for label in ("best", "last"):
                model = run_dir / "weights" / f"{label}.pt"
                if not model.is_file():
                    continue
                artifacts.append(
                    Artifact(
                        id=f"model:trained:{run_dir.name}:{label}",
                        kind="model",
                        name=f"{run_dir.name}/{label}.pt",
                        path=self._relative(model),
                        status="ready",
                        size_bytes=model.stat().st_size,
                        created_at=self._timestamp(model),
                        modified_at=self._timestamp(model),
                        protected=protected,
                        details={
                            "source": "trained",
                            "run": run_dir.name,
                            "role": label,
                            "monthly_workflow": manifest.get("monthly_workflow"),
                        },
                    )
                )
        return artifacts, referenced_datasets

    def scan(self, *, reconcile: bool = True) -> list[Artifact]:
        if reconcile:
            self.reconcile_control_status()
        latest_dataset = read_pointer(self.dataset_root / LATEST_DATASET, self.dataset_root)
        latest_run = read_pointer(self.training_root / LATEST_RUN, self.training_root)
        artifacts, referenced_datasets = self._scan_runs(latest_run)

        if self.dataset_root.is_dir():
            for dataset in sorted(self.dataset_root.iterdir()):
                if not dataset.is_dir():
                    continue
                metadata = _load_json(dataset / "metadata.json")
                building = dataset.name.startswith(".") and dataset.name.endswith(".building")
                required = [dataset / "data.yaml", dataset / "metadata.json", dataset / "images", dataset / "labels"]
                status = "building" if building else ("ready" if all(path.exists() for path in required) else "incomplete")
                is_latest = latest_dataset == dataset.resolve()
                referenced = dataset.resolve() in referenced_datasets
                artifacts.append(
                    Artifact(
                        id=f"dataset:{dataset.name}",
                        kind="dataset",
                        name=dataset.name,
                        path=self._relative(dataset),
                        status=status,
                        size_bytes=_directory_size(dataset),
                        created_at=str(metadata.get("created_at") or self._timestamp(dataset)),
                        modified_at=self._timestamp(dataset),
                        latest=is_latest,
                        protected=referenced or building,
                        details={
                            "images": int(metadata.get("generated_image_count", 0) or 0),
                            "instances": int(metadata.get("instance_count", 0) or 0),
                            "classes": int(metadata.get("class_count", 0) or 0),
                            "referenced_by_training": referenced,
                        },
                    )
                )

        if self.pretrained_root.is_dir():
            for model in sorted(self.pretrained_root.glob("*.pt")):
                artifacts.append(
                    Artifact(
                        id=f"model:pretrained:{model.name}",
                        kind="model",
                        name=model.name,
                        path=self._relative(model),
                        status="ready" if model.stat().st_size > 0 else "incomplete",
                        size_bytes=model.stat().st_size,
                        created_at=self._timestamp(model),
                        modified_at=self._timestamp(model),
                        protected=True,
                        details={"source": "pretrained"},
                    )
                )
        return sorted(artifacts, key=lambda item: (item.kind, item.created_at, item.name), reverse=True)

    def refresh_catalog(self) -> dict[str, Any]:
        artifacts = self.scan()
        storage = sum(
            item.size_bytes
            for item in artifacts
            if item.kind in {"dataset", "training_run"}
            or (item.kind == "model" and item.details.get("source") == "pretrained")
        )
        payload = {
            "schema_version": SCHEMA_VERSION,
            "generated_at": _now(),
            "summary": {
                "datasets": sum(item.kind == "dataset" for item in artifacts),
                "training_runs": sum(item.kind == "training_run" for item in artifacts),
                "models": sum(item.kind == "model" for item in artifacts),
                "managed_storage_bytes": storage,
            },
            "artifacts": [item.to_dict() for item in artifacts],
        }
        _atomic_write(self.catalog_path, json.dumps(payload, ensure_ascii=False, indent=2) + "\n")
        return payload

    def artifact(self, artifact_id: str) -> Artifact:
        for item in self.scan():
            if item.id == artifact_id:
                return item
        raise KeyError(f"找不到產物：{artifact_id}")

    def set_latest(self, artifact_id: str) -> Path:
        artifact = self.artifact(artifact_id)
        target = self.project_root / artifact.path
        if artifact.kind == "dataset" and artifact.status == "ready":
            pointer = self.dataset_root / LATEST_DATASET
            write_pointer(pointer, target, self.dataset_root)
        elif artifact.kind == "training_run":
            pointer = self.training_root / LATEST_RUN
            write_pointer(pointer, target, self.training_root)
        else:
            raise ValueError("只有可用資料集或訓練 run 可以設為目前產物")
        self.refresh_catalog()
        return target.resolve()

    @staticmethod
    def _remove_pointer_to(pointer: Path, root: Path, target: Path, *, descendants: bool = False) -> None:
        selected = read_pointer(pointer, root)
        if selected is None:
            return
        if selected == target.resolve() or (descendants and _is_within(selected, target)):
            pointer.unlink(missing_ok=True)

    def delete(self, artifact_id: str) -> dict[str, Any]:
        """Delete one managed artifact while preserving active and referenced work."""
        artifact = self.artifact(artifact_id)
        target = (self.project_root / artifact.path).resolve()

        if artifact.kind == "dataset":
            if not _is_within(target, self.dataset_root) or target == self.dataset_root:
                raise RuntimeError(f"拒絕刪除資料集管理目錄外的路徑：{target}")
            if artifact.status == "building" or artifact.details.get("referenced_by_training"):
                raise ValueError("這個資料集仍在產生或已被訓練產物引用，不能刪除")
            self._remove_pointer_to(self.dataset_root / LATEST_DATASET, self.dataset_root, target)
            shutil.rmtree(target)
        elif artifact.kind == "training_run":
            if not _is_within(target, self.training_root) or target == self.training_root:
                raise RuntimeError(f"拒絕刪除訓練管理目錄外的路徑：{target}")
            if artifact.status in {"running", "paused", "resumable"}:
                raise ValueError("這個訓練 run 正在使用或仍可續訓，不能刪除")
            self._remove_pointer_to(self.training_root / LATEST_RUN, self.training_root, target)
            self._remove_pointer_to(
                self.training_root / LEGACY_LATEST_MODEL,
                self.training_root,
                target,
                descendants=True,
            )
            shutil.rmtree(target)
        elif artifact.kind == "model" and artifact.details.get("source") == "trained":
            if not _is_within(target, self.training_root) or target.suffix.casefold() != ".pt":
                raise RuntimeError(f"拒絕刪除訓練管理目錄外的模型：{target}")
            run_name = str(artifact.details.get("run") or "")
            run = next((item for item in self.scan() if item.id == f"run:{run_name}"), None)
            if run is not None and run.status in {"running", "paused", "resumable"}:
                raise ValueError("這個模型所屬的訓練 run 正在使用或仍可續訓，不能刪除")
            self._remove_pointer_to(self.training_root / LEGACY_LATEST_MODEL, self.training_root, target)
            target.unlink()
        else:
            raise ValueError("這個產物受保護，不能刪除")

        return self.refresh_catalog()

    def verify(self, artifact: Artifact) -> list[str]:
        path = (self.project_root / artifact.path).resolve()
        errors: list[str] = []
        if not path.exists():
            return [f"路徑不存在：{path}"]
        if artifact.kind == "dataset":
            for name in ("data.yaml", "metadata.json", "images", "labels"):
                if not (path / name).exists():
                    errors.append(f"缺少 {name}")
            if not errors:
                data = self._yaml(path / "data.yaml")
                for split in ("train", "val"):
                    relative = data.get(split)
                    if relative and not (path / str(relative)).exists():
                        errors.append(f"{split} 路徑不存在：{relative}")
                image_count = sum(
                    item.suffix.casefold() in IMAGE_SUFFIXES
                    for item in (path / "images").rglob("*")
                    if item.is_file()
                )
                label_count = sum(1 for item in (path / "labels").rglob("*.txt") if item.is_file())
                if image_count == 0 or label_count == 0:
                    errors.append("圖片或標註為空")
        elif artifact.kind == "training_run":
            if not (path / "args.yaml").is_file():
                errors.append("缺少 args.yaml")
            if artifact.status == "completed" and not (path / "weights" / "best.pt").is_file():
                errors.append("完成的 run 缺少 weights/best.pt")
        elif artifact.kind == "model" and (not path.is_file() or path.stat().st_size == 0):
            errors.append("模型檔不存在或為空")
        return errors

    def cache_files(self) -> list[Path]:
        if not self.dataset_root.is_dir():
            return []
        files: list[Path] = []
        root = self.dataset_root.resolve()
        for directory, subdirs, names in os.walk(root, followlinks=False):
            base = Path(directory).resolve()
            if not base.is_relative_to(root):
                subdirs.clear()
                continue
            subdirs[:] = [name for name in subdirs
                          if not Path(directory, name).is_symlink()
                          and not Path(directory, name).is_junction()]
            parts = base.relative_to(root).parts
            originals = {Path(name).stem for name in names if Path(name).suffix.casefold() in IMAGE_SUFFIXES}
            for name in names:
                path = base / name
                # An NPY array is disposable only while its source image exists.
                image_cache = "images" in parts and path.suffix.casefold() == ".npy" and path.stem in originals
                label_cache = "labels" in parts and path.suffix.casefold() == ".cache"
                if (image_cache or label_cache) and not path.is_symlink() and path.is_file():
                    files.append(path)
        return files

    def model_prune_candidates(self) -> list[Path]:
        """Return expendable weights while preserving the configured base and resumable runs."""
        keep: set[Path] = set()
        configured_model = self.config.get("training", {}).get("model")
        if configured_model:
            keep.add(self._path(str(configured_model)))
        candidates: list[Path] = []
        if self.pretrained_root.is_dir():
            for model in self.pretrained_root.glob("*.pt"):
                if model.resolve() not in keep:
                    candidates.append(model.resolve())

        if self.training_root.is_dir():
            for run_dir in self.training_root.iterdir():
                if not run_dir.is_dir():
                    continue
                manifest = _load_json(run_dir / TRAINING_MANIFEST)
                # Unknown, running, paused and resumable runs may still need last.pt.
                if manifest.get("status") != "completed":
                    continue
                for model in (run_dir / "weights").glob("*.pt"):
                    if model.name.casefold() != "best.pt" and model.resolve() not in keep:
                        candidates.append(model.resolve())
        return sorted(set(candidates))

    def prune_model_files(self, *, apply: bool = False) -> dict[str, Any]:
        candidates = self.model_prune_candidates()
        total = sum(path.stat().st_size for path in candidates)
        if apply:
            allowed_roots = (self.pretrained_root, self.training_root)
            for path in candidates:
                if not any(_is_within(path, root) for root in allowed_roots):
                    raise RuntimeError(f"拒絕刪除模型管理目錄外的檔案：{path}")
                path.unlink()
        return {
            "file_count": len(candidates),
            "size_bytes": total,
            "applied": apply,
            "paths": [self._relative(path) for path in candidates],
        }

    def clear_rebuildable_cache(self, *, apply: bool = False) -> dict[str, Any]:
        if apply:
            control = _load_json(self.control_root / "status.json")
            running = control.get("state") in {"running", "starting", "stopping"}
            if self.training_root.is_dir():
                running = running or any(
                    _load_json(run / TRAINING_MANIFEST).get("status") == "running"
                    for run in self.training_root.iterdir() if run.is_dir()
                )
            if running:
                raise RuntimeError("訓練仍在執行，請先停止訓練再清理資料集快取。")
        files = self.cache_files()
        total = sum(path.stat().st_size for path in files)
        if apply:
            for path in files:
                if not _is_within(path, self.dataset_root):
                    raise RuntimeError(f"拒絕刪除管理目錄外的檔案：{path}")
                path.unlink()
            self.refresh_catalog()
        return {"file_count": len(files), "size_bytes": total, "applied": apply}


def _print_table(artifacts: Iterable[Artifact]) -> None:
    print(f"{'TYPE':<14} {'STATUS':<11} {'SIZE':>10}  {'LATEST':<6} ID")
    for item in artifacts:
        print(
            f"{item.kind:<14} {item.status:<11} {format_bytes(item.size_bytes):>10}  "
            f"{('*' if item.latest else ''):<6} {item.id}"
        )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Bloom Mushroom Finder 產物盤點與安全管理")
    parser.add_argument("--config", type=Path, default=CONFIG_PATH)
    subparsers = parser.add_subparsers(dest="command", required=True)
    list_parser = subparsers.add_parser("list", help="列出資料集、run 與模型")
    list_parser.add_argument("--kind", choices=["dataset", "training_run", "model"])
    list_parser.add_argument("--json", action="store_true")
    subparsers.add_parser("refresh", help="重建產物 catalog")
    verify = subparsers.add_parser("verify", help="驗證所有或指定產物")
    verify.add_argument("artifact_id", nargs="?")
    verify.add_argument("--json", action="store_true")
    latest = subparsers.add_parser("set-latest", help="設定目前資料集或 run")
    latest.add_argument("artifact_id")
    clean = subparsers.add_parser("clean-cache", help="盤點或清除可重建的 NPY/cache")
    clean.add_argument("--apply", action="store_true", help="實際刪除；省略時只預覽")
    prune = subparsers.add_parser("prune-models", help="保留設定的預訓練模型與各 run 的 best.pt")
    prune.add_argument("--apply", action="store_true", help="實際刪除；省略時只預覽")
    return parser


def main() -> int:
    args = build_parser().parse_args()
    manager = ArtifactManager.from_config_file(args.config)
    if args.command == "list":
        artifacts = manager.scan()
        if args.kind:
            artifacts = [item for item in artifacts if item.kind == args.kind]
        if args.json:
            print(json.dumps([item.to_dict() for item in artifacts], ensure_ascii=False, indent=2))
        else:
            _print_table(artifacts)
        return 0
    if args.command == "refresh":
        payload = manager.refresh_catalog()
        print(f"已更新 {manager.catalog_path}：{len(payload['artifacts'])} 個產物")
        return 0
    if args.command == "verify":
        artifacts = [manager.artifact(args.artifact_id)] if args.artifact_id else manager.scan()
        results = {item.id: manager.verify(item) for item in artifacts}
        if args.json:
            print(json.dumps(results, ensure_ascii=False, indent=2))
        else:
            for artifact_id, errors in results.items():
                print(f"{'FAIL' if errors else 'OK  '} {artifact_id}")
                for error in errors:
                    print(f"     - {error}")
        return 1 if any(results.values()) else 0
    if args.command == "set-latest":
        print(f"目前產物已設為：{manager.set_latest(args.artifact_id)}")
        return 0
    if args.command == "clean-cache":
        result = manager.clear_rebuildable_cache(apply=args.apply)
        action = "已清除" if result["applied"] else "可清除"
        print(f"{action} {result['file_count']} 個可重建快取，共 {format_bytes(result['size_bytes'])}")
        if not result["applied"]:
            print("這只是預覽；確認後加上 --apply 才會實際刪除。")
        return 0
    if args.command == "prune-models":
        result = manager.prune_model_files(apply=args.apply)
        action = "已移除" if result["applied"] else "可移除"
        print(f"{action} {result['file_count']} 個模型權重，共 {format_bytes(result['size_bytes'])}")
        for path in result["paths"]:
            print(f"- {path}")
        if result["applied"]:
            manager.refresh_catalog()
        else:
            print("這只是預覽；確認後加上 --apply 才會實際刪除。")
        return 0
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
