"""Persistent user settings and non-destructive config.yaml overrides."""

from __future__ import annotations

import copy
import json
import threading
from pathlib import Path
from typing import Any

from .paths import PHONE_AUTOMATION_SETTINGS_PATH


STUDIO_SETTINGS_LOCK = threading.RLock()


def merge_studio_settings(current: dict[str, Any], changes: dict[str, Any]) -> dict[str, Any]:
    """Apply only supplied sections; null removes a legacy section."""
    result = copy.deepcopy(current)
    for key, value in changes.items():
        if value is None:
            result.pop(key, None)
        elif key in {"config_overrides", "discord_webhook"} and isinstance(value, dict):
            result[key] = _deep_merge(result.get(key, {}) if isinstance(result.get(key), dict) else {}, copy.deepcopy(value))
        else:
            result[key] = copy.deepcopy(value)
    return result


def load_studio_settings(path: Path | None = None) -> dict[str, Any]:
    path = path or PHONE_AUTOMATION_SETTINGS_PATH
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, OSError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def save_studio_settings(
    settings: dict[str, Any],
    path: Path | None = None,
) -> None:
    from .phone_automation import atomic_write_json

    with STUDIO_SETTINGS_LOCK:
        atomic_write_json(path or PHONE_AUTOMATION_SETTINGS_PATH, settings)


def save_studio_section(
    section: str,
    value: dict[str, Any],
    path: Path | None = None,
) -> None:
    with STUDIO_SETTINGS_LOCK:
        settings = load_studio_settings(path)
        settings[section] = value
        save_studio_settings(settings, path)


def _deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(base.get(key), dict):
            _deep_merge(base[key], value)
        else:
            base[key] = value
    return base


def apply_config_overrides(config: dict[str, Any]) -> dict[str, Any]:
    settings = load_studio_settings()
    overrides = settings.get("config_overrides", {})
    if isinstance(overrides, dict):
        _deep_merge(config, overrides)
    return config
