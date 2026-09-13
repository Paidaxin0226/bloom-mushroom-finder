from __future__ import annotations

import json
import socket
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from bloompilot.device_control import (
    DeviceControlError,
    DeviceResponse,
    JsonLineTransport,
    UsbDeviceController,
    reconnect_usb,
    restart_locwarp,
)


class JsonLineTransportTests(unittest.TestCase):
    def test_round_trip_validates_protocol_and_request_id(self) -> None:
        client, server = socket.socketpair()
        client.settimeout(2)
        server.settimeout(2)

        def worker() -> None:
            request = json.loads(server.recv(4096).split(b"\n", 1)[0].decode("utf-8"))
            response = {
                "v": 1,
                "type": "result",
                "id": request["id"],
                "ok": True,
                "message": "paired",
                "status": {"running": False},
            }
            server.sendall(json.dumps(response).encode("utf-8") + b"\n")
            server.close()

        thread = threading.Thread(target=worker)
        thread.start()
        transport = JsonLineTransport(client)
        response = transport.request(
            {"v": 1, "type": "pair", "id": "abc", "client": "test"}
        )
        transport.close()
        thread.join(timeout=2)
        self.assertTrue(response["ok"])
        self.assertEqual(response["status"], {"running": False})

    def test_usb_controller_handshakes_and_executes_status_over_forwarded_socket(self) -> None:
        listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        listener.bind(("127.0.0.1", 0))
        listener.listen(1)
        port = listener.getsockname()[1]

        def read_request(stream: socket.SocketIO) -> dict[str, object]:
            return json.loads(stream.readline().decode("utf-8"))

        def write_result(stream: socket.SocketIO, request: dict[str, object]) -> None:
            payload = {
                "v": 1,
                "type": "result",
                "id": request["id"],
                "ok": True,
                "message": "ok",
                "status": {"current": {"lat": 25.0, "lng": 121.0}, "running": False},
            }
            stream.write(json.dumps(payload).encode("utf-8") + b"\n")
            stream.flush()

        def worker() -> None:
            connection, _ = listener.accept()
            with connection, connection.makefile("rwb", buffering=0) as stream:
                handshake = read_request(stream)
                self.assertNotIn("pin", handshake)
                write_result(stream, handshake)
                status = read_request(stream)
                self.assertEqual(status["command"], "status")
                write_result(stream, status)
            listener.close()

        class FakeForward:
            def __init__(self, *_args: object, **_kwargs: object) -> None:
                self.closed = False
                self.started = False

            def start_service(self, *, timeout: float = 12) -> None:
                self.started = True

            def open(self, *, timeout: float = 12) -> int:
                return port

            def close(self, *, timeout: float = 8) -> None:
                self.closed = True

        thread = threading.Thread(target=worker)
        thread.start()
        with patch("bloompilot.device_control.AdbForward", FakeForward):
            controller = UsbDeviceController(Path("adb"), "serial")
            response = controller.connect()
            self.assertTrue(response.ok)
            self.assertEqual(controller.connection_generation, 1)
            self.assertEqual(controller.command("status", timeout=0.5).status["current"]["lat"], 25.0)
            self.assertEqual(controller._transport._connection.gettimeout(), 45.0)
            controller.close()
        thread.join(timeout=2)

    def test_recovery_retries_service_startup_without_replaying_commands(self) -> None:
        controller = Mock()
        controller.connect.side_effect = [OSError("service died"), DeviceControlError("not ready"), "paired"]
        stop = Mock()
        stop.is_set.return_value = False
        stop.wait.return_value = False
        self.assertEqual(reconnect_usb(controller, stop_event=stop), "paired")
        self.assertEqual(controller.connect.call_count, 3)
        controller.connect.assert_called_with(timeout=8.0, only_if_disconnected=True)
        self.assertEqual([call.args[0] for call in stop.wait.call_args_list], [0.0, 2.0, 4.0])
        controller.command.assert_not_called()

    def test_low_memory_restart_force_stops_locwarp_then_reconnects(self) -> None:
        controller = Mock(adb=Path("adb"), serial="serial")
        expected = DeviceResponse(True, "paired", {}, {})
        stop = Mock()
        stop.is_set.return_value = False
        stop.wait.return_value = False
        completed = Mock(returncode=0, stdout="", stderr="")
        with (
            patch("bloompilot.device_control.subprocess.run", return_value=completed) as run,
            patch("bloompilot.device_control.reconnect_usb", return_value=expected) as reconnect,
        ):
            response = restart_locwarp(controller, stop_event=stop)
        self.assertIs(response, expected)
        controller.close.assert_called_once_with(forward_timeout=1.0)
        self.assertEqual(
            run.call_args.args[0][-4:],
            ["shell", "am", "force-stop", "com.locwarp.mock"],
        )
        reconnect.assert_called_once_with(controller, stop_event=stop, status=None)
        stop.wait.assert_called_once_with(0.75)

    def test_recovery_stops_retrying_when_cancelled_during_backoff(self) -> None:
        controller = Mock()
        controller.connect.side_effect = OSError("service died")
        stop = Mock()
        stop.is_set.return_value = False
        stop.wait.side_effect = [False, True]
        with self.assertRaisesRegex(DeviceControlError, "已取消"):
            reconnect_usb(controller, stop_event=stop)
        self.assertEqual(controller.connect.call_count, 1)

    def test_failed_recovery_has_a_finite_attempt_limit(self) -> None:
        controller = Mock()
        controller.connect.side_effect = OSError("service died")
        stop = Mock()
        stop.is_set.return_value = False
        stop.wait.return_value = False
        with self.assertRaisesRegex(DeviceControlError, "連續 3 次恢復失敗"):
            reconnect_usb(controller, stop_event=stop)
        self.assertEqual(controller.connect.call_count, 3)

    def test_recovery_does_not_replace_an_already_recovered_socket(self) -> None:
        client, server = socket.socketpair()
        controller = UsbDeviceController(Path("adb"), "serial")
        controller._transport = JsonLineTransport(client)
        try:
            with patch.object(controller, "command", return_value="alive") as command, patch("bloompilot.device_control.AdbForward") as forward:
                self.assertEqual(controller.connect(timeout=0.1, only_if_disconnected=True), "alive")
                forward.assert_not_called()
                command.assert_called_once()
                self.assertTrue(controller.connected)
        finally:
            controller.close()
            server.close()

    def test_connect_handshake_is_bounded_even_with_silent_android(self) -> None:
        client, server = socket.socketpair()
        forward = Mock()
        forward.open.return_value = 12345
        controller = UsbDeviceController(Path("adb"), "serial", socket_factory=lambda *_args, **_kwargs: client)
        started = time.monotonic()
        try:
            with patch("bloompilot.device_control.AdbForward", return_value=forward):
                with self.assertRaisesRegex(DeviceControlError, "連線中斷|逾時"):
                    controller.connect(timeout=0.05)
            self.assertLess(time.monotonic() - started, 0.5)
            self.assertFalse(controller.connected)
            self.assertEqual(client.fileno(), -1)
            forward.close.assert_called_once()
        finally:
            controller.close()
            server.close()

    def test_rejects_mismatched_response_id(self) -> None:
        client, server = socket.socketpair()
        client.settimeout(2)

        def worker() -> None:
            server.recv(4096)
            server.sendall(
                b'{"v":1,"type":"result","id":"wrong","ok":true,"message":""}\n'
            )
            server.close()

        thread = threading.Thread(target=worker)
        thread.start()
        transport = JsonLineTransport(client)
        with self.assertRaisesRegex(DeviceControlError, "request id"):
            transport.request({"v": 1, "type": "command", "id": "right"})
        transport.close()
        thread.join(timeout=2)

    def test_connect_waits_for_android_socket_without_restarting_service_or_forward(self) -> None:
        first, closed_peer = socket.socketpair()
        closed_peer.close()
        ready, peer = socket.socketpair()
        peer.settimeout(2)
        forward = Mock()
        forward.open.return_value = 12345
        factory = Mock(side_effect=[first, ready])
        controller = UsbDeviceController(Path("adb"), "serial", socket_factory=factory)

        def respond() -> None:
            with peer:
                request = json.loads(peer.recv(4096).split(b"\n", 1)[0])
                peer.sendall(json.dumps({
                    "v": 1, "type": "result", "id": request["id"], "ok": True,
                }).encode() + b"\n")

        worker = threading.Thread(target=respond)
        worker.start()
        try:
            with patch("bloompilot.device_control.AdbForward", return_value=forward):
                self.assertTrue(controller.connect(timeout=1).ok)
            self.assertTrue(controller.connected)
            self.assertEqual(controller.connection_generation, 1)
            self.assertEqual(factory.call_count, 2)
            self.assertEqual(first.fileno(), -1)
            forward.start_service.assert_called_once()
            forward.open.assert_called_once()
        finally:
            controller.close()
            worker.join(timeout=2)

    def test_connect_repeated_eof_keeps_one_deadline_and_cleans_every_socket(self) -> None:
        sockets: list[socket.socket] = []

        def disconnected(*_args, **_kwargs):
            client, peer = socket.socketpair()
            peer.close()
            sockets.append(client)
            return client

        forward = Mock()
        forward.open.return_value = 12345
        controller = UsbDeviceController(Path("adb"), "serial", socket_factory=disconnected)
        started = time.monotonic()
        with patch("bloompilot.device_control.AdbForward", return_value=forward):
            with self.assertRaises(DeviceControlError):
                controller.connect(timeout=0.05)
        self.assertLess(time.monotonic() - started, 0.5)
        self.assertFalse(controller.connected)
        self.assertTrue(sockets)
        self.assertTrue(all(connection.fileno() == -1 for connection in sockets))
        forward.close.assert_called_once()

    def test_connect_does_not_retry_rejected_handshake(self) -> None:
        client, peer = socket.socketpair()
        peer.settimeout(2)
        factory = Mock(return_value=client)
        controller = UsbDeviceController(Path("adb"), "serial", socket_factory=factory)

        def reject() -> None:
            with peer:
                request = json.loads(peer.recv(4096).split(b"\n", 1)[0])
                peer.sendall(json.dumps({
                    "v": 1, "type": "result", "id": request["id"],
                    "ok": False, "message": "rejected",
                }).encode() + b"\n")

        worker = threading.Thread(target=reject)
        worker.start()
        try:
            with patch("bloompilot.device_control.AdbForward"):
                with self.assertRaisesRegex(DeviceControlError, "rejected"):
                    controller.connect(timeout=1)
            factory.assert_called_once()
            self.assertFalse(controller.connected)
        finally:
            controller.close()
            worker.join(timeout=2)

    def test_bounded_command_times_out_at_either_lock_without_sending_later(self) -> None:
        for lock_name in ("controller", "transport"):
            with self.subTest(lock=lock_name):
                client, server = socket.socketpair()
                client.settimeout(0.5)
                controller = UsbDeviceController(Path("adb"), "serial")
                controller._transport = JsonLineTransport(client)
                lock = controller._lifecycle_lock if lock_name == "controller" else controller._transport._lock
                locked = threading.Event()
                release = threading.Event()

                def hold_lock() -> None:
                    with lock:
                        locked.set()
                        release.wait(2)

                holder = threading.Thread(target=hold_lock)
                holder.start()
                try:
                    self.assertTrue(locked.wait(1))
                    started = time.monotonic()
                    with self.assertRaisesRegex(DeviceControlError, "逾時"):
                        controller.command("teleport", {"lat": 24, "lng": 120}, timeout=0.05)
                    self.assertLess(time.monotonic() - started, 0.5)
                    release.set()
                    holder.join(timeout=1)
                    # No queued worker may send the teleport after shutdown.
                    server.settimeout(0.05)
                    with self.assertRaises(socket.timeout):
                        server.recv(4096)
                    self.assertNotEqual(client.fileno(), -1)
                finally:
                    release.set()
                    holder.join(timeout=1)
                    controller.close()
                    server.close()

    def test_response_timeout_closes_stream_so_late_replies_cannot_be_reused(self) -> None:
        client, server = socket.socketpair()
        controller = UsbDeviceController(Path("adb"), "serial")
        controller._transport = JsonLineTransport(client)
        client.settimeout(45)
        server.settimeout(1)
        try:
            started = time.monotonic()
            with self.assertRaisesRegex(DeviceControlError, "USB 控制連線中斷"):
                controller.command("teleport", {"lat": 24, "lng": 120}, timeout=0.05)
            self.assertLess(time.monotonic() - started, 0.5)
            sent = json.loads(server.recv(4096).split(b"\n", 1)[0])
            self.assertEqual(sent["command"], "teleport")
            self.assertEqual(client.fileno(), -1)
            self.assertFalse(controller.connected)
            with self.assertRaisesRegex(DeviceControlError, "連線已關閉"):
                controller.command("status", timeout=0.05)
        finally:
            controller.close()
            server.close()

    def test_heartbeat_skips_in_flight_foreground_command_without_closing_socket(self) -> None:
        client, server = socket.socketpair()
        client.settimeout(1)
        server.settimeout(1)
        controller = UsbDeviceController(Path("adb"), "serial")
        controller._transport = JsonLineTransport(client)
        errors: list[Exception] = []

        def foreground() -> None:
            try:
                controller.command("status")
            except Exception as exc:
                errors.append(exc)

        worker = threading.Thread(target=foreground)
        worker.start()
        try:
            request = json.loads(server.recv(4096).split(b"\n", 1)[0])
            started = time.monotonic()
            self.assertFalse(controller.keepalive(idle_seconds=15, timeout=0.05))
            self.assertLess(time.monotonic() - started, 0.5)
            # The foreground request can take longer than the heartbeat's
            # deadline without being interrupted or followed by a queued probe.
            server.settimeout(0.08)
            with self.assertRaises(socket.timeout):
                server.recv(4096)
            server.sendall(json.dumps({
                "v": 1, "type": "result", "id": request["id"], "ok": True,
            }).encode() + b"\n")
            worker.join(timeout=1)
            self.assertFalse(worker.is_alive())
            self.assertEqual(errors, [])
            self.assertTrue(controller.connected)
            self.assertTrue(controller.keepalive(idle_seconds=15, timeout=0.05))
            with self.assertRaises(socket.timeout):
                server.recv(4096)
        finally:
            server.close()
            worker.join(timeout=1)
            controller.close()

    def test_heartbeat_uses_recent_responses_and_probes_again_only_after_idle(self) -> None:
        client, server = socket.socketpair()
        client.settimeout(1)
        server.settimeout(1)
        controller = UsbDeviceController(Path("adb"), "serial")
        controller._transport = JsonLineTransport(client)
        seen: list[str] = []

        def respond() -> None:
            with server.makefile("rwb", buffering=0) as stream:
                for ok in (False, True):
                    request = json.loads(stream.readline())
                    seen.append(request["command"])
                    stream.write(json.dumps({
                        "v": 1, "type": "result", "id": request["id"],
                        "ok": ok, "message": "ok" if ok else "rejected",
                    }).encode() + b"\n")

        worker = threading.Thread(target=respond)
        worker.start()
        try:
            with patch("bloompilot.device_control.time.monotonic", return_value=100):
                with self.assertRaisesRegex(DeviceControlError, "rejected"):
                    controller.command("setSpeed", {"speedKmh": -1})
            with patch("bloompilot.device_control.time.monotonic", return_value=110):
                self.assertTrue(controller.keepalive(idle_seconds=15, timeout=0.1))
                self.assertEqual(seen, ["setSpeed"])
            with patch("bloompilot.device_control.time.monotonic", return_value=116):
                self.assertTrue(controller.keepalive(idle_seconds=15, timeout=0.1))
            worker.join(timeout=1)
            self.assertEqual(seen, ["setSpeed", "ping"])
            self.assertEqual(client.gettimeout(), 1)
        finally:
            controller.close()
            worker.join(timeout=1)
            server.close()

    def test_heartbeat_timeout_marks_disconnected_and_is_not_hidden_by_old_response(self) -> None:
        client, server = socket.socketpair()
        controller = UsbDeviceController(Path("adb"), "serial")
        controller._transport = JsonLineTransport(client)
        try:
            with self.assertRaisesRegex(DeviceControlError, "USB 控制連線中斷"):
                controller.keepalive(idle_seconds=15, timeout=0.05)
            self.assertFalse(controller.connected)
            controller._last_response_at = time.monotonic()
            with self.assertRaisesRegex(DeviceControlError, "連線已關閉"):
                controller.keepalive(idle_seconds=15, timeout=0.05)
        finally:
            controller.close()
            server.close()

    def test_partial_response_does_not_extend_total_deadline(self) -> None:
        client, server = socket.socketpair()
        client.settimeout(45)
        server.settimeout(1)
        stop = threading.Event()

        def trickle_response() -> None:
            try:
                server.recv(4096)
                end = time.monotonic() + 0.8
                while time.monotonic() < end and not stop.wait(0.01):
                    server.sendall(b" ")
                server.shutdown(socket.SHUT_WR)
            except OSError:
                pass

        worker = threading.Thread(target=trickle_response)
        worker.start()
        transport = JsonLineTransport(client)
        try:
            started = time.monotonic()
            with self.assertRaises(DeviceControlError):
                transport.request({"id": "bounded"}, deadline=started + 0.08)
            self.assertLess(time.monotonic() - started, 0.5)
            self.assertEqual(client.fileno(), -1)
        finally:
            stop.set()
            transport.close()
            worker.join(timeout=1)
            server.close()


if __name__ == "__main__":
    unittest.main()
