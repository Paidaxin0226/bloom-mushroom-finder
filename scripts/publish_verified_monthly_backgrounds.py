"""Publish a completed real-device validation without overwriting old photos."""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from bloompilot.monthly_route import MONTHLY_BACKGROUND_ROUTE
from bloompilot.monthly_workflow import MonthlyWorkflowStore, _route_key, validate_cycle
from bloompilot.phone_automation import iter_rectangular_scan_points, slugify_city
from bloompilot.route_library import RouteLibrary


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cycle", required=True)
    parser.add_argument("--attempt", required=True, type=int)
    args = parser.parse_args()
    cycle = validate_cycle(args.cycle)
    evidence = ROOT / "runtime" / "monthly_activity" / "real_device_validation" / cycle / f"attempt_{args.attempt:03d}"
    status_path = evidence / "status.json"
    state = json.loads(status_path.read_text(encoding="utf-8-sig"))
    if state["state"] != "completed" or state["verified"] != state["total"]:
        raise RuntimeError("Only a fully completed and verified attempt may be published")
    source = evidence / "capture" / cycle / "backgrounds"
    target = ROOT / "data" / "monthly_activity" / cycle / "backgrounds"
    for directory in (source, target):
        if directory.resolve() != directory or not directory.resolve().is_relative_to(ROOT):
            raise RuntimeError(f"Unexpected capture directory: {directory}")
    if target.exists() and any(path.is_file() or path.is_symlink() for path in target.rglob("*")):
        raise RuntimeError("The destination contains files; publication will not overwrite them")

    expected: set[str] = set()
    city_counts: dict[str, int] = {}
    for index, waypoint in enumerate(RouteLibrary(evidence).waypoints("route.json"), 1):
        if waypoint.scan_spans is not None:
            raise RuntimeError("This validation expects the fixed monthly rectangle route")
        left, right, up, down = waypoint.resolved_radii
        points = list(iter_rectangular_scan_points(
            waypoint.horizontal_radius, waypoint.vertical_radius,
            left_radius=left, right_radius=right, up_radius=up, down_radius=down,
        ))
        folder = f"{_route_key(MONTHLY_BACKGROUND_ROUTE)}/wp_{index:05d}_{slugify_city(waypoint.id)[:48]}"
        expected.update(f"{folder}/{point.stem}.png" for point in points)
        city_counts[waypoint.name] = len(points)
    files = {path.relative_to(source).as_posix(): path for path in source.rglob("*.png")}
    if set(files) != expected or len(files) != state["total"]:
        raise RuntimeError(f"Search-grid mismatch: missing={sorted(expected - files.keys())}, unexpected={sorted(files.keys() - expected)}")
    verified = json.loads((evidence / "verified-backgrounds.json").read_text(encoding="utf-8"))
    sizes = {item["path"]: item["size_bytes"] for item in verified}
    if set(sizes) != expected or any(files[name].stat().st_size != sizes[name] for name in expected):
        raise RuntimeError("Files changed after the completed image validation")
    hashes: dict[str, list[str]] = {}
    for name, path in files.items():
        hashes.setdefault(hashlib.sha256(path.read_bytes()).hexdigest(), []).append(name)
    audit = {
        "attempt": args.attempt, "cycle": cycle, "count": len(files),
        "city_counts": city_counts, "bytes": sum(sizes.values()),
        "duplicate_image_groups": [names for names in hashes.values() if len(names) > 1],
        "destination": str(target), "published_at": datetime.now().astimezone().isoformat(),
    }
    # Remove empty directories only. rmdir refuses unexpected files, even if
    # another process creates one after the preflight check above.
    if target.exists():
        for directory in sorted((path for path in target.rglob("*") if path.is_dir()), key=lambda path: len(path.parts), reverse=True):
            if directory.resolve() != directory or not directory.is_relative_to(target):
                raise RuntimeError(f"Unexpected destination subdirectory: {directory}")
            directory.rmdir()
        target.rmdir()
    target.parent.mkdir(parents=True, exist_ok=True)
    source.rename(target)
    store = MonthlyWorkflowStore()
    store.mark_collection_started(cycle, [MONTHLY_BACKGROUND_ROUTE])
    store.update_manifest(cycle, collection_started_at=state["started_at"], validation=audit)
    store.mark_collection_complete(cycle, captured=len(files), routes=[MONTHLY_BACKGROUND_ROUTE])
    (evidence / "publication.json").write_text(json.dumps(audit, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(audit, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
