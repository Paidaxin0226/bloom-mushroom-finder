from __future__ import annotations

import argparse
import sys
import threading
import time
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from bloompilot.phone_stream import ScrcpyFrameSource
from bloompilot.phone_tools import find_toolchain, list_adb_devices


def main() -> int:
    parser = argparse.ArgumentParser(description="Capture a few Android USB frames without opening the GUI")
    parser.add_argument("--frames", type=int, default=3)
    parser.add_argument("--timeout", type=float, default=15.0)
    args = parser.parse_args()

    toolchain = find_toolchain()
    devices = [device for device in list_adb_devices(toolchain) if device.state == "device"]
    if not devices:
        print("No authorized Android device was found.", file=sys.stderr)
        return 1

    stop_event = threading.Event()
    source = ScrcpyFrameSource(
        toolchain,
        devices[0].serial,
        status=lambda text: print(text, flush=True),
        cancel_event=stop_event,
    )
    started = time.perf_counter()
    shapes: list[tuple[int, ...]] = []
    try:
        source.start(timeout=args.timeout)
        startup_ms = (time.perf_counter() - started) * 1000
        capture_started = time.perf_counter()
        for _ in range(args.frames):
            image = source.capture_image(timeout=args.timeout)
            shapes.append(tuple(image.shape))
    finally:
        source.close()

    capture_seconds = time.perf_counter() - capture_started
    if len(shapes) < args.frames:
        print(f"Only received {len(shapes)} frame(s).", file=sys.stderr)
        return 1
    fps = len(shapes) / max(0.001, capture_seconds)
    print(
        f"USB stream OK: {len(shapes)} frames, {shapes[-1]}, "
        f"startup {startup_ms:.0f} ms, capture {fps:.1f} fps"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
