"""Offline diagnostics: no windows, serial ports, sockets or motor actions."""
import json
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
import threading
from types import SimpleNamespace, MethodType
import unittest
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / "PROGRAM"))
from eventlog import EventLog, record_event
from dwin_bridge import CommandRouter, SerialBridge, Upload, TOUCH_VP
from dgus_tunnel import DgusTunnelWire
from app import App
from test_dwin_bridge import fake_app, FakeWire
from test_dgus_tunnel import Clock, Wire


def rows(log):
    return [json.loads(line) for line in log.path.read_text().splitlines()]


class EventLogTests(unittest.TestCase):
    def test_parallel_records_are_complete_json_and_exclude_private_payloads(self):
        with TemporaryDirectory() as directory:
            log = EventLog(directory)
            def write():
                for i in range(40):
                    log.record("screen_button", vp=4096, key=i, key_name="left",
                               gcode="G0 X999", raw_frame="private-data", reason="secret long message")
            threads = [threading.Thread(target=write) for _ in range(4)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join()
            saved = rows(log)
            self.assertEqual(len(saved), 160)
            self.assertTrue(all(row["vp"] == 4096 for row in saved))
            text = log.path.read_text()
            self.assertNotIn("G0 X999", text)
            self.assertNotIn("private-data", text)
            self.assertNotIn("secret", text)

    def test_rotation_is_bounded_and_every_retained_line_is_json(self):
        with TemporaryDirectory() as directory:
            log = EventLog(directory, max_bytes=8192, backups=2)
            for i in range(1000):
                log.record("screen_dispatch_result", key=i, key_name="right", result_code=0)
            files = list(log.path.parent.iterdir())
            self.assertEqual(len(files), 3)
            self.assertLessEqual(sum(path.stat().st_size for path in files), 3 * 8192)
            for path in files:
                for line in path.read_text().splitlines():
                    self.assertEqual(json.loads(line)["event"], "screen_dispatch_result")

    def test_logging_failure_does_not_change_command_dispatch(self):
        app = fake_app()
        app.eventlog = Mock()
        app.eventlog.record.side_effect = PermissionError("disk unavailable")
        result, _ = CommandRouter(app).dispatch(Upload(TOUCH_VP, (14,), 1),
                                               armed=False, linked=True, now=1.1)
        self.assertEqual(result, 0)
        app.stop.assert_called_once_with()
        record_event(SimpleNamespace(), "optional_no_logger")
        with TemporaryDirectory() as directory:
            log = EventLog(directory)
            with patch.object(Path, "mkdir", side_effect=PermissionError):
                log.record("write_failure")  # No exception escapes to worker/UI.

    def test_button_is_persisted_before_dispatch_and_result_afterwards(self):
        with TemporaryDirectory() as directory:
            app = fake_app()
            app.eventlog = EventLog(directory)
            def observe_stop():
                self.assertEqual(rows(app.eventlog)[-1]["event"], "screen_dispatch_begin")
            app.stop.side_effect = observe_stop
            result, _ = CommandRouter(app).dispatch(Upload(TOUCH_VP, (14,), 1),
                                                   armed=False, linked=True, now=1.1)
            saved = rows(app.eventlog)
            self.assertEqual([row["event"] for row in saved],
                             ["screen_dispatch_begin", "screen_dispatch_result"])
            self.assertEqual(saved[0]["key_name"], "stop")
            self.assertEqual(saved[1]["result_code"], result)

    def test_first_lease_loss_records_panel_ack_and_ui_ages_once(self):
        with TemporaryDirectory() as directory:
            bridge = Mock()
            bridge.diagnostic_state.return_value = {"ack_age_s": 1.2, "panel_age_s": 2.4,
                                                     "ui_age_s": .06, "lease_ready": False}
            app = SimpleNamespace(eventlog=EventLog(directory), connected=True, simulation=False,
                                  dwin_window=SimpleNamespace(bridge=bridge),
                                  network_controller=lambda: True, network_lease_ready=lambda: False,
                                  controller=SimpleNamespace(cancelled=threading.Event()),
                                  network_lease_seen=True, stop=Mock(), status=Mock())
            App.update_network_lease(app)
            App.update_network_lease(app)
            saved = rows(app.eventlog)
            self.assertEqual(len(saved), 1)
            self.assertEqual(saved[0]["event"], "network_lease_first_loss")
            self.assertEqual(saved[0]["panel_age_s"], 2.4)
            app.stop.assert_called_once_with()

    def test_real_stop_records_screen_caller_without_changing_stop_behavior(self):
        with TemporaryDirectory() as directory:
            app = fake_app()
            app.eventlog = EventLog(directory)
            app.preview_revision = 0
            for name in ("preserve_draft", "cancel_svg", "rebase_editor_history", "draft_notice",
                         "invalidate_editor", "update_controls", "redraw"):
                setattr(app, name, Mock())
            app.paper_text, app.status = Mock(), Mock()
            app.controller.stop = Mock()
            app.stop = MethodType(App.stop, app)
            CommandRouter(app).dispatch(Upload(TOUCH_VP, (14,), 1),
                                        armed=False, linked=True, now=1.1)
            saved = rows(app.eventlog)
            self.assertEqual(saved[1]["event"], "stop_requested")
            self.assertEqual(saved[1]["reason"], "screen_stop")
            self.assertEqual(saved[1]["caller"], "_dispatch")
            self.assertFalse(app.zero)
            app.controller.stop.assert_called_once_with()

    def test_worker_error_is_persisted_before_disconnect(self):
        with TemporaryDirectory() as directory:
            log = EventLog(directory)
            bridge = SerialBridge(Mock(return_value=FakeWire(short=True)), eventlog=log)
            bridge.start("COM_TEST", touch_mode="upload")
            bridge.thread.join(1)
            self.assertFalse(bridge.thread.is_alive())
            saved = rows(log)
            self.assertEqual([row["event"] for row in saved],
                             ["screen_wire_error", "screen_wire_disconnected"])
            self.assertEqual(saved[0]["error_type"], "OSError")
            self.assertIn("panel_age_s", saved[0])

    def test_tunnel_snapshot_and_bridge_snapshot_do_not_write_or_deadlock(self):
        clock = Clock()
        wire = Wire(clock)
        tunnel = DgusTunnelWire("tcp://192.168.1.10:8888", wire=wire, clock=clock)
        state = tunnel.diagnostic_state()
        self.assertIsNone(state["ack_age_s"])
        self.assertFalse(state["lease_ready"])
        self.assertEqual(wire.sent, [])
        bridge = SerialBridge(clock=clock)
        bridge.network_wire = tunnel
        snapshot = bridge.diagnostic_state()
        self.assertIsNone(snapshot["panel_age_s"])
        self.assertFalse(snapshot["connected"])
        tunnel.close()


if __name__ == "__main__":
    unittest.main()
