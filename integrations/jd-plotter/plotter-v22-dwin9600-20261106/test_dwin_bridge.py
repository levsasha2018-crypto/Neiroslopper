"""DWIN regressions: fake serial, DEMO only; never opens physical hardware."""
import queue
import struct
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from controller import Controller, REQUIRED
from geometry import Axes, Sheet
from pen_lift import PenLiftSettings
from dwin_bridge import (DGUSParser, Upload, SerialBridge, CommandRouter, snapshot,
                         link_state, write_words, read_words, show_page, TOUCH_VP, STEP_VP,
                         SPEED_VP, RESULT_VP, LINK_VP, ARMED_VP, CURRENT_PAGE_VP, HOME_PAGE)


def upload(vp, words):
    payload = b"\x83" + struct.pack(">HB", vp, len(words)) + struct.pack(">" + "H" * len(words), *words)
    return b"\x5a\xa5" + bytes((len(payload),)) + payload


class Var:
    def __init__(self, value):
        self.value = value

    def get(self):
        return self.value

    def set(self, value):
        self.value = value


def fake_app():
    app = SimpleNamespace(
        closed=False, connected=True, zero=True, sheet=Sheet(210, 297), simulation=True,
        paths=[[(0, 0), (1, 1)]], settings=dict(REQUIRED), position=(12.34, -5.67),
        controller=SimpleNamespace(busy=False, cancelled=threading.Event()), svg_loading=False,
        step=Var("1"), feed=Var("50"), pen_enabled=Var(True), pen_continuous=Var(True),
        hold=Var(True), progress={"value": 42.5}, dwin_pen_state=1,
        axes=lambda: Axes(True, -1, 1), pen_ready=lambda: True,
    )
    app.drawing_sheet = lambda: app.sheet
    for name in ("jog", "set_zero", "set_corner", "go_start", "draw", "stop", "load_test",
                 "open_svg", "open_editor", "open_assistant", "open_camera", "submit", "change_hold", "note"):
        setattr(app, name, Mock())
    return app


class ParserTests(unittest.TestCase):
    def test_frames_and_word_byte_order(self):
        self.assertEqual(write_words(0x1200, (5,)), bytes.fromhex("5AA5058212000005"))
        self.assertEqual(read_words(0x1000), bytes.fromhex("5AA50483100001"))
        self.assertEqual(show_page(0), bytes.fromhex("5AA5078200845A010000"))
        self.assertEqual(show_page(7), bytes.fromhex("5AA5078200845A010007"))
        self.assertEqual(DGUSParser().feed(upload(TOUCH_VP, (13,)), 7), [Upload(TOUCH_VP, (13,), 7)])

    def test_every_split_and_single_byte_chunks(self):
        raw = upload(TOUCH_VP, (1,)) + upload(SPEED_VP, (65,))
        expected = [Upload(TOUCH_VP, (1,), 4), Upload(SPEED_VP, (65,), 4)]
        for split in range(len(raw) + 1):
            parser = DGUSParser()
            self.assertEqual(parser.feed(raw[:split], 4) + parser.feed(raw[split:], 4), expected)
        parser = DGUSParser()
        self.assertEqual([e for byte in raw for e in parser.feed(bytes((byte,)), 4)], expected)

    def test_noise_ack_write_bad_length_do_not_become_buttons(self):
        parser = DGUSParser()
        noise = b"noise\x5a\xa5\x00" + bytes.fromhex("5AA503824F4B") + write_words(TOUCH_VP, (13,))
        malformed = bytes.fromhex("5AA50683100002000D")
        self.assertEqual(parser.feed(noise + malformed + upload(TOUCH_VP, (14,)), 0),
                         [Upload(TOUCH_VP, (14,), 0)])

    def test_large_noise_buffer_bounded(self):
        parser = DGUSParser()
        parser.feed(b"x" * 100000 + b"\x5a")
        self.assertEqual(bytes(parser.buffer), b"\x5a")
        parser.feed(b"\xa5\xff" + b"x" * 1000)
        self.assertLessEqual(len(parser.buffer), 257)

    def test_corrupted_length_does_not_hide_following_stop(self):
        for bad in (bytes.fromhex("5AA5FF831000010001"),
                    bytes.fromhex("5AA5FE831000010001"),
                    bytes.fromhex("5AA5FE991000010001")):
            with self.subTest(header=bad.hex()):
                self.assertEqual(DGUSParser().feed(bad + upload(TOUCH_VP, (14,)), 4),
                                 [Upload(TOUCH_VP, (14,), 4)])

    def test_invalid_writes_rejected(self):
        for vp, words in ((-1, (1,)), (0x10000, (1,)), (1, ()), (1, (65536,)), (1, (True,)), (1, (0,) * 126)):
            with self.assertRaises(ValueError):
                write_words(vp, words)
        for page in (-1, 65536, True, 1.5):
            with self.assertRaises(ValueError):
                show_page(page)


class RouterTests(unittest.TestCase):
    def setUp(self):
        self.app = fake_app()
        self.router = CommandRouter(self.app)

    def send(self, vp=TOUCH_VP, value=1, **kw):
        return self.router.dispatch(Upload(vp, (value,), 5), armed=kw.get("armed", True),
                                    linked=kw.get("linked", True), now=kw.get("now", 5.2))[0]

    def test_remote_disabled_and_disconnected_never_jog(self):
        self.assertEqual(self.send(armed=False), 1)
        self.assertEqual(self.send(linked=False), 2)
        self.app.jog.assert_not_called()

    def test_stop_allowed_without_arming_even_stale_and_busy(self):
        self.app.controller.busy = True
        self.assertEqual(self.send(value=14, armed=False, now=90), 0)
        self.app.stop.assert_called_once_with()

    def test_received_stop_survives_subsequent_display_disconnect(self):
        self.assertEqual(self.send(value=14, armed=False, linked=False, now=90), 0)
        self.app.stop.assert_called_once_with()

    def test_busy_rejects_not_queue(self):
        self.app.controller.busy = True
        self.assertEqual(self.send(), 3)
        self.app.jog.assert_not_called()

    def test_old_or_future_commands_rejected(self):
        self.assertEqual(self.send(now=6.1), 8)
        self.assertEqual(self.send(now=4.9), 8)

    def test_axes_reuse_ui_directions_and_step(self):
        for command, delta in ((1, (-1, 0)), (2, (1, 0)), (3, (0, -1)), (4, (0, 1))):
            self.assertEqual(self.send(value=command), 0)
            self.app.jog.assert_called_with(*delta)
        self.assertEqual(self.send(STEP_VP, 4), 0)
        self.assertEqual(self.app.step.get(), "50")

    def test_unrepresentable_pc_step_never_moves_by_misleading_panel_preset(self):
        self.app.step.set("2,5")
        self.assertEqual(self.send(value=1), 6)
        self.app.jog.assert_not_called()
        self.assertEqual(self.send(STEP_VP, 4), 0)
        self.assertEqual(self.send(value=1), 0)

    def test_steps_and_feed_whitelist(self):
        self.assertEqual(self.send(STEP_VP, 6), 6)
        self.assertEqual(self.send(SPEED_VP, 0), 6)
        self.assertEqual(self.send(SPEED_VP, 101), 6)
        self.assertEqual(self.send(SPEED_VP, 100), 0)
        self.assertEqual(self.app.feed.get(), "100")

    def test_motion_profile_and_cancel_guards(self):
        self.app.settings[110] = 999
        self.assertEqual(self.send(), 7)
        self.app.settings = dict(REQUIRED)
        self.app.pen_ready = lambda: False
        self.assertEqual(self.send(), 7)
        self.app.pen_ready = lambda: True
        self.app.controller.cancelled.set()
        self.assertEqual(self.send(), 2)
        self.app.jog.assert_not_called()

    def test_calibration_and_draw_need_two_corners(self):
        self.app.zero = False
        self.assertEqual(self.send(value=11), 4)
        self.assertEqual(self.send(value=13), 4)
        self.app.zero = True
        self.app.sheet = None
        self.assertEqual(self.send(value=13), 4)
        self.app.draw.assert_not_called()

    def test_draw_reuses_existing_pc_confirmation(self):
        self.assertEqual(self.send(value=13), 9)
        self.app.draw.assert_called_once_with()
        self.app.submit.assert_not_called()

    def test_no_arbitrary_command_or_multiword(self):
        self.assertEqual(self.send(value=12345), 5)
        self.assertEqual(self.send(vp=0xFFFF), 5)
        self.assertEqual(self.router.dispatch(Upload(TOUCH_VP, (13, 14), 5), armed=True, linked=True, now=5)[0], 6)
        self.app.submit.assert_not_called()

    def test_pen_uses_explicit_profiled_controller_actions(self):
        self.assertEqual(self.send(value=19), 0)
        self.app.submit.assert_called_with("pen_up_hold")
        self.assertEqual(self.send(value=20), 0)
        self.app.submit.assert_called_with("pen_down_hold")
        self.app.pen_continuous.set(False)
        self.assertEqual(self.send(value=19), 7)

    def test_manual_pen_disabled_rejects_before_controller_submission(self):
        self.app.pen_enabled.set(False)
        self.assertEqual(self.send(value=19), 7)
        self.assertEqual(self.send(value=20), 7)
        self.app.submit.assert_not_called()

    def test_hold_toggle_uses_existing_public_app_method(self):
        self.assertEqual(self.send(value=21), 0)
        self.assertFalse(self.app.hold.get())
        self.app.change_hold.assert_called_once_with()
        self.assertEqual(self.send(value=21), 0)
        self.assertTrue(self.app.hold.get())
        self.app.controller.busy = True
        self.assertEqual(self.send(value=21), 3)
        self.assertTrue(self.app.hold.get())

    def test_telemetry_32bit_signed_hundredths_screen_mapping(self):
        state = snapshot(self.app, True)
        self.assertEqual(len(state), 16)
        self.assertEqual(state[0], 254)  # DEMO flag, never physical-GRBL flag.
        self.assertEqual(state[2:4], (0, 567))
        self.assertEqual(state[4:6], (0, 1234))
        self.assertEqual(state[6:10], (0, 21000, 0, 29700))
        self.assertEqual(state[11:], (42, 2, 1, 1, 1))
        self.app.position = (-3, 4)
        state = snapshot(self.app, False)
        self.assertEqual(state[2:6], (65535, 65136, 65535, 65236))

    def test_demo_and_cancelled_links_are_not_claimed_as_physical_grbl(self):
        self.assertEqual(link_state(self.app), 1)
        self.app.simulation = False
        self.assertEqual(link_state(self.app), 2)
        self.assertTrue(snapshot(self.app, False)[0] & 1)
        self.app.controller.cancelled.set()
        self.assertEqual(link_state(self.app), 1)
        self.assertFalse(snapshot(self.app, False)[0] & 1)
        self.assertEqual(link_state(self.app, linked=False), 0)

    def test_invalid_telemetry_does_not_break_the_desktop_pump(self):
        self.app.position = (float("inf"), float("nan"))
        self.app.progress["value"] = float("nan")
        state = snapshot(self.app, False)
        self.assertEqual(state[2:6], (0, 0, 0, 0))
        self.assertEqual(state[11], 0)
        self.app.position = (-1e308, 1e308)
        self.assertEqual(snapshot(self.app, False)[2:6], (0x8000, 0, 0x8000, 0))


class FakeWire:
    def __init__(self, *, short=False, polling=False):
        self.input = queue.Queue()
        self.writes = []
        self.closed = False
        self.short = short
        self.polling = polling
        self.key = 0
        self.selectors = (1, 50)

    def reset_input_buffer(self):
        while not self.input.empty():
            self.input.get_nowait()

    def write(self, data):
        self.writes.append(data)
        if self.polling:
            if data == read_words(TOUCH_VP):
                self.input.put(upload(TOUCH_VP, (self.key,)))
            if data == read_words(STEP_VP, 2):
                self.input.put(upload(STEP_VP, self.selectors))
            if data == write_words(TOUCH_VP, (0,)):
                self.key = 0
        return len(data) - 1 if self.short else len(data)

    def read(self, size):
        try:
            return self.input.get(timeout=.01)
        except queue.Empty:
            return b""

    def close(self):
        self.closed = True


def wait_for(predicate, timeout=1):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(.005)
    return False


class WorkerTests(unittest.TestCase):
    def setUp(self):
        self.wire = FakeWire()
        self.factory = Mock(return_value=self.wire)
        self.bridge = SerialBridge(self.factory)

    def tearDown(self):
        self.bridge.close()
        if self.bridge.thread:
            self.bridge.thread.join(1)

    def start(self, **kwargs):
        kwargs.setdefault("touch_mode", "upload")
        self.bridge.start("COM_TEST", forbidden_ports=("COM9",), **kwargs)
        self.assertTrue(self.bridge.connected.wait(1))

    def test_no_serial_until_explicit_start(self):
        self.factory.assert_not_called()
        self.start()
        self.factory.assert_called_once_with("COM_TEST", 115200, timeout=.05, write_timeout=.25)
        self.assertEqual(self.wire.writes[0], write_words(TOUCH_VP, (0,)))
        self.assertEqual(self.wire.writes[1], write_words(RESULT_VP, (0, 1, 0)))
        self.assertNotIn(show_page(0), self.wire.writes)
        self.assertNotIn(show_page(HOME_PAGE), self.wire.writes)

    def test_start_does_not_interrupt_splash_or_existing_navigation(self):
        clock = Mock(return_value=10.0)
        self.bridge = SerialBridge(self.factory, clock=clock)
        self.start()
        self.assertNotIn(read_words(CURRENT_PAGE_VP), self.wire.writes)
        clock.return_value = 12.01
        self.assertTrue(wait_for(lambda: read_words(CURRENT_PAGE_VP) in self.wire.writes))
        self.wire.input.put(upload(CURRENT_PAGE_VP, (4,)))
        time.sleep(.03)
        self.assertNotIn(show_page(HOME_PAGE), self.wire.writes)
        self.assertEqual(self.bridge.pending(), [])

    def test_boot_recovery_changes_only_old_unknown_page(self):
        for page in (9, 65535):
            with self.subTest(page=page):
                wire = FakeWire()
                clock = Mock(return_value=10.0)
                bridge = SerialBridge(Mock(return_value=wire), clock=clock)
                try:
                    bridge.start("COM_TEST", touch_mode="upload")
                    self.assertTrue(bridge.connected.wait(1))
                    clock.return_value = 12.01
                    self.assertTrue(wait_for(lambda: read_words(CURRENT_PAGE_VP) in wire.writes))
                    wire.input.put(upload(CURRENT_PAGE_VP, (page,)))
                    self.assertTrue(wait_for(lambda: show_page(HOME_PAGE) in wire.writes))
                    self.assertEqual(bridge.pending(), [])
                finally:
                    bridge.close()
                    bridge.thread.join(1)

    def test_native_splash_is_never_shortened_by_bridge(self):
        clock = Mock(return_value=10.0)
        self.bridge = SerialBridge(self.factory, clock=clock)
        self.start()
        clock.return_value = 12.01
        self.assertTrue(wait_for(lambda: read_words(CURRENT_PAGE_VP) in self.wire.writes))
        self.wire.input.put(upload(CURRENT_PAGE_VP, (0,)))
        time.sleep(.03)
        self.assertNotIn(show_page(HOME_PAGE), self.wire.writes)
        self.assertEqual(CURRENT_PAGE_VP, 0x0014)

    def test_grbl_port_case_insensitive_blocked_before_factory(self):
        with self.assertRaises(ValueError):
            self.bridge.start("com9", forbidden_ports=("COM9",))
        self.factory.assert_not_called()

    def test_real_transport_defaults_to_passive_preflight(self):
        self.assertEqual(SerialBridge().preflight_duration, 1.5)
        self.assertEqual(self.bridge.preflight_duration, 0)
        for duration in (-1, float("nan"), float("inf"), 11):
            with self.assertRaises(ValueError):
                SerialBridge(preflight_duration=duration)

    def test_passive_preflight_rejects_grbl_before_any_binary_write(self):
        for banner in (b"Grbl 1.1h ['$' for help]\r\n", b"[PEN:A3,ACTIVE=LOW]\r\n",
                       b"[VER:1.1h]\r\n", b"[MSG:Reset to continue]\r\n"):
            with self.subTest(banner=banner):
                wire = FakeWire()
                wire.input.put(banner[:3])
                wire.input.put(banner[3:])
                bridge = SerialBridge(Mock(return_value=wire), preflight_duration=.05)
                bridge.start("COM_UNKNOWN")
                bridge.thread.join(1)
                self.assertFalse(bridge.connected.is_set())
                self.assertIn("GRBL", bridge.error)
                self.assertEqual(wire.writes, [])
                self.assertTrue(wire.closed)

    def test_cancel_during_passive_preflight_does_not_write_goodbye_frames(self):
        self.bridge = SerialBridge(self.factory, preflight_duration=.5)
        self.bridge.start("COM_UNKNOWN")
        self.assertTrue(wait_for(lambda: self.factory.called))
        self.bridge.close()
        self.bridge.thread.join(1)
        self.assertEqual(self.wire.writes, [])
        self.assertFalse(self.bridge.connected.is_set())
        self.assertIsNone(self.bridge.error)

    def test_passive_preflight_allows_dgus_after_listening_without_writes(self):
        self.wire.input.put(upload(TOUCH_VP, (13,)))
        self.bridge = SerialBridge(self.factory, preflight_duration=.04)
        self.bridge.start("COM_UNKNOWN", touch_mode="upload")
        self.assertTrue(self.bridge.connected.wait(1))
        self.assertEqual(self.wire.writes[0], write_words(TOUCH_VP, (0,)))
        self.assertEqual(self.bridge.pending(), [])  # old DRAW never replayed

    def test_single_release_duplicate_guard_and_ack_ignore(self):
        self.start()
        self.wire.input.put(upload(TOUCH_VP, (1,)) * 2 + bytes.fromhex("5AA503824F4B"))
        self.assertTrue(wait_for(lambda: self.bridge.events.qsize() == 1))
        time.sleep(.03)
        self.assertEqual([e.words for e in self.bridge.pending()], [(1,)])

    def test_stop_priority_clears_pending_motion(self):
        self.start()
        for _ in range(64):
            self.bridge.events.put_nowait(Upload(TOUCH_VP, (1,), time.monotonic()))
        self.wire.input.put(upload(TOUCH_VP, (14,)))
        self.assertTrue(self.bridge.stop_requested.wait(1))
        self.assertEqual([e.words for e in self.bridge.pending()], [(14,)])
        self.assertTrue(self.bridge.events.empty())

    def test_stop_at_end_of_full_chunk_beats_queue_overflow(self):
        self.start()
        for _ in range(64):
            self.bridge.events.put_nowait(Upload(TOUCH_VP, (1,), time.monotonic()))
        self.wire.input.put(upload(TOUCH_VP, (1,)) + upload(TOUCH_VP, (14,)))
        self.assertTrue(self.bridge.stop_requested.wait(1))
        self.assertEqual([e.words for e in self.bridge.pending()], [(14,)])
        self.assertIsNone(self.bridge.error)

    def test_stop_is_kept_even_if_clear_write_fails(self):
        self.start()
        self.wire.short = True
        self.wire.input.put(upload(TOUCH_VP, (14,)))
        self.bridge.thread.join(1)
        self.assertFalse(self.bridge.connected.is_set())
        self.assertIn("Неполная запись", self.bridge.error)
        self.assertEqual([e.words for e in self.bridge.pending()], [(14,)])

    def test_partial_write_fails_closed_no_retry(self):
        self.wire.short = True
        self.bridge.start("COM_TEST")
        self.bridge.thread.join(1)
        self.assertFalse(self.bridge.connected.is_set())
        self.assertIn("Неполная запись", self.bridge.error)
        self.assertEqual(len(self.wire.writes), 1)
        self.assertTrue(self.wire.closed)

    def test_latest_telemetry_replaces_old_queue(self):
        self.bridge.connected.set()
        self.bridge.publish([b"old"])
        self.bridge.publish([b"new"])
        self.assertEqual(self.bridge.latest, (b"new",))
        self.bridge.close()
        self.assertIsNone(self.bridge.latest)

    def test_poll_mode_receives_button_then_clears_value(self):
        self.wire.polling = True
        self.start(touch_mode="poll")
        self.wire.key = 13
        self.assertTrue(wait_for(lambda: not self.bridge.events.empty()))
        self.assertEqual([e.words for e in self.bridge.pending()], [(13,)])
        self.assertEqual(self.wire.key, 0)
        time.sleep(.3)
        self.assertEqual(self.bridge.pending(), [])

    def test_poll_stop_not_dropped_by_previous_latched_jog(self):
        self.start(touch_mode="poll")
        self.wire.input.put(upload(TOUCH_VP, (1,)))
        self.assertTrue(wait_for(lambda: self.bridge.events.qsize() == 1))
        self.wire.input.put(upload(TOUCH_VP, (14,)))
        self.assertTrue(self.bridge.stop_requested.wait(1))
        self.assertEqual([e.words for e in self.bridge.pending()], [(14,)])

    def test_poll_different_key_not_dropped_while_waiting_for_zero(self):
        self.start(touch_mode="poll")
        self.wire.input.put(upload(TOUCH_VP, (1,)))
        self.assertTrue(wait_for(lambda: self.bridge.events.qsize() == 1))
        self.bridge.pending()
        first_poll_count = self.wire.writes.count(read_words(TOUCH_VP))
        self.assertTrue(wait_for(lambda: self.wire.writes.count(read_words(TOUCH_VP)) > first_poll_count))
        self.wire.input.put(upload(TOUCH_VP, (2,)))
        self.assertTrue(wait_for(lambda: self.bridge.events.qsize() == 1))
        self.assertEqual([e.words for e in self.bridge.pending()], [(2,)])

    def test_polled_step_and_speed_arrive_before_following_jog(self):
        self.wire.polling = True
        self.start(touch_mode="poll")
        self.assertTrue(wait_for(lambda: read_words(STEP_VP, 2) in self.wire.writes))
        time.sleep(.03)
        self.wire.selectors = (5, 100)
        self.wire.key = 1
        self.assertTrue(wait_for(lambda: self.bridge.events.qsize() == 3))
        self.assertEqual([(e.vp, e.words) for e in self.bridge.pending()],
                         [(STEP_VP, (5,)), (SPEED_VP, (100,)), (TOUCH_VP, (1,))])

    def test_poll_selectors_no_initial_replay_then_changed_words(self):
        self.wire.polling = True
        self.start(touch_mode="poll")
        self.assertTrue(wait_for(lambda: read_words(STEP_VP, 2) in self.wire.writes))
        time.sleep(.03)
        self.assertEqual(self.bridge.pending(), [])
        self.wire.selectors = (5, 80)
        self.assertTrue(wait_for(lambda: self.bridge.events.qsize() == 2))
        self.assertEqual([(e.vp, e.words) for e in self.bridge.pending()], [(STEP_VP, (5,)), (SPEED_VP, (80,))])

    def test_orderly_close_clears_link_label(self):
        self.start()
        self.bridge.close()
        self.bridge.thread.join(1)
        self.assertEqual(self.wire.writes[-1], write_words(LINK_VP, (0, 0)))
        from dwin_bridge import STATUS_VP
        self.assertIn(write_words(STATUS_VP, (0,) * 16), self.wire.writes[-3:])

    def test_restart_drops_previous_connection_notices(self):
        self.bridge.notices.put(("error", "Previous connection"))
        self.start()
        self.assertEqual(self.bridge.notices.get_nowait(), ("connected", "COM_TEST"))
        self.assertTrue(self.bridge.notices.empty())


class PersistentPenTests(unittest.TestCase):
    def setUp(self):
        self.controller = Controller(start_worker=False)
        self.profile = PenLiftSettings(True, True, True, 50, 50, True, True)
        self.axes = Axes(True, -1, 1)
        with patch("serial.Serial", side_effect=AssertionError("Physical serial is forbidden")):
            self.controller.execute("connect", {"port": "DEMO", "axes": self.axes, "pen": self.profile})

    def tearDown(self):
        self.controller.disconnect()

    def test_up_really_persists_and_down_releases_no_axis_motion(self):
        session = self.controller.session
        initial = session.wire.position[:]
        self.controller.execute("pen_up_hold", {"axes": self.axes, "pen": self.profile})
        self.assertTrue(session.coil_on)
        self.assertFalse(session.pen_down)
        self.assertEqual(session.wire.position, initial)
        self.controller.execute("pen_down_hold", {"axes": self.axes, "pen": self.profile})
        self.assertFalse(session.coil_on)
        self.assertTrue(session.pen_down)
        self.assertEqual(session.wire.position, initial)

    def test_noncontinuous_up_rejected_without_axis_motion(self):
        bounded = PenLiftSettings(True, True, True, 50, 50, True, False)
        # A matching bounded report reaches the manual-state guard, not merely
        # the independent firmware mismatch check.
        from pen_lift import firmware_profile
        self.controller.session.pen_firmware = firmware_profile(False, False)
        with self.assertRaisesRegex(ValueError, "непрерывного"):
            self.controller.execute("pen_up_hold", {"axes": self.axes, "pen": bounded})
        self.assertFalse(self.controller.session.coil_on)


class HeadlessPumpTests(unittest.TestCase):
    """Exercise DwinWindow.pump without creating any Tk or serial window."""
    def setUp(self):
        from dwin_ui import DwinWindow
        self.panel = DwinWindow.__new__(DwinWindow)
        self.app = self.panel.app = fake_app()
        self.panel.closed = False
        self.panel.bridge = SerialBridge()
        self.panel.router = CommandRouter(self.app)
        self.panel.last_status = self.panel.last_result = 0
        self.panel.last_selectors = None
        self.panel.armed_since = 0
        self.panel.armed = Var(True)
        self.panel.info = Var("")
        self.panel.button = Mock()
        self.panel.ports = Mock()
        self.panel.poll_button = Mock()
        self.panel.bridge.connected.set()

    def tearDown(self):
        self.panel.bridge.close()

    def test_demo_badge_never_claims_real_machine(self):
        self.panel.pump()
        self.assertIn(write_words(RESULT_VP, (0, 1, 1)), self.panel.bridge.latest)
        self.app.simulation = False
        self.panel.last_status = 0
        self.panel.pump()
        self.assertIn(write_words(RESULT_VP, (0, 2, 1)), self.panel.bridge.latest)

    def test_disconnect_drops_arming_and_old_events(self):
        self.panel.bridge.connected.clear()
        self.panel.bridge.notices.put(("disconnected", "COM_TEST"))
        self.panel.bridge.events.put_nowait(Upload(TOUCH_VP, (1,), time.monotonic()))
        self.panel.pump()
        self.assertFalse(self.panel.armed.get())
        self.assertEqual(self.panel.armed_since, float("inf"))
        self.assertEqual(self.panel.last_result, 2)
        self.app.jog.assert_not_called()

    def test_rejected_selector_is_restored_to_real_pc_value(self):
        self.panel.armed.set(False)
        self.panel.last_selectors = (1, 50)
        self.panel.bridge.events.put_nowait(Upload(STEP_VP, (5,), time.monotonic()))
        self.panel.pump()
        self.assertEqual(self.panel.last_result, 1)
        self.assertIn(write_words(STEP_VP, (1,)), self.panel.bridge.latest)

    def test_pending_stop_wins_over_ui_batch_on_recent_disconnect(self):
        self.panel.bridge.connected.clear()
        self.panel.bridge.events.put_nowait(Upload(TOUCH_VP, (1,), time.monotonic()))
        self.panel.bridge.stop_requested.set()
        self.panel.pump()
        self.app.stop.assert_called_once_with()
        self.app.jog.assert_not_called()


class DwinUITests(unittest.TestCase):
    def setUp(self):
        import tkinter as tk
        from app import App
        self.root = tk.Tk()
        self.root.withdraw()
        with patch("serial.tools.list_ports.comports", return_value=[]):
            self.app = App(self.root)
            self.app.open_dwin()
        self.panel = self.app.dwin_window
        self.panel.window.withdraw()

    def tearDown(self):
        self.app.close()

    def test_open_dialog_does_not_connect_or_arm(self):
        self.assertIsNone(self.panel.bridge.thread)
        self.assertFalse(self.panel.armed.get())
        self.assertTrue(self.panel.poll_mode.get())

    def test_second_identical_uno_is_visible_but_main_ports_are_excluded(self):
        boards = [SimpleNamespace(device=name, vid=0x2341, pid=0x0043)
                  for name in ("COM9", "COM10", "COM11")]
        self.app.port.set("com9")
        self.app.port_name = "com11"
        with patch("serial.tools.list_ports.comports", return_value=boards):
            self.panel.refresh()
        self.assertEqual(tuple(self.panel.ports["values"]), ("COM10",))
        self.assertEqual(self.panel.port.get(), "COM10")
        self.assertIsNone(self.panel.bridge.thread)

    def test_refresh_tracks_changed_main_selection_without_guessing_usb_id(self):
        boards = [SimpleNamespace(device=name, vid=0x2341, pid=0x0043)
                  for name in ("COM9", "COM10")]
        with patch("serial.tools.list_ports.comports", return_value=boards):
            self.app.port.set("COM9")
            self.panel.refresh()
            self.assertEqual(self.panel.port.get(), "COM10")
            self.app.port.set("COM10")
            self.panel.refresh()
        self.assertEqual(tuple(self.panel.ports["values"]), ("COM9",))
        self.assertEqual(self.panel.port.get(), "COM9")

    def test_stale_display_selection_cannot_open_newly_selected_main_port(self):
        self.panel.port.set("COM10")
        self.app.port.set("com10")
        with patch("dwin_ui.messagebox.showerror") as message, patch("serial.Serial") as serial:
            self.panel.toggle()
        message.assert_called_once()
        serial.assert_not_called()
        self.assertIsNone(self.panel.bridge.thread)

    def test_refresh_does_not_relabel_already_open_display_port(self):
        self.panel.port.set("COM10")
        self.panel.bridge.port = "COM10"
        self.panel.bridge.thread = Mock()
        self.panel.bridge.thread.is_alive.return_value = True
        self.app.port.set("COM10")
        boards = [SimpleNamespace(device=name, vid=0x2341, pid=0x0043)
                  for name in ("COM9", "COM10")]
        with patch("serial.tools.list_ports.comports", return_value=boards):
            self.panel.refresh()
        self.assertEqual(tuple(self.panel.ports["values"]), ("COM9",))
        self.assertEqual(self.panel.port.get(), "COM10")

    def test_arm_requires_live_separate_bridge(self):
        self.panel.armed.set(True)
        self.panel.arm()
        self.assertFalse(self.panel.armed.get())

    def test_key_received_before_arming_cannot_start_motion(self):
        self.panel.bridge.connected.set()
        received = time.monotonic() - .2
        self.panel.bridge.events.put_nowait(Upload(TOUCH_VP, (1,), received))
        self.panel.armed.set(True)
        self.panel.arm()
        with patch.object(self.app, "jog") as jog:
            self.panel.pump()
        jog.assert_not_called()
        self.assertEqual(self.panel.last_result, 1)

    def test_grbl_connect_cannot_reuse_live_display_port(self):
        self.panel.bridge.port = "COM_TEST"
        self.panel.bridge.thread = Mock()
        self.panel.bridge.thread.is_alive.return_value = True
        self.app.port.set("COM_TEST")
        with patch("app.messagebox.showerror") as message, patch.object(self.app, "submit") as submit:
            self.app.connect()
        submit.assert_not_called()
        message.assert_called_once()

    def test_telemetry_has_contiguous_result_link_and_armed_words(self):
        self.panel.bridge.connected.set()
        self.panel.armed.set(True)
        self.panel.arm()
        self.panel.last_result = 6
        self.panel.pump()
        self.assertIn(write_words(RESULT_VP, (6, 1, 1)), self.panel.bridge.latest)
        self.assertEqual((RESULT_VP, LINK_VP, ARMED_VP), (0x1110, 0x1111, 0x1112))

    def test_rejected_selector_restores_actual_pc_value(self):
        self.panel.bridge.connected.set()
        self.panel.last_selectors = (1, 50)
        self.app.step.set("1")
        self.app.feed.set("50")
        self.panel.bridge.events.put_nowait(Upload(STEP_VP, (5,), time.monotonic()))
        self.panel.pump()
        self.assertEqual(self.panel.last_result, 1)
        self.assertEqual(self.app.step.get(), "1")
        self.assertIn(write_words(STEP_VP, (1,)), self.panel.bridge.latest)
        self.assertIn(write_words(SPEED_VP, (50,)), self.panel.bridge.latest)


if __name__ == "__main__":
    unittest.main()
