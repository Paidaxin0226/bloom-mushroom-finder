"""ADB/scrcpy discovery and device enumeration, independent of desktop UI."""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import threading
import time
from dataclasses import dataclass
from pathlib import Path

from .paths import SCRCPY_DIR

CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


class LiveError(RuntimeError):
    """An error that can be presented directly to the user."""


@dataclass(frozen=True)
class Toolchain:
    scrcpy: Path
    adb: Path
    server: Path
    version: str


@dataclass(frozen=True)
class AdbDevice:
    serial: str
    state: str
    model: str = ""


def _process_kwargs() -> dict[str, int]:
    return {"creationflags": CREATE_NO_WINDOW} if os.name == "nt" else {}


def _known_scrcpy_candidates() -> list[Path]:
    candidates: list[Path] = []
    configured = os.environ.get("SCRCPY_DIR")
    if configured:
        candidates.append(Path(configured) / ("scrcpy.exe" if os.name == "nt" else "scrcpy"))

    executable = shutil.which("scrcpy")
    if executable:
        candidates.append(Path(executable))

    executable_name = "scrcpy.exe" if os.name == "nt" else "scrcpy"
    candidates.extend(
        [
            SCRCPY_DIR / executable_name,
        ]
    )

    if os.name == "nt":
        local_app_data = os.environ.get("LOCALAPPDATA")
        if local_app_data:
            local_root = Path(local_app_data)
            candidates.append(local_root / "Microsoft" / "WinGet" / "Links" / "scrcpy.exe")
            packages = local_root / "Microsoft" / "WinGet" / "Packages"
            if packages.is_dir():
                try:
                    for package in packages.glob("Genymobile.scrcpy_*"):
                        candidates.extend(package.rglob("scrcpy.exe"))
                except OSError:
                    pass

    unique: list[Path] = []
    seen: set[str] = set()
    for candidate in candidates:
        key = str(candidate).casefold()
        if key not in seen:
            seen.add(key)
            unique.append(candidate)
    return unique


def _scrcpy_version(scrcpy: Path) -> str:
    result = subprocess.run(
        [str(scrcpy), "--version"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=10,
        **_process_kwargs(),
    )
    output = f"{result.stdout}\n{result.stderr}"
    match = re.search(r"(?mi)^scrcpy\s+([^\s<]+)", output)
    if result.returncode != 0 or not match:
        raise LiveError(f"無法讀取 scrcpy 版本：{output.strip() or scrcpy}")
    return match.group(1)


def find_toolchain() -> Toolchain:
    for scrcpy in _known_scrcpy_candidates():
        if not scrcpy.is_file():
            continue
        scrcpy = scrcpy.resolve()
        parent = scrcpy.parent
        adb_name = "adb.exe" if os.name == "nt" else "adb"
        adb = parent / adb_name
        if not adb.is_file():
            found_adb = shutil.which("adb")
            if not found_adb:
                continue
            adb = Path(found_adb)

        server_candidates = [
            parent / "scrcpy-server",
            parent / "scrcpy-server.jar",
        ]
        server_candidates.extend(sorted(parent.glob("scrcpy-server-v*"), reverse=True))
        server = next((path for path in server_candidates if path.is_file()), None)
        if server is None:
            continue
        return Toolchain(
            scrcpy=scrcpy.resolve(),
            adb=adb.resolve(),
            server=server.resolve(),
            version=_scrcpy_version(scrcpy),
        )

    raise LiveError(
        "找不到完整的 scrcpy（需要 scrcpy、adb、scrcpy-server）。"
        "請先執行「01_安裝環境.bat」。"
    )


class ScrcpyControlWindow:
    """Own one interactive scrcpy window for desktop mouse control."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._process: subprocess.Popen[bytes] | None = None
        self._serial = ""

    def _clear_finished_locked(self) -> None:
        if self._process is not None and self._process.poll() is not None:
            self._process = None
            self._serial = ""

    def status(self) -> dict[str, object]:
        with self._lock:
            self._clear_finished_locked()
            return {
                "active": self._process is not None,
                "serial": self._serial or None,
                "pid": self._process.pid if self._process is not None else None,
            }

    def open(self, serial: str) -> dict[str, object]:
        serial = str(serial).strip()
        if not serial:
            raise LiveError("沒有選擇 Android 手機。")

        with self._lock:
            self._clear_finished_locked()
            if self._process is not None and self._serial == serial:
                return self.status()
            self._close_locked()

            toolchain = find_toolchain()
            command = [
                str(toolchain.scrcpy),
                "--serial", serial,
                "--no-audio",
                "--max-size=1200",
                "--max-fps=30",
                "--video-bit-rate=8M",
                "--stay-awake",
                "--window-title=Bloom Mushroom Finder 手機滑鼠控制",
            ]
            try:
                process = subprocess.Popen(
                    command,
                    cwd=toolchain.scrcpy.parent,
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    **_process_kwargs(),
                )
            except OSError as exc:
                raise LiveError(f"無法開啟手機滑鼠控制：{exc}") from exc

            # Catch invalid command lines and immediate USB failures without
            # delaying the pause response long enough to make the UI feel stuck.
            time.sleep(0.25)
            if process.poll() is not None:
                raise LiveError("手機滑鼠控制視窗未能啟動；請確認 USB 偵錯連線仍有效。")
            self._process = process
            self._serial = serial
            return self.status()

    def _close_locked(self) -> None:
        process = self._process
        self._process = None
        self._serial = ""
        if process is None or process.poll() is not None:
            return
        process.terminate()
        try:
            process.wait(timeout=2.0)
        except subprocess.TimeoutExpired:
            process.kill()
            try:
                process.wait(timeout=1.0)
            except subprocess.TimeoutExpired:
                pass

    def close(self) -> dict[str, object]:
        with self._lock:
            self._close_locked()
            return self.status()


def _adb_command(toolchain: Toolchain, serial: str | None, *arguments: str) -> list[str]:
    command = [str(toolchain.adb)]
    if serial:
        command.extend(["-s", serial])
    command.extend(arguments)
    return command


def _run_adb(
    toolchain: Toolchain,
    serial: str | None,
    *arguments: str,
    timeout: float = 20,
    check: bool = True,
) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(
        _adb_command(toolchain, serial, *arguments),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
        **_process_kwargs(),
    )
    if check and result.returncode != 0:
        detail = (result.stderr or result.stdout).strip()
        raise LiveError(f"ADB 執行失敗：{detail or '未知錯誤'}")
    return result


def list_adb_devices(toolchain: Toolchain, include_model: bool = True) -> list[AdbDevice]:
    _run_adb(toolchain, None, "start-server", timeout=15)
    result = _run_adb(toolchain, None, "devices", timeout=10)
    devices: list[AdbDevice] = []
    for line in result.stdout.splitlines()[1:]:
        fields = line.strip().split()
        if len(fields) < 2:
            continue
        serial, state = fields[0], fields[1]
        model = ""
        if include_model and state == "device":
            model_result = _run_adb(
                toolchain,
                serial,
                "shell",
                "getprop",
                "ro.product.model",
                timeout=5,
                check=False,
            )
            model = model_result.stdout.strip()
        devices.append(AdbDevice(serial=serial, state=state, model=model))
    return devices
