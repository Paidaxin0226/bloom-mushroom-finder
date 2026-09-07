from __future__ import annotations

import threading
import unittest

from bloompilot.discord_webhook import (
    DiscordWebhookNotifier,
    build_discord_hit_payload,
    normalize_discord_webhook_settings,
    validate_discord_webhook_url,
)


WEBHOOK_URL = "https://discord.com/api/webhooks/123456789012345678/abcdefghijklmnopqrstuvwxyz_ABCDEFGHIJKLMNOPQRSTUVWXYZ-0123456789"


class DiscordWebhookValidationTests(unittest.TestCase):
    def test_accepts_official_discord_webhook(self) -> None:
        self.assertEqual(WEBHOOK_URL, validate_discord_webhook_url(f"  {WEBHOOK_URL}  "))
        self.assertEqual(
            WEBHOOK_URL + "?thread_id=987654321",
            validate_discord_webhook_url(WEBHOOK_URL + "?thread_id=987654321"),
        )

    def test_rejects_non_discord_and_malformed_urls(self) -> None:
        invalid = [
            "http://discord.com/api/webhooks/12345/abcdefghijklmnopqrstuvwxyz",
            "https://example.com/api/webhooks/12345/abcdefghijklmnopqrstuvwxyz",
            "https://discord.com.evil.test/api/webhooks/12345/abcdefghijklmnopqrstuvwxyz",
            "https://discord.com/api/webhooks/12345/short",
            WEBHOOK_URL + "?wait=true",
        ]
        for value in invalid:
            with self.subTest(value=value), self.assertRaises(ValueError):
                validate_discord_webhook_url(value)

    def test_normalizes_without_mutating_original_settings(self) -> None:
        settings = {
            "other": {"kept": True},
            "discord_webhook": {"enabled": True, "webhook_url": f" {WEBHOOK_URL} "},
        }
        normalized = normalize_discord_webhook_settings(settings)

        self.assertIsNot(settings, normalized)
        self.assertEqual(WEBHOOK_URL, normalized["discord_webhook"]["webhook_url"])
        self.assertEqual(f" {WEBHOOK_URL} ", settings["discord_webhook"]["webhook_url"])
        self.assertEqual({"kept": True}, normalized["other"])

    def test_enabled_setting_requires_a_url(self) -> None:
        with self.assertRaisesRegex(ValueError, "啟用 Discord"):
            normalize_discord_webhook_settings({
                "discord_webhook": {"enabled": True, "webhook_url": ""},
            })


class DiscordWebhookPayloadTests(unittest.TestCase):
    def test_hit_payload_includes_detection_details_and_map_link(self) -> None:
        payload = build_discord_hit_payload(
            "geo_sequence_recognition",
            {
                "waypoint_name": "台北車站",
                "point": {"grid_x": -2, "grid_y": 3},
            },
            [{
                "class_name": "Large_Blue",
                "confidence": 0.876,
                "participant_count": 4,
                "crop_data_url": "data:image/jpeg;base64,secret-image-data",
                "estimated_coordinate": {"lat": 25.0478, "lng": 121.517},
            }],
        )

        self.assertEqual({"parse": []}, payload["allowed_mentions"])
        self.assertIn("1 筆", payload["content"])
        embed = payload["embeds"][0]
        self.assertEqual("🍄 大型藍色蘑菇", embed["title"])
        self.assertEqual("**88%**", embed["fields"][1]["value"])
        self.assertEqual("**4 人**", embed["fields"][0]["value"])
        self.assertIn("https://www.google.com/maps?q=25.0478000,121.5170000", embed["description"])
        self.assertIn("台北車站 · 格位 x=-2, y=+3", embed["footer"]["text"])
        self.assertIn("估算座標", embed["description"])
        self.assertNotIn("secret-image-data", str(payload))

    def test_precise_coordinate_is_not_labeled_as_estimate(self) -> None:
        payload = build_discord_hit_payload(
            "screen_recognition",
            {"point": {"lat": 25.0, "lng": 121.0}},
            [{
                "class_name": "Large_Water",
                "precise_coordinate": {"lat": 43.70615, "lng": -79.641128},
            }],
        )

        description = payload["embeds"][0]["description"]
        self.assertIn("`43.7061500, -79.6411280`", description)
        self.assertIn("精確座標", description)
        self.assertNotIn("約 43.7061500", description)

    def test_failed_precise_capture_does_not_fall_back_to_scan_center(self) -> None:
        payload = build_discord_hit_payload(
            "screen_recognition",
            {"point": {"lat": 25.0, "lng": 121.0}},
            [{"class_name": "Large_Water", "coordinate_error": "not available"}],
        )

        self.assertNotIn("google.com/maps", payload["embeds"][0]["description"])


class DiscordWebhookNotifierTests(unittest.TestCase):
    def test_disabled_notifier_does_not_start_sender(self) -> None:
        sender_called = threading.Event()
        notifier = DiscordWebhookNotifier(
            lambda: {"discord_webhook": {"enabled": False, "webhook_url": WEBHOOK_URL}},
            sender=lambda _url, _payload: sender_called.set(),
        )
        try:
            self.assertFalse(notifier.notify("screen_recognition", {}, [{"class_name": "target"}]))
            self.assertFalse(sender_called.wait(0.05))
        finally:
            notifier.close()

    def test_enabled_notifier_delivers_in_background(self) -> None:
        delivered = threading.Event()
        observed: list[tuple[object, object]] = []

        def sender(url: object, payload: object) -> None:
            observed.append((url, payload))
            delivered.set()

        notifier = DiscordWebhookNotifier(
            lambda: {"discord_webhook": {"enabled": True, "webhook_url": WEBHOOK_URL}},
            sender=sender,
        )
        try:
            self.assertTrue(notifier.notify(
                "screen_recognition",
                {"grid_x": 0, "grid_y": 0},
                [{"class_name": "Large_Red", "confidence": 0.9}],
            ))
            self.assertTrue(delivered.wait(1.0))
            self.assertEqual(WEBHOOK_URL, observed[0][0])
            self.assertIn("大型紅色蘑菇", str(observed[0][1]))
        finally:
            notifier.close()


if __name__ == "__main__":
    unittest.main()
