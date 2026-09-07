"""Canonical project paths shared by the GUI and worker processes."""

from pathlib import Path


PACKAGE_ROOT = Path(__file__).resolve().parent
PROJECT_ROOT = PACKAGE_ROOT.parent

CONFIG_PATH = PROJECT_ROOT / "config.yaml"
DATA_DIR = PROJECT_ROOT / "data"
SOURCE_DATA_DIR = DATA_DIR / "source"
BACKGROUND_SOURCE_DIR = SOURCE_DATA_DIR / "backgrounds"
GENERATED_DATASETS_DIR = DATA_DIR / "generated"
PRETRAINED_MODELS_DIR = PROJECT_ROOT / "models" / "pretrained"
TRAINING_RUNS_DIR = PROJECT_ROOT / "outputs" / "training"
RECOGNITION_RUNS_DIR = PROJECT_ROOT / "outputs" / "recognition"
TRAINING_CONTROL_DIR = PROJECT_ROOT / "runtime" / "training_control"
PHONE_AUTOMATION_SETTINGS_PATH = PROJECT_ROOT / "runtime" / "phone_automation" / "settings.json"
DEVICE_SCALE_CALIBRATIONS_PATH = PROJECT_ROOT / "runtime" / "phone_automation" / "device_scales.json"
ARTIFACT_CATALOG_PATH = PROJECT_ROOT / "runtime" / "artifacts" / "catalog.json"
SCRCPY_DIR = PROJECT_ROOT / "vendor" / "scrcpy"
ROUTES_DIR = PROJECT_ROOT / "routes"
MONTHLY_ACTIVITY_DIR = DATA_DIR / "monthly_activity"
MONTHLY_ACTIVITY_STATE_PATH = PROJECT_ROOT / "runtime" / "monthly_activity" / "state.json"
RESIDENT_MODELS_DIR = PROJECT_ROOT / "models" / "resident"
