"""Backfill participant badge numbers in saved recognition JSONL records."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
from typing import Any

import cv2

from bloompilot.badge_number import enrich_detections_with_badge_numbers
from bloompilot.paths import RECOGNITION_RUNS_DIR


def backfill_log(path: Path, *, overwrite: bool = False) -> dict[str, int]:
    stats = {
        "records": 0,
        "detections": 0,
        "recognized": 0,
        "new": 0,
        "image_errors": 0,
        "json_errors": 0,
    }
    temporary = path.with_name(path.name + ".participant-counts.tmp")
    changed = False
    with path.open("r", encoding="utf-8") as source, temporary.open(
        "w", encoding="utf-8", newline="\n"
    ) as destination:
        for raw_line in source:
            try:
                record: dict[str, Any] = json.loads(raw_line)
            except (TypeError, ValueError):
                stats["json_errors"] += 1
                destination.write(raw_line)
                continue
            stats["records"] += 1
            detections = [
                detection
                for detection in record.get("detections", [])
                if isinstance(detection, dict)
            ]
            stats["detections"] += len(detections)
            pending = [
                detection
                for detection in detections
                if overwrite or "participant_count" not in detection
            ]
            if pending:
                original = record.get("original")
                image_path = path.parent / original if isinstance(original, str) else None
                frame = cv2.imread(str(image_path)) if image_path is not None else None
                if frame is None:
                    stats["image_errors"] += 1
                else:
                    before = sum("participant_count" in detection for detection in pending)
                    before_payload = json.dumps(
                        pending, ensure_ascii=False, sort_keys=True, separators=(",", ":")
                    )
                    enrich_detections_with_badge_numbers(frame, pending)
                    after = sum("participant_count" in detection for detection in pending)
                    stats["new"] += max(0, after - before)
                    if json.dumps(
                        pending, ensure_ascii=False, sort_keys=True, separators=(",", ":")
                    ) != before_payload:
                        changed = True
            stats["recognized"] += sum(
                "participant_count" in detection for detection in detections
            )
            destination.write(
                json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n"
            )
    if changed:
        os.replace(temporary, path)
    else:
        temporary.unlink(missing_ok=True)
    return stats


def main() -> int:
    parser = argparse.ArgumentParser(
        description="辨識既有 detections.jsonl 命中框內的人數徽章。"
    )
    parser.add_argument(
        "root",
        nargs="?",
        type=Path,
        default=RECOGNITION_RUNS_DIR,
        help="辨識輸出根目錄或單一 detections.jsonl",
    )
    parser.add_argument("--overwrite", action="store_true", help="重算已有人數欄位")
    args = parser.parse_args()
    root = args.root.resolve()
    logs = [root] if root.is_file() else sorted(root.rglob("detections.jsonl"))
    total = {
        "records": 0,
        "detections": 0,
        "recognized": 0,
        "new": 0,
        "image_errors": 0,
        "json_errors": 0,
    }
    for log in logs:
        stats = backfill_log(log, overwrite=args.overwrite)
        for key, value in stats.items():
            total[key] += value
        print(
            f"{log.parent.name}: {stats['recognized']}/{stats['detections']} 已辨識"
            f"（新增 {stats['new']}）"
        )
    print(
        f"完成：{len(logs)} 份紀錄、{total['detections']} 個命中、"
        f"辨識 {total['recognized']}、新增 {total['new']}、"
        f"圖片錯誤 {total['image_errors']}、JSON 錯誤 {total['json_errors']}"
    )
    return 0 if total["json_errors"] == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
