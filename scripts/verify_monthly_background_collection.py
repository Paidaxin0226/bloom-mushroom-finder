"""Run one fresh, real-device monthly collection and preserve audit evidence."""

from __future__ import annotations

import argparse
import json
import logging
import sys
import threading
import traceback
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from bloompilot.device_control import UsbDeviceController
from bloompilot.diagnostics import configure_diagnostics
from bloompilot.memory_watchdog import RESTART_SCREEN_MAP, classify_pikmin_restart_screen
from bloompilot.monthly_route import ensure_monthly_background_route
from bloompilot.monthly_workflow import MonthlyWorkflowStore, collect_fixed_route_backgrounds, validate_cycle
from bloompilot.paths import BACKGROUND_SOURCE_DIR, ROUTES_DIR
from bloompilot.phone_automation import AdbClient
from bloompilot.phone_tools import find_toolchain
from bloompilot.route_library import RouteLibrary


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cycle", required=True)
    parser.add_argument("--serial", required=True)
    parser.add_argument("--attempt", type=int, required=True)
    args = parser.parse_args()
    cycle = validate_cycle(args.cycle)
    evidence = ROOT / "runtime" / "monthly_activity" / "real_device_validation" / cycle / f"attempt_{args.attempt:03d}"
    evidence.mkdir(parents=True, exist_ok=False)
    configure_diagnostics(evidence)
    stop = threading.Event()
    def watch_stop_request() -> None:
        while not stop.wait(0.5):
            if (evidence / "stop-request.txt").is_file():
                stop.set()
    threading.Thread(target=watch_stop_request, name="validation-stop-request", daemon=True).start()
    state: dict[str, object] = {
        "attempt": args.attempt, "cycle": cycle, "serial": args.serial,
        "started_at": datetime.now().astimezone().isoformat(), "state": "starting", "done": 0,
    }

    def publish(**changes: object) -> None:
        state.update(changes)
        state["updated_at"] = datetime.now().astimezone().isoformat()
        temporary = evidence / "status.tmp"
        temporary.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
        temporary.replace(evidence / "status.json")

    # The production heartbeat can report concurrently with the scan worker.
    report_lock = threading.RLock()

    def report(message: str) -> None:
        with report_lock:
            logging.getLogger("bloompilot.validation").info(message)
            publish(message=message)
            print(message, flush=True)

    def progress(done: int, total: int, detail: dict) -> None:
        with report_lock:
            publish(done=done, total=total, detail=detail)

    toolchain = find_toolchain()
    adb = AdbClient(toolchain.adb, args.serial)

    def diagnostics(label: str) -> None:
        for package in ("com.locwarp.mock", "com.nianticlabs.pikmin"):
            try:
                result = adb.run("shell", "dumpsys", "activity", "exit-info", package, timeout=12)
                (evidence / f"{label}-{package}-exits.txt").write_text(str(result.stdout), encoding="utf-8")
            except Exception as exc:
                (evidence / f"{label}-{package}-diagnostic-error.txt").write_text(str(exc), encoding="utf-8")
        try:
            (evidence / f"{label}-screen.png").write_bytes(adb.capture_png(attempts=1, timeout=15))
        except Exception as exc:
            (evidence / f"{label}-screen-error.txt").write_text(str(exc), encoding="utf-8")

    controller = UsbDeviceController(toolchain.adb, args.serial)
    # Every attempt uses an empty workspace. Existing monthly photos and prior
    # failed attempts remain intact; only a fully verified run is published.
    store = MonthlyWorkflowStore(
        project_root=ROOT, workspace_root=evidence / "capture",
        state_path=evidence / "workflow-state.json",
    )
    try:
        route, document = ensure_monthly_background_route(BACKGROUND_SOURCE_DIR, ROUTES_DIR)
        (evidence / "route.json").write_text(json.dumps(document, ensure_ascii=False, indent=2), encoding="utf-8")
        total = document["metadata"]["total_search_points"]
        publish(total=total, towns=document["metadata"]["towns"])
        if store.list_backgrounds(cycle):
            raise RuntimeError("A fresh attempt must start with an empty capture directory")
        report(f"第 {args.attempt} 次全新蒐集：使用獨立空白目錄，目標 {total} 張。")
        adb.ensure_ready()
        diagnostics("before")
        controller.connect(timeout=30)
        publish(state="running")
        result = collect_fixed_route_backgrounds(
            store=store, route_library=RouteLibrary(ROUTES_DIR), controller=controller,
            adb=toolchain.adb, serial=args.serial, cycle=cycle, route_paths=[route],
            settle_seconds=4.0, swipe_duration_ms=800, stop_event=stop,
            status=report, progress=progress,
        )
        publish(state="verifying", result=result)
        records = store.list_backgrounds(cycle)
        if result["captured"] != total or len(records) != total:
            raise RuntimeError(f"Incomplete collection: captured={result['captured']}, files={len(records)}, expected={total}")
        invalid = []
        for item in records:
            payload = store.background_file(cycle, item["path"]).read_bytes()
            if classify_pikmin_restart_screen(payload) != RESTART_SCREEN_MAP:
                invalid.append(item["path"])
        if invalid:
            raise RuntimeError(f"Saved frames failed final 2D map verification: {invalid}")
        diagnostics("after")
        (evidence / "verified-backgrounds.json").write_text(json.dumps(records, ensure_ascii=False, indent=2), encoding="utf-8")
        publish(state="completed", verified=len(records), bytes=sum(item["size_bytes"] for item in records),
                completed_at=datetime.now().astimezone().isoformat())
        report(f"完整蒐集成功，已逐張驗證 {len(records)}/{total} 張為可解碼 2D 地圖。")
        return 0
    except BaseException as exc:
        stop.set()
        error = traceback.format_exc()
        (evidence / "failure.txt").write_text(error, encoding="utf-8")
        with report_lock:
            publish(state="failed", error=str(exc), completed_at=datetime.now().astimezone().isoformat())
        diagnostics("failure")
        print(error, file=sys.stderr, flush=True)
        return 1
    finally:
        stop.set()
        controller.close(forward_timeout=1)


if __name__ == "__main__":
    raise SystemExit(main())
