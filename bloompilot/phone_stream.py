"""Low-latency USB screen capture without a Tkinter dependency."""

from __future__ import annotations

import random
import socket
import subprocess
import threading
import time
from typing import Callable, Generic, TypeVar

import cv2
import numpy as np

from .phone_tools import (
    LiveError, Toolchain, _adb_command, _process_kwargs, _run_adb, list_adb_devices,
)

REMOTE_SERVER_PATH = "/data/local/tmp/scrcpy-server-yolo.jar"
T = TypeVar("T")


class LatestSlot(Generic[T]):
    """A one-item mailbox. New data replaces old data instead of building latency."""

    def __init__(self) -> None:
        self._condition = threading.Condition()
        self._value: T | None = None
        self._sequence = 0

    def put(self, value: T) -> int:
        with self._condition:
            self._value = value
            self._sequence += 1
            self._condition.notify_all()
            return self._sequence

    def wait_new(
        self,
        last_sequence: int,
        stop_event: threading.Event,
        timeout: float = 0.25,
    ) -> tuple[int, T | None]:
        with self._condition:
            if self._sequence == last_sequence and not stop_event.is_set():
                self._condition.wait(timeout)
            if self._sequence == last_sequence:
                return last_sequence, None
            return self._sequence, self._value

    def get_new(self, last_sequence: int) -> tuple[int, T | None]:
        with self._condition:
            if self._sequence == last_sequence:
                return last_sequence, None
            return self._sequence, self._value

    def wake(self) -> None:
        with self._condition:
            self._condition.notify_all()


class ScrcpyRawCapture:
    """Use scrcpy-server's raw H.264 mode over an ADB USB tunnel."""

    def __init__(
        self,
        toolchain: Toolchain,
        serial: str,
        max_size: int,
        max_fps: int,
        bit_rate: int,
    ) -> None:
        self.toolchain = toolchain
        self.serial = serial
        self.max_size = max_size
        self.max_fps = max_fps
        self.bit_rate = bit_rate
        self._port: int | None = None
        self._remote_socket_name: str | None = None
        self._socket: socket.socket | None = None
        self._server_process: subprocess.Popen[str] | None = None
        self._server_log: list[str] = []
        self._log_lock = threading.Lock()

    @staticmethod
    def _available_port() -> int:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
            probe.bind(("127.0.0.1", 0))
            return int(probe.getsockname()[1])

    def _drain_server_log(self, stream: object, status: Callable[[str], None]) -> None:
        if not hasattr(stream, "readline"):
            return
        while True:
            line = stream.readline()  # type: ignore[attr-defined]
            if not line:
                break
            cleaned = str(line).strip()
            if cleaned:
                with self._log_lock:
                    self._server_log.append(cleaned)
                    del self._server_log[:-8]
                if "error" in cleaned.casefold() or "warn" in cleaned.casefold():
                    status(cleaned)

    def _last_server_log(self) -> str:
        with self._log_lock:
            return " | ".join(self._server_log[-4:])

    def _connect(self, timeout: float = 8.0) -> socket.socket:
        assert self._port is not None and self._remote_socket_name is not None
        deadline = time.monotonic() + timeout
        last_error: OSError | None = None

        # ADB accepts a forwarded host TCP port even before the Android abstract
        # socket exists. Wait until scrcpy-server is actually listening so the
        # first connection is never a false-positive or closed during encoder
        # initialization.
        while time.monotonic() < deadline:
            if self._server_process and self._server_process.poll() is not None:
                detail = self._last_server_log()
                raise LiveError(f"手機端 scrcpy 服務提前停止。{detail}")
            sockets = _run_adb(
                self.toolchain,
                self.serial,
                "shell",
                "cat",
                "/proc/net/unix",
                timeout=3,
                check=False,
            )
            if self._remote_socket_name in sockets.stdout:
                break
            time.sleep(0.1)
        else:
            detail = self._last_server_log()
            raise LiveError(f"手機端 scrcpy 視訊插座沒有啟動。{detail}")

        while time.monotonic() < deadline:
            try:
                connection = socket.create_connection(("127.0.0.1", self._port), timeout=0.5)
                connection.settimeout(0.5)
                return connection
            except OSError as exc:
                last_error = exc
                time.sleep(0.08)
        detail = self._last_server_log()
        raise LiveError(f"無法連上手機視訊串流：{detail or last_error}")

    def run(
        self,
        on_frame: Callable[[np.ndarray, float], None],
        status: Callable[[str], None],
        stop_event: threading.Event,
    ) -> None:
        try:
            import av
        except ImportError as exc:
            raise LiveError("缺少 PyAV；請執行「01_安裝環境.bat」。") from exc

        device = next(
            (item for item in list_adb_devices(self.toolchain, include_model=False) if item.serial == self.serial),
            None,
        )
        if device is None:
            raise LiveError(f"找不到手機：{self.serial}")
        if device.state != "device":
            hint = "請解鎖手機並允許 USB 偵錯授權。" if device.state == "unauthorized" else "請重新連接 USB。"
            raise LiveError(f"手機狀態為 {device.state}；{hint}")

        status("正在把 scrcpy 視訊服務送到手機…")
        _run_adb(
            self.toolchain,
            self.serial,
            "push",
            str(self.toolchain.server),
            REMOTE_SERVER_PATH,
            timeout=30,
        )
        if stop_event.is_set():
            return

        self._port = self._available_port()
        scid = f"{random.randint(1, 0x7FFFFFFF):08x}"
        self._remote_socket_name = f"scrcpy_{scid}"
        socket_name = f"localabstract:{self._remote_socket_name}"
        _run_adb(
            self.toolchain,
            self.serial,
            "forward",
            f"tcp:{self._port}",
            socket_name,
            timeout=10,
        )
        if stop_event.is_set():
            return

        server_command = _adb_command(
            self.toolchain,
            self.serial,
            "shell",
            f"CLASSPATH={REMOTE_SERVER_PATH}",
            "app_process",
            "/",
            "com.genymobile.scrcpy.Server",
            self.toolchain.version,
            f"scid={scid}",
            "log_level=warn",
            "tunnel_forward=true",
            "audio=false",
            "control=false",
            "cleanup=false",
            "raw_stream=true",
            "video_codec=h264",
            f"video_bit_rate={self.bit_rate}",
            f"max_size={self.max_size}",
            f"max_fps={self.max_fps}",
        )
        self._server_process = subprocess.Popen(
            server_command,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
            **_process_kwargs(),
        )
        assert self._server_process.stdout is not None
        threading.Thread(
            target=self._drain_server_log,
            args=(self._server_process.stdout, status),
            daemon=True,
        ).start()

        status("正在建立低延遲 H.264 USB 串流…")
        self._socket = self._connect()
        codec = av.CodecContext.create("h264", "r")
        codec.options = {"flags": "low_delay"}
        codec.thread_count = 1
        status("USB 畫面已連線")

        while not stop_event.is_set():
            try:
                chunk = self._socket.recv(262_144)
            except socket.timeout:
                continue
            except OSError:
                if stop_event.is_set():
                    break
                raise
            if not chunk:
                if stop_event.is_set():
                    break
                detail = self._last_server_log()
                raise LiveError(f"手機視訊串流已中斷。{detail}")
            for packet in codec.parse(chunk):
                for frame in codec.decode(packet):
                    # The timestamp is taken immediately after decode. The one-slot
                    # pipeline prevents old decoded frames from waiting for YOLO.
                    on_frame(frame.to_ndarray(format="bgr24"), time.perf_counter())

    def stop(self) -> None:
        connection, self._socket = self._socket, None
        if connection is not None:
            try:
                connection.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            connection.close()

        process, self._server_process = self._server_process, None
        if process is not None and process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                process.kill()

        if self._port is not None:
            _run_adb(
                self.toolchain,
                self.serial,
                "forward",
                "--remove",
                f"tcp:{self._port}",
                timeout=5,
                check=False,
            )
            self._port = None
            self._remote_socket_name = None


class ScrcpyFrameSource:
    """Run :class:`ScrcpyRawCapture` in the background and expose fresh frames.

    The decoder continuously replaces a one-item slot, so a slow consumer never
    works through stale video.  Callers can request either the decoded BGR image
    or a locally encoded PNG for compatibility with the older ADB screenshot
    pipeline.
    """

    def __init__(
        self,
        toolchain: Toolchain,
        serial: str,
        *,
        max_size: int = 2340,
        max_fps: int = 15,
        bit_rate: int = 16_000_000,
        status: Callable[[str], None] | None = None,
        cancel_event: threading.Event | None = None,
        fallback_capture_image: Callable[[], np.ndarray] | None = None,
    ) -> None:
        self.capture = ScrcpyRawCapture(
            toolchain,
            serial,
            max_size=max_size,
            max_fps=max_fps,
            bit_rate=bit_rate,
        )
        self.status = status or (lambda _message: None)
        self.cancel_event = cancel_event
        self.fallback_capture_image = fallback_capture_image
        self._using_fallback = False
        self.frames: LatestSlot[tuple[np.ndarray, float]] = LatestSlot()
        self._stop_event = threading.Event()
        self._finished = threading.Event()
        self._thread: threading.Thread | None = None
        self._cancel_thread: threading.Thread | None = None
        self._error: BaseException | None = None
        self._last_sequence = 0
        self._consumer_lock = threading.Lock()

    def _cancelled(self) -> bool:
        return self._stop_event.is_set() or bool(
            self.cancel_event is not None and self.cancel_event.is_set()
        )

    def _run(self) -> None:
        try:
            self.capture.run(
                lambda image, received_at: self.frames.put((image, received_at)),
                self.status,
                self._stop_event,
            )
        except BaseException as exc:  # surfaced synchronously by start/capture_image
            self._error = exc
        finally:
            self._finished.set()
            self.frames.wake()

    def start(self, *, timeout: float = 12.0) -> None:
        if self._thread is not None:
            return
        self._thread = threading.Thread(
            target=self._run,
            name="bloompilot-scrcpy-frames",
            daemon=True,
        )
        self._thread.start()
        if self.cancel_event is not None:
            def stop_when_cancelled() -> None:
                while not self._stop_event.wait(0.25):
                    if self.cancel_event.is_set():
                        self._stop_event.set()
                        self.frames.wake()
                        self.capture.stop()
                        return

            self._cancel_thread = threading.Thread(
                target=stop_when_cancelled,
                name="bloompilot-scrcpy-cancel",
                daemon=True,
            )
            self._cancel_thread.start()
        deadline = time.monotonic() + max(0.1, float(timeout))
        while not self._cancelled():
            sequence, item = self.frames.wait_new(
                0,
                self._stop_event,
                timeout=min(0.25, max(0.0, deadline - time.monotonic())),
            )
            if item is not None and sequence > 0:
                return
            if self._error is not None:
                raise LiveError(f"scrcpy 畫面串流啟動失敗：{self._error}") from self._error
            if self._finished.is_set():
                raise LiveError("scrcpy 畫面串流在第一張影格前停止。")
            if time.monotonic() >= deadline:
                raise LiveError("等待 scrcpy 第一張畫面逾時。")
        raise LiveError("scrcpy 畫面串流已取消。")

    def capture_image(self, *, timeout: float = 3.0) -> np.ndarray:
        """Return a fresh frame, using a new screenshot if video stalls.

        Keep the decoder running during fallback so video can recover. After
        the first stall, probe it briefly instead of adding the full timeout
        to every screenshot. Never reuse the last frame to hide a timeout.
        """
        with self._consumer_lock:
            try:
                image = self._capture_stream_image(
                    timeout=min(timeout, 0.1) if self._using_fallback else timeout,
                )
            except LiveError as exc:
                if self.fallback_capture_image is None or self._cancelled():
                    raise
                if not self._using_fallback:
                    self.status(f"scrcpy 畫面暫時無法取得，改用 ADB 即時截圖繼續辨識：{exc}")
                self._using_fallback = True
                image = self.fallback_capture_image()
                if self._cancelled():
                    raise LiveError("scrcpy 畫面串流已取消。")
                # Any video received during the screenshot may predate it.
                # Resume only with a later video frame on the next call.
                self._last_sequence, _ = self.frames.get_new(self._last_sequence)
                return image
            if self._using_fallback:
                self._using_fallback = False
                self.status("scrcpy 新畫面已恢復，繼續使用低延遲串流辨識。")
            return image

    def _capture_stream_image(self, *, timeout: float) -> np.ndarray:
        deadline = time.monotonic() + max(0.1, float(timeout))
        while not self._cancelled():
            if self._error is not None:
                raise LiveError(f"scrcpy 畫面串流已中斷：{self._error}") from self._error
            if self._finished.is_set():
                raise LiveError("scrcpy 畫面串流已停止。")
            sequence, item = self.frames.wait_new(
                self._last_sequence,
                self._stop_event,
                timeout=min(0.25, max(0.0, deadline - time.monotonic())),
            )
            if self._cancelled():
                break
            if item is not None:
                self._last_sequence = sequence
                return item[0]
            if time.monotonic() >= deadline:
                raise LiveError("等待 scrcpy 新畫面逾時。")
        raise LiveError("scrcpy 畫面串流已取消。")

    def capture_png(self, *, timeout: float = 3.0) -> bytes:
        image = self.capture_image(timeout=timeout)
        ok, encoded = cv2.imencode(".png", image)
        if not ok:
            raise LiveError("無法將 scrcpy 畫面編碼為 PNG。")
        return encoded.tobytes()

    def close(self) -> None:
        self._stop_event.set()
        self.frames.wake()
        self.capture.stop()
        thread, self._thread = self._thread, None
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=3.0)
        cancel_thread, self._cancel_thread = self._cancel_thread, None
        if cancel_thread is not None and cancel_thread is not threading.current_thread():
            cancel_thread.join(timeout=0.5)
