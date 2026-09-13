import threading
import time
import unittest
from unittest.mock import patch

from bloompilot.web.events import EventHub
from bloompilot.web.runtime import AppRuntime, JobManager


class RoutePauseTests(unittest.TestCase):
    def start_worker(self):
        manager = JobManager(EventHub())
        ready = threading.Event()
        def run(ctx):
            ready.wait(2)
            while not ctx.stop_event.is_set():
                ctx.pause_checkpoint()
                ctx.stop_event.wait(0.01)
        record = manager.start('geo_sequence_recognition', run, sequence_name='test route')
        self.addCleanup(lambda: (ready.set(), manager.stop(record.id)))
        return manager, record, ready

    def test_pending_pause_survives_preparation_until_checkpoint(self):
        manager, record, ready = self.start_worker()
        manager.begin_recognition_pause(record.id)
        self.assertTrue(record.pause_requested)
        self.assertFalse(record.paused)
        result = []
        waiter = threading.Thread(target=lambda: result.append(manager.await_recognition_pause(record.id)), daemon=True)
        waiter.start()
        self.assertTrue(waiter.is_alive())
        ready.set()
        waiter.join(2)
        self.assertFalse(waiter.is_alive())
        self.assertTrue(result[0].paused)
        self.assertFalse(record.pause_requested)
        self.assertEqual(record.sequence_name, 'test route')

    def test_cancel_before_checkpoint_does_not_pause_later(self):
        manager, record, ready = self.start_worker()
        manager.begin_recognition_pause(record.id)
        manager.cancel_recognition_pause(record.id)
        with self.assertRaisesRegex(RuntimeError, '取消'):
            manager.await_recognition_pause(record.id)
        ready.set()
        self.assertFalse(record.paused)
        self.assertFalse(record.pause_requested)

    def test_explicit_pause_timeout_clears_pending_state(self):
        manager, record, ready = self.start_worker()
        manager.begin_recognition_pause(record.id)
        with self.assertRaises(RuntimeError):
            manager.await_recognition_pause(record.id, timeout=.01)
        self.assertFalse(record.pause_requested)
        self.assertFalse(record.paused)
        ready.set()

    def test_stop_releases_pending_waiter_without_resurrecting_job(self):
        manager, record, ready = self.start_worker()
        manager.begin_recognition_pause(record.id)
        manager.stop(record.id)
        with self.assertRaises(RuntimeError):
            manager.await_recognition_pause(record.id)
        self.assertEqual(record.state, 'stopping')
        self.assertFalse(record.paused)

    def test_nonblocking_runtime_request_can_be_cancelled(self):
        manager, record, ready = self.start_worker()
        runtime = AppRuntime()
        runtime.jobs = manager
        with patch.object(runtime, '_finish_recognition_pause') as finish:
            runtime.request_recognition_pause(record.id)
            self.assertTrue(record.pause_requested)
            runtime.cancel_pending_recognition_pause(record.id)
            ready.set()
            time.sleep(.05)
            finish.assert_not_called()


if __name__ == '__main__':
    unittest.main()
