from __future__ import annotations

import json
import logging
import math
import os
import socket
import subprocess
import threading
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

PROTOCOL_VERSION = 1
USB_SOCKET_NAME = "bloompilot_control_v1"
USB_SERVICE_COMPONENT = "com.locwarp.mock/.UsbControlService"
USB_SERVICE_ACTION = "com.locwarp.mock.USB_CONTROL_START"
LOCWARP_PACKAGE = "com.locwarp.mock"
# LocWarp portable backups are bounded to 4 MiB. Keep a little protocol
# headroom so they can be imported/exported through the same local USB tunnel.
MAX_MESSAGE_BYTES = 5 * 1024 * 1024
CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
LOGGER = logging.getLogger(__name__)


class DeviceControlError(RuntimeError):
    """A USB bridge or device command error that is safe to show in the GUI."""


@dataclass(frozen=True)
class DeviceResponse:
    ok: bool
    message: str
    status: dict[str, Any]
    raw: dict[str, Any]


def _request_id() -> str:
    return uuid.uuid4().hex


def _process_kwargs() -> dict[str, int]:
    return {"creationflags": CREATE_NO_WINDOW} if os.name == "nt" else {}


def _remaining_seconds(deadline: float) -> float:
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise DeviceControlError("USB 控制等候逾時。")
    return remaining


@contextmanager
def _locked_until(lock: Any, deadline: float | None):
    acquired = lock.acquire() if deadline is None else lock.acquire(timeout=_remaining_seconds(deadline))
    if not acquired:
        raise DeviceControlError("等候 USB 控制通道逾時。")
    try:
        yield
    finally:
        lock.release()


class JsonLineTransport:
    """Synchronous, bounded JSON-lines transport used over the ADB tunnel."""

    def __init__(self, connection: socket.socket, *, max_bytes: int = MAX_MESSAGE_BYTES) -> None:
        self._connection = connection
        self._max_bytes = max_bytes
        self._buffer = bytearray()
        self._lock = threading.Lock()
        self._closed = False

    @property
    def closed(self) -> bool:
        return self._closed

    def request(self, payload: dict[str, Any], *, deadline: float | None = None) -> dict[str, Any]:
        request_id = str(payload.get("id", ""))
        encoded = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode(
            "utf-8"
        )
        if len(encoded) > self._max_bytes:
            raise DeviceControlError("USB 控制指令超過大小限制。")
        with _locked_until(self._lock, deadline):
            if self._closed:
                raise DeviceControlError("USB 控制連線已關閉。")
            original_timeout = self._connection.gettimeout()
            try:
                if deadline is not None:
                    self._connection.settimeout(_remaining_seconds(deadline))
                self._connection.sendall(encoded + b"\n")
                response = self._read_message(deadline=deadline)
                if str(response.get("id", "")) != request_id:
                    raise DeviceControlError("USB 控制回應的 request id 不一致。")
                if response.get("v") != PROTOCOL_VERSION or response.get("type") != "result":
                    raise DeviceControlError("Android 回傳了不支援的控制協定。")
            except (OSError, TimeoutError) as exc:
                # A late response cannot safely be reused by another request.
                self.close()
                raise DeviceControlError(f"USB 控制連線中斷：{exc}") from exc
            except DeviceControlError:
                self.close()
                raise
            finally:
                if deadline is not None and not self._closed:
                    self._connection.settimeout(original_timeout)
        return response

    def _read_message(self, *, deadline: float | None = None) -> dict[str, Any]:
        while True:
            # A slow stream of partial JSON must not restart the timeout.
            if deadline is not None:
                self._connection.settimeout(_remaining_seconds(deadline))
            newline = self._buffer.find(b"\n")
            if newline >= 0:
                raw = bytes(self._buffer[:newline])
                del self._buffer[: newline + 1]
                if not raw:
                    continue
                try:
                    value = json.loads(raw.decode("utf-8"))
                except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                    raise DeviceControlError("Android 回傳了無效的 JSON。") from exc
                if not isinstance(value, dict):
                    raise DeviceControlError("Android 控制回應必須是 JSON object。")
                return value
            if len(self._buffer) >= self._max_bytes:
                raise DeviceControlError("Android 控制回應超過大小限制。")
            chunk = self._connection.recv(min(4096, self._max_bytes - len(self._buffer)))
            if not chunk:
                raise DeviceControlError("Android 已關閉 USB 控制連線。")
            self._buffer.extend(chunk)

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            self._connection.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        self._connection.close()


class AdbForward:
    """Owns one dynamically allocated ADB TCP -> Android localabstract tunnel."""

    def __init__(self, adb: Path, serial: str, socket_name: str = USB_SOCKET_NAME) -> None:
        self.adb = Path(adb)
        self.serial = serial.strip()
        self.socket_name = socket_name
        self.local_port: int | None = None

    def start_service(self, *, timeout: float = 12.0) -> None:
        if not self.adb.is_file():
            raise DeviceControlError(f"找不到 ADB：{self.adb}")
        if not self.serial:
            raise DeviceControlError("沒有選擇 Android 手機。")
        result = subprocess.run(
            [
                str(self.adb),
                "-s",
                self.serial,
                "shell",
                "am",
                "start-foreground-service",
                "-n",
                USB_SERVICE_COMPONENT,
                "-a",
                USB_SERVICE_ACTION,
            ],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            **_process_kwargs(),
        )
        detail = "\n".join(
            value.strip()
            for value in (str(result.stdout or ""), str(result.stderr or ""))
            if value.strip()
        )
        if result.returncode != 0 or any(
            line.lstrip().startswith(("Error:", "Security exception:"))
            for line in detail.splitlines()
        ):
            raise DeviceControlError(
                f"無法透過已授權的 ADB 啟動 USB 控制：{detail or '未知錯誤'}"
            )

    def open(self, *, timeout: float = 12.0) -> int:
        if not self.adb.is_file():
            raise DeviceControlError(f"找不到 ADB：{self.adb}")
        if not self.serial:
            raise DeviceControlError("沒有選擇 Android 手機。")
        result = subprocess.run(
            [
                str(self.adb),
                "-s",
                self.serial,
                "forward",
                "tcp:0",
                f"localabstract:{self.socket_name}",
            ],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            **_process_kwargs(),
        )
        output = str(result.stdout or "").strip()
        if result.returncode != 0 or not output.isdigit():
            detail = str(result.stderr or output).strip()
            raise DeviceControlError(f"無法建立 ADB 控制通道：{detail or '未知錯誤'}")
        self.local_port = int(output)
        return self.local_port

    def close(self, *, timeout: float = 8.0) -> None:
        port = self.local_port
        self.local_port = None
        if port is None or not self.adb.is_file() or not self.serial:
            return
        try:
            self._remove(port, timeout)
        except (OSError, subprocess.TimeoutExpired):
            LOGGER.warning("ADB forward cleanup failed: serial=%s port=%s", self.serial, port)

    def _remove(self, port: int, timeout: float) -> None:
        subprocess.run(
            [
                str(self.adb),
                "-s",
                self.serial,
                "forward",
                "--remove",
                f"tcp:{port}",
            ],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            check=False,
            **_process_kwargs(),
        )


class UsbDeviceController:
    """High-level client for Android's user-enabled USB command service."""

    def __init__(
        self,
        adb: Path,
        serial: str,
        *,
        timeout: float = 45.0,
        socket_factory: Callable[..., socket.socket] = socket.create_connection,
    ) -> None:
        self.adb = Path(adb)
        self.serial = serial.strip()
        self.timeout = timeout
        self._socket_factory = socket_factory
        self._lifecycle_lock = threading.RLock()
        self._forward: AdbForward | None = None
        self._transport: JsonLineTransport | None = None
        self._last_response_at: float | None = None
        self.connection_generation = 0

    @property
    def connected(self) -> bool:
        with self._lifecycle_lock:
            return self._transport is not None and not self._transport.closed

    def connect(self, *, timeout: float = 30.0, only_if_disconnected: bool = False) -> DeviceResponse:
        if not math.isfinite(timeout) or timeout <= 0:
            raise DeviceControlError("USB 控制等候時間必須大於 0。")
        deadline = time.monotonic() + timeout
        with _locked_until(self._lifecycle_lock, deadline):
            # A UI poll and a scan can discover the same outage. Reuse a
            # connection already recovered by the other caller.
            if only_if_disconnected and self.connected:
                return self.command("status", timeout=_remaining_seconds(deadline))
            self.close(forward_timeout=min(1.0, _remaining_seconds(deadline)))
            forward = AdbForward(self.adb, self.serial)
            transport: JsonLineTransport | None = None
            try:
                forward.start_service(timeout=min(12.0, _remaining_seconds(deadline)))
                port = forward.open(timeout=min(12.0, _remaining_seconds(deadline)))
                connection = self._socket_factory(
                    ("127.0.0.1", port), timeout=min(8.0, _remaining_seconds(deadline))
                )
                connection.settimeout(self.timeout)
                transport = JsonLineTransport(connection)
                raw = transport.request(
                    {
                        "v": PROTOCOL_VERSION,
                        "type": "pair",
                        "id": _request_id(),
                        "client": "BloomPilot Control Center",
                    },
                    deadline=deadline,
                )
                response = self._response(raw)
                if not response.ok:
                    raise DeviceControlError(response.message or "ADB USB 連線失敗。")
                self._forward = forward
                self._transport = transport
                self._last_response_at = time.monotonic()
                self.connection_generation += 1
                LOGGER.info("USB connected: serial=%s generation=%s", self.serial, self.connection_generation)
                return response
            except Exception:
                LOGGER.warning("USB connect failed: serial=%s", self.serial, exc_info=True)
                if transport is not None:
                    transport.close()
                # Cleanup must not add the normal eight-second ADB timeout to
                # a failed attempt, but 100 ms is too short on a busy Windows
                # ADB server and leaves stale forwards behind.
                forward.close(timeout=1.0)
                raise

    def command(
        self,
        command: str,
        payload: dict[str, Any] | None = None,
        *,
        require_ok: bool = True,
        timeout: float | None = None,
    ) -> DeviceResponse:
        """Execute a command, optionally bounding lock waits and all socket I/O."""
        if timeout is not None and (not math.isfinite(timeout) or timeout <= 0):
            raise DeviceControlError("USB 控制等候時間必須大於 0。")
        deadline = None if timeout is None else time.monotonic() + timeout
        with _locked_until(self._lifecycle_lock, deadline):
            transport = self._transport
            if transport is None:
                raise DeviceControlError("尚未連接 Android USB 控制。")
            command_name = str(command or "").strip()
            if not command_name or len(command_name) > 80:
                raise DeviceControlError("裝置控制指令名稱無效。")
            try:
                raw = transport.request(
                    {
                        "v": PROTOCOL_VERSION,
                        "type": "command",
                        "id": _request_id(),
                        "command": command_name,
                        "payload": dict(payload or {}),
                    },
                    deadline=deadline,
                )
            except DeviceControlError:
                LOGGER.warning("USB command failed: serial=%s command=%s", self.serial, command_name, exc_info=True)
                raise
            # Even a rejected command proves that the USB channel is alive.
            self._last_response_at = time.monotonic()
            response = self._response(raw)
            if require_ok and not response.ok:
                raise DeviceControlError(response.message or "Android 拒絕了控制指令。")
            return response

    def status(self) -> dict[str, Any]:
        return self.command("status").status

    def keepalive(self, *, idle_seconds: float, timeout: float) -> bool:
        """Probe an idle channel; foreground traffic already keeps it alive.

        Return False when busy, so callers do not mistake a skipped probe for
        recovery. Never queue a short heartbeat behind a foreground command:
        its depleted deadline could close an otherwise healthy shared socket.
        """
        if not self._lifecycle_lock.acquire(blocking=False):
            return False
        try:
            if (
                self._transport is not None
                and not self._transport.closed
                and self._last_response_at is not None
                and time.monotonic() - self._last_response_at < idle_seconds
            ):
                return True
            # Heartbeats must remain cheap while the phone is busy rendering
            # Pikmin Bloom.  A full status response serializes saved routes,
            # bookmarks, and service state, which can exceed the heartbeat
            # deadline and tear down an otherwise healthy control channel.
            self.command("ping", require_ok=False, timeout=timeout)
            return True
        finally:
            self._lifecycle_lock.release()

    def close(self, *, forward_timeout: float = 8.0) -> None:
        with self._lifecycle_lock:
            transport = self._transport
            forward = self._forward
            self._transport = None
            self._forward = None
            self._last_response_at = None
            if transport is not None:
                transport.close()
            if forward is not None:
                forward.close(timeout=forward_timeout)

    def _response(self, raw: dict[str, Any]) -> DeviceResponse:
        status = raw.get("status")
        return DeviceResponse(
            ok=bool(raw.get("ok", False)),
            message=str(raw.get("message", "")),
            status=dict(status) if isinstance(status, dict) else {},
            raw=raw,
        )

    def __enter__(self) -> "UsbDeviceController":
        return self

    def __exit__(self, *_args: object) -> None:
        self.close()


def reconnect_usb(
    controller: UsbDeviceController,
    *,
    stop_event: threading.Event | None = None,
    status: Callable[[str], None] | None = None,
) -> DeviceResponse:
    """Retry service startup with backoff; never replay a device command here."""
    stop = stop_event or threading.Event()
    last_error: Exception | None = None
    for attempt, delay in enumerate((0.0, 2.0, 4.0), start=1):
        if stop.is_set() or stop.wait(delay):
            raise DeviceControlError("USB 控制恢復已取消。")
        message = f"USB 控制短暫中斷，正在自動重新連接（{attempt}/3）…"
        LOGGER.info(message)
        if status is not None:
            status(message)
        try:
            return controller.connect(timeout=8.0, only_if_disconnected=True)
        except (DeviceControlError, OSError, subprocess.TimeoutExpired) as exc:
            last_error = exc
            LOGGER.warning("USB reconnect attempt %s failed: %s", attempt, exc)
    raise DeviceControlError(f"USB 控制服務連續 3 次恢復失敗：{last_error}") from last_error


def restart_locwarp(
    controller: UsbDeviceController,
    *,
    stop_event: threading.Event | None = None,
    status: Callable[[str], None] | None = None,
) -> DeviceResponse:
    """Restart LocWarp itself, then rebuild its USB service connection."""
    stop = stop_event or threading.Event()
    if stop.is_set():
        raise DeviceControlError("LocWarp 重新啟動已取消。")
    message = "手機記憶體偏低，正在重新啟動 LocWarp 並恢復 USB 控制…"
    LOGGER.info(message)
    if status is not None:
        status(message)
    controller.close(forward_timeout=1.0)
    try:
        result = subprocess.run(
            [
                str(controller.adb), "-s", controller.serial,
                "shell", "am", "force-stop", LOCWARP_PACKAGE,
            ],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=8,
            **_process_kwargs(),
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise DeviceControlError(f"無法重新啟動 LocWarp：{exc}") from exc
    if result.returncode != 0:
        detail = str(result.stderr or result.stdout).strip()
        raise DeviceControlError(f"無法停止 LocWarp：{detail or '未知錯誤'}")
    if stop.wait(0.75):
        raise DeviceControlError("LocWarp 重新啟動已取消。")
    response = reconnect_usb(controller, stop_event=stop, status=status)
    if status is not None:
        status("LocWarp 與 USB 控制已重新啟動，正在恢復目前節點。")
    return response
