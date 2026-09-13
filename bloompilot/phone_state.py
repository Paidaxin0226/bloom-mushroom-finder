"""Read physical Android availability independently of the LocWarp socket."""

from __future__ import annotations

import re
import subprocess
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .phone_tools import _process_kwargs


def parse_power_state(power: str, policy: str) -> dict[str, Any]:
    screen = "unknown"
    match = re.search(r"Display Power: state=(ON|OFF|DOZE_SUSPEND|DOZE)\b", power)
    if not match:
        match = re.search(r"\bscreenState=SCREEN_STATE_(ON|OFF)\b", policy)
    if match:
        screen = {"ON": "on", "OFF": "off", "DOZE": "dozing", "DOZE_SUSPEND": "dozing"}[match[1]]
    else:
        match = re.search(r"\bmWakefulness=(Awake|Asleep|Dozing|Dreaming)\b", power)
        if match:
            screen = {"Awake": "on", "Asleep": "off", "Dozing": "dozing", "Dreaming": "on"}[match[1]]
    locked = re.search(r"(?m)^\s*(?:showing|mKeyguardShowing|mShowingLockscreen|mIsShowing)=(true|false)\b", policy)
    level = re.search(r"\bmBatteryLevel=(\d+)\b", power)
    powered = re.search(r"\bmIsPowered=(true|false)\b", power)
    return {
        "screen_state": screen,
        "locked": None if locked is None else locked[1] == "true",
        "battery_percent": None if level is None else max(0, min(100, int(level[1]))),
        "power_connected": None if powered is None else powered[1] == "true",
    }


class PhoneStateMonitor:
    """Cache bounded ADB probes; a dead phone never requires a USB handshake."""

    def __init__(self, *, cache_seconds: float = 5, clock=time.monotonic, run=subprocess.run) -> None:
        self.cache_seconds = cache_seconds
        self.clock = clock
        self.run = run
        self._lock = threading.RLock()
        self._cached: tuple[str, str, float, dict[str, Any]] | None = None

    def _run(self, adb: Path, serial: str, *args: str, deadline: float):
        remaining = deadline - self.clock()
        if remaining <= 0:
            raise TimeoutError("手機狀態查詢逾時。")
        return self.run(
            [str(adb), "-s", serial, *args], capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=min(3, remaining), **_process_kwargs(),
        )

    def read(self, adb: Path, serial: str, *, force: bool = False) -> dict[str, Any]:
        serial = serial.strip()
        with self._lock:
            cached = self._cached
            if not force and cached and cached[:2] == (str(adb), serial) and self.clock() - cached[2] < self.cache_seconds:
                return dict(cached[3])
            state: dict[str, Any] = {
                "serial": serial, "adb_state": "unselected" if not serial else "unknown",
                "screen_state": "unknown", "locked": None, "battery_percent": None,
                "power_connected": None, "checked_at": datetime.now(timezone.utc).isoformat(), "error": None,
            }
            if not serial:
                return state
            deadline = self.clock() + 7
            try:
                result = self._run(adb, serial, "get-state", deadline=deadline)
                output = str(result.stdout or "").strip()
                detail = f"{output}\n{result.stderr or ''}".lower()
                if result.returncode == 0 and output == "device":
                    state["adb_state"] = "device"
                    power = self._run(adb, serial, "shell", "dumpsys", "power", deadline=deadline)
                    if power.returncode != 0:
                        raise RuntimeError(str(power.stderr or "無法讀取手機電源狀態。"))
                    # Preserve valid power data even when a ROM denies the lock query.
                    state.update(parse_power_state(str(power.stdout or ""), ""))
                    policy = self._run(adb, serial, "shell", "dumpsys", "window", "policy", deadline=deadline)
                    if policy.returncode != 0:
                        raise RuntimeError(str(policy.stderr or "無法讀取手機鎖定狀態。"))
                    state.update(parse_power_state(str(power.stdout or ""), str(policy.stdout or "")))
                elif "unauthorized" in detail or "no permissions" in detail:
                    state["adb_state"] = "unauthorized"
                elif "offline" in detail:
                    state["adb_state"] = "offline"
                elif "not found" in detail or "no devices" in detail:
                    state["adb_state"] = "missing"
                elif output in {"recovery", "bootloader", "sideload"}:
                    state["adb_state"] = output
                else:
                    state["error"] = "ADB 暫時無法確認手機狀態。"
            except (OSError, subprocess.TimeoutExpired, TimeoutError, RuntimeError) as exc:
                state["error"] = str(exc).splitlines()[0] or "手機狀態查詢失敗。"
            self._cached = (str(adb), serial, self.clock(), state)
            return dict(state)

    def wake(self, adb: Path, serial: str) -> dict[str, Any]:
        with self._lock:
            current = self.read(adb, serial, force=True)
            if current["adb_state"] != "device":
                raise RuntimeError("手機未透過 ADB 在線，無法喚醒；請確認開機、USB 連線與偵錯授權。")
            deadline = self.clock() + 6
            for args in (("shell", "input", "keyevent", "KEYCODE_WAKEUP"), ("shell", "wm", "dismiss-keyguard")):
                result = self._run(adb, serial, *args, deadline=deadline)
                if result.returncode != 0:
                    raise RuntimeError(str(result.stderr or "手機喚醒失敗。"))
            # Secure PIN/password locks remain locked; report the actual state.
            return self.read(adb, serial, force=True)
