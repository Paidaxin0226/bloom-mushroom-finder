from __future__ import annotations

import io
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from fastapi.testclient import TestClient
from PIL import Image

from bloompilot.monthly_workflow import MonthlyWorkflowStore, collect_fixed_route_backgrounds, _route_key
from bloompilot.device_control import DeviceControlError, UsbConnectionError
from bloompilot.memory_watchdog import PikminMemoryWatchdog, PIKMIN_PACKAGE
from bloompilot.phone_automation import MapRecoveryError, ScanPoint, ScanSummary, atomic_write_bytes
from bloompilot.web.runtime import AppRuntime
from bloompilot.web.server import create_app
from test_geo_recognition_sequence import FakeController
from test_memory_watchdog import restart_frame, RESTART_SCREEN_MAP, RESTART_SCREEN_HOME, RESTART_SCREEN_LOADING, RESTART_SCREEN_GARDEN


class MonthlyReviewTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=Path.cwd())
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.store = MonthlyWorkflowStore(project_root=self.root, workspace_root=self.root / 'monthly', state_path=self.root / 'state.json', resident_root=self.root / 'resident')
        self.backgrounds = self.store.backgrounds_dir('2026-09') / 'route/wp_00001_Berlin'
        self.backgrounds.mkdir(parents=True)
        for name in ('one', 'two'):
            Image.new('RGB', (1080, 2340), 'green').save(self.backgrounds / f'{name}.png')
        self.paths = [item['path'] for item in self.store.list_backgrounds('2026-09')]

    def test_thumbnail_is_small_and_changes_when_original_is_replaced(self):
        before = self.store.background_thumbnail('2026-09', self.paths[0])
        with Image.open(io.BytesIO(before)) as image:
            self.assertEqual(image.format, 'JPEG')
            self.assertLessEqual(image.width, 240)
            self.assertLessEqual(image.height, 360)
        Image.new('RGB', (100, 200), 'red').save(self.backgrounds / 'one.png')
        self.assertNotEqual(before, self.store.background_thumbnail('2026-09', self.paths[0]))
        self.assertEqual(self.store.list_backgrounds('2026-09')[0]['waypoint'], 'Berlin')

    def test_bulk_delete_validates_all_paths_before_removing_any_and_revokes_review(self):
        self.store.approve_backgrounds('2026-09')
        for bad_path in ('../private.png', 'missing.png'):
            with self.assertRaises((ValueError, FileNotFoundError)):
                self.store.delete_backgrounds('2026-09', [self.paths[0], bad_path])
            self.assertEqual(len(self.store.list_backgrounds('2026-09')), 2)
            self.assertTrue(self.store.review_is_current('2026-09'))
        result = self.store.delete_backgrounds('2026-09', [*self.paths, self.paths[0]])
        self.assertEqual(result['deleted'], 2)
        self.assertEqual(result['background_count'], 0)
        self.assertFalse(self.store.review_is_current('2026-09'))

    def test_background_http_endpoints_and_busy_protection(self):
        runtime = AppRuntime.__new__(AppRuntime)
        runtime.monthly_workflow = self.store
        runtime.monthly_status = self.store.status
        runtime.jobs = Mock()
        runtime.jobs.active.return_value = None
        client = TestClient(create_app(runtime))
        url = '/api/monthly-activity/background'
        params = {'cycle': '2026-09', 'path': self.paths[0], 'thumbnail': 'true'}
        response = client.get(url, params=params)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers['content-type'], 'image/jpeg')
        self.assertEqual(client.get(url, params={**params, 'thumbnail': 'false'}).headers['content-type'], 'image/png')
        self.assertEqual(client.get(url, params={**params, 'path': '../private.png'}).status_code, 422)
        self.assertEqual(client.get(url, params={**params, 'path': 'missing.png'}).status_code, 404)
        endpoint = '/api/monthly-activity/delete-backgrounds'
        for paths in (None, 'one.png', [], [None], ['one.png'] * 501):
            self.assertEqual(client.post(endpoint, json={'cycle': '2026-09', 'paths': paths}).status_code, 422)
        runtime.jobs.active.return_value = object()
        self.assertEqual(client.post(endpoint, json={'cycle': '2026-09', 'paths': self.paths}).status_code, 409)
        self.assertEqual(len(self.store.list_backgrounds('2026-09')), 2)
        runtime.jobs.active.return_value = None
        self.assertEqual(client.post(endpoint, json={'cycle': '2026-09', 'paths': self.paths}).json()['background_count'], 0)


class MonthlyRecoveryTests(unittest.TestCase):
    def run_collection(self, callback, *, prepare_error=None, preview=None, verify=None):
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as temp:
            root = Path(temp)
            store = MonthlyWorkflowStore(project_root=root, workspace_root=root / 'monthly', state_path=root / 'state.json', resident_root=root / 'resident')
            existing = store.backgrounds_dir('2026-09') / _route_key('route.json') / 'existing.png'
            existing.parent.mkdir(parents=True)
            existing.write_bytes(b'previous collection')
            controller = FakeController()
            watchdog = Mock()
            watchdog.prepare_for_scan.side_effect = prepare_error
            watchdog.before_capture.return_value = False
            watchdog.recover_after_usb_service_restart.return_value = True
            waypoint = SimpleNamespace(id='berlin', name='Berlin', lat=52.5, lng=13.4, point_count=1, horizontal_radius=0, vertical_radius=0, resolved_radii=(0, 0, 0, 0), scan_spans=None)
            library = Mock()
            library.waypoints.return_value = [waypoint]
            stop = threading.Event()
            def scan(*args, **kwargs):
                callback(controller, watchdog, stop, kwargs)
                return ScanSummary(1, 0, not stop.is_set(), 'completed')
            with patch('bloompilot.monthly_workflow.AdbClient'), patch('bloompilot.monthly_workflow.PikminMemoryWatchdog', return_value=watchdog), patch('bloompilot.monthly_workflow.run_rectangular_scan', side_effect=scan):
                try:
                    return collect_fixed_route_backgrounds(store=store, route_library=library, controller=controller, adb=root / 'adb', serial='test', cycle='2026-09', route_paths=['route.json'], settle_seconds=0, swipe_duration_ms=800, stop_event=stop, status=lambda _: None, progress=lambda *args: None, preview=preview)
                finally:
                    if prepare_error:
                        self.assertEqual(existing.read_bytes(), b'previous collection')
                    if verify:
                        verify(store)

    def test_invalid_or_non_map_capture_is_not_saved(self):
        for payload in (b'', b'broken png', restart_frame(RESTART_SCREEN_HOME), restart_frame(RESTART_SCREEN_LOADING), restart_frame(RESTART_SCREEN_GARDEN)):
            with self.subTest(payload_size=len(payload)):
                def scan(controller, watchdog, stop, kwargs):
                    kwargs['on_capture'](ScanPoint(0, 'center', 0), payload)
                def verify(store):
                    self.assertEqual(store.list_backgrounds('2026-09'), [])
                    self.assertIsNone(store.manifest('2026-09').get('collection_completed_at'))
                with self.assertRaisesRegex(MapRecoveryError, '不會儲存'):
                    self.run_collection(scan, verify=verify)

    def test_retry_of_same_grid_replaces_one_file_without_adding_duplicate(self):
        first = restart_frame(RESTART_SCREEN_MAP)
        second = restart_frame('map_centered')
        def scan(controller, watchdog, stop, kwargs):
            point = ScanPoint(0, 'center', 0)
            kwargs['on_capture'](point, first)
            kwargs['on_capture'](point, second)
        def verify(store):
            items = store.list_backgrounds('2026-09')
            self.assertEqual(len(items), 1)
            self.assertEqual(store.background_file('2026-09', items[0]['path']).read_bytes(), second)
        self.run_collection(scan, verify=verify)

    def test_failed_next_grid_keeps_only_previous_successful_capture(self):
        good = restart_frame(RESTART_SCREEN_MAP)
        def scan(controller, watchdog, stop, kwargs):
            kwargs['on_capture'](ScanPoint(0, 'center', 0), good)
            kwargs['on_capture'](ScanPoint(1, 'grid', 1, grid_x=1), b'failed')
        def verify(store):
            items = store.list_backgrounds('2026-09')
            self.assertEqual(len(items), 1)
            self.assertEqual(items[0]['name'], 'scan_000_center.png')
            self.assertEqual(store.background_file('2026-09', items[0]['path']).read_bytes(), good)
            self.assertIsNone(store.manifest('2026-09').get('collection_completed_at'))
        with self.assertRaises(MapRecoveryError):
            self.run_collection(scan, verify=verify)

    def test_preview_failure_does_not_leave_a_saved_capture(self):
        def scan(controller, watchdog, stop, kwargs):
            kwargs['on_capture'](ScanPoint(0, 'center', 0), restart_frame(RESTART_SCREEN_MAP))
        with self.assertRaisesRegex(RuntimeError, 'preview failed'):
            self.run_collection(scan, preview=Mock(side_effect=RuntimeError('preview failed')), verify=lambda store: self.assertEqual(store.list_backgrounds('2026-09'), []))

    def test_failed_atomic_replacement_preserves_good_file_and_cleans_partial(self):
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as temp:
            path = Path(temp) / 'scan_000_center.png'
            path.write_bytes(b'previous successful capture')
            with patch.object(Path, 'replace', side_effect=OSError('disk error')):
                with self.assertRaises(OSError):
                    atomic_write_bytes(path, b'incomplete replacement')
            self.assertEqual(path.read_bytes(), b'previous successful capture')
            self.assertEqual(list(Path(temp).iterdir()), [path])

    def test_connection_change_during_map_recovery_reapplies_location_before_grid_replay(self):
        def scan(controller, watchdog, stop, kwargs):
            controller.connection_generation += 1
            count = 0
            def recover():
                nonlocal count
                count += 1
                self.assertEqual(controller.current, {'lat': 52.5, 'lng': 13.4})
                if count == 1:
                    controller.connection_generation += 1
                    controller.current = {'lat': 0, 'lng': 0}
                return True
            watchdog.recover_after_usb_service_restart.side_effect = recover
            point = ScanPoint(1, 'grid', 1, grid_x=1, grid_y=0)
            self.assertTrue(kwargs['before_capture'](point))
            self.assertFalse(kwargs['before_capture'](point))
            self.assertEqual(count, 2)
        self.assertEqual(self.run_collection(scan)['captured'], 1)

    def test_failed_usb_retries_release_game_ram_then_restore_waypoint_before_grid_replay(self):
        def scan(controller, watchdog, stop, kwargs):
            controller.connected = False
            real_command = controller.command
            events = []

            def command(name, payload=None, **options):
                if not controller.connected:
                    raise UsbConnectionError('尚未連接 Android USB 控制。')
                if name == 'teleport':
                    events.append('teleport')
                return real_command(name, payload, **options)

            controller.command = command
            adb = Mock()
            adb.run.side_effect = lambda *args, **options: events.append('stop game')
            recovery = PikminMemoryWatchdog(adb, stop_event=stop)
            watchdog.recover_usb_connection.side_effect = recovery.recover_usb_connection

            def restart(device, **options):
                events.append('restart USB')
                controller.current = {'lat': 0, 'lng': 0}
                return device.connect()

            def map_recovery():
                events.append('restore map')
                self.assertEqual(controller.current, {'lat': 52.5, 'lng': 13.4})
                return True

            watchdog.recover_after_usb_service_restart.side_effect = map_recovery
            point = ScanPoint(1, 'grid', 6, grid_x=0, grid_y=6)
            with (
                patch('bloompilot.memory_watchdog.reconnect_usb', side_effect=DeviceControlError('連續 3 次恢復失敗')),
                patch('bloompilot.memory_watchdog.restart_locwarp', side_effect=restart),
            ):
                self.assertTrue(kwargs['before_capture'](point))
                self.assertFalse(kwargs['before_capture'](point))
            self.assertEqual(events, ['stop game', 'restart USB', 'teleport', 'restore map'])
            adb.run.assert_called_once_with('shell', 'am', 'force-stop', PIKMIN_PACKAGE, timeout=12)
        self.assertEqual(self.run_collection(scan)['captured'], 1)

    def test_exhausted_usb_recovery_stops_before_map_or_capture(self):
        def scan(controller, watchdog, stop, kwargs):
            controller.connected = False
            controller.command = Mock(side_effect=UsbConnectionError('尚未連接 Android USB 控制。'))
            watchdog.recover_usb_connection.side_effect = DeviceControlError('still disconnected')
            with self.assertRaisesRegex(MapRecoveryError, 'USB 完整恢復失敗'):
                kwargs['before_capture'](ScanPoint(1, 'grid', 6, grid_y=6))
            watchdog.recover_after_usb_service_restart.assert_not_called()
            stop.set()
        self.run_collection(scan)

    def test_low_memory_locwarp_restart_failure_enters_full_usb_recovery(self):
        def scan(controller, watchdog, stop, kwargs):
            controller.connected = False
            watchdog.before_capture.side_effect = DeviceControlError('連續 3 次恢復失敗')
            real_command = controller.command

            def command(name, payload=None, **options):
                if not controller.connected:
                    raise UsbConnectionError('尚未連接 Android USB 控制。')
                return real_command(name, payload, **options)

            controller.command = command
            watchdog.recover_usb_connection.side_effect = lambda device: device.connect()
            self.assertTrue(kwargs['before_capture'](ScanPoint(1, 'grid', 6, grid_y=6)))
            watchdog.recover_usb_connection.assert_called_once_with(controller)
            watchdog.recover_after_usb_service_restart.assert_called_once_with()
            self.assertEqual(controller.current, {'lat': 52.5, 'lng': 13.4})
        self.run_collection(scan)

    def test_cancelled_disconnected_recovery_does_not_replay_command(self):
        def scan(controller, watchdog, stop, kwargs):
            controller.connected = False
            controller.command = Mock(side_effect=UsbConnectionError('尚未連接 Android USB 控制。'))

            def cancel(_controller):
                stop.set()
                raise DeviceControlError('已取消')

            watchdog.recover_usb_connection.side_effect = cancel
            self.assertFalse(kwargs['before_capture'](ScanPoint(1, 'grid', 6, grid_y=6)))
            controller.command.assert_called_once()
            watchdog.recover_after_usb_service_restart.assert_not_called()
        self.run_collection(scan)

    def test_ui_reconnect_during_failed_command_still_restores_current_grid(self):
        def scan(controller, watchdog, stop, kwargs):
            controller.connected = False
            real_command = controller.command

            def command(name, payload=None, **options):
                if not controller.connected:
                    controller.connected = True
                    controller.connection_generation += 1
                    raise UsbConnectionError('USB 控制連線中斷')
                return real_command(name, payload, **options)

            controller.command = command
            self.assertTrue(kwargs['before_capture'](ScanPoint(1, 'grid', 6, grid_y=6)))
            watchdog.recover_usb_connection.assert_called_once_with(controller)
            watchdog.recover_after_usb_service_restart.assert_called_once_with()
        self.run_collection(scan)

    def test_live_command_rejection_does_not_restart_usb_or_game(self):
        def scan(controller, watchdog, stop, kwargs):
            controller.connection_generation += 1
            controller.command = Mock(side_effect=DeviceControlError('teleport rejected'))
            with self.assertRaises(MapRecoveryError):
                kwargs['before_capture'](ScanPoint(1, 'grid', 6, grid_y=6))
            watchdog.recover_usb_connection.assert_not_called()
            watchdog.recover_after_usb_service_restart.assert_not_called()
            stop.set()
        self.run_collection(scan)

    def test_repeated_failure_stops_with_map_error_without_final_recenter(self):
        def scan(controller, watchdog, stop, kwargs):
            controller.connection_generation += 1
            watchdog.recover_after_usb_service_restart.return_value = False
            with self.assertRaises(MapRecoveryError):
                kwargs['before_capture'](ScanPoint(1, 'grid', 0))
            self.assertEqual(watchdog.recover_after_usb_service_restart.call_count, 3)
            stop.set()
        self.run_collection(scan)

    def test_cancellation_during_usb_recovery_does_not_retry(self):
        def scan(controller, watchdog, stop, kwargs):
            controller.connection_generation += 1
            watchdog.recover_after_usb_service_restart.side_effect = lambda: stop.set() or False
            kwargs['before_capture'](ScanPoint(1, 'grid', 0))
            self.assertEqual(watchdog.recover_after_usb_service_restart.call_count, 1)
        self.run_collection(scan)

    def test_initialization_failure_stops_heartbeat(self):
        before = {t.ident for t in threading.enumerate() if t.name == 'monthly-background-usb'}
        with self.assertRaisesRegex(RuntimeError, 'startup failed'):
            self.run_collection(lambda *args: None, prepare_error=RuntimeError('startup failed'))
        self.assertEqual(before, {t.ident for t in threading.enumerate() if t.name == 'monthly-background-usb'})
