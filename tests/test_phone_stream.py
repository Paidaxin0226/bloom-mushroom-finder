from __future__ import annotations

import threading
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import numpy as np

from bloompilot.phone_stream import ScrcpyFrameSource
from bloompilot.phone_tools import LiveError, ScrcpyControlWindow, Toolchain


class FakeRawCapture:
    def __init__(self, frame: np.ndarray) -> None:
        self.frame = frame
        self.stopped = threading.Event()

    def run(self, on_frame, _status, stop_event: threading.Event) -> None:
        on_frame(self.frame, 1.0)
        stop_event.wait(2.0)

    def stop(self) -> None:
        self.stopped.set()


class ScrcpyFrameSourceTests(unittest.TestCase):
    def make_source(self, fallback=None, cancel_event=None, status=None) -> ScrcpyFrameSource:
        return ScrcpyFrameSource(
            Toolchain(Path("scrcpy"), Path("adb"), Path("server"), "3.3"),
            "phone",
            fallback_capture_image=fallback,
            cancel_event=cancel_event,
            status=status,
        )

    def test_background_source_exposes_decoded_latest_frame_and_closes(self) -> None:
        toolchain = Toolchain(Path("scrcpy"), Path("adb"), Path("server"), "3.3")
        expected = np.full((12, 8, 3), 73, dtype=np.uint8)
        source = ScrcpyFrameSource(toolchain, "phone")
        fake = FakeRawCapture(expected)
        source.capture = fake

        source.start(timeout=1.0)
        actual = source.capture_image(timeout=1.0)
        source.close()

        np.testing.assert_array_equal(actual, expected)
        self.assertTrue(fake.stopped.is_set())

    def test_stalled_stream_uses_fresh_screenshots_and_recovers(self) -> None:
        initial = np.full((12, 8, 3), 10, dtype=np.uint8)
        screenshot = np.full_like(initial, 20)
        recovered = np.full_like(initial, 30)
        messages: list[str] = []
        source = self.make_source(status=messages.append)

        def capture_screenshot():
            # Video arriving during the screenshot must not be returned as a
            # new image after the newer screenshot has already been consumed.
            source.frames.put((initial, 2.0))
            return screenshot.copy()

        fallback = Mock(side_effect=capture_screenshot)
        source.fallback_capture_image = fallback
        source.frames.put((initial, 1.0))
        np.testing.assert_array_equal(source.capture_image(), initial)
        for _ in range(2):
            np.testing.assert_array_equal(source.capture_image(timeout=0.1), screenshot)
        self.assertEqual(fallback.call_count, 2)
        self.assertEqual(len(messages), 1)
        self.assertIn("ADB", messages[0])

        source.frames.put((recovered, 3.0))
        np.testing.assert_array_equal(source.capture_image(), recovered)
        self.assertEqual(fallback.call_count, 2)
        self.assertEqual(len(messages), 2)
        self.assertIn("已恢復", messages[-1])

    def test_failed_or_finished_stream_uses_fallback_for_png_and_image(self) -> None:
        import cv2

        expected = np.full((12, 8, 3), 20, dtype=np.uint8)
        for failed in (False, True):
            with self.subTest(failed=failed):
                fallback = Mock(return_value=expected)
                source = self.make_source(fallback)
                source.frames.put((np.zeros_like(expected), 1.0))
                if failed:
                    source._error = OSError("USB stream disconnected")
                else:
                    source._finished.set()
                np.testing.assert_array_equal(source.capture_image(), expected)
                payload = source.capture_png()
                decoded = cv2.imdecode(np.frombuffer(payload, np.uint8), cv2.IMREAD_COLOR)
                np.testing.assert_array_equal(decoded, expected)
                self.assertEqual(fallback.call_count, 2)

    def test_fallback_failure_is_propagated(self) -> None:
        error = RuntimeError("ADB screenshot failed too")
        source = self.make_source(Mock(side_effect=error))
        source._error = OSError("USB stream disconnected")
        with self.assertRaises(RuntimeError) as caught:
            source.capture_image()
        self.assertIs(caught.exception, error)

    def test_cancellation_does_not_start_fallback(self) -> None:
        cancelled = threading.Event()
        fallback = Mock()
        source = self.make_source(fallback, cancel_event=cancelled)
        source.frames.put((np.zeros((12, 8, 3), dtype=np.uint8), 1.0))
        cancelled.set()
        with self.assertRaisesRegex(LiveError, "已取消"):
            source.capture_image()
        fallback.assert_not_called()

    def test_cancellation_during_fallback_discards_screenshot(self) -> None:
        cancelled = threading.Event()
        source = self.make_source(cancel_event=cancelled)

        def cancel_during_capture():
            cancelled.set()
            return np.zeros((12, 8, 3), dtype=np.uint8)

        source.fallback_capture_image = cancel_during_capture
        source._error = OSError("USB stream disconnected")
        with self.assertRaisesRegex(LiveError, "已取消"):
            source.capture_image()

    def test_timeout_without_fallback_still_reports_failure(self) -> None:
        source = self.make_source()
        with self.assertRaisesRegex(LiveError, "新畫面逾時"):
            source.capture_image(timeout=0.1)


class ScrcpyControlWindowTests(unittest.TestCase):
    def test_opens_one_interactive_window_and_closes_it(self) -> None:
        toolchain = Toolchain(Path("scrcpy.exe"), Path("adb.exe"), Path("server"), "4.1")
        process = Mock()
        process.pid = 321
        process.poll.return_value = None
        control = ScrcpyControlWindow()

        with (
            patch("bloompilot.phone_tools.find_toolchain", return_value=toolchain),
            patch("bloompilot.phone_tools.subprocess.Popen", return_value=process) as popen,
            patch("bloompilot.phone_tools.time.sleep"),
        ):
            opened = control.open("phone-1")
            reopened = control.open("phone-1")

        self.assertTrue(opened["active"])
        self.assertEqual(opened["pid"], 321)
        self.assertEqual(reopened, opened)
        popen.assert_called_once()
        command = popen.call_args.args[0]
        self.assertIn("--serial", command)
        self.assertIn("phone-1", command)
        self.assertIn("--no-audio", command)
        self.assertIn("--stay-awake", command)

        closed = control.close()
        self.assertFalse(closed["active"])
        process.terminate.assert_called_once_with()
        process.wait.assert_called_once_with(timeout=2.0)

    def test_reports_an_immediate_scrcpy_failure(self) -> None:
        toolchain = Toolchain(Path("scrcpy.exe"), Path("adb.exe"), Path("server"), "4.1")
        process = Mock()
        process.poll.return_value = 1
        control = ScrcpyControlWindow()

        with (
            patch("bloompilot.phone_tools.find_toolchain", return_value=toolchain),
            patch("bloompilot.phone_tools.subprocess.Popen", return_value=process),
            patch("bloompilot.phone_tools.time.sleep"),
            self.assertRaisesRegex(RuntimeError, "未能啟動"),
        ):
            control.open("phone-1")


if __name__ == "__main__":
    unittest.main()
