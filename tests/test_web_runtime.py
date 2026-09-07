from __future__ import annotations

import json
import threading
import tempfile
import time
import unittest
import warnings
from contextlib import nullcontext
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, PropertyMock, patch

import cv2
import numpy as np

warnings.filterwarnings("ignore", message="Using `httpx` with `starlette.testclient` is deprecated")

from fastapi.testclient import TestClient

from bloompilot.web.events import EventHub
from bloompilot.web.runtime import (
    AppRuntime,
    DeviceSession,
    JobManager,
    JobRecord,
    _open_recognition_frame_source,
    geo_waypoints_from_payload,
)
from bloompilot.device_control import DeviceResponse
from bloompilot.device_scale import DeviceScaleStore, MarkerDisplacement, new_device_scale_calibration
from bloompilot.training_control import TrainingControl
from bloompilot.web.server import create_app
from bloompilot.route_library import RouteLibrary
from bloompilot.settings_store import load_studio_settings, save_studio_settings


def sample_device_scale(serial: str = "test"):
    return new_device_scale_calibration(
        serial=serial,
        screen_width=1080,
        screen_height=2400,
        origin_lat=25,
        origin_lng=121,
        east_probe_m=120,
        north_probe_m=120,
        east_coordinate_delta=0.0012,
        north_coordinate_delta=0.0011,
        east_pixels=180,
        north_pixels=185,
    )


class RecognitionFrameSourceTests(unittest.TestCase):
    def test_open_source_configures_adb_fallback_for_runtime_failures(self) -> None:
        expected = np.full((12, 8, 3), 42, dtype=np.uint8)
        ok, encoded = cv2.imencode(".png", expected)
        self.assertTrue(ok)
        client = Mock()
        client.capture_png.return_value = encoded.tobytes()
        toolchain = SimpleNamespace(adb=Path("adb"), server=Path("server"), version="3.3")
        stopped = threading.Event()
        with (
            patch("bloompilot.web.runtime.AdbClient", return_value=client) as adb,
            patch("bloompilot.web.runtime.ScrcpyFrameSource.start"),
        ):
            source = _open_recognition_frame_source(toolchain, "phone", stopped, Mock())
        self.assertIsNotNone(source)
        adb.assert_called_once_with(toolchain.adb, "phone")
        source._error = OSError("USB video disconnected after startup")
        np.testing.assert_array_equal(source.capture_image(), expected)
        client.capture_png.assert_called_once_with()
        source.close()


class DeviceSessionTests(unittest.TestCase):
    def test_status_reconnects_same_controller_after_service_exit(self) -> None:
        session = DeviceSession(Mock())
        controller = Mock()
        controller.connected = False
        controller.connect.return_value = DeviceResponse(True, "paired", {"running": False}, {})
        session._controller = controller
        session._serial = "phone"
        status = session.status()
        self.assertEqual(status, {"connected": True, "serial": "phone", "running": False})
        self.assertIs(session._controller, controller)
        controller.connect.assert_called_once_with(timeout=8.0, only_if_disconnected=True)

    def test_polling_does_not_reconnect_after_explicit_disconnect(self) -> None:
        session = DeviceSession(Mock())
        controller = Mock()
        session._controller = controller
        session._serial = "phone"
        session.disconnect()
        self.assertEqual(session.status(), {"connected": False, "serial": ""})
        controller.connect.assert_not_called()

    def test_command_preserves_command_specific_usb_result_fields(self) -> None:
        controller = Mock()
        controller.connected = True
        controller.command.return_value = DeviceResponse(
            True,
            "Search completed",
            {"running": False},
            {
                "v": 1,
                "type": "result",
                "id": "request-1",
                "ok": True,
                "message": "Search completed",
                "results": [{"label": "Taipei", "lat": 25.03, "lng": 121.56}],
                "status": {"running": False},
            },
        )
        controller.status.return_value = {"running": False}
        hub = Mock()
        session = DeviceSession(hub)
        session._controller = controller
        session._serial = "android-1"

        result = session.command("searchAddress", {"query": "Taipei"})

        self.assertEqual("Taipei", result["results"][0]["label"])
        self.assertEqual({"running": False}, result["status"])
        self.assertNotIn("id", result)
        controller.status.assert_not_called()
        hub.publish.assert_called_once_with({
            "type": "device_update",
            "device": {"connected": True, "serial": "android-1", "running": False},
        })


class JobManagerTests(unittest.TestCase):
    def test_detection_listener_receives_same_event_payload(self) -> None:
        delivered = threading.Event()
        observed: list[tuple[str, object, object]] = []

        def listener(kind: str, location: object, detections: object) -> None:
            observed.append((kind, location, detections))
            delivered.set()

        manager = JobManager(EventHub(), detection_listener=listener)

        def run(context):
            context.detections(
                {"waypoint_name": "台北", "point": {"grid_x": 1, "grid_y": -1}},
                [{"class_name": "Large_Blue", "confidence": 0.9}],
            )

        manager.start("screen_recognition", run)

        self.assertTrue(delivered.wait(1))
        self.assertEqual("screen_recognition", observed[0][0])
        self.assertEqual("台北", observed[0][1]["waypoint_name"])
        self.assertEqual("Large_Blue", observed[0][2][0]["class_name"])

    def test_recognition_job_pauses_at_checkpoint_and_resumes(self) -> None:
        manager = JobManager(EventHub())
        runner_started = threading.Event()
        restored: list[bool] = []

        def run(context):
            runner_started.set()
            while not context.stop_event.is_set():
                if context.pause_checkpoint():
                    restored.append(True)
                    break
                time.sleep(0.005)
            return {"restored": bool(restored)}

        record = manager.start("screen_recognition", run)
        self.assertTrue(runner_started.wait(1))
        paused = manager.pause_recognition(record.id, timeout=1)
        self.assertTrue(paused.paused)
        manager.update(
            record.id,
            excursion={
                "origin": {"lat": 25.0, "lng": 121.0},
                "target": {"lat": 25.1, "lng": 121.1},
            },
        )
        resumed = manager.resume_recognition(record.id)
        self.assertFalse(resumed.paused)
        deadline = time.monotonic() + 1
        while manager.get(record.id).state == "running" and time.monotonic() < deadline:
            time.sleep(0.005)
        self.assertEqual(restored, [True])
        self.assertEqual(manager.get(record.id).state, "completed")

    def test_cancel_stop_clears_event_and_does_not_resend_until_requested_again(self) -> None:
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as temporary:
            control = TrainingControl(Path(temporary))
            manager = JobManager(EventHub())
            record = JobRecord(id="training-1", kind="training", state="running")
            manager._jobs[record.id] = record
            manager._stops[record.id] = threading.Event()
            manager._active_id = record.id
            manager.stop(record.id)
            # Cancellation also works before the runner has sent the file request.
            manager.cancel_training_stop(record.id, control)
            manager.sync_training_stop(record.id, control)
            self.assertIsNone(control.requested_action())
            manager.stop(record.id)
            manager.sync_training_stop(record.id, control)
            self.assertEqual(control.requested_action(), "stop")
            manager.cancel_training_stop(record.id, control)
            manager.sync_training_stop(record.id, control)
            self.assertEqual(record.state, "running")
            self.assertFalse(manager._stops[record.id].is_set())
            self.assertIsNone(control.requested_action())
            manager.stop(record.id)
            manager.sync_training_stop(record.id, control)
            (control.directory / "stop.request").replace(control.directory / "stop.accepted")
            with self.assertRaisesRegex(RuntimeError, "已生效"):
                manager.cancel_training_stop(record.id, control)
            self.assertTrue(manager._stops[record.id].is_set())
            self.assertEqual(record.state, "stopping")
            with self.assertRaises(KeyError):
                manager.cancel_training_stop("previous-job", control)

    def test_failure_history_and_events_are_plain_text(self) -> None:
        hub = Mock(spec=EventHub)
        manager = JobManager(hub)

        def fail(_context):
            raise RuntimeError("訓練程序失敗。\r\n\x1b[Kbatch 2/10\r\n\x1b[31mCUDA out of memory\x1b[0m")

        record = manager.start("training", fail)
        deadline = time.monotonic() + 2
        while manager.get(record.id).state in {"queued", "running"} and time.monotonic() < deadline:
            time.sleep(0.01)
        history = manager.list()[0]
        self.assertEqual(history["state"], "error")
        self.assertEqual(history["message"], "訓練程序失敗。\nbatch 2/10\nCUDA out of memory")
        self.assertNotIn("\x1b", history["error"])
        self.assertIn("batch 2/10\nCUDA out of memory", history["error"])
        for call in hub.publish.call_args_list:
            self.assertNotIn("\x1b", call.args[0]["job"]["message"])

    def test_job_completes_and_serializes_result(self) -> None:
        manager = JobManager(EventHub())
        record = manager.start("test", lambda context: {"path": context.manager.__class__.__name__})
        deadline = time.monotonic() + 2
        while manager.get(record.id).state in {"queued", "running"} and time.monotonic() < deadline:
            time.sleep(0.01)
        completed = manager.get(record.id)
        self.assertEqual(completed.state, "completed")
        self.assertEqual(completed.result, {"path": "JobManager"})

    def test_only_one_heavy_job_runs_at_a_time_and_stop_is_cooperative(self) -> None:
        manager = JobManager(EventHub())
        started = threading.Event()

        def wait_for_stop(context):
            started.set()
            context.stop_event.wait(2)
            return {"stopped": True}

        first = manager.start("long", wait_for_stop)
        self.assertTrue(started.wait(1))
        with self.assertRaises(RuntimeError):
            manager.start("other", lambda _context: None)
        manager.stop(first.id)
        deadline = time.monotonic() + 2
        while manager.get(first.id).state in {"queued", "running", "stopping"} and time.monotonic() < deadline:
            time.sleep(0.01)
        self.assertEqual(manager.get(first.id).state, "cancelled")


class DatasetRuntimeTests(unittest.TestCase):
    def test_pause_records_search_coordinate_then_allows_teleport(self) -> None:
        runtime = AppRuntime()
        paused = JobRecord(
            id="recognition-1",
            kind="screen_recognition",
            state="running",
            paused=True,
            resume_coordinate={"lat": 25.0, "lng": 121.0},
            resume_serial="phone-1",
        )
        with (
            patch.object(runtime.jobs, "pause_recognition") as pause,
            patch.object(runtime.jobs, "update", return_value=paused) as update,
            patch.object(runtime.device, "status", return_value={"current": {"lat": 25.0, "lng": 121.0}}),
            patch.object(type(runtime.device), "serial", new_callable=PropertyMock, return_value="phone-1"),
            patch.object(runtime.screen_control, "open") as open_control,
        ):
            result = runtime.pause_recognition("recognition-1")
        self.assertIs(result, paused)
        pause.assert_called_once_with("recognition-1")
        self.assertEqual(update.call_args.kwargs["resume_coordinate"], {"lat": 25.0, "lng": 121.0})
        self.assertEqual(update.call_args.kwargs["resume_serial"], "phone-1")
        open_control.assert_not_called()

        final = JobRecord(**{**paused.__dict__, "excursion": {
            "origin": {"lat": 25.0, "lng": 121.0},
            "target": {"lat": 25.123, "lng": 121.456},
        }})
        with (
            patch.object(runtime.jobs, "get", return_value=paused),
            patch.object(runtime.jobs, "update", return_value=final) as update,
            patch.object(runtime.device, "command") as command,
        ):
            result = runtime.teleport_while_paused(
                paused.id,
                lat=25.123,
                lng=121.456,
                label="Large Blue",
            )
        self.assertIs(result, final)
        command.assert_called_once_with("teleport", {"lat": 25.123, "lng": 121.456})
        excursion = update.call_args.kwargs["excursion"]
        self.assertEqual(excursion["origin"], {"lat": 25.0, "lng": 121.0})
        self.assertEqual(excursion["target"], {"lat": 25.123, "lng": 121.456})
        self.assertEqual(excursion["serial"], "phone-1")

    def test_resume_reinjects_restarts_and_waits_ten_seconds(self) -> None:
        runtime = AppRuntime()
        paused = JobRecord(
            id="recognition-1",
            kind="screen_recognition",
            state="running",
            paused=True,
            resume_coordinate={"lat": 25.0, "lng": 121.0},
            resume_serial="phone-1",
            excursion={
                "origin": {"lat": 25.0, "lng": 121.0},
                "target": {"lat": 25.123, "lng": 121.456},
                "serial": "phone-1",
            },
        )
        resumed = JobRecord(id=paused.id, kind=paused.kind, state="running")
        client = Mock()
        watchdog = Mock()
        watchdog.prepare_for_scan.return_value = True
        delay = Mock()
        delay.wait.return_value = False
        with (
            patch.object(runtime.jobs, "get", return_value=paused),
            patch.object(runtime.jobs, "update"),
            patch.object(runtime.jobs, "set_recognition_filter") as set_filter,
            patch.object(runtime.jobs, "stop_event", return_value=delay),
            patch.object(runtime.jobs, "resume_recognition", return_value=resumed) as resume,
            patch.object(runtime.device, "command") as command,
            patch.object(runtime.screen_control, "close") as close_control,
            patch("bloompilot.web.runtime.find_toolchain", return_value=SimpleNamespace(adb=Path("adb"))),
            patch("bloompilot.web.runtime.AdbClient", return_value=client),
            patch("bloompilot.web.runtime.PikminMemoryWatchdog", return_value=watchdog),
        ):
            result = runtime.resume_recognition(
                paused.id,
                {"target_names": ["Large Blue"], "participant_count": 2},
            )
        self.assertIs(result, resumed)
        command.assert_called_once_with("teleport", {"lat": 25.0, "lng": 121.0})
        client.ensure_ready.assert_called_once_with()
        watchdog.prepare_for_scan.assert_called_once_with(force_restart=True)
        delay.wait.assert_called_once_with(10.0)
        set_filter.assert_called_once_with(
            paused.id,
            {"target_names": ["Large Blue"], "participant_count": 2},
        )
        close_control.assert_called_once_with()
        resume.assert_called_once_with(paused.id)

    def test_only_forwards_adjustable_speed_parameters(self) -> None:
        runtime = AppRuntime()
        context = Mock()
        with (
            patch.object(runtime, "config", return_value={}),
            patch.object(runtime.jobs, "start", side_effect=lambda kind, runner, **_kwargs: runner(context)),
            patch("bloompilot.web.runtime.generate_dataset", return_value=Path("dataset_test")) as generate,
        ):
            result = runtime.start_dataset({"parallel_workers": 30, "progress_interval": 7, "square_size": 80})
        self.assertEqual(result, {"output": "dataset_test"})
        self.assertEqual(generate.call_args.kwargs["parallel_workers_override"], 30)
        self.assertNotIn("progress_interval_override", generate.call_args.kwargs)
        self.assertNotIn("square_override", generate.call_args.kwargs)

    def test_only_project_trained_models_are_listed_and_accepted(self) -> None:
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as temporary:
            root = Path(temporary)
            weights = root / "outputs/training/run_a/weights"
            weights.mkdir(parents=True)
            best = weights / "best.pt"
            best.write_bytes(b"model")
            dataset = root / "data/generated/dataset_a"
            dataset.mkdir(parents=True)
            (dataset / "metadata.json").write_text(
                json.dumps({"class_names": ["Large_Blue", "Large_Red"]}),
                encoding="utf-8",
            )
            (weights.parent / ".artifact.json").write_text(
                json.dumps({"dataset": "data/generated/dataset_a"}),
                encoding="utf-8",
            )
            pretrained = root / "models/pretrained/base.pt"
            pretrained.parent.mkdir(parents=True)
            pretrained.write_bytes(b"base")
            runtime = AppRuntime()
            with patch.object(runtime, "config", return_value={"_project_root": str(root)}):
                self.assertEqual([model["path"] for model in runtime.models()], [str(best.resolve())])
                self.assertEqual(runtime.models()[0]["name"], "run_a / best.pt")
                self.assertEqual(runtime.models()[0]["class_names"], ["Large_Blue", "Large_Red"])
                self.assertEqual(runtime.recognition_model({"model_path": str(best)}), best.resolve())
                with self.assertRaisesRegex(ValueError, "專案"):
                    runtime.start_recognition({"model_path": str(pretrained)})

    def test_phone_recognition_default_model_is_stored_without_copying_weight(self) -> None:
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as temporary:
            root = Path(temporary)
            weights = root / "outputs/training/run_a/weights"
            weights.mkdir(parents=True)
            best = weights / "best.pt"
            best.write_bytes(b"model")
            stored: dict[str, object] = {}

            def load_settings() -> dict[str, object]:
                return dict(stored)

            def save_settings(value: dict[str, object]) -> None:
                stored.clear()
                stored.update(value)

            runtime = AppRuntime()
            with (
                patch.object(runtime, "config", return_value={
                    "_project_root": str(root),
                    "paths": {"training_runs": "outputs/training"},
                }),
                patch("bloompilot.web.runtime.load_studio_settings", side_effect=load_settings),
                patch("bloompilot.web.runtime.save_studio_settings", side_effect=save_settings),
            ):
                self.assertFalse(runtime.models()[0]["default"])
                selected = runtime.set_default_recognition_model(str(best))
                self.assertTrue(selected["default"])
                self.assertEqual(stored["recognition_default_model"], "outputs/training/run_a/weights/best.pt")
                self.assertEqual(runtime.recognition_model({}), best.resolve())
                self.assertEqual(list(weights.glob("*.pt")), [best])

    def test_recognition_ignores_user_size_and_scale_overrides(self) -> None:
        runtime = AppRuntime()
        context = Mock()
        model = Path("managed.pt")
        calibration = sample_device_scale()
        controller = Mock()
        with (
            patch.object(runtime, "recognition_model", return_value=model),
            patch.object(runtime.device_scales, "get", return_value=calibration),
            patch.object(runtime.jobs, "start", side_effect=lambda kind, runner, **_kwargs: runner(context)),
            patch.object(runtime.device, "status", return_value={"current": {"lat": 25, "lng": 121}}),
            patch.object(runtime.device, "command"),
            patch.object(runtime.device, "controller", return_value=controller),
            patch("bloompilot.web.runtime.find_toolchain", return_value=SimpleNamespace(adb=Path("adb"))),
            patch("bloompilot.web.runtime.run_recognition_scan", return_value=(Path("out"), {})) as scan,
        ):
            runtime.start_recognition({
                "serial": "test",
                "image_size": 32,
                "viewport_width_m": 555,
                "estimate_coordinates": True,
                "target_names": ["Large_Blue"],
                "participant_count": 0,
            })
        self.assertEqual(scan.call_args.kwargs["image_size"], 1024)
        self.assertTrue(scan.call_args.kwargs["precise_coordinates"])
        self.assertIs(scan.call_args.kwargs["coordinate_controller"], controller)
        self.assertNotIn("coordinate_scale", scan.call_args.kwargs)
        self.assertEqual(scan.call_args.kwargs["target_names"], ["Large_Blue"])
        self.assertEqual(scan.call_args.kwargs["participant_count"], 0)

    def test_search_scale_check_reuses_registered_device_without_measuring(self) -> None:
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as directory:
            store = DeviceScaleStore(Path(directory) / "device_scales.json")
            expected = sample_device_scale("phone-a")
            store.save(expected)
            runtime = AppRuntime(device_scale_store=store)
            ctx = SimpleNamespace(message=Mock(), stop_event=threading.Event())
            with patch("bloompilot.web.runtime.AdbClient") as adb_client:
                actual = runtime.ensure_device_scale_for_search(
                    "phone-a",
                    ctx=ctx,
                    toolchain=SimpleNamespace(adb=Path("adb.exe")),
                )
            self.assertEqual(actual, expected)
            adb_client.assert_not_called()
            self.assertIn("已有比例尺註冊資料", ctx.message.call_args.args[0])

    def test_search_scale_check_measures_and_registers_new_device(self) -> None:
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as directory:
            store = DeviceScaleStore(Path(directory) / "device_scales.json")
            runtime = AppRuntime(device_scale_store=store)
            ctx = SimpleNamespace(message=Mock(), stop_event=threading.Event())
            client = Mock()
            client.screen_size.return_value = SimpleNamespace(width=1080, height=2400)
            client.capture_png.return_value = b"reference"
            watchdog = Mock()
            watchdog.prepare_for_scan.return_value = True
            measured = sample_device_scale("new-phone")

            def save_measurement(**_kwargs):
                store.save(measured)
                return {"ok": True, "calibration": measured.to_dict(), "preview": ""}

            with (
                patch("bloompilot.web.runtime._recognition_frame_source", return_value=nullcontext(None)),
                patch("bloompilot.web.runtime.AdbClient", return_value=client),
                patch("bloompilot.web.runtime.PikminMemoryWatchdog", return_value=watchdog),
                patch("bloompilot.web.runtime.pikmin_map_locator_visible", return_value=True),
                patch.object(runtime, "calibrate_device_scale", side_effect=save_measurement) as measure,
            ):
                actual = runtime.ensure_device_scale_for_search(
                    "new-phone",
                    ctx=ctx,
                    toolchain=SimpleNamespace(adb=Path("adb.exe")),
                )

            self.assertEqual(actual, measured)
            watchdog.prepare_for_scan.assert_called_once_with()
            self.assertEqual(client.run.call_args.args[:4], ("shell", "input", "swipe", "540"))
            self.assertEqual(
                client.run.call_args.args[4:8],
                ("1032", "540", "1320", "350"),
            )
            self.assertTrue(measure.call_args.kwargs["allow_active_job"])
            self.assertEqual(store.get("new-phone"), measured)
            self.assertIn("已自動量測並註冊", ctx.message.call_args.args[0])

    def test_device_status_marks_only_uncalibrated_serial_as_requiring_setup(self) -> None:
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as directory:
            store = DeviceScaleStore(Path(directory) / "device_scales.json")
            runtime = AppRuntime(device_scale_store=store)
            with patch.object(
                runtime.device,
                "status",
                return_value={"connected": True, "serial": "phone-a"},
            ):
                self.assertTrue(runtime.device_status()["scale_calibration_required"])
                store.save(sample_device_scale("phone-a"))
                self.assertFalse(runtime.device_status()["scale_calibration_required"])

    def test_recognition_uses_scale_saved_for_selected_phone(self) -> None:
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as directory:
            store = DeviceScaleStore(Path(directory) / "device_scales.json")
            calibration = new_device_scale_calibration(
                serial="phone-a",
                screen_width=1080,
                screen_height=2400,
                origin_lat=25,
                origin_lng=121,
                east_probe_m=42,
                north_probe_m=42,
                east_coordinate_delta=0.0004,
                north_coordinate_delta=0.00038,
                east_pixels=72,
                north_pixels=76,
            )
            store.save(calibration)
            runtime = AppRuntime(device_scale_store=store)
            context = Mock()
            controller = Mock()
            with (
                patch.object(runtime, "recognition_model", return_value=Path("managed.pt")),
                patch.object(runtime.jobs, "start", side_effect=lambda kind, runner, **_kwargs: runner(context)),
                patch.object(runtime.device, "status", return_value={"current": {"lat": 25, "lng": 121}}),
                patch.object(runtime.device, "command"),
                patch.object(runtime.device, "controller", return_value=controller),
                patch("bloompilot.web.runtime.find_toolchain", return_value=SimpleNamespace(adb=Path("adb"))),
                patch("bloompilot.web.runtime.run_recognition_scan", return_value=(Path("out"), {})) as scan,
            ):
                runtime.start_recognition({
                    "serial": "phone-a",
                    "estimate_coordinates": True,
                })

            self.assertTrue(scan.call_args.kwargs["precise_coordinates"])
            self.assertIs(scan.call_args.kwargs["coordinate_controller"], controller)
            self.assertNotIn("coordinate_scale", scan.call_args.kwargs)

    def test_geo_sequence_randomizes_execution_copy_without_changing_payload(self) -> None:
        runtime = AppRuntime()
        context = Mock()
        payload = {
            "serial": "phone-a",
            "model_path": "managed.pt",
            "randomize_waypoints": True,
            "waypoints": [
                {"id": "first", "name": "First", "lat": 25.0, "lng": 121.0},
                {"id": "second", "name": "Second", "lat": 25.1, "lng": 121.1},
            ],
        }
        shuffler = Mock()
        shuffler.shuffle.side_effect = lambda values: values.reverse()
        with (
            patch.object(runtime, "recognition_model", return_value=Path("managed.pt")),
            patch.object(runtime.device_scales, "get", return_value=sample_device_scale("phone-a")),
            patch.object(runtime.jobs, "start", side_effect=lambda _kind, runner, **_kwargs: runner(context)),
            patch.object(runtime.device, "controller", return_value=Mock()),
            patch("bloompilot.web.runtime.random.SystemRandom", return_value=shuffler),
            patch("bloompilot.web.runtime.find_toolchain", return_value=SimpleNamespace(adb=Path("adb"))),
            patch(
                "bloompilot.web.runtime.run_geographic_recognition_sequence",
                return_value=(Path("out"), {}),
            ) as run_sequence,
        ):
            runtime.start_geo_sequence_recognition(payload)

        self.assertEqual(
            [item.id for item in run_sequence.call_args.kwargs["waypoints"]],
            ["second", "first"],
        )
        self.assertEqual([item["id"] for item in payload["waypoints"]], ["first", "second"])

    def test_fixed_settings_override_legacy_values_and_reject_invalid_workers_before_saving(self) -> None:
        runtime = AppRuntime()
        geo_sequences = [{"id": "large-route", "waypoints": [{"id": "one"}]}]
        settings = {
            "viewport_coordinate_scale": {"enabled": False, "viewport_width_m": 500},
            "recognition_filter_defaults": {
                "all_targets": False,
                "target_names": ["Large_Blue", "Large_Red"],
                "participant_count": 0,
            },
            "geo_sequences": geo_sequences,
            "config_overrides": {
                "generation": {"parallel_workers": 48, "square_size_pixels": 80, "progress_interval": 9},
                "phone_automation": {"recognition": {"image_size": 32}},
            },
        }
        with (
            patch("bloompilot.web.runtime.save_studio_settings") as save,
            patch.object(runtime, "settings_payload", return_value={}),
        ):
            runtime.save_settings(settings)
            stored = save.call_args.args[0]
            self.assertIs(stored["geo_sequences"], geo_sequences)
            self.assertEqual(stored["viewport_coordinate_scale"], {"enabled": False, "viewport_width_m": 627.9})
            self.assertEqual(
                stored["recognition_filter_defaults"],
                {"all_targets": False, "target_names": ["Large_Blue", "Large_Red"], "participant_count": 0},
            )
            self.assertEqual(stored["config_overrides"]["generation"], {"parallel_workers": 48, "square_size_pixels": 256})
            self.assertEqual(stored["config_overrides"]["phone_automation"]["recognition"]["image_size"], 1024)
            self.assertEqual(settings["viewport_coordinate_scale"]["viewport_width_m"], 500)
            self.assertEqual(settings["config_overrides"]["generation"]["square_size_pixels"], 80)
            settings["config_overrides"]["generation"]["parallel_workers"] = 1.5
            with self.assertRaisesRegex(ValueError, "parallel_workers"):
                runtime.save_settings(settings)
            self.assertEqual(save.call_count, 1)



class GeoSequencePayloadTests(unittest.TestCase):
    def test_parses_waypoints_with_per_location_search_settings(self) -> None:
        waypoints = geo_waypoints_from_payload({
            "waypoints": [{
                "id": "taipei-1",
                "name": "台北車站",
                "lat": 25.0478,
                "lng": 121.517,
                "wait_seconds": 10,
                "horizontal_radius": 2,
                "vertical_radius": 1,
                "spacing_m": 80,
            }]
        })
        self.assertEqual(waypoints[0].name, "台北車站")
        self.assertEqual(waypoints[0].wait_seconds, 10)
        self.assertEqual(waypoints[0].point_count, 15)

    def test_parses_asymmetric_waypoint_ranges(self) -> None:
        waypoints = geo_waypoints_from_payload({
            "waypoints": [{
                "lat": 25.0478,
                "lng": 121.517,
                "left_radius": 0,
                "right_radius": 2,
                "up_radius": 1,
                "down_radius": 3,
            }]
        })
        self.assertEqual(waypoints[0].resolved_radii, (0, 2, 1, 3))
        self.assertEqual(waypoints[0].point_count, 15)

    def test_rejects_invalid_or_duplicate_waypoints(self) -> None:
        with self.assertRaisesRegex(ValueError, "經緯度"):
            geo_waypoints_from_payload({"waypoints": [{"lat": 91, "lng": 121}]})
        with self.assertRaisesRegex(ValueError, "重複"):
            geo_waypoints_from_payload({"waypoints": [
                {"id": "same", "lat": 25, "lng": 121},
                {"id": "same", "lat": 26, "lng": 122},
            ]})


class RouteLibraryTests(unittest.TestCase):
    @staticmethod
    def route_document(name: str = "測試路線", waypoint_count: int = 1) -> dict:
        return {
            "schema": "bloompilot.geo-sequence.v1",
            "metadata": {"city_count": waypoint_count, "total_search_points": waypoint_count},
            "sequence": {
                "version": 1,
                "id": "test-route",
                "name": name,
                "updated_at": "2026-08-28T00:00:00+00:00",
                "waypoints": [
                    {
                        "id": f"point-{index}",
                        "name": f"位置 {index}",
                        "lat": 25 + index / 1000,
                        "lng": 121,
                        "wait_seconds": 1,
                        "left_radius": 0,
                        "right_radius": 0,
                        "up_radius": 0,
                        "down_radius": 0,
                        "spacing_m": 100,
                    }
                    for index in range(waypoint_count)
                ],
            },
        }

    def test_lists_only_route_documents_from_bounded_headers(self) -> None:
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as temporary:
            root = Path(temporary) / "routes"
            nested = root / "bundled"
            nested.mkdir(parents=True)
            (nested / "route.json").write_text(
                json.dumps(self.route_document(waypoint_count=2), ensure_ascii=False),
                encoding="utf-8",
            )
            (nested / "index.json").write_text(json.dumps({"schema": "route-index"}), encoding="utf-8")
            routes = RouteLibrary(root).list()
            self.assertEqual(len(routes), 1)
            self.assertEqual(routes[0]["path"], "bundled/route.json")
            self.assertEqual(routes[0]["name"], "測試路線")
            self.assertEqual(routes[0]["waypoint_count"], 2)
            self.assertFalse(routes[0]["imported"])

    def test_private_monthly_routes_are_hidden_from_recognition_list(self) -> None:
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as temporary:
            root = Path(temporary) / "routes"
            private = root / "_monthly"
            private.mkdir(parents=True)
            (private / "background.json").write_text(
                json.dumps(self.route_document("背景內部路線"), ensure_ascii=False),
                encoding="utf-8",
            )
            self.assertEqual(RouteLibrary(root).list(), [])
            self.assertEqual(
                RouteLibrary(root).route_path("_monthly/background.json"),
                private / "background.json",
            )

    def test_import_is_validated_stored_and_overwritten_by_source_name(self) -> None:
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as temporary:
            root = Path(temporary) / "routes"
            library = RouteLibrary(root)
            first = library.import_document("我的路線.json", self.route_document("第一版"))
            second = library.import_document("我的路線.json", self.route_document("第二版", 2))
            self.assertEqual(first["path"], "imported/我的路線.json")
            self.assertEqual(second["path"], first["path"])
            self.assertEqual(second["name"], "第二版")
            self.assertEqual(second["waypoint_count"], 2)
            self.assertEqual(len(library.list()), 1)
            saved = json.loads(library.route_path(second["path"]).read_text(encoding="utf-8"))
            self.assertEqual(saved["metadata"]["waypoint_count"], 2)
            self.assertEqual(saved["sequence"]["name"], "第二版")
            with self.assertRaisesRegex(ValueError, "routes"):
                library.route_path("../outside.json")

    def test_rejects_invalid_import_before_creating_file(self) -> None:
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as temporary:
            root = Path(temporary) / "routes"
            library = RouteLibrary(root)
            with self.assertRaisesRegex(ValueError, "至少需要"):
                library.import_document("empty.json", {"sequence": {"waypoints": []}})
            self.assertFalse(library.imported_directory.exists())

    def test_deletes_only_custom_routes_from_imported_directory(self) -> None:
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as temporary:
            root = Path(temporary) / "routes"
            bundled = root / "bundled"
            bundled.mkdir(parents=True)
            (bundled / "route.json").write_text(
                json.dumps(self.route_document("內建路線"), ensure_ascii=False),
                encoding="utf-8",
            )
            library = RouteLibrary(root)
            custom = library.import_document("custom.json", self.route_document("自訂路線"))
            deleted = library.delete(custom["path"])
            self.assertEqual(deleted["name"], "自訂路線")
            self.assertFalse((root / custom["path"]).exists())
            with self.assertRaisesRegex(ValueError, "內建"):
                library.delete("bundled/route.json")


class WebApiTests(unittest.TestCase):
    def test_discord_bot_token_only_settings_resolve_and_persist_destination(self) -> None:
        runtime = AppRuntime()
        token = "test.bot.token-for-offline-validation"
        channel = "333333333333333333"
        try:
            with (
                patch("bloompilot.web.runtime.resolve_bot_channel", return_value=channel) as resolve,
                patch("bloompilot.web.runtime.save_studio_settings") as save,
                patch.object(runtime, "settings_payload", return_value={}),
            ):
                runtime.save_settings({"discord_webhook": {"enabled": True, "mode": "bot", "bot_token": token}})
                resolve.assert_called_once_with(token, "")
                self.assertEqual(channel, save.call_args.args[0]["discord_webhook"]["channel_id"])
                self.assertEqual(token, save.call_args.args[0]["discord_webhook"]["bot_token"])
        finally:
            runtime.close()

    def test_discord_bot_discovery_and_test_endpoints(self) -> None:
        runtime = AppRuntime()
        token = "test.bot.token-for-offline-validation"
        channel = "333333333333333333"
        discovered = {"bot_name": "Bloom Bot", "channels": [{"id": channel, "name": "hits", "guild_name": "Test", "category_name": ""}], "warnings": []}
        with (
            patch.object(runtime, "discord_bot_channels", return_value=discovered) as discover,
            patch.object(runtime, "test_discord_bot", return_value={"ok": True, "message": "sent", "channel_id": channel}) as send,
            TestClient(create_app(runtime)) as client,
        ):
            response = client.post("/api/notifications/discord/channels", json={"bot_token": token})
            self.assertEqual(200, response.status_code)
            self.assertEqual(discovered, response.json())
            response = client.post("/api/notifications/discord/test", json={"mode": "bot", "bot_token": token, "channel_id": channel})
            self.assertEqual(200, response.status_code)
            self.assertEqual(channel, response.json()["channel_id"])
        discover.assert_called_once_with(token)
        send.assert_called_once_with(token, channel)

    def test_discord_bot_errors_are_returned_to_the_ui(self) -> None:
        runtime = AppRuntime()
        with TestClient(create_app(runtime)) as client:
            with patch.object(runtime, "discord_bot_channels", side_effect=ValueError("請輸入 Bot Token")):
                response = client.post("/api/notifications/discord/channels", json={"bot_token": "invalid"})
                self.assertEqual(422, response.status_code)
            with patch.object(runtime, "test_discord_bot", side_effect=RuntimeError("Discord 權限不足")):
                response = client.post("/api/notifications/discord/test", json={"mode": "bot", "bot_token": "invalid"})
                self.assertEqual(502, response.status_code)
                self.assertIn("權限不足", response.json()["detail"])

    def test_screen_control_api_opens_and_closes_desktop_mirroring(self) -> None:
        runtime = AppRuntime()
        opened = {"ok": True, "active": True, "serial": "phone-1", "pid": 321}
        closed = {"ok": True, "active": False, "serial": None, "pid": None}
        with (
            patch.object(runtime, "open_screen_control", return_value=opened) as open_control,
            patch.object(runtime, "close_screen_control", return_value=closed) as close_control,
            TestClient(create_app(runtime)) as client,
        ):
            self.assertEqual(client.post("/api/device/screen-control").json(), opened)
            self.assertEqual(client.delete("/api/device/screen-control").json(), closed)

        open_control.assert_called_once_with()
        close_control.assert_called_once_with()

    def test_discord_webhook_test_endpoint_delegates_delivery(self) -> None:
        runtime = AppRuntime()
        webhook_url = (
            "https://discord.com/api/webhooks/123456789012345678/"
            "abcdefghijklmnopqrstuvwxyz_ABCDEFGHIJKLMNOPQRSTUVWXYZ-0123456789"
        )
        with (
            patch.object(
                runtime,
                "test_discord_webhook",
                return_value={"ok": True, "message": "Discord 測試播報已送出。"},
            ) as send,
            TestClient(create_app(runtime)) as client,
        ):
            response = client.post(
                "/api/notifications/discord/test",
                json={"webhook_url": webhook_url},
            )

        self.assertEqual(200, response.status_code)
        self.assertTrue(response.json()["ok"])
        send.assert_called_once_with(webhook_url)

    def test_recognition_api_pauses_then_visits_and_resumes(self) -> None:
        runtime = AppRuntime()
        paused = JobRecord(
            id="recognition-1",
            kind="screen_recognition",
            state="running",
            paused=True,
            excursion={
                "origin": {"lat": 25.0, "lng": 121.0},
                "target": {"lat": 25.123, "lng": 121.456},
            },
        )
        resumed = JobRecord(id=paused.id, kind=paused.kind, state="running")
        with (
            patch.object(runtime, "pause_recognition", return_value=paused) as pause,
            patch.object(runtime, "teleport_while_paused", return_value=paused) as visit,
            patch.object(runtime, "resume_recognition", return_value=resumed) as resume,
            TestClient(create_app(runtime)) as client,
        ):
            paused_response = client.post("/api/jobs/recognition-1/pause")
            self.assertEqual(paused_response.status_code, 200)
            pause.assert_called_once_with("recognition-1")
            response = client.post(
                "/api/jobs/recognition-1/visit-hit",
                json={"lat": 25.123, "lng": 121.456, "label": "Large Blue"},
            )
            self.assertEqual(response.status_code, 200)
            self.assertTrue(response.json()["job"]["paused"])
            visit.assert_called_once_with(
                "recognition-1",
                lat=25.123,
                lng=121.456,
                label="Large Blue",
            )
            returned = client.post(
                "/api/jobs/recognition-1/resume",
                json={"target_names": ["Large Blue"], "participant_count": 2},
            )
            self.assertEqual(returned.status_code, 200)
            self.assertFalse(returned.json()["job"]["paused"])
            resume.assert_called_once_with(
                "recognition-1",
                {"target_names": ["Large Blue"], "participant_count": 2},
            )

    def test_project_route_api_imports_lists_and_serves_content(self) -> None:
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as temporary:
            runtime = AppRuntime()
            runtime.route_library = RouteLibrary(Path(temporary) / "routes")
            payload = RouteLibraryTests.route_document("API 路線", 2)
            with TestClient(create_app(runtime)) as client:
                imported = client.post("/api/routes/import", json={"source_name": "api.json", "data": payload})
                self.assertEqual(imported.status_code, 200)
                route = imported.json()["route"]
                self.assertEqual(route["path"], "imported/api.json")
                self.assertEqual(client.get("/api/routes").json(), [route])
                content = client.get("/api/routes/content", params={"path": route["path"]})
                self.assertEqual(content.status_code, 200)
                self.assertEqual(content.json()["sequence"]["name"], "API 路線")
                self.assertEqual(client.get("/api/routes/content", params={"path": "../outside.json"}).status_code, 400)
                deleted = client.delete("/api/routes", params={"path": route["path"]})
                self.assertEqual(deleted.status_code, 200)
                self.assertEqual(deleted.json()["route"]["name"], "API 路線")
                self.assertEqual(client.get("/api/routes").json(), [])

    def test_cancel_training_stop_api_handles_pending_claimed_and_finished_jobs(self) -> None:
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as temporary:
            runtime = AppRuntime()
            control = TrainingControl(Path(temporary))
            record = JobRecord(id="run-1", kind="training", state="running")
            runtime.jobs._jobs[record.id] = record
            runtime.jobs._stops[record.id] = threading.Event()
            runtime.jobs._active_id = record.id
            with (
                patch("bloompilot.web.runtime.TRAINING_CONTROL_DIR", control.directory),
                TestClient(create_app(runtime)) as client,
            ):
                self.assertEqual(client.post("/api/jobs/run-1/stop").json()["job"]["state"], "stopping")
                runtime.jobs.sync_training_stop(record.id, control)
                cancelled = client.post("/api/jobs/run-1/cancel-stop")
                self.assertEqual(cancelled.status_code, 200)
                self.assertEqual(cancelled.json()["job"]["state"], "running")
                self.assertIsNone(control.requested_action())
                client.post("/api/jobs/run-1/stop")
                runtime.jobs.sync_training_stop(record.id, control)
                (control.directory / "stop.request").replace(control.directory / "stop.accepted")
                late = client.post("/api/jobs/run-1/cancel-stop")
                self.assertEqual(late.status_code, 409)
                self.assertIn("已生效", late.json()["detail"])
                runtime.jobs.update(record.id, state="cancelled")
                self.assertEqual(client.post("/api/jobs/run-1/cancel-stop").status_code, 409)
                self.assertEqual(client.post("/api/jobs/unknown/cancel-stop").status_code, 404)

    def test_health_and_disconnected_device_status(self) -> None:
        with TestClient(create_app()) as client:
            health = client.get("/api/health")
            self.assertEqual(health.status_code, 200)
            self.assertTrue(health.json()["ok"])
            status = client.get("/api/device/status")
            self.assertEqual(status.status_code, 200)
            self.assertFalse(status.json()["connected"])

    def test_unknown_job_kind_is_rejected(self) -> None:
        with TestClient(create_app()) as client:
            response = client.post("/api/jobs/not-real", json={})
            self.assertEqual(response.status_code, 404)

    def test_removed_single_center_recognition_is_rejected(self) -> None:
        with TestClient(create_app()) as client:
            response = client.post("/api/jobs/geo_recognition", json={})
            self.assertEqual(response.status_code, 404)

    def test_invalid_geo_sequence_is_rejected_before_starting_job(self) -> None:
        with TestClient(create_app()) as client:
            response = client.post("/api/jobs/geo_sequence_recognition", json={"waypoints": []})
            self.assertEqual(response.status_code, 422)
            self.assertIn("至少需要一個節點", response.json()["detail"])

    def test_viewport_calibration_steps_recenters_and_returns_preview(self) -> None:
        class FakeAdbClient:
            def __init__(self) -> None:
                self.actions: list[tuple] = []

            def ensure_ready(self) -> None:
                self.actions.append(("ready",))

            def screen_size(self) -> tuple[int, int]:
                return (1080, 2400)

            def swipe(self, direction: str, size: object, duration_ms: int) -> None:
                self.actions.append(("swipe", direction, size, duration_ms))

            def recenter(self, size: object) -> None:
                self.actions.append(("recenter", size))

            def capture_png(self) -> bytes:
                ok, encoded = cv2.imencode(".png", np.zeros((24, 12, 3), dtype=np.uint8))
                self.assert_encoding(ok)
                self.actions.append(("capture",))
                return encoded.tobytes()

            @staticmethod
            def assert_encoding(ok: bool) -> None:
                if not ok:
                    raise AssertionError("test image encoding failed")

        fake_adb = FakeAdbClient()
        with (
            patch("bloompilot.web.runtime.find_toolchain", return_value=SimpleNamespace(adb=Path("adb.exe"))),
            patch("bloompilot.web.runtime.AdbClient", return_value=fake_adb),
            TestClient(create_app()) as client,
        ):
            stepped = client.post("/api/device/viewport-calibration", json={
                "serial": "phone-1",
                "action": "step",
                "direction": "west",
                "settle_seconds": 0,
            })
            centered = client.post("/api/device/viewport-calibration", json={
                "serial": "phone-1",
                "action": "recenter",
                "settle_seconds": 0,
            })
            captured = client.post("/api/device/viewport-calibration", json={
                "serial": "phone-1",
                "action": "capture",
                "settle_seconds": 0,
            })

        self.assertEqual(stepped.status_code, 200)
        self.assertTrue(stepped.json()["preview"].startswith("data:image/jpeg;base64,"))
        self.assertEqual(captured.status_code, 200)
        self.assertEqual(captured.json()["screen_width"], 12)
        self.assertEqual(captured.json()["screen_height"], 24)
        self.assertEqual(centered.status_code, 200)
        self.assertIn(("swipe", "west", (1080, 2400), 800), fake_adb.actions)
        self.assertIn(("recenter", (1080, 2400)), fake_adb.actions)

    def test_viewport_calibration_step_requires_direction(self) -> None:
        with TestClient(create_app()) as client:
            response = client.post("/api/device/viewport-calibration", json={
                "serial": "phone-1",
                "action": "step",
                "settle_seconds": 0,
            })
        self.assertEqual(response.status_code, 400)
        self.assertIn("必須指定", response.json()["detail"])

    def test_device_scale_calibration_uses_short_vertical_round_trip_and_persists(self) -> None:
        class FakeDevice:
            connected = True
            serial = "phone-1"

            def __init__(self) -> None:
                self.commands: list[tuple[str, dict[str, float]]] = []

            @staticmethod
            def status() -> dict[str, object]:
                return {"connected": True, "current": {"lat": 25.0, "lng": 121.0}}

            def command(self, command: str, payload: dict[str, float]) -> dict[str, object]:
                self.commands.append((command, payload))
                return {"ok": True}

            @staticmethod
            def disconnect() -> dict[str, object]:
                return {"connected": False}

            @staticmethod
            def close() -> None:
                return None

        class FakeAdbClient:
            @staticmethod
            def ensure_ready() -> None:
                return None

            @staticmethod
            def capture_png() -> bytes:
                image = np.zeros((240, 120, 3), dtype=np.uint8)
                ok, encoded = cv2.imencode(".png", image)
                if not ok:
                    raise AssertionError("test image encoding failed")
                return encoded.tobytes()

        movements = [
            ValueError("位置更新尚未顯示"),
            MarkerDisplacement(0, -80, 40, 120, 40, 40, 9),
            MarkerDisplacement(0, -80, 40, 120, 40, 40, 9),
            ValueError("原始位置尚未恢復"),
            MarkerDisplacement(0, 79, 40, 40, 40, 119, 8),
            MarkerDisplacement(0, 79, 40, 40, 40, 119, 8),
        ]
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as directory:
            runtime = AppRuntime(
                device_scale_store=DeviceScaleStore(Path(directory) / "device_scales.json")
            )
            fake_device = FakeDevice()
            runtime.device = fake_device  # type: ignore[assignment]
            with (
                patch("bloompilot.web.runtime.find_toolchain", return_value=SimpleNamespace(adb=Path("adb.exe"))),
                patch("bloompilot.web.runtime.AdbClient", return_value=FakeAdbClient()),
                patch("bloompilot.web.runtime.pikmin_map_locator_visible", return_value=True),
                patch(
                    "bloompilot.web.runtime.measure_player_marker_displacement",
                    side_effect=movements,
                ),
                patch("bloompilot.web.runtime.SCALE_PROBE_POLL_SECONDS", 0),
                TestClient(create_app(runtime)) as client,
            ):
                calibrated = client.post(
                    "/api/device/scale-calibration",
                    json={"serial": "phone-1", "settle_seconds": 0.5},
                )
                loaded = client.get(
                    "/api/device/scale-calibration",
                    params={"serial": "phone-1"},
                )

            self.assertEqual(calibrated.status_code, 200)
            self.assertTrue(calibrated.json()["preview"].startswith("data:image/jpeg;base64,"))
            self.assertEqual(loaded.status_code, 200)
            calibration = loaded.json()["calibration"]
            self.assertEqual(calibration["serial"], "phone-1")
            self.assertFalse(loaded.json()["required"])
            self.assertLessEqual(calibration["east_probe_m"], 65)
            self.assertAlmostEqual(calibration["east_pixels"], 80)
            self.assertAlmostEqual(calibration["north_pixels"], 80)
            self.assertAlmostEqual(
                calibration["meters_per_pixel_x"],
                calibration["meters_per_pixel_y"],
            )
            self.assertEqual(len(fake_device.commands), 2)
            self.assertEqual(fake_device.commands[1][1], {"lat": 25.0, "lng": 121.0})

    def test_device_scale_refuses_hidden_locator_without_overwriting_previous_value(self) -> None:
        class FakeDevice:
            connected = True
            serial = "phone-1"

            def __init__(self) -> None:
                self.commands: list[tuple[str, dict[str, float]]] = []

            @staticmethod
            def status() -> dict[str, object]:
                return {"connected": True, "current": {"lat": 25.0, "lng": 121.0}}

            def command(self, command: str, payload: dict[str, float]) -> dict[str, object]:
                self.commands.append((command, payload))
                return {"ok": True}

        image = np.zeros((240, 120, 3), dtype=np.uint8)
        ok, encoded = cv2.imencode(".png", image)
        self.assertTrue(ok)
        fake_adb = Mock()
        fake_adb.capture_png.return_value = encoded.tobytes()
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as directory:
            store = DeviceScaleStore(Path(directory) / "device_scales.json")
            previous = new_device_scale_calibration(
                serial="phone-1",
                screen_width=1080,
                screen_height=2400,
                origin_lat=25,
                origin_lng=121,
                east_probe_m=40,
                north_probe_m=40,
                east_coordinate_delta=0.0004,
                north_coordinate_delta=0.00036,
                east_pixels=70,
                north_pixels=75,
            )
            store.save(previous)
            runtime = AppRuntime(device_scale_store=store)
            fake_device = FakeDevice()
            runtime.device = fake_device  # type: ignore[assignment]
            with (
                patch("bloompilot.web.runtime.find_toolchain", return_value=SimpleNamespace(adb=Path("adb.exe"))),
                patch("bloompilot.web.runtime.AdbClient", return_value=fake_adb),
                patch("bloompilot.web.runtime.pikmin_map_locator_visible", return_value=False),
            ):
                with self.assertRaisesRegex(RuntimeError, "右下角定位按鈕"):
                    runtime.calibrate_device_scale(serial="phone-1", settle_seconds=0)

            self.assertEqual(store.get("phone-1"), previous)
            self.assertEqual(fake_device.commands, [])

    def test_geo_sequences_are_saved_to_disk_and_reloaded(self) -> None:
        sequence = {
            "version": 1,
            "id": "taipei-route",
            "name": "台北巡迴",
            "updated_at": "2026-08-23T00:00:00Z",
            "waypoints": [{
                "id": "station",
                "name": "台北車站",
                "lat": 25.0478,
                "lng": 121.517,
                "wait_seconds": 10,
                "horizontal_radius": 2,
                "vertical_radius": 1,
                "spacing_m": 80,
            }],
        }
        with tempfile.TemporaryDirectory() as directory:
            settings_path = Path(directory) / "settings.json"
            with (
                patch(
                    "bloompilot.web.runtime.load_studio_settings",
                    side_effect=lambda: load_studio_settings(settings_path),
                ),
                patch(
                    "bloompilot.web.runtime.save_studio_settings",
                    side_effect=lambda settings: save_studio_settings(settings, settings_path),
                ),
            ):
                with TestClient(create_app()) as client:
                    saved = client.put("/api/settings", json={"geo_sequences": [sequence]})
                    self.assertEqual(saved.status_code, 200)

                self.assertTrue(settings_path.is_file())
                with TestClient(create_app()) as reloaded_client:
                    reloaded = reloaded_client.get("/api/settings")
                    self.assertEqual(reloaded.status_code, 200)
                    self.assertEqual(reloaded.json()["settings"]["geo_sequences"], [sequence])


if __name__ == "__main__":
    unittest.main()
