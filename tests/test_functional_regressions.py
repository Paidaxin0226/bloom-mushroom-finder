from __future__ import annotations

import copy
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from fastapi.testclient import TestClient
from bloompilot.discord_webhook import DiscordWebhookNotifier
from bloompilot.route_library import RouteLibrary
from bloompilot.settings_store import load_studio_settings, save_studio_settings, merge_studio_settings
from bloompilot.web.runtime import AppRuntime, JobRecord, JobManager
from bloompilot.web.server import create_app

WEBHOOK = "https://discord.com/api/webhooks/123456789012345678/abcdefghijklmnopqrstuvwxyz_ABCDEFGHIJKLMNOPQRSTUVWXYZ-0123456789"
NEW_WEBHOOK = WEBHOOK.replace("123456789012345678", "223456789012345678")


def document(name="route"):
    return {"sequence": {"id": name, "name": name, "waypoints": [
        {"id": "one", "lat": 25, "lng": 121, "left_radius": 0, "right_radius": 0, "up_radius": 0, "down_radius": 0},
    ]}}


class LiveNotificationTests(unittest.TestCase):
    def wait_drained(self, notifier):
        deadline = time.monotonic() + 3
        while notifier._queue.unfinished_tasks and time.monotonic() < deadline:
            time.sleep(0.01)
        self.assertEqual(notifier._queue.unfinished_tasks, 0)

    def test_queued_hits_follow_saved_webhook_change_without_recreating_notifier(self):
        settings = {"discord_webhook": {"enabled": True, "webhook_url": WEBHOOK}}
        calls = []
        notifier = DiscordWebhookNotifier(lambda: settings, sender=lambda url, payload: calls.append(url))
        try:
            with patch.object(notifier, "_ensure_thread"):
                self.assertTrue(notifier.notify("screen_recognition", {}, [{"class_name": "Large_Blue"}]))
            settings["discord_webhook"]["webhook_url"] = NEW_WEBHOOK
            notifier._ensure_thread()
            self.wait_drained(notifier)
            self.assertEqual(calls, [NEW_WEBHOOK])
            self.assertTrue(notifier.notify("screen_recognition", {}, [{"class_name": "Large_Blue"}]))
            self.wait_drained(notifier)
            self.assertEqual(calls, [NEW_WEBHOOK, NEW_WEBHOOK])
        finally:
            notifier.close()

    def test_disabling_discards_queued_hits_and_can_be_enabled_again(self):
        settings = {"discord_webhook": {"enabled": True, "webhook_url": WEBHOOK}}
        sender = Mock()
        notifier = DiscordWebhookNotifier(lambda: settings, sender=sender)
        try:
            with patch.object(notifier, "_ensure_thread"):
                notifier.notify("screen_recognition", {}, [{"class_name": "Large_Blue"}])
            settings["discord_webhook"]["enabled"] = False
            notifier._ensure_thread()
            self.wait_drained(notifier)
            sender.assert_not_called()
            settings["discord_webhook"]["enabled"] = True
            notifier.notify("screen_recognition", {}, [{"class_name": "Large_Blue"}])
            self.wait_drained(notifier)
            sender.assert_called_once()
        finally:
            notifier.close()

    def test_each_batch_uses_latest_mode_token_and_channel(self):
        settings = {"discord_webhook": {"enabled": True, "webhook_url": WEBHOOK}}
        calls = []
        def webhook_sender(url, payload):
            calls.append(url)
            settings["discord_webhook"] = {
                "enabled": True, "mode": "bot", "bot_token": "test.bot.token-for-offline-validation",
                "channel_id": "333333333333333333",
            }
        bot_sender = Mock()
        notifier = DiscordWebhookNotifier(lambda: settings, sender=webhook_sender, bot_sender=bot_sender)
        try:
            with patch("bloompilot.discord_bot.resolve_bot_channel", return_value="333333333333333333"):
                notifier.notify("screen_recognition", {}, [{"class_name": "Large_Blue"}] * 11)
                self.wait_drained(notifier)
            self.assertEqual(calls, [WEBHOOK])
            self.assertEqual(bot_sender.call_count, 2)
            self.assertEqual(bot_sender.call_args.args[:2], ("test.bot.token-for-offline-validation", "333333333333333333"))
        finally:
            notifier.close()


class SettingsPatchTests(unittest.TestCase):
    def test_notification_toggle_preserves_credentials(self):
        original = {"discord_webhook": {"enabled": True, "mode": "bot", "bot_token": "test-token", "channel_id": "123"}}
        disabled = merge_studio_settings(original, {"discord_webhook": {"enabled": False}})
        self.assertFalse(disabled["discord_webhook"]["enabled"])
        self.assertEqual(disabled["discord_webhook"]["bot_token"], "test-token")
        restored = merge_studio_settings(disabled, {"discord_webhook": {"enabled": True}})
        self.assertEqual(restored, original)

    def test_settings_patch_is_allowed_from_electron_and_local_renderer(self):
        runtime = AppRuntime()
        with TestClient(create_app(runtime)) as client:
            for origin in ("null", "http://127.0.0.1:5175"):
                response = client.options("/api/settings", headers={
                    "Origin": origin,
                    "Access-Control-Request-Method": "PATCH",
                    "Access-Control-Request-Headers": "content-type",
                })
                self.assertEqual(response.status_code, 200, response.text)
                self.assertIn("PATCH", response.headers["access-control-allow-methods"])

    def test_merge_preserves_other_sections_and_does_not_alias_inputs(self):
        current = {"discord_webhook": {"webhook_url": NEW_WEBHOOK}, "config_overrides": {"training": {"batch": 2}}}
        changes = {"config_overrides": {"generation": {"parallel_workers": 3}}}
        expected = copy.deepcopy(current)
        merged = merge_studio_settings(current, changes)
        self.assertEqual(merged["discord_webhook"]["webhook_url"], NEW_WEBHOOK)
        self.assertEqual(merged["config_overrides"]["training"]["batch"], 2)
        merged["config_overrides"]["generation"]["parallel_workers"] = 9
        self.assertEqual(current, expected)
        self.assertEqual(changes["config_overrides"]["generation"]["parallel_workers"], 3)

    def test_settings_patch_preserves_webhook_and_removes_only_legacy_sequences(self):
        with tempfile.TemporaryDirectory() as directory, patch(
            "bloompilot.settings_store.PHONE_AUTOMATION_SETTINGS_PATH", Path(directory) / "settings.json"
        ):
            runtime = AppRuntime()
            with patch.object(runtime, "config", return_value={}), TestClient(create_app(runtime)) as client:
                save_studio_settings({
                    "discord_webhook": {"enabled": True, "webhook_url": NEW_WEBHOOK},
                    "recognition_default_model": "outputs/training/new/weights/best.pt",
                    "geo_sequences": [{"name": "legacy"}],
                })
                response = client.patch("/api/settings", json={"recognition_filter_defaults": {"participant_count": 3}})
                self.assertEqual(response.status_code, 200, response.text)
                self.assertEqual(response.json()["settings"]["discord_webhook"]["webhook_url"], NEW_WEBHOOK)
                response = client.patch("/api/settings", json={"geo_sequences": None})
                self.assertNotIn("geo_sequences", response.json()["settings"])
                self.assertEqual(load_studio_settings()["recognition_default_model"], "outputs/training/new/weights/best.pt")

    def test_invalid_patch_preserves_saved_settings(self):
        with tempfile.TemporaryDirectory() as directory, patch(
            "bloompilot.settings_store.PHONE_AUTOMATION_SETTINGS_PATH", Path(directory) / "settings.json"
        ):
            runtime = AppRuntime()
            with patch.object(runtime, "config", return_value={}), TestClient(create_app(runtime)) as client:
                original = {"discord_webhook": {"enabled": True, "webhook_url": WEBHOOK}}
                save_studio_settings(original)
                response = client.patch("/api/settings", json={"discord_webhook": {"enabled": True, "webhook_url": "invalid"}})
                self.assertEqual(response.status_code, 400)
                self.assertEqual(load_studio_settings(), original)


class RouteAndJobTests(unittest.TestCase):
    def test_deleting_one_route_keeps_other_routes_loadable(self):
        with tempfile.TemporaryDirectory() as directory:
            runtime = AppRuntime()
            runtime.route_library = RouteLibrary(Path(directory) / "routes")
            with TestClient(create_app(runtime)) as client:
                for name in ("alpha", "beta"):
                    self.assertEqual(client.post("/api/routes/import", json={"source_name": name + ".json", "data": document(name)}).status_code, 200)
                self.assertEqual(client.delete("/api/routes", params={"path": "imported/alpha.json"}).status_code, 200)
                self.assertEqual([route["path"] for route in client.get("/api/routes").json()], ["imported/beta.json"])
                response = client.get("/api/routes/content", params={"path": "imported/beta.json"})
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.json()["sequence"]["name"], "beta")

    def test_running_route_cannot_be_deleted_or_overwritten_but_other_routes_can(self):
        with tempfile.TemporaryDirectory() as directory:
            runtime = AppRuntime()
            runtime.route_library = RouteLibrary(Path(directory) / "routes")
            runtime.route_library.import_document("active.json", document("active"))
            active = JobRecord(id="job", kind="geo_sequence_recognition", state="running", route_path="imported/active.json")
            try:
                with patch.object(runtime.jobs, "active", return_value=active):
                    with self.assertRaisesRegex(ValueError, "正在執行"):
                        runtime.delete_route("imported/../imported/active.json")
                    with self.assertRaisesRegex(ValueError, "正在執行"):
                        runtime.import_route("active.json", document("replaced"))
                    other = runtime.import_route("other.json", document("other"))
                    runtime.delete_route(other["path"])
                    self.assertEqual(runtime.route_library.waypoints("imported/active.json")[0].id, "one")
            finally:
                runtime.close()

    def test_job_updates_have_increasing_revisions_and_independent_snapshots(self):
        hub = Mock()
        manager = JobManager(hub)
        manager._jobs["job"] = JobRecord(id="job", kind="screen_recognition")
        manager.update("job", state="running", message="first")
        manager.update("job", paused=True, message="second")
        first, second = [call.args[0]["job"] for call in hub.publish.call_args_list]
        self.assertLess(first["revision"], second["revision"])
        self.assertFalse(first["paused"])
        self.assertTrue(second["paused"])
