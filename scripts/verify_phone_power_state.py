"""Check screen-off detection and wake only after a full collection has ended."""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from bloompilot.monthly_workflow import validate_cycle
from bloompilot.phone_automation import AdbClient
from bloompilot.phone_state import PhoneStateMonitor
from bloompilot.phone_tools import find_toolchain


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cycle", required=True)
    parser.add_argument("--attempt", required=True, type=int)
    parser.add_argument("--serial", required=True)
    args = parser.parse_args()
    evidence = ROOT / "runtime/monthly_activity/real_device_validation" / validate_cycle(args.cycle) / f"attempt_{args.attempt:03d}"
    collection = json.loads((evidence / "status.json").read_text(encoding="utf-8"))
    if collection["state"] != "completed":
        raise RuntimeError("Finish the full background collection before changing the screen state")
    if collection["serial"] != args.serial:
        raise RuntimeError("The test must use the phone from the completed collection")
    adb = find_toolchain().adb
    client = AdbClient(adb, args.serial)
    monitor = PhoneStateMonitor(cache_seconds=0)
    results: dict = {"serial": args.serial, "before": monitor.read(adb, args.serial, force=True)}
    if results["before"]["adb_state"] != "device":
        raise RuntimeError("The phone must be online before testing screen sleep")

    def wait_for_screen(states: set[str]) -> dict:
        deadline = time.monotonic() + 10
        while True:
            state = monitor.read(adb, args.serial, force=True)
            if state["adb_state"] == "device" and state["screen_state"] in states:
                return state
            if time.monotonic() >= deadline:
                raise RuntimeError(f"Expected screen {states}, observed {state}")
            time.sleep(0.5)

    try:
        client.run("shell", "input", "keyevent", "KEYCODE_SLEEP", timeout=5)
        results["asleep"] = wait_for_screen({"off", "dozing"})
        results["wake_result"] = monitor.wake(adb, args.serial)
        results["awake"] = wait_for_screen({"on"})
        results["passed"] = True
    except BaseException as exc:
        results["passed"] = False
        results["error"] = str(exc)
        raise
    finally:
        # Always restore the screen; secure locks remain under the user's control.
        try:
            results["final"] = monitor.wake(adb, args.serial)
        except Exception as exc:
            results["restore_error"] = str(exc)
        (evidence / "phone-power-validation.json").write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(results, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
