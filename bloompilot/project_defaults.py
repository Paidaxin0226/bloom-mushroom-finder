"""Fixed parameters for the dedicated Bloom Mushroom Finder capture/training workflow."""

from typing import Any
INFERENCE_IMAGE_SIZE = 1024
VIEWPORT_WIDTH_M = 627.9
SQUARE_SIZE_PIXELS = 256
MAX_GEO_SEQUENCE_WAYPOINTS = 50_000
MAX_GEO_SEQUENCE_SEARCH_POINTS = 1_000_000_000
MAX_GEO_SEQUENCE_RADIUS = 10_000
MAX_GEO_SEQUENCE_FILE_BYTES = 32 * 1024 * 1024
# Keep injected map drags out of launcher/system gesture hot zones.  The
# vertical span remains 66% of the display so existing route calibration and
# coverage metadata stay valid; only the whole gesture is shifted upward.
MAP_GESTURE_TOP_FRACTION = 0.10
MAP_GESTURE_BOTTOM_FRACTION = 0.76


def synthesis_workers(value: Any) -> int:
    try:
        workers = int(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError("parallel_workers 必須是大於 0 的整數") from exc
    if isinstance(value, bool) or workers < 1 or (isinstance(value, float) and workers != value):
        raise ValueError("parallel_workers 必須是大於 0 的整數")
    return workers


def apply_fixed_parameters(config: dict[str, Any]) -> dict[str, Any]:
    generation = config["generation"]
    generation["square_size_pixels"] = SQUARE_SIZE_PIXELS
    generation["parallel_workers"] = synthesis_workers(generation.get("parallel_workers", 1))
    generation["progress_interval"] = generation["parallel_workers"]
    config.setdefault("phone_automation", {}).setdefault("recognition", {})["image_size"] = INFERENCE_IMAGE_SIZE
    return config


def fixed_studio_settings(settings: dict[str, Any]) -> dict[str, Any]:
    """Keep legacy stored controls from advertising values the app ignores."""
    # Large geographic sequences can contain tens of thousands of waypoints.
    # Copy only the small branches we mutate so every settings request does not
    # duplicate the entire route in memory.
    settings = dict(settings)
    scale = settings.get("viewport_coordinate_scale")
    if isinstance(scale, dict):
        scale = dict(scale)
        settings["viewport_coordinate_scale"] = scale
        scale["viewport_width_m"] = VIEWPORT_WIDTH_M
    overrides = settings.get("config_overrides", {})
    if isinstance(overrides, dict):
        overrides = dict(overrides)
        settings["config_overrides"] = overrides
        generation = overrides.get("generation")
        if isinstance(generation, dict):
            generation = dict(generation)
            overrides["generation"] = generation
            generation["square_size_pixels"] = SQUARE_SIZE_PIXELS
            generation.pop("progress_interval", None)
            if "parallel_workers" in generation:
                generation["parallel_workers"] = synthesis_workers(generation["parallel_workers"])
        phone = overrides.get("phone_automation", {})
        if isinstance(phone, dict):
            phone = dict(phone)
            overrides["phone_automation"] = phone
            recognition = phone.get("recognition")
            if isinstance(recognition, dict):
                recognition = dict(recognition)
                phone["recognition"] = recognition
                recognition["image_size"] = INFERENCE_IMAGE_SIZE
    return settings
