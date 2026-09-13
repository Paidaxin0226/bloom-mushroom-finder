from __future__ import annotations

import subprocess
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from fastapi.testclient import TestClient

from bloompilot.phone_state import PhoneStateMonitor, parse_power_state
from bloompilot.web.runtime import AppRuntime
from bloompilot.web.server import create_app


def completed(stdout="", *, stderr="", returncode=0):
    return subprocess.CompletedProcess([], returncode, stdout, stderr)


class PhoneStateTests(unittest.TestCase):
    def test_mi9_power_and_policy_report_screen_lock_and_battery(self):
        value = parse_power_state("mWakefulness=Awake\nmIsPowered=true\nmBatteryLevel=100\nmWakefulness=1", "showing=false\nscreenState=SCREEN_STATE_ON")
        self.assertEqual(value, {"screen_state": "on", "locked": False, "battery_percent": 100, "power_connected": True})

    def test_screen_off_overrides_awake_during_display_transition(self):
        self.assertEqual(parse_power_state("mWakefulness=Awake", "screenState=SCREEN_STATE_OFF\nshowing=true")["screen_state"], "off")

    def test_sleep_doze_and_missing_fields_are_not_assumed_awake_or_unlocked(self):
        for text, expected in (("mWakefulness=Asleep", "off"), ("mWakefulness=Dozing", "dozing"), ("Display Power: state=DOZE_SUSPEND", "dozing"), ("unrecognized ROM", "unknown")):
            with self.subTest(text=text):
                value = parse_power_state(text, "")
                self.assertEqual(value["screen_state"], expected)
                self.assertIsNone(value["locked"])

    def test_offline_missing_unauthorized_and_recovery_never_query_power_or_start_usb(self):
        for output, error, code, expected in (("", "error: device offline", 1, "offline"), ("", "device 'phone' not found", 1, "missing"), ("", "device unauthorized", 1, "unauthorized"), ("recovery", "", 0, "recovery")):
            with self.subTest(expected=expected):
                run = Mock(return_value=completed(output, stderr=error, returncode=code))
                state = PhoneStateMonitor(run=run).read(Path("adb"), "phone")
                self.assertEqual(state["adb_state"], expected)
                self.assertEqual(state["screen_state"], "unknown")
                self.assertIsNone(state["battery_percent"])
                run.assert_called_once()
                self.assertEqual(run.call_args.args[0], ["adb", "-s", "phone", "get-state"])

    def test_cache_expires_and_offline_result_clears_previous_screen_and_battery(self):
        now = [0]
        run = Mock(side_effect=[completed("device"), completed("mWakefulness=Awake\nmBatteryLevel=88"), completed("showing=false"), completed(stderr="device offline", returncode=1)])
        monitor = PhoneStateMonitor(clock=lambda: now[0], run=run)
        self.assertEqual(monitor.read(Path("adb"), "phone")["battery_percent"], 88)
        now[0] = 4
        self.assertEqual(monitor.read(Path("adb"), "phone")["screen_state"], "on")
        self.assertEqual(run.call_count, 3)
        now[0] = 5
        state = monitor.read(Path("adb"), "phone")
        self.assertEqual(state["adb_state"], "offline")
        self.assertIsNone(state["battery_percent"])
        self.assertEqual(state["screen_state"], "unknown")

    def test_probe_timeout_is_bounded_and_reported_as_unknown(self):
        run = Mock(side_effect=subprocess.TimeoutExpired("adb", 3))
        result = PhoneStateMonitor(run=run).read(Path("adb"), "phone")
        self.assertEqual(result["adb_state"], "unknown")
        self.assertTrue(result["error"])
        self.assertLessEqual(run.call_args.kwargs["timeout"], 3)

    def test_partial_probe_retains_power_but_does_not_guess_lock_state(self):
        run = Mock(side_effect=[completed("device"), completed("mWakefulness=Asleep"), completed(stderr="Permission denied", returncode=1)])
        result = PhoneStateMonitor(run=run).read(Path("adb"), "phone")
        self.assertEqual(result["adb_state"], "device")
        self.assertEqual(result["screen_state"], "off")
        self.assertIsNone(result["locked"])
        self.assertIn("Permission denied", result["error"])

    def test_wake_reports_secure_lock_without_claiming_to_unlock_it(self):
        run = Mock(side_effect=[completed("device"), completed("mWakefulness=Asleep"), completed("showing=true"), completed(), completed(), completed("device"), completed("mWakefulness=Awake"), completed("showing=true")])
        result = PhoneStateMonitor(run=run).wake(Path("adb"), "phone")
        self.assertEqual(result["screen_state"], "on")
        self.assertTrue(result["locked"])
        commands = [call.args[0][3:] for call in run.call_args_list]
        self.assertIn(["shell", "input", "keyevent", "KEYCODE_WAKEUP"], commands)
        self.assertNotIn(["shell", "input", "keyevent", "KEYCODE_POWER"], commands)

    def test_wake_refuses_unavailable_phone(self):
        run = Mock(return_value=completed(stderr="device not found", returncode=1))
        with self.assertRaisesRegex(RuntimeError, "無法喚醒"):
            PhoneStateMonitor(run=run).wake(Path("adb"), "phone")
        run.assert_called_once()

    def test_runtime_offline_probe_prevents_stale_usb_status_and_reconnect(self):
        runtime = AppRuntime()
        try:
            runtime.device._serial = "phone"
            runtime.phone_monitor = Mock()
            runtime.phone_monitor.read.return_value = {"adb_state": "missing", "screen_state": "unknown"}
            with patch("bloompilot.web.runtime.find_toolchain", return_value=Mock(adb=Path("adb"))), patch.object(runtime.device, "status") as usb_status:
                result = runtime.device_status()
            self.assertFalse(result["connected"])
            self.assertEqual(result["phone"]["adb_state"], "missing")
            usb_status.assert_not_called()
        finally:
            runtime.close()

    def test_phone_http_routes_validate_serial_and_return_observed_state(self):
        runtime = Mock()
        runtime.phone_state.return_value = {"adb_state": "device", "screen_state": "off"}
        runtime.wake_phone.return_value = {"adb_state": "device", "screen_state": "on", "locked": True}
        client = TestClient(create_app(runtime))
        self.assertEqual(client.get("/api/device/phone-state").status_code, 422)
        self.assertEqual(client.get("/api/device/phone-state", params={"serial": "phone"}).json()["screen_state"], "off")
        self.assertTrue(client.post("/api/device/wake", json={"serial": "phone"}).json()["locked"])
        runtime.wake_phone.side_effect = RuntimeError("offline")
        self.assertEqual(client.post("/api/device/wake", json={"serial": "phone"}).status_code, 409)


if __name__ == "__main__":
    unittest.main()
