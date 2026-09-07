from __future__ import annotations

import json
import tempfile
import unittest
from dataclasses import asdict
from pathlib import Path
from unittest.mock import Mock, patch

from fastapi.testclient import TestClient

from bloompilot.geo_recognition import GeoRecognitionWaypoint
from bloompilot.route_library import RouteLibrary
from bloompilot.web.runtime import AppRuntime
from bloompilot.web.server import create_app
import test_geo_waypoint_skip as skip_tests


class GeoWaypointDeleteTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(dir=Path.cwd())
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name) / "routes"
        self.path = self.root / "bundled" / "route.json"
        self.path.parent.mkdir(parents=True)
        self.waypoints = [
            GeoRecognitionWaypoint("first", "第一站", 25.1, 121.1, 0, 0, 0),
            GeoRecognitionWaypoint("second", "第二站", 25.2, 121.2, 0, 0, 0),
            GeoRecognitionWaypoint("third", "第三站", 25.3, 121.3, 0, 0, 0),
        ]
        self.document = {
            "schema": "bloompilot.geo-sequence.v1",
            "exported_at": "original",
            "metadata": {"city_count": 3, "total_search_points": 3, "source": {"name": "retained"}},
            "sequence": {
                "version": 1, "id": "route", "name": "測試路線", "updated_at": "original",
                "waypoints": [dict({key: value for key, value in asdict(item).items() if value is not None}, custom_note="keep") for item in self.waypoints],
            },
        }
        self.write_document()
        self.context = skip_tests.sequence_context()
        self.runtime = AppRuntime()
        self.runtime.route_library = RouteLibrary(self.root)
        self.runtime.jobs = self.context.manager
        self.runtime.jobs.update(self.context.job_id, route_path="bundled/route.json")

    def write_document(self):
        self.path.write_text(json.dumps(self.document, ensure_ascii=False), encoding="utf-8")

    def begin(self, waypoint_id="second", sequence_index=0):
        return self.context.begin_waypoint({
            "waypoint_id": waypoint_id, "waypoint_name": waypoint_id,
            "sequence_index": sequence_index, "total_points": 3,
        })

    def test_api_deletes_by_id_from_original_route_and_reload_keeps_other_fields(self):
        # The randomized execution index is deliberately different from the file index.
        event = self.begin("second", 0)
        with TestClient(create_app(self.runtime)) as client:
            response = client.post("/api/jobs/sequence/delete-waypoint", json={"waypoint_id": "second"})
            self.assertEqual(response.status_code, 200, response.text)
            self.assertTrue(response.json()["job"]["deleting_waypoint"])
            self.assertEqual(response.json()["job"]["route_path"], "bundled/route.json")
            self.assertTrue(event.is_set())
            self.assertFalse(self.context.stop_event.is_set())
            first_saved = self.path.read_bytes()
            # A duplicate click during the same transition cannot delete the next node.
            self.assertEqual(client.post("/api/jobs/sequence/delete-waypoint", json={"waypoint_id": "second"}).status_code, 200)
            self.assertEqual(self.path.read_bytes(), first_saved)
            self.assertTrue(self.context.finish_waypoint())
            next_event = self.begin("third", 1)
            self.assertEqual(client.post("/api/jobs/sequence/delete-waypoint", json={"waypoint_id": "second"}).status_code, 409)
            self.assertFalse(next_event.is_set())
            saved = client.get("/api/routes/content", params={"path": "bundled/route.json"}).json()

        self.assertEqual(saved["sequence"]["waypoints"], [self.document["sequence"]["waypoints"][0], self.document["sequence"]["waypoints"][2]])
        self.assertEqual(saved["metadata"], {"city_count": 2, "waypoint_count": 2, "total_search_points": 2, "source": {"name": "retained"}})
        self.assertEqual(saved["exported_at"], "original")
        self.assertNotEqual(saved["sequence"]["updated_at"], "original")
        # A fresh library instance sees the persisted modification.
        reloaded = RouteLibrary(self.root).list()
        self.assertEqual(reloaded[0]["waypoint_count"], 2)
        self.assertFalse((self.root / "imported").exists())

    def test_failed_atomic_save_does_not_delete_or_skip(self):
        original = self.path.read_bytes()
        event = self.begin()
        with TestClient(create_app(self.runtime)) as client, patch("bloompilot.route_library.os.replace", side_effect=OSError("disk failure")):
            response = client.post("/api/jobs/sequence/delete-waypoint", json={"waypoint_id": "second"})
            self.assertEqual(response.status_code, 500)
            self.assertEqual(self.path.read_bytes(), original)
            self.assertFalse(event.is_set())
            self.assertFalse(self.runtime.jobs.get("sequence").skipping_waypoint)
            self.assertFalse(self.path.with_suffix(".json.tmp").exists())

    def test_rejects_missing_stale_paused_and_unbound_requests_without_mutation(self):
        original = self.path.read_bytes()
        event = self.begin()
        with TestClient(create_app(self.runtime)) as client:
            endpoint = "/api/jobs/sequence/delete-waypoint"
            self.assertEqual(client.post("/api/jobs/missing/delete-waypoint", json={"waypoint_id": "second"}).status_code, 404)
            self.assertEqual(client.post(endpoint, json={}).status_code, 422)
            self.assertEqual(client.post(endpoint, json={"waypoint_id": "first"}).status_code, 409)
            for changes in ({"paused": True}, {"resuming": True}, {"route_path": None}, {"state": "stopping"}, {"kind": "screen_recognition"}):
                with self.subTest(changes=changes):
                    self.runtime.jobs.update("sequence", **changes)
                    self.assertEqual(client.post(endpoint, json={"waypoint_id": "second"}).status_code, 409)
                    self.runtime.jobs.update("sequence", paused=False, resuming=False, route_path="bundled/route.json", state="running", kind="geo_sequence_recognition")
            self.runtime.jobs._pausing.add("sequence")
            self.assertEqual(client.post(endpoint, json={"waypoint_id": "second"}).status_code, 409)
            self.runtime.jobs._pausing.clear()
            self.begin("missing-node")
            self.assertEqual(client.post(endpoint, json={"waypoint_id": "missing-node"}).status_code, 409)
        self.assertEqual(self.path.read_bytes(), original)
        self.assertFalse(event.is_set())
        self.assertFalse(self.runtime.jobs.get("sequence").skipping_waypoint)

    def test_skip_does_not_delete_and_cannot_be_upgraded_mid_transition(self):
        original = self.path.read_bytes()
        self.begin()
        self.runtime.jobs.skip_geo_waypoint("sequence", "second")
        with self.assertRaisesRegex(RuntimeError, "正在跳過"):
            self.runtime.delete_geo_waypoint("sequence", "second")
        self.assertEqual(self.path.read_bytes(), original)

    def test_custom_route_deletion_counts_masked_points_and_keeps_empty_route(self):
        custom = self.runtime.route_library.import_document("custom.json", self.document)
        path = self.root / custom["path"]
        data = json.loads(path.read_text(encoding="utf-8"))
        data["sequence"]["waypoints"][1].update({"left_radius": 2, "scan_spans": [[0, -2, 0]]})
        path.write_text(json.dumps(data), encoding="utf-8")
        self.runtime.jobs.update("sequence", route_path=custom["path"])
        self.begin()
        self.runtime.delete_geo_waypoint("sequence", "second")
        self.assertEqual(json.loads(path.read_text(encoding="utf-8"))["metadata"]["total_search_points"], 2)
        for waypoint_id in ("first", "third"):
            self.context.finish_waypoint()
            self.begin(waypoint_id)
            self.runtime.delete_geo_waypoint("sequence", waypoint_id)
        with TestClient(create_app(self.runtime)) as client:
            content = client.get("/api/routes/content", params={"path": custom["path"]})
            self.assertEqual(content.status_code, 200)
            self.assertEqual(content.json()["sequence"]["waypoints"], [])
            summary = next(item for item in client.get("/api/routes").json() if item["path"] == custom["path"])
            self.assertEqual((summary["waypoint_count"], summary["search_point_count"]), (0, 0))

    def test_start_binds_original_route_before_randomization(self):
        payload = {"waypoints": self.document["sequence"]["waypoints"], "route_path": "bundled/route.json", "randomize_waypoints": True}
        with patch.object(self.runtime, "recognition_model", return_value=Path("model.pt")), patch.object(self.runtime.jobs, "start") as start:
            self.runtime.start_geo_sequence_recognition(payload)
        self.assertEqual(start.call_args.kwargs["route_path"], "bundled/route.json")
        self.assertEqual([item["id"] for item in payload["waypoints"]], ["first", "second", "third"])
        with patch.object(self.runtime.jobs, "start") as start:
            with self.assertRaises(ValueError):
                self.runtime.start_geo_sequence_recognition({**payload, "route_path": "../outside.json"})
            start.assert_not_called()

    def test_worker_deletes_current_node_and_preserves_hits_while_continuing(self):
        def on_hits(context, _location, _detections):
            self.runtime.jobs = context.manager
            context.manager.update(context.job_id, route_path="bundled/route.json")
            self.runtime.delete_geo_waypoint(context.job_id, "first")

        hit = {"class_id": 0, "class_name": "target", "confidence": 0.9, "xyxy": [20, 30, 50, 60]}
        summary, _, records, _, controller, _ = skip_tests.GeoWaypointSkipTests().run_sequence(
            self.waypoints, hits=Mock(side_effect=[[hit], [], []]), detection_update=on_hits,
        )
        self.assertTrue(summary.completed)
        self.assertEqual(summary.completed_waypoints, 2)
        self.assertEqual(summary.detection_count, 1)
        self.assertEqual(records[0]["detections"][0]["class_name"], "target")
        self.assertIn(25.2, [payload.get("lat") for _, payload in controller.commands])
        self.assertEqual([item["id"] for item in json.loads(self.path.read_text(encoding="utf-8"))["sequence"]["waypoints"]], ["second", "third"])

    def test_worker_deletes_only_remaining_node_and_completes(self):
        self.document["sequence"]["waypoints"] = [self.document["sequence"]["waypoints"][0]]
        self.write_document()

        def status(context, message):
            if message.startswith("已到達 第一站"):
                self.runtime.jobs = context.manager
                context.manager.update(context.job_id, route_path="bundled/route.json")
                self.runtime.delete_geo_waypoint(context.job_id, "first")

        summary, _, _, updates, _, _ = skip_tests.GeoWaypointSkipTests().run_sequence(self.waypoints[:1], status=status)
        self.assertTrue(summary.completed)
        self.assertEqual((summary.completed_waypoints, summary.skipped_waypoints), (0, 1))
        self.assertEqual(updates[-1][:2], (1, 1))
        self.assertEqual(json.loads(self.path.read_text(encoding="utf-8"))["sequence"]["waypoints"], [])


if __name__ == "__main__":
    unittest.main()
