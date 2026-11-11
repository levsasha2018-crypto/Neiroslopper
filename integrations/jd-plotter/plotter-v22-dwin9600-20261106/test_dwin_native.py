"""Offline checks for the native board/desktop handoff."""
from pathlib import Path
import queue
import sys
import threading
import time
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parent / "PROGRAM"))
import dwin_native as protocol
from controller import Controller, GrblSession
from geometry import Axes


def packet(kind, body=b""):
    return protocol.MAGIC + bytes((kind,)) + len(body).to_bytes(2, "big") + body


def board_state(*, owner=False, pending=22, boot=4, panel_online=True):
    flags = 1 | 2 | 4 | (32 if panel_online else 0) | (8 if owner else 0)
    return protocol.STATE_STRUCT.pack(flags, 1, 50, 2000, 3000, 1000, 1000,
                                      210000, 297000, 5, boot, pending, 13 if pending else 0, 10 if pending else 0)


class FakeWire:
    peer_address = "192.168.1.8"

    def __init__(self):
        self.incoming = queue.Queue()
        self.outgoing = []
        self.closed = False
        banner = packet(protocol.HELLO, protocol.HELLO_TEXT)
        self.incoming.put(banner[:4])
        self.incoming.put(banner[4:] + packet(protocol.STATE, board_state()) +
                          packet(protocol.EVENT, protocol.EVENT_STRUCT.pack(3, 250, 13, 10, 0x1000)))

    def read(self, count):
        try:
            return self.incoming.get(timeout=.01)
        except queue.Empty:
            return b""

    def write(self, data):
        kind, body = data[3], data[6:]
        self.outgoing.append((kind, body))
        if kind == protocol.HEARTBEAT:
            if body == bytes((protocol.CLAIM,)):
                self.incoming.put(packet(protocol.STATE, board_state(owner=True)) +
                                  packet(protocol.ACK, bytes((1, 0))))
            elif body == bytes((protocol.RENEW,)):
                self.incoming.put(packet(protocol.ACK, bytes((1, 0))))
        elif kind == protocol.RELEASE:
            self.incoming.put(packet(protocol.STATE, board_state()) + packet(protocol.ACK, bytes((0, 0))))
        return len(data)

    def close(self):
        self.closed = True


class NativeDwinTests(unittest.TestCase):
    def client(self, *, interactive=False):
        wire = FakeWire()
        client = protocol.NativeDwinClient("tcp://192.168.1.8:8888", expected_peer=wire.peer_address,
                                           wire_factory=lambda *args, **kwargs: wire, interactive=interactive)
        client.start()
        self.addCleanup(client.thread.join, 1)
        self.addCleanup(client.close)
        self.assertTrue(client.connected.wait(1))
        return client, wire

    def test_observer_receives_waiting_request_without_acquiring_motion(self):
        client, wire = self.client()
        self.assertEqual(client.snapshot().request_id, 22)
        self.assertTrue(client.snapshot().has_sheet)
        self.assertEqual(client.snapshot().origin_mm, (1, 1))
        self.assertFalse(client.lease_ready())
        for _ in range(30):
            logs = client.pending()
            if logs:
                break
            time.sleep(.01)
        self.assertEqual([(row.key, row.result_code) for row in logs], [(13, 10)])
        self.assertTrue(all(kind == protocol.PC_STATUS for kind, _ in wire.outgoing))

    def test_explicit_claim_and_release_and_duplicate_response(self):
        client, wire = self.client()
        self.assertFalse(client.respond(23, 0))
        self.assertTrue(client.claim())
        self.assertTrue(client.lease_ready())
        self.assertTrue(client.respond(22, 0))
        self.assertFalse(client.respond(22, 0))
        self.assertTrue(client.release())
        self.assertFalse(client.lease_ready())
        self.assertEqual(sum(kind == protocol.PC_RESULT for kind, _ in wire.outgoing), 1)

    def test_release_retries_temporary_owned_ack_without_replaying_motion(self):
        client, wire = self.client()
        self.assertTrue(client.claim())
        normal_write = wire.write

        def settling_write(data):
            if data[3] == protocol.RELEASE and not any(kind == protocol.RELEASE for kind, _ in wire.outgoing):
                wire.outgoing.append((protocol.RELEASE, b''))
                wire.incoming.put(packet(protocol.STATE, board_state(owner=True)) +
                                  packet(protocol.ACK, bytes((1, 0))))
                return len(data)
            return normal_write(data)

        with patch.object(wire, 'write', side_effect=settling_write):
            self.assertTrue(client.release())
        self.assertEqual(sum(kind == protocol.RELEASE for kind, _ in wire.outgoing), 2)
        self.assertTrue(client.connected.is_set())
        self.assertFalse(client.lease_ready())

    def test_slow_renew_stays_connected_until_ack_arrives(self):
        client, wire = self.client()
        renewed = threading.Event()
        original = wire.write

        def delay_renew(data):
            if data[3] == protocol.HEARTBEAT and data[6:] == bytes((protocol.RENEW,)) and not renewed.is_set():
                renewed.set()
                return len(data)
            return original(data)

        with patch.object(wire, 'write', side_effect=delay_renew):
            self.assertTrue(client.claim())
            self.assertTrue(renewed.wait(1))
            time.sleep(1.30)
            self.assertTrue(client.connected.is_set())
            wire.incoming.put(packet(protocol.STATE, board_state(owner=True)) +
                              packet(protocol.ACK, bytes((1, 0))))
            self.assertTrue(client.release())
        self.assertIsNone(client.error)

    def test_link_probe_requires_responsive_gui_and_leaves_request_pending(self):
        client, wire = self.client()
        challenge = (19).to_bytes(4, "big")
        wire.incoming.put(packet(protocol.PC_PROBE, challenge))
        for _ in range(50):
            with client._condition:
                if client._probe == challenge:
                    break
            time.sleep(.01)
        self.assertNotIn((protocol.PC_PONG, challenge), wire.outgoing)
        client.answer_probe()
        for _ in range(50):
            if (protocol.PC_PONG, challenge) in wire.outgoing:
                break
            time.sleep(.01)
        self.assertIn((protocol.PC_PONG, challenge), wire.outgoing)
        self.assertEqual(client.snapshot().request_id, 22)
        self.assertFalse(client.lease_ready())

    def test_assistant_and_preview_frames_do_not_acquire_motion(self):
        client, wire = self.client(interactive=True)
        text = "Как задать лист?"
        wire.incoming.put(packet(protocol.PANEL_QUERY, b"\x00\x00\x00\x09\x01" + text.encode("cp1251")))
        requests = []
        for _ in range(50):
            requests = client.panel_requests()
            if requests:
                break
            time.sleep(.01)
        self.assertEqual(requests, [(9, protocol.ASK, text)])
        client.panel_answer(9, 5, "Отметьте два угла.")
        client.panel_sheet(10, (1, 2, 416, 270), ((1, 2, 10, 20),) * protocol.MAX_SEGMENTS)
        for _ in range(50):
            if any(kind == protocol.PANEL_SHEET for kind, _ in wire.outgoing):
                break
            time.sleep(.01)
        outgoing = dict(wire.outgoing)
        self.assertIn(protocol.PANEL_READY, outgoing)
        self.assertEqual(outgoing[protocol.PANEL_ANSWER][5:].decode("cp1251"), "Отметьте два угла.")
        self.assertEqual(len(outgoing[protocol.PANEL_SHEET]), 12 + 8 * protocol.MAX_SEGMENTS)
        self.assertNotIn(protocol.HEARTBEAT, outgoing)
        self.assertEqual(client.snapshot().request_id, 22)

    def test_duplicate_panel_query_runs_only_once_even_after_queue_drain(self):
        client, wire = self.client(interactive=True)
        query = packet(protocol.PANEL_QUERY, b"\x00\x00\x00\x09\x01test")
        wire.incoming.put(query + query)
        for _ in range(50):
            requests = client.panel_requests()
            if requests:
                break
            time.sleep(.01)
        self.assertEqual(requests, [(9, protocol.ASK, "test")])
        cancel = packet(protocol.PANEL_QUERY, b"\x00\x00\x00\x09\x04")
        wire.incoming.put(query + cancel)
        for _ in range(50):
            requests = client.panel_requests()
            if requests:
                break
            time.sleep(.01)
        self.assertEqual(requests, [(9, protocol.CANCEL, "")])

    def test_panel_link_cycle_discards_old_queries_before_serving_new_ones(self):
        client, wire = self.client(interactive=True)
        probe = (1).to_bytes(4, "big")
        wire.incoming.put(packet(protocol.PANEL_QUERY, b"\x00\x00\x00\x09\x01old") +
                          packet(protocol.STATE, board_state(panel_online=False)) +
                          packet(protocol.PANEL_QUERY, b"\x00\x00\x00\x0a\x01offline") +
                          packet(protocol.STATE, board_state()) +
                          packet(protocol.PANEL_QUERY, b"\x00\x00\x00\x0b\x01new") +
                          packet(protocol.PC_PROBE, probe))
        for _ in range(50):
            if client._probe == probe:
                break
            time.sleep(.01)
        self.assertTrue(client.panel_interrupted())
        self.assertFalse(client.panel_interrupted())
        self.assertEqual(client.panel_requests(), [(11, protocol.ASK, "new")])

    def native_window(self):
        from dwin_ui import NativeDwinWindow

        window = NativeDwinWindow.__new__(NativeDwinWindow)
        window.app = Mock(connected=True, native_panel=True, controller=Mock(busy=False))
        window.closed = window._manual_reconnect = False
        window._motion_session = window.app.controller.session
        window._retry_at = None
        window._retry_delay = window.RETRY_INITIAL
        window.info = Mock()
        window.panel_assistant = None
        bridge = window.bridge = Mock(notices=queue.SimpleQueue())
        bridge.thread.is_alive.return_value = True
        bridge.pending.return_value = []
        bridge.snapshot.return_value = None
        bridge.connected.is_set.return_value = False
        return window, bridge

    def test_window_reconnect_waits_for_old_socket_worker(self):
        window, bridge = self.native_window()
        with patch.object(window, 'start') as start:
            window.reconnect()
            window.reconnect()
            window.pump()
            start.assert_not_called()
            bridge.close.assert_called_once()
            bridge.thread.is_alive.return_value = False
            window.pump()
            start.assert_called_once()

    def test_window_retries_failed_observer_only_while_idle_on_the_same_motion_link(self):
        window, bridge = self.native_window()
        bridge.thread.is_alive.return_value = False
        assistant = window.panel_assistant = Mock()
        with patch.object(window, 'start') as start, patch('dwin_ui.time.monotonic', return_value=10) as clock:
            window.pump()
            assistant.close.assert_called_once()
            window.app.apply_panel_state.assert_called_once_with(None)
            start.assert_not_called()
            self.assertEqual(window._retry_at, 10 + window.RETRY_INITIAL)
            clock.return_value = window._retry_at
            window.app.controller.busy = True
            window.pump()
            start.assert_not_called()
            window.app.controller.busy = False
            window.pump()
            start.assert_called_once_with()

    def test_window_does_not_reuse_old_observer_for_new_motion_session(self):
        window, bridge = self.native_window()
        window.app.controller.session = object()
        with patch.object(window, 'start') as start, patch.object(window, 'close') as close:
            window.pump()
            close.assert_called_once_with()
            start.assert_not_called()
            bridge.snapshot.assert_not_called()

    def test_window_starts_assistant_after_delayed_physical_panel_reply(self):
        window, bridge = self.native_window()
        window.app.paths = []
        window.app.progress = {'value': 0}
        bridge.interactive = True
        bridge.panel_interrupted.return_value = False
        bridge.panel_requests.return_value = []
        with patch('dwin_assistant.PanelAssistant') as assistant, patch('assistant.runtime.Assistant'):
            window.pump()  # TCP handshake is still in progress.
            bridge.connected.is_set.return_value = True
            bridge.snapshot.return_value = protocol.NativeState.decode(board_state(panel_online=False), time.monotonic())
            window.pump()  # TCP is ready, DWIN is still off.
            assistant.assert_not_called()
            bridge.snapshot.return_value = protocol.NativeState.decode(board_state(), time.monotonic())
            window.pump()
            window.pump()
            assistant.assert_called_once()
            self.assertEqual(window.panel_assistant, assistant.return_value)

    def test_confirmed_request_rechecked_after_ownership_before_motion(self):
        controller = Controller(start_worker=False)
        wire = SimpleNamespace(peer_address="192.168.1.8")
        session = SimpleNamespace(native_panel=True, native_owned=False, wire=wire,
                                  coil_on=False, abort=Mock(), refresh_g54_offset=Mock(),
                                  work_offset=(1.0, 1.0))
        controller.session = session
        controller.execute = Mock()
        panel = Mock(peer_address=wire.peer_address)
        panel.snapshot.return_value = protocol.NativeState.decode(board_state(owner=True), time.monotonic())
        params = dict(panel_client=panel, axes=Axes(), panel_request=(4, 3, 22, 13))
        with self.assertRaisesRegex(ValueError, "изменились"):
            controller.execute_job("draw", params)
        controller.execute.assert_not_called()
        panel.release.assert_called_once()
        session.abort.assert_not_called()
        params["panel_request"] = (4, 5, 22, 13)
        controller.execute_job("draw", params)
        controller.execute.assert_called_once()
        panel.respond.assert_called_once_with(22, 0)

    def test_passive_close_does_not_reset_local_motion(self):
        wire = Mock()
        session = GrblSession(wire, threading.Event(), Mock())
        session.trusted = session.native_panel = True
        session.close()
        wire.write.assert_not_called()
        wire.close.assert_called_once()


if __name__ == "__main__":
    unittest.main()
