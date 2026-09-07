"""Bounded on-disk diagnostics for unattended desktop runs."""

from __future__ import annotations

import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path

from .paths import PROJECT_ROOT


def configure_diagnostics(directory: Path | None = None) -> Path:
    directory = directory or PROJECT_ROOT / "runtime" / "logs"
    directory.mkdir(parents=True, exist_ok=True)
    path = (directory / "bloompilot.log").resolve()
    logger = logging.getLogger("bloompilot")
    for handler in logger.handlers:
        if isinstance(handler, RotatingFileHandler) and handler.baseFilename == str(path):
            return path
    handler = RotatingFileHandler(path, maxBytes=5 * 1024 * 1024, backupCount=3, encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s [%(threadName)s] %(message)s"))
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)
    logger.info("Desktop diagnostics started")
    return path
