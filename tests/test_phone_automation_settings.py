from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from bloompilot.settings_store import (
    apply_config_overrides,
    load_studio_settings,
    save_studio_section,
)


class PhoneAutomationSettingsTests(unittest.TestCase):
    def test_sections_are_merged_and_can_be_loaded_again(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "settings.json"
            with patch(
                "bloompilot.settings_store.PHONE_AUTOMATION_SETTINGS_PATH",
                path,
            ):
                save_studio_section("background", {"city": "paris"})
                save_studio_section(
                    "recognition",
                    {
                        "selected_targets": ["Large_Red"],
                        "sound_alert": True,
                    },
                )
                settings = load_studio_settings()
            self.assertEqual(settings["background"], {"city": "paris"})
            self.assertEqual(
                settings["recognition"]["selected_targets"],
                ["Large_Red"],
            )
            self.assertTrue(settings["recognition"]["sound_alert"])

    def test_invalid_json_falls_back_to_defaults(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "settings.json"
            path.write_text("not json", encoding="utf-8")
            with patch(
                "bloompilot.settings_store.PHONE_AUTOMATION_SETTINGS_PATH",
                path,
            ):
                self.assertEqual(load_studio_settings(), {})

    def test_config_overrides_are_deep_merged(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "settings.json"
            with patch(
                "bloompilot.settings_store.PHONE_AUTOMATION_SETTINGS_PATH",
                path,
            ):
                save_studio_section(
                    "config_overrides",
                    {"training": {"epochs": 55}},
                )
                config = apply_config_overrides(
                    {
                        "training": {"epochs": 100, "batch": 3},
                        "generation": {"square_size_pixels": 256},
                    }
                )
            self.assertEqual(config["training"], {"epochs": 55, "batch": 3})
            self.assertEqual(config["generation"]["square_size_pixels"], 256)


if __name__ == "__main__":
    unittest.main()
