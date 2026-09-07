from __future__ import annotations

import base64
import io
import json
import threading
import unittest
from email.parser import BytesParser
from email.policy import default
from unittest.mock import Mock, patch
from urllib.error import HTTPError, URLError

from bloompilot.discord_bot import (
    discover_bot_channels, post_discord_bot, resolve_bot_channel, validate_bot_token,
)
from bloompilot.discord_webhook import (
    DiscordWebhookError, DiscordWebhookNotifier, build_discord_hit_messages,
    normalize_discord_webhook_settings, post_discord_webhook,
)

WEBHOOK_URL = "https://discord.com/api/webhooks/123456789012345678/abcdefghijklmnopqrstuvwxyz_ABCDEFGHIJKLMNOPQRSTUVWXYZ-0123456789"

TOKEN = "example.bot.token-for-offline-tests-only"
GUILD = "111111111111111111"
BOT = "222222222222222222"
CHANNEL = "333333333333333333"
ROLE = "444444444444444444"
PERMISSIONS = (1 << 10) | (1 << 11) | (1 << 14) | (1 << 15)
PNG = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAusB9Y9ZQmcAAAAASUVORK5CYII=")
CROP = "data:image/png;base64," + base64.b64encode(PNG).decode()


def response(status=200, value=None):
    result = Mock()
    result.__enter__ = Mock(return_value=result)
    result.__exit__ = Mock(return_value=False)
    result.status = status
    result.read.return_value = json.dumps(value).encode() if value is not None else b""
    return result


class BotDiscoveryTests(unittest.TestCase):
    def test_token_and_legacy_settings(self):
        self.assertEqual(TOKEN, validate_bot_token(f" Bot {TOKEN} "))
        for invalid in ("", "short", TOKEN + "\r\nInjected: value"):
            with self.subTest(value=invalid), self.assertRaises(ValueError):
                validate_bot_token(invalid)
        legacy = normalize_discord_webhook_settings({"discord_webhook": {"enabled": True, "webhook_url": WEBHOOK_URL}})
        self.assertEqual("webhook", legacy["discord_webhook"]["mode"])
        config = normalize_discord_webhook_settings({"discord_webhook": {"mode": "bot", "enabled": True, "bot_token": TOKEN}})
        self.assertEqual(TOKEN, config["discord_webhook"]["bot_token"])
        with self.assertRaises(ValueError):
            normalize_discord_webhook_settings({"discord_webhook": {"mode": "bot", "enabled": True}})

    def test_discovery_filters_permissions_and_honors_member_overwrites(self):
        base = {"id": CHANNEL, "name": "命中播報", "type": 0}
        denied = {"id": GUILD, "type": 0, "deny": str(1 << 15), "allow": "0"}
        role_allow = {"id": ROLE, "type": 0, "allow": str(1 << 15), "deny": "0"}
        member_deny = {"id": BOT, "type": 1, "deny": str(1 << 11), "allow": "0"}
        with patch("bloompilot.discord_bot.discord_request", side_effect=[
            {"id": BOT, "username": "Bloom Bot", "bot": True},
            [{"id": GUILD, "name": "測試伺服器", "permissions": str(PERMISSIONS)}],
            {"user": {"id": BOT}, "roles": [ROLE]},
            [base, {**base, "id": "555555555555555555", "type": 2},
             {**base, "permission_overwrites": [denied]},
             {**base, "id": "666666666666666666", "permission_overwrites": [denied, role_allow]},
             {**base, "permission_overwrites": [denied, role_allow, member_deny]}],
        ]) as call:
            result = discover_bot_channels(TOKEN)
        self.assertEqual([CHANNEL, "666666666666666666"], [channel["id"] for channel in result["channels"]])
        self.assertEqual([], result["warnings"])
        self.assertNotIn(TOKEN, str(result))
        self.assertTrue(all(item.kwargs["bot_token"] == TOKEN for item in call.call_args_list))

    def test_only_unambiguous_destination_is_automatically_selected(self):
        with patch("bloompilot.discord_bot.discover_bot_channels", return_value={"channels": [{"id": CHANNEL}], "warnings": []}):
            self.assertEqual(CHANNEL, resolve_bot_channel(TOKEN))
        for result in (
            {"channels": [{"id": CHANNEL}, {"id": ROLE}], "warnings": []},
            {"channels": [{"id": CHANNEL}], "warnings": ["部分頻道無法讀取"]},
            {"channels": [], "warnings": ["請邀請機器人加入伺服器"]},
        ):
            with self.subTest(result=result), patch("bloompilot.discord_bot.discover_bot_channels", return_value=result), self.assertRaises(ValueError):
                resolve_bot_channel(TOKEN)

    def test_administrator_bypasses_overwrites_and_empty_guilds_are_explained(self):
        from bloompilot.discord_bot import _can_broadcast
        self.assertTrue(_can_broadcast(1 << 3, GUILD, {}, {"permission_overwrites": [{"id": GUILD, "type": 0, "deny": str(PERMISSIONS)}]}))
        with patch("bloompilot.discord_bot.discord_request", side_effect=[{"id": BOT, "bot": True}, []]):
            result = discover_bot_channels(TOKEN)
        self.assertEqual([], result["channels"])
        self.assertIn("邀請", result["warnings"][0])


class DiscordImageDeliveryTests(unittest.TestCase):
    def test_each_image_matches_its_embed_even_when_one_crop_is_invalid(self):
        detections = [{"class_name": "Large_Red", "crop_data_url": "bad"}, {"class_name": "Large_Blue", "crop_data_url": CROP}]
        payload, files = next(build_discord_hit_messages("screen_recognition", {}, detections))
        self.assertNotIn("image", payload["embeds"][0])
        self.assertEqual("attachment://target-2.png", payload["embeds"][1]["image"]["url"])
        self.assertEqual(PNG, files[0].data)
        self.assertNotIn(CROP, str(payload))
        with patch("bloompilot.discord_webhook.build_opener") as opener:
            opener.return_value.open.return_value = response(204)
            post_discord_webhook(WEBHOOK_URL, payload, files=files)
            request = opener.return_value.open.call_args.args[0]
        mime = BytesParser(policy=default).parsebytes(f"Content-Type: {request.get_header('Content-type')}\r\n\r\n".encode() + request.data)
        parts = list(mime.iter_parts())
        actual = json.loads(parts[0].get_payload(decode=True))
        self.assertEqual([{"id": 0, "filename": "target-2.png"}], actual["attachments"])
        self.assertEqual('files[0]', parts[1].get_param('name', header='content-disposition'))
        self.assertEqual(PNG, parts[1].get_payload(decode=True))
        self.assertIsNone(request.get_header("Authorization"))

    def test_large_events_deliver_all_hits_with_bounded_embeds(self):
        detections = [{"class_name": "長名稱" * 1000, "crop_data_url": CROP, "precise_coordinate": {"lat": -89.1234567, "lng": -179.1234567}} for _ in range(13)]
        batches = list(build_discord_hit_messages("geo_sequence_recognition", {"waypoint_name": "長地點" * 1000}, detections))
        self.assertEqual([5, 5, 3], [len(payload["embeds"]) for payload, _ in batches])
        for payload, files in batches:
            count = 0
            for embed in payload["embeds"]:
                count += len(embed["title"]) + len(embed["description"]) + len(embed["footer"]["text"])
                count += sum(len(field["name"]) + len(field["value"]) for field in embed["fields"])
            self.assertLessEqual(count, 6000)
            self.assertEqual(len(payload["embeds"]), len(files))

    def test_bot_upload_uses_authorization_and_omits_webhook_only_fields(self):
        payload, files = next(build_discord_hit_messages("screen_recognition", {}, [{"crop_data_url": CROP}]))
        with patch("bloompilot.discord_webhook.build_opener") as opener:
            opener.return_value.open.return_value = response(value={"id": "message"})
            post_discord_bot(TOKEN, CHANNEL, payload, files=files)
            request = opener.return_value.open.call_args.args[0]
        self.assertEqual(f"https://discord.com/api/v10/channels/{CHANNEL}/messages", request.full_url)
        self.assertEqual(f"Bot {TOKEN}", request.get_header("Authorization"))
        self.assertIn(b"attachment://target-1.png", request.data)
        self.assertNotIn(b'"username"', request.data)
        self.assertNotIn(TOKEN.encode(), request.data)

    def test_invalid_oversized_or_remote_images_keep_text_notification(self):
        for crop in (None, "data:image/png;base64,broken", "data:image/png;base64," + "A" * 800000, "https://example.com/image.png", "file:///private.png"):
            with self.subTest(crop_type=type(crop).__name__):
                payload, files = next(build_discord_hit_messages("screen_recognition", {}, [{"crop_data_url": crop, "participant_count": None}]))
                self.assertEqual([], files)
                self.assertNotIn("image", payload["embeds"][0])
                self.assertEqual("未辨識", payload["embeds"][0]["fields"][0]["value"])

    def test_http_errors_are_safe_and_rate_limit_retries(self):
        for code in (401, 403, 404, 429):
            with self.subTest(code=code), patch("bloompilot.discord_webhook.build_opener") as opener:
                opener.return_value.open.side_effect = HTTPError(WEBHOOK_URL, code, TOKEN, {}, io.BytesIO(b'{"retry_after":60}'))
                with self.assertRaises(DiscordWebhookError) as raised:
                    post_discord_webhook(WEBHOOK_URL, {"content": "test"})
                self.assertEqual(code, raised.exception.status)
                self.assertNotIn(TOKEN, str(raised.exception))
                self.assertNotIn(WEBHOOK_URL, str(raised.exception))
        with patch("bloompilot.discord_webhook.build_opener") as opener, patch("bloompilot.discord_webhook.time.sleep") as sleep:
            opener.return_value.open.side_effect = [HTTPError(WEBHOOK_URL, 429, "limited", {}, io.BytesIO(b'{"retry_after":0.1}')), response(204)]
            post_discord_webhook(WEBHOOK_URL, {"content": "test"})
            sleep.assert_called_once_with(0.1)
            self.assertEqual(2, opener.return_value.open.call_count)
        with patch("bloompilot.discord_webhook.build_opener") as opener:
            opener.return_value.open.side_effect = URLError(WEBHOOK_URL)
            with self.assertRaises(DiscordWebhookError) as raised:
                post_discord_webhook(WEBHOOK_URL, {"content": "test"})
            self.assertNotIn(WEBHOOK_URL, str(raised.exception))

    def test_bot_notifier_uploads_in_background_and_recovers_after_failure(self):
        delivered = threading.Event()
        errors = []
        calls = []
        def send(token, channel, payload, *, files):
            calls.append((token, channel, payload, files))
            if len(calls) == 1:
                raise DiscordWebhookError("測試失敗")
            delivered.set()
        notifier = DiscordWebhookNotifier(
            lambda: {"discord_webhook": {"mode": "bot", "enabled": True, "bot_token": TOKEN, "channel_id": CHANNEL}},
            bot_sender=send, error_callback=lambda kind, message: errors.append(message),
        )
        try:
            for _ in range(2):
                self.assertTrue(notifier.notify("screen_recognition", {}, [{"class_name": "Large_Blue", "crop_data_url": CROP}]))
            self.assertTrue(delivered.wait(2))
            self.assertEqual(["測試失敗"], errors)
            self.assertEqual(PNG, calls[1][3][0].data)
            self.assertEqual((TOKEN, CHANNEL), calls[1][:2])
        finally:
            notifier.close()
        self.assertFalse(notifier.notify("screen_recognition", {}, [{"class_name": "target"}]))


if __name__ == "__main__":
    unittest.main()
