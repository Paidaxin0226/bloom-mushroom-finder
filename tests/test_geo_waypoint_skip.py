from __future__ import annotations

import json
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from fastapi.testclient import TestClient

from bloompilot.geo_recognition import GeoRecognitionWaypoint, run_geographic_recognition_sequence
from bloompilot.geo_scan import wait_for_arrival
from bloompilot.web.events import EventHub
from bloompilot.web.runtime import AppRuntime, JobContext, JobManager, JobPauseGate, JobRecord
from bloompilot.web.server import create_app
from test_geo_recognition_sequence import FakeController, FakeMemoryWatchdog, TEST_PNG


def sequence_context() -> JobContext:
    manager = JobManager(EventHub())
    record = JobRecord(id="sequence", kind="geo_sequence_recognition", state="running")
    manager._jobs[record.id] = record
    manager._active_id = record.id
    manager._stops[record.id] = threading.Event()
    manager._pause_gates[record.id] = JobPauseGate()
    return JobContext(manager, record.id, manager._stops[record.id], manager._pause_gates[record.id])


class GeoWaypointSkipTests(unittest.TestCase):
    def run_sequence(self, waypoints, *, status=None, hits=None, detection_update=None, arrival=None, resolver=None):
        context = sequence_context()
        controller = FakeController()
        adb = Mock()
        adb.capture_png.return_value = TEST_PNG
        model = Mock()
        model.names = {0: "target"}
        model.predict.return_value = [object()]
        updates = []
        messages = []

        def report(message):
            messages.append(message)
            if status:
                status(context, message)

        with tempfile.TemporaryDirectory(dir=Path.cwd()) as temporary:
            root = Path(temporary)
            model_path = root / "model.pt"
            model_path.write_bytes(b"model")
            with (
                patch("torch.cuda.is_available", return_value=False),
                patch("ultralytics.YOLO", return_value=model),
                patch("bloompilot.geo_recognition.AdbClient", return_value=adb),
                patch("bloompilot.geo_recognition.PikminMemoryWatchdog", return_value=FakeMemoryWatchdog()),
                patch("bloompilot.phone_automation.SCREEN_STABLE_SECONDS", 0),
                patch("bloompilot.geo_recognition.detections_from_result", side_effect=hits or (lambda _: [])),
                patch("bloompilot.badge_number.enrich_detections_with_badge_numbers"),
                patch("bloompilot.mushroom_coordinate.MushroomCoordinateResolver", side_effect=lambda *args, **kwargs: resolver(context) if resolver else Mock()),
                patch("bloompilot.geo_recognition.wait_for_arrival", side_effect=lambda *args, **kwargs: arrival(context, *args, **kwargs) if arrival else wait_for_arrival(*args, **kwargs)),
            ):
                output, summary = run_geographic_recognition_sequence(
                    controller=controller,
                    adb=root / "adb.exe",
                    serial="phone",
                    model_path=model_path,
                    waypoints=waypoints,
                    waypoint_started=context.begin_waypoint,
                    waypoint_finished=context.finish_waypoint,
                    stop_event=context.stop_event,
                    movement_mode="navigate" if arrival else "teleport",
                    settle_seconds=0,
                    confidence=0.45,
                    image_size=32,
                    bookmark_detection=False,
                    precise_coordinates=resolver is not None,
                    output_root=root / "output",
                    status=report,
                    progress=lambda done, total, detail: updates.append((done, total, detail)),
                    detection_update=lambda location, detections: detection_update(context, location, detections) if detection_update else None,
                )
            saved = json.loads((output / "summary.json").read_text(encoding="utf-8"))
            records = [json.loads(line) for line in (output / "detections.jsonl").read_text(encoding="utf-8").splitlines()]
        self.assertIsNone(context.manager.get(context.job_id).current_waypoint)
        self.assertNotIn(context.job_id, context.manager._waypoint_stops)
        return summary, saved, records, updates, controller, messages

    def test_skip_wait_continues_next_node_and_finishes_progress(self):
        def status(context, message):
            if message.startswith("已到達 第一站"):
                context.manager.skip_geo_waypoint(context.job_id, "first")

        summary, saved, _, updates, controller, _ = self.run_sequence([
            GeoRecognitionWaypoint("first", "第一站", 25.1, 121.1, 60, 1, 0),
            GeoRecognitionWaypoint("second", "第二站", 25.2, 121.2, 0, 0, 0),
        ], status=status)
        self.assertTrue(summary.completed)
        self.assertEqual((summary.completed_waypoints, summary.skipped_waypoints, summary.visited), (1, 1, 1))
        self.assertEqual(summary.skipped_points, 3)
        self.assertEqual(updates[-1][:2], (4, 4))
        self.assertEqual(saved["skipped_waypoints"][0]["waypoint_id"], "first")
        self.assertEqual([payload["lat"] for command, payload in controller.commands if command == "teleport"], [25.1, 25.2, 24.0])

    def test_skip_during_recognition_preserves_hit_and_continues(self):
        def detected(context, _location, _detections):
            context.manager.skip_geo_waypoint(context.job_id, "first")

        hit = {"class_id": 0, "class_name": "target", "confidence": 0.9, "xyxy": [20, 30, 50, 60]}
        summary, saved, records, updates, _, _ = self.run_sequence([
            GeoRecognitionWaypoint("first", "第一站", 25.1, 121.1, 0, 1, 0),
            GeoRecognitionWaypoint("second", "第二站", 25.2, 121.2, 0, 0, 0),
        ], hits=Mock(side_effect=[[hit], []]), detection_update=detected)
        self.assertTrue(summary.completed)
        self.assertEqual(summary.detection_count, 1)
        self.assertEqual(summary.skipped_waypoints, 1)
        self.assertEqual(saved["matched_frames"], 1)
        self.assertEqual(records[0]["waypoint_id"], "first")
        self.assertEqual(updates[-1][:2], (4, 4))

    def test_skip_last_node_completes_sequence(self):
        def status(context, message):
            if message.startswith("已到達 最後一站"):
                context.manager.skip_geo_waypoint(context.job_id, "last")

        summary, _, _, updates, _, messages = self.run_sequence([
            GeoRecognitionWaypoint("last", "最後一站", 25.1, 121.1, 60, 0, 0),
        ], status=status)
        self.assertEqual((summary.completed_waypoints, summary.skipped_waypoints), (0, 1))
        self.assertTrue(summary.completed)
        self.assertEqual(updates[-1][:2], (1, 1))
        self.assertTrue(any("已是最後一個節點" in message for message in messages))

    def test_skip_finishes_current_target_without_opening_remaining_targets(self):
        resolver = Mock()
        resolver.stop_event = threading.Event()

        def create_resolver(context):
            def resolve(_detection, **_options):
                context.manager.skip_geo_waypoint(context.job_id, "first")
                return {"lat": 25.11, "lng": 121.11}
            resolver.resolve.side_effect = resolve
            return resolver

        hits = [
            {"class_id": 0, "class_name": "target", "confidence": 0.9, "xyxy": [20, 30, 50, 60]},
            {"class_id": 0, "class_name": "target", "confidence": 0.9, "xyxy": [70, 100, 100, 130]},
        ]
        summary, _, records, _, _, _ = self.run_sequence([
            GeoRecognitionWaypoint("first", "第一站", 25.1, 121.1, 0, 1, 0),
            GeoRecognitionWaypoint("second", "第二站", 25.2, 121.2, 0, 0, 0),
        ], hits=Mock(side_effect=[hits, []]), resolver=create_resolver)
        resolver.resolve.assert_called_once()
        self.assertTrue(summary.completed)
        self.assertEqual(summary.detection_count, 2)
        self.assertIn("precise_coordinate", records[0]["detections"][0])
        self.assertIn("已略過", records[0]["detections"][1]["coordinate_error"])

    def test_skip_navigation_continues_to_following_node(self):
        def arrival(context, *args, **kwargs):
            if args[1].lat == 25.2:
                context.manager.skip_geo_waypoint(context.job_id, "second")
            return wait_for_arrival(*args, **kwargs)

        summary, _, _, updates, controller, _ = self.run_sequence([
            GeoRecognitionWaypoint("first", "第一站", 25.1, 121.1, 0, 0, 0),
            GeoRecognitionWaypoint("second", "第二站", 25.2, 121.2, 0, 0, 0),
            GeoRecognitionWaypoint("third", "第三站", 25.3, 121.3, 0, 0, 0),
        ], arrival=arrival)
        self.assertTrue(summary.completed)
        self.assertEqual((summary.completed_waypoints, summary.skipped_waypoints), (2, 1))
        self.assertEqual(updates[-1][:2], (3, 3))
        self.assertEqual([payload["lat"] for command, payload in controller.commands if command == "navigate"], [25.2, 25.3])

    def test_stop_takes_priority_over_skip(self):
        def status(context, message):
            if message.startswith("已到達 第一站"):
                context.manager.skip_geo_waypoint(context.job_id, "first")
                context.manager.stop(context.job_id)

        summary, _, _, _, controller, _ = self.run_sequence([
            GeoRecognitionWaypoint("first", "第一站", 25.1, 121.1, 60, 0, 0),
            GeoRecognitionWaypoint("second", "第二站", 25.2, 121.2, 0, 0, 0),
        ], status=status)
        self.assertFalse(summary.completed)
        self.assertEqual(summary.stop_reason, "cancelled")
        self.assertNotIn(25.2, [payload.get("lat") for _, payload in controller.commands])


class GeoWaypointSkipApiTests(unittest.TestCase):
    def test_api_binds_skip_to_current_node_and_keeps_job_running(self):
        context = sequence_context()
        runtime = AppRuntime()
        runtime.jobs = context.manager
        first_event = context.begin_waypoint({"waypoint_id": "first", "waypoint_name": "第一站", "sequence_index": 0, "total_points": 2})
        with TestClient(create_app(runtime)) as client:
            response = client.post("/api/jobs/sequence/skip-waypoint", json={"waypoint_id": "first"})
            self.assertEqual(response.status_code, 200)
            self.assertTrue(response.json()["job"]["skipping_waypoint"])
            self.assertEqual(response.json()["job"]["state"], "running")
            self.assertTrue(first_event.is_set())
            self.assertFalse(context.stop_event.is_set())
            self.assertEqual(client.post("/api/jobs/sequence/skip-waypoint", json={"waypoint_id": "first"}).status_code, 200)
            self.assertTrue(context.finish_waypoint())
            next_event = context.begin_waypoint({"waypoint_id": "second", "waypoint_name": "第二站", "sequence_index": 1, "total_points": 2})
            self.assertEqual(client.post("/api/jobs/sequence/skip-waypoint", json={"waypoint_id": "first"}).status_code, 409)
            self.assertFalse(next_event.is_set())
            self.assertEqual(client.post("/api/jobs/missing/skip-waypoint", json={"waypoint_id": "first"}).status_code, 404)
            self.assertEqual(client.post("/api/jobs/sequence/skip-waypoint", json={}).status_code, 422)
            context.manager.update(context.job_id, paused=True)
            self.assertEqual(client.post("/api/jobs/sequence/skip-waypoint", json={"waypoint_id": "second"}).status_code, 409)
            self.assertFalse(next_event.is_set())
            context.manager.stop(context.job_id)
            self.assertTrue(next_event.is_set())
            self.assertEqual(client.post("/api/jobs/sequence/skip-waypoint", json={"waypoint_id": "second"}).status_code, 409)


if __name__ == "__main__":
    unittest.main()
