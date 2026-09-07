"""Capture visually distinct frames during a real Pikmin Bloom cold start.

This diagnostic intentionally uses the same scrcpy stream as the application so
short-lived logo and transition screens are not missed by repeated ADB screencaps.
"""

from __future__ import annotations

import argparse
import json
import time
from pathlib import Path

import cv2
import numpy as np

from bloompilot.memory_watchdog import classify_pikmin_restart_screen
from bloompilot.phone_automation import AdbClient
from bloompilot.phone_stream import ScrcpyFrameSource
from bloompilot.phone_tools import find_toolchain, list_adb_devices


PACKAGE_NAME = "com.nianticlabs.pikmin"


def _arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--serial", default="", help="ADB serial; defaults to the only connected device")
    parser.add_argument("--seconds", type=float, default=35.0)
    parser.add_argument("--output", type=Path, default=Path("scratch/restart-state-capture"))
    return parser.parse_args()


def _choose_serial(toolchain, requested: str) -> str:
    if requested:
        return requested
    devices = [device for device in list_adb_devices(toolchain) if device.state == "device"]
    if len(devices) != 1:
        names = ", ".join(device.serial for device in devices) or "none"
        raise SystemExit(f"Expected one connected device, found: {names}")
    return devices[0].serial


def _thumbnail(image: np.ndarray) -> np.ndarray:
    gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
    return cv2.resize(gray, (54, 117), interpolation=cv2.INTER_AREA)


def main() -> int:
    args = _arguments()
    toolchain = find_toolchain()
    serial = _choose_serial(toolchain, args.serial.strip())
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=True)

    adb = AdbClient(toolchain.adb, serial)
    adb.ensure_ready()
    stream = ScrcpyFrameSource(
        toolchain,
        serial,
        max_size=2340,
        max_fps=30,
        status=lambda message: print(message, flush=True),
    )
    manifest: list[dict[str, object]] = []
    saved_images: list[tuple[float, str, np.ndarray]] = []
    previous: np.ndarray | None = None
    last_saved_at = -10.0
    started_at = time.monotonic()

    try:
        adb.run("shell", "input", "keyevent", "KEYCODE_WAKEUP", timeout=8)
        adb.run("shell", "wm", "dismiss-keyguard", timeout=8)
        stream.start(timeout=30.0)
        adb.run("shell", "am", "force-stop", PACKAGE_NAME, timeout=8)
        adb.run(
            "shell",
            "monkey",
            "-p",
            PACKAGE_NAME,
            "-c",
            "android.intent.category.LAUNCHER",
            "1",
            timeout=12,
        )
        started_at = time.monotonic()
        while time.monotonic() - started_at < max(1.0, args.seconds):
            image = stream.capture_image(timeout=3.0)
            elapsed = time.monotonic() - started_at
            thumb = _thumbnail(image)
            difference = 255.0 if previous is None else float(
                np.mean(cv2.absdiff(thumb, previous))
            )
            should_save = (
                previous is None
                or (difference >= 3.0 and elapsed - last_saved_at >= 0.12)
                or elapsed - last_saved_at >= 1.0
            )
            if should_save:
                ok, encoded = cv2.imencode(".png", image)
                if not ok:
                    continue
                state = classify_pikmin_restart_screen(encoded.tobytes())
                name = f"{len(manifest):03d}_{elapsed:06.2f}_{state}.png"
                cv2.imwrite(str(output / name), image)
                manifest.append(
                    {
                        "file": name,
                        "seconds": round(elapsed, 3),
                        "state": state,
                        "difference": round(difference, 3),
                    }
                )
                saved_images.append((elapsed, state, image.copy()))
                previous = thumb
                last_saved_at = elapsed
    finally:
        stream.close()

    (output / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    tiles: list[np.ndarray] = []
    for elapsed, state, image in saved_images:
        tile = cv2.resize(image, (180, 390), interpolation=cv2.INTER_AREA)
        cv2.rectangle(tile, (0, 0), (180, 34), (0, 0, 0), -1)
        cv2.putText(
            tile,
            f"{elapsed:05.2f}s {state}",
            (5, 22),
            cv2.FONT_HERSHEY_SIMPLEX,
            0.43,
            (255, 255, 255),
            1,
            cv2.LINE_AA,
        )
        tiles.append(tile)
    if tiles:
        columns = 6
        blank = np.zeros_like(tiles[0])
        rows = []
        for start in range(0, len(tiles), columns):
            row = tiles[start : start + columns]
            rows.append(cv2.hconcat(row + [blank] * (columns - len(row))))
        cv2.imwrite(str(output / "contact-sheet.png"), cv2.vconcat(rows))

    print(output)
    print(f"saved {len(manifest)} distinct frames")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
