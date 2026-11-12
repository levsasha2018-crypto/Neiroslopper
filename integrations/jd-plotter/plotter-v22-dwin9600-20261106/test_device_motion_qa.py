"""Device lifecycle scenarios with the real worker and fake serial transports."""
from pathlib import Path
import queue
import sys
import threading
import time
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parent / 'PROGRAM'))

import controller
from dwin_native import NativeDwinClient, NativeState, STATE_STRUCT
from geometry import Axes, Sheet
from pen_lift import PenLiftSettings


class MotionLifecycleTests(unittest.TestCase):
    def setUp(self):
        trace = patch.object(controller, 'SerialTrace', return_value=Mock(failure=Mock(return_value=None)))
        trace.start()
        self.addCleanup(trace.stop)
        self.machine = controller.Controller()
        self.addCleanup(self.close)
        self.pen = PenLiftSettings(True, False, True, 50, 50, True, True)
        self.paths = [[(2, 3), (7, 3)], [(9, 9), (11, 9)]]

    def close(self):
        self.machine.shutdown()
        self.machine.worker.join(3)
        self.assertFalse(self.machine.worker.is_alive())

    def event_until(self, predicate, seconds=4):
        events = []
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            try:
                event = self.machine.events.get(timeout=max(.001, deadline - time.monotonic()))
            except queue.Empty:
                break
            events.append(event)
            if predicate(event):
                return events
        self.fail(f'Expected event did not arrive: {events!r}')

    def job(self, action, **params):
        params.setdefault('axes', Axes())
        params.setdefault('pen', self.pen.as_dict())
        self.assertTrue(self.machine.submit(action, **params), action)
        return self.event_until(lambda event: event == ('done', action))

    def prepare(self):
        events = self.job('connect', port='DEMO')
        self.assertIn(('connected', 'DEMO'), events)
        self.assertFalse(self.machine.zero)
        self.assertFalse(any(kind == 'error' for kind, _ in self.job('zero')))
        for delta in ((30, 0), (0, 20)):
            self.assertFalse(any(kind == 'error' for kind, _ in self.job('jog', delta=delta, feed=900)))
        self.assertIn(('calibration', (True, Sheet(30, 20))), self.job('corner'))
        return self.machine.session.wire

    def test_wrong_order_invalid_input_then_draw_with_raised_transfers(self):
        self.assertTrue(any(kind == 'error' for kind, _ in self.job('draw', paths=self.paths)))
        self.job('connect', port='DEMO')
        wire = self.machine.session.wire
        for action, params in (('corner', {}), ('draw', {'paths': self.paths})):
            self.assertTrue(any(kind == 'error' for kind, _ in self.job(action, **params)))
        self.assertTrue(self.machine.connected)
        self.assertEqual(wire.ink_moves, [])
        self.job('zero')
        self.assertTrue(any(kind == 'error' for kind, _ in self.job('corner')))
        self.assertIsNone(self.machine.sheet)

        for delta in ((30, 0), (0, 20)):
            self.job('jog', delta=delta, feed=900)
        self.assertIn(('calibration', (True, Sheet(30, 20))), self.job('corner'))
        checkpoint = len(wire.ink_moves)
        self.assertTrue(any(kind == 'error' for kind, _ in self.job('jog', delta=(1, 0), feed=900)))
        for paths in ([], [[(0, 0), (31, 5)]], [[(0, 0), (float('nan'), 5)]]):
            self.assertTrue(any(kind == 'error' for kind, _ in self.job('draw', paths=paths, feed=900)))
        self.assertEqual(len(wire.ink_moves), checkpoint)
        events = self.job('draw', paths=self.paths, feed=900)
        self.assertIn(('finished', 'draw'), events)
        self.assertEqual(wire.position, (11, 9))
        self.assertEqual([down for _, down in wire.ink_moves[checkpoint:]], [False, True, False, True])
        self.assertFalse(wire.coil_on)
        self.assertTrue(self.machine.connected)

    def test_repeated_presses_during_draw_then_stop_require_recalibration(self):
        wire = self.prepare()
        moving = threading.Event()
        check = threading.Event()
        write = wire.write

        def hold_first_move(data):
            result = write(data)
            if data.startswith(b'G1 ') and not moving.is_set():
                moving.set()
            return result

        read = wire.read

        def hold_read(size):
            if moving.is_set() and not check.is_set():
                check.wait(2)
            return read(size)

        wire.write, wire.read = hold_first_move, hold_read
        try:
            self.assertTrue(self.machine.submit('draw', axes=Axes(), pen=self.pen.as_dict(), paths=self.paths, feed=900))
            self.assertTrue(moving.wait(3))
            self.assertFalse(self.machine.submit('draw', axes=Axes(), paths=self.paths, feed=900))
            self.assertFalse(self.machine.submit('jog', axes=Axes(), delta=(1, 0), feed=900))
            self.machine.stop()
            self.machine.stop()
        finally:
            check.set()
        events = self.event_until(lambda event: event == ('done', 'draw'))
        self.assertNotIn(('finished', 'draw'), events)
        self.assertFalse(self.machine.connected)
        self.assertFalse(self.machine.zero)
        self.assertEqual(sum(command.startswith(b'G1 ') for command in wire.history), 1)
        self.assertFalse(wire.coil_on)
        self.job('connect', port='DEMO')
        self.assertIsNone(self.machine.sheet)
        events = self.job('draw', paths=self.paths, feed=900)
        self.assertTrue(any(kind == 'error' for kind, _ in events))
        self.assertEqual(self.machine.session.wire.ink_moves, [])

    def test_long_drawing_pipelines_moves_and_preserves_pen_boundaries(self):
        wire = self.prepare()
        checkpoint = len(wire.ink_moves)
        largest_burst = 0
        write = wire.write

        def measure(data):
            nonlocal largest_burst
            count = write(data)
            if data.startswith(b'G1 '):
                largest_burst = max(largest_burst, wire.rx.count(b'ok\r\n'))
                if len(wire.ink_moves) - checkpoint == 100:
                    self.assertTrue(self.machine.change_drawing_feed(1800))
            return count

        wire.write = measure
        stroke = [(2 + (i % 70) / 10, 2 + i / 1000) for i in range(12000)]
        events = self.job('draw', paths=[stroke], feed=900, travel_feed=1200)
        self.assertIn(('finished', 'draw'), events)
        self.assertGreater(largest_burst, 1)
        self.assertEqual(len(wire.ink_moves) - checkpoint, len(stroke))
        self.assertIn('F1200', wire.ink_moves[checkpoint][0])
        self.assertFalse(wire.ink_moves[checkpoint][1])
        self.assertIn('F900', wire.ink_moves[checkpoint + 1][0])
        self.assertTrue(any('F1800' in command for command, _ in wire.ink_moves[checkpoint + 100:]))
        self.assertFalse(self.machine.change_drawing_feed(1500))
        self.assertTrue(all(down for _, down in wire.ink_moves[checkpoint + 1:]))
        self.assertEqual(wire.position, stroke[-1])
        self.assertFalse(wire.coil_on)

    def test_alarm_during_a_buffered_stroke_requires_recalibration(self):
        self.pen = PenLiftSettings()
        wire = self.prepare()
        write = wire.write
        sent = 0

        def alarm(data):
            nonlocal sent
            if data.startswith(b'G1 '):
                sent += 1
                if sent == 5:
                    wire.respond('ALARM:3')
                    return len(data)
            return write(data)

        wire.write = alarm
        paths = [[(i / 10, 2) for i in range(100)]]
        events = self.job('draw', paths=paths, feed=900)
        self.assertNotIn(('finished', 'draw'), events)
        self.assertTrue(any(kind == 'error' and 'ALARM:3' in detail for kind, detail in events))
        self.assertLess(sent, 15)
        self.assertIn(b'\x18', wire.history)
        self.assertFalse(self.machine.zero)

    def test_short_write_during_drawing_is_not_replayed(self):
        wire = self.prepare()
        write = wire.write
        attempted = []

        def incomplete(data):
            if data.startswith(b'G1 '):
                attempted.append(data)
                return len(data) - 1
            return write(data)

        wire.write = incomplete
        events = self.job('draw', paths=self.paths, feed=900)
        self.assertEqual(len(attempted), 1)
        self.assertNotIn(('finished', 'draw'), events)
        self.assertTrue(any(kind == 'error' and 'Неполная запись' in detail for kind, detail in events))
        self.assertIn(b'\x18', wire.history)
        self.assertFalse(self.machine.connected)
        self.assertIsNone(self.machine.sheet)

    def test_without_lift_drawing_includes_transfers(self):
        self.pen = PenLiftSettings()
        wire = self.prepare()
        checkpoint = len(wire.ink_moves)
        self.assertIn(('finished', 'draw'), self.job('draw', paths=self.paths, feed=900))
        self.assertEqual([down for _, down in wire.ink_moves[checkpoint:]], [True, True, True, True])
        self.assertNotIn(b'M8\n', wire.history)

    def test_idle_at_wrong_endpoint_does_not_report_success(self):
        self.pen = PenLiftSettings()
        wire = self.prepare()
        write = wire.write

        def stop_at_wrong_point(data):
            result = write(data)
            if data == b'G4 P0\n':
                wire.position = (5, 9)
            return result

        wire.write = stop_at_wrong_point
        events = self.job('draw', paths=self.paths, feed=900)
        self.assertNotIn(('finished', 'draw'), events)
        self.assertTrue(any(kind == 'error' and 'конечную точку' in detail for kind, detail in events))
        self.assertFalse(self.machine.connected)
        self.assertFalse(self.machine.zero)

    def test_stop_during_identity_check_prevents_connection(self):
        started, unblock = threading.Event(), threading.Event()

        class SlowIdentity(controller.SimWire):
            def write(self, data):
                result = super().write(data)
                if data == b'$I\n':
                    started.set()
                return result

            def read(self, size):
                if started.is_set():
                    unblock.wait(2)
                return super().read(size)

        with patch.object(controller, 'SimWire', SlowIdentity):
            self.assertTrue(self.machine.submit('connect', port='DEMO'))
            try:
                self.assertTrue(started.wait(3))
                self.assertFalse(self.machine.submit('connect', port='DEMO'))
                self.machine.stop()
            finally:
                unblock.set()
            events = self.event_until(lambda event: event == ('done', 'connect'))
        self.assertNotIn(('connected', 'DEMO'), events)
        self.assertIsNone(self.machine.session)
        self.assertFalse(self.machine.zero)


class NativeMotionTests(unittest.TestCase):
    def test_stop_uses_one_session_snapshot_during_disconnect(self):
        session = Mock()

        class LosingSession:
            def __init__(self, current):
                self.cancelled = threading.Event()
                self._session = current

            @property
            def session(self):
                current, self._session = self._session, None
                return current

        controller.Controller.stop(LosingSession(session))
        session.abort.assert_called_once_with()

    def test_lease_requires_observed_network_peer_even_if_both_peers_missing(self):
        machine = controller.Controller(start_worker=False)
        machine.session = SimpleNamespace(native_panel=True, wire=SimpleNamespace())
        panel = Mock(peer_address=None)
        with self.assertRaisesRegex(ValueError, 'той же MKS'):
            machine.execute_job('draw', {'panel_client': panel})
        panel.claim.assert_not_called()

    def test_screen_go_to_start_request_cannot_authorize_drawing(self):
        machine = controller.Controller(start_worker=False)
        peer = '192.168.1.8'
        machine.session = SimpleNamespace(native_panel=True, native_owned=False,
                                          wire=SimpleNamespace(peer_address=peer), coil_on=False)
        panel = Mock(peer_address=peer)
        panel.snapshot.return_value = SimpleNamespace(pc_owned=True, confirmed=True, boot_id=4,
                                                       generation=2, request_id=51, request_key=12)
        with patch.object(machine, 'execute') as execute:
            with self.assertRaisesRegex(ValueError, 'не соответствует'):
                machine.execute_job('draw', {'panel_client': panel, 'panel_request': (4, 2, 51, 12)})
        execute.assert_not_called()
        panel.respond.assert_not_called()
        panel.release.assert_called_once_with()

    def test_release_ack_does_not_succeed_if_board_still_reports_pc_ownership(self):
        panel = NativeDwinClient('tcp://192.168.1.8:8888')
        panel.connected.set()
        panel._phase = 'releasing'
        panel._state = NativeState(9, 1, 50, (0, 0), (0, 0), 0, 0, 2, 4, 0, 0, 0, time.monotonic())
        with self.assertRaisesRegex(OSError, 'продолжает считать ПК владельцем'):
            panel._on_ack(bytes((0, 0)), 'release')
        self.assertEqual(panel._phase, 'releasing')

    def test_panel_reboot_rejects_state_from_new_boot_on_old_connection(self):
        panel = NativeDwinClient('tcp://192.168.1.8:8888')
        panel._on_state(STATE_STRUCT.pack(1, 1, 50, 0, 0, 0, 0, 0, 0, 2, 4, 0, 0, 0))
        with self.assertRaisesRegex(OSError, 'перезапущена'):
            panel._on_state(STATE_STRUCT.pack(1, 1, 50, 0, 0, 0, 0, 0, 0, 2, 5, 0, 0, 0))
        self.assertEqual(panel.snapshot().boot_id, 4)


if __name__ == '__main__':
    unittest.main()
