from __future__ import annotations

import argparse
import gc
import json
import sys
import tracemalloc
from datetime import UTC, datetime
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from bloompilot.geo_recognition import validate_geo_waypoints
from bloompilot.web.runtime import geo_waypoints_from_payload


def main(path: Path, output: Path) -> None:
    path = path.resolve()
    tracemalloc.start()
    text = path.read_text(encoding="utf-8")
    payload = json.loads(text)
    raw_waypoints = payload["sequence"]["waypoints"]
    waypoints = geo_waypoints_from_payload({"waypoints": raw_waypoints})
    total_points = validate_geo_waypoints(waypoints)
    del text, raw_waypoints, payload
    gc.collect()
    current, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    report = {
        "schema": "bloompilot.geo-route-memory-report.v1",
        "measured_at": datetime.now(UTC).replace(microsecond=0).isoformat(),
        "route": str(path),
        "file_size_bytes": path.stat().st_size,
        "waypoints": len(waypoints),
        "search_points": total_points,
        "python_traced_current_bytes_after_raw_json_release": current,
        "python_traced_peak_bytes_during_parse_and_normalize": peak,
        "python_traced_peak_mib": round(peak / 1024 / 1024, 2),
    }
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description="量測大型座標序列解析與正規化的 Python 記憶體峰值。")
    result.add_argument("route", type=Path)
    result.add_argument("--output", type=Path, required=True)
    return result


if __name__ == "__main__":
    arguments = parser().parse_args()
    main(arguments.route, arguments.output)
