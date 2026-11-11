"""Offline JD-DGUS/LAN tests. No hardware, network, or visible UI is opened."""
import unittest
from unittest.mock import Mock

from dgus_tunnel import (ACK, HEARTBEAT, HELLO, HELLO_TEXT, UART_RX, UART_TX,
                         DgusTunnelWire, TunnelParser, frame)
from dwin_bridge import CommandRouter, SerialBridge, TOUCH_VP, Upload
from dwin_ui import display_endpoint
from test_dwin_bridge import fake_app, FakeWire, wait_for
from dwin_bridge import STEP_VP, read_words


class Clock:
    def __init__(self):
        self.now = 10.0

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


class Wire:
    def __init__(self, clock, hello=HELLO_TEXT, *, acknowledgements=True):
        self.clock = clock
        self.incoming = bytearray(frame(HELLO, hello)) if hello is not None else bytearray()
        self.sent = []
        self.closed = False
        self.acknowledgements = acknowledgements
        self.short_write = False

    def read(self, count):
        self.clock.advance(.02)
        result = bytes(self.incoming[:count])
        del self.incoming[:count]
        return result

    def write(self, data):
        self.sent.append(bytes(data))
        if self.short_write:
            return len(data) - 1
        if self.acknowledgements:
            for kind, payload in TunnelParser().feed(data):
                if kind == HEARTBEAT:
                    self.incoming.extend(frame(ACK, payload + b"\0"))
        return len(data)

    def close(self):
        self.closed = True


class ParserTests(unittest.TestCase):
    def test_byte_order_and_every_fragment(self):
        raw = frame(UART_TX, b"\x5a\xa5")
        self.assertEqual(raw, bytes.fromhex("4A44010100025AA5"))
        combined = raw + frame(ACK, b"\1\0")
        expected = [(UART_TX, b"\x5a\xa5"), (ACK, b"\1\0")]
        for split in range(len(combined) + 1):
            parser = TunnelParser()
            self.assertEqual(parser.feed(combined[:split]) + parser.feed(combined[split:]), expected)
        parser = TunnelParser()
        self.assertEqual([event for byte in combined for event in parser.feed(bytes([byte]))], expected)

    def test_malformed_headers_fail_closed(self):
        for data in (b"GRBL!!", b"JD\2\1\0\0", b"JD\1\x66\0\0", b"JD\1\1\2\1"):
            with self.subTest(data=data), self.assertRaises(OSError):
                TunnelParser().feed(data)

    def test_bounded_buffers_and_unknown_type(self):
        with self.assertRaises(OSError):
            TunnelParser().feed(b"x" * 8193)
        for kind, data in ((0x66, b""), (UART_TX, b"x" * 513)):
            with self.assertRaises(ValueError):
                frame(kind, data)


class TunnelTests(unittest.TestCase):
    def make_tunnel(self, *, hello=HELLO_TEXT, acknowledgements=True):
        clock = Clock()
        wire = Wire(clock, hello, acknowledgements=acknowledgements)
        wanted = [False]
        tunnel = DgusTunnelWire("tcp://192.168.1.10:8888", wire=wire, clock=clock,
                                lease_provider=lambda: wanted[0])
        return tunnel, wire, clock, wanted

    def test_no_display_data_before_hello_and_reject_wrong_version(self):
        tunnel, wire, _, _ = self.make_tunnel()
        self.assertTrue(tunnel.hello)
        self.assertEqual(wire.sent, [])
        clock = Clock()
        wire = Wire(clock, b"JD-DGUS/2")
        with self.assertRaises(OSError):
            DgusTunnelWire("tcp://192.168.1.10:8888", wire=wire, clock=clock)
        self.assertTrue(wire.closed)
        self.assertEqual(wire.sent, [])

    def test_missing_hello_times_out_without_writes(self):
        clock = Clock()
        wire = Wire(clock, None)
        with self.assertRaises(TimeoutError):
            DgusTunnelWire("tcp://192.168.1.10:8888", wire=wire, clock=clock, hello_timeout=.1)
        self.assertTrue(wire.closed)
        self.assertEqual(wire.sent, [])

    def test_wrong_port_public_ip_and_uart_speed_rejected_before_open(self):
        for address, speed in (("tcp://192.168.1.10:23", 115200),
                               ("tcp://8.8.8.8:8888", 115200),
                               ("tcp://192.168.1.10:8888", 38400)):
            with self.subTest(address=address, speed=speed), self.assertRaises(ValueError):
                DgusTunnelWire(address, speed)

    def test_panel_9600_and_legacy_115200_keep_native_frames_and_start_unarmed(self):
        for baud in (9600, 115200):
            clock = Clock()
            wire = Wire(clock)
            tunnel = DgusTunnelWire("tcp://192.168.1.10:8888", baud,
                                    wire=wire, clock=clock)
            self.assertEqual(tunnel.baudrate, baud)
            version_query = bytes.fromhex("5AA50483000F01")
            tunnel.write(version_query)
            packets = [packet for data in wire.sent for packet in TunnelParser().feed(data)]
            self.assertIn((UART_TX, version_query), packets)
            self.assertIn((HEARTBEAT, b"\0"), packets)
            self.assertFalse(tunnel.lease_ready())
            wire.incoming.extend(frame(UART_RX, bytes.fromhex("5AA50683000F016322")))
            self.assertEqual(tunnel.read(100), bytes.fromhex("5AA50683000F016322"))
            tunnel.close()

    def test_native_uart_round_trip_and_ack_not_leaked(self):
        tunnel, wire, _, _ = self.make_tunnel()
        raw = bytes.fromhex("5AA50483100001")
        self.assertEqual(tunnel.write(raw), len(raw))
        self.assertEqual(TunnelParser().feed(wire.sent[-1]), [(UART_TX, raw)])
        wire.incoming.extend(frame(UART_RX, raw))
        self.assertEqual(tunnel.read(3), raw[:3])
        self.assertEqual(tunnel.read(200), raw[3:])
        self.assertEqual(tunnel.in_waiting, 0)

    def test_lease_requires_fresh_positive_ack_and_live_ui(self):
        tunnel, _, clock, wanted = self.make_tunnel()
        tunnel.read(1)
        self.assertFalse(tunnel.lease_ready())
        wanted[0] = True
        self.assertFalse(tunnel.lease_ready())
        tunnel.read(1)
        self.assertTrue(tunnel.lease_ready())
        clock.advance(1.01)
        self.assertFalse(tunnel.lease_ready())
        tunnel.read(1)
        self.assertTrue(tunnel.lease_ready())
        wanted[0] = False
        self.assertFalse(tunnel.lease_ready())
        tunnel.read(1)
        self.assertFalse(tunnel.ack_owned)

    def test_no_ack_ever_times_out_and_no_retry(self):
        tunnel, wire, clock, wanted = self.make_tunnel(acknowledgements=False)
        wanted[0] = True
        tunnel.read(1)
        clock.advance(1.51)
        with self.assertRaises(TimeoutError):
            tunnel.read(1)
        tunnel.close()
        count = len(wire.sent)
        with self.assertRaises(OSError):
            tunnel.write(b"test")
        self.assertEqual(len(wire.sent), count)
        self.assertTrue(wire.closed)

    def test_pending_reset_ack_not_ready(self):
        tunnel, wire, _, wanted = self.make_tunnel(acknowledgements=False)
        wanted[0] = True
        wire.incoming.extend(frame(ACK, b"\1\1"))
        tunnel.read(1)
        self.assertFalse(tunnel.lease_ready())

    def test_malformed_and_overflow_rx_fail(self):
        for response in (frame(ACK, b"\1"), frame(ACK, b"\2\0"), frame(UART_RX, b""),
                         frame(HEARTBEAT, b"\1"), frame(HELLO, HELLO_TEXT)):
            tunnel, wire, _, _ = self.make_tunnel()
            wire.incoming.extend(response)
            with self.subTest(response=response), self.assertRaises(OSError):
                tunnel.read(1)
        tunnel, wire, _, _ = self.make_tunnel()
        tunnel.buffer.extend(b"x" * 8192)
        wire.incoming.extend(frame(UART_RX, b"x"))
        with self.assertRaises(OSError):
            tunnel._receive()

    def test_short_write_and_close_release_without_replay(self):
        tunnel, wire, _, _ = self.make_tunnel()
        wire.short_write = True
        with self.assertRaises(OSError):
            tunnel.write(b"command")
        self.assertEqual(len(wire.sent), 1)
        wire.short_write = False
        tunnel.close()
        self.assertEqual(TunnelParser().feed(wire.sent[-1]), [(HEARTBEAT, b"\0")])
        count = len(wire.sent)
        tunnel.close()
        self.assertEqual(len(wire.sent), count)


class IntegrationTests(unittest.TestCase):
    def test_tcp_9600_passes_panel_rate_and_polls_at_100ms_without_arming(self):
        clock = Mock(return_value=10.0)
        wire = FakeWire(polling=True)
        factory = Mock(return_value=wire)
        bridge = SerialBridge(factory, clock=clock)
        try:
            bridge.start("tcp://192.168.1.10:8888", baudrate=9600, touch_mode="poll")
            self.assertTrue(bridge.connected.wait(1))
            factory.assert_called_once_with("tcp://192.168.1.10:8888", 9600,
                                            timeout=.05, write_timeout=.25)
            selector_query = read_words(STEP_VP, 2)
            self.assertTrue(wait_for(lambda: wire.writes.count(selector_query) == 1))
            clock.return_value = 10.05
            # Wait for a complete worker read pass without advancing its clock.
            import time
            time.sleep(.03)
            self.assertEqual(wire.writes.count(selector_query), 1)
            clock.return_value = 10.101
            self.assertTrue(wait_for(lambda: wire.writes.count(selector_query) == 2))
            self.assertFalse(bridge.control_lease)
        finally:
            bridge.close()
            if bridge.thread:
                bridge.thread.join(1)

    def test_ui_pump_lease_expires_even_worker_alive(self):
        clock = Clock()
        bridge = SerialBridge(clock=clock)
        bridge.set_control_lease(True)
        self.assertFalse(bridge._lease_wanted())
        bridge._panel_responded()
        self.assertTrue(bridge._lease_wanted())
        clock.advance(2.01)
        self.assertFalse(bridge._lease_wanted())
        bridge.set_control_lease(True)
        self.assertFalse(bridge._lease_wanted())
        bridge._panel_responded()
        self.assertTrue(bridge._lease_wanted())
        bridge.close()
        self.assertFalse(bridge._lease_wanted())

    def test_display_endpoint_matches_main_host_and_handles_ipv6(self):
        self.assertEqual(display_endpoint("tcp://192.168.1.10:23"), "tcp://192.168.1.10:8888")
        self.assertEqual(display_endpoint("tcp://[fd00::1]:23"), "tcp://[fd00::1]:8888")
        with self.assertRaises(ValueError):
            display_endpoint("tcp://192.168.1.10:8888")

    def test_bridge_rejects_invalid_network_channel_before_starting_worker(self):
        bridge = SerialBridge()
        for address, speed in (("tcp://192.168.1.10:23", 115200),
                               ("tcp://8.8.8.8:8888", 115200),
                               ("tcp://192.168.1.10:8888", 38400)):
            with self.subTest(address=address, speed=speed), self.assertRaises(ValueError):
                bridge.start(address, baudrate=speed)
        self.assertIsNone(bridge.thread)

    def test_panel_jog_obeys_machine_interlock_but_stop_remains_available(self):
        app = fake_app()
        app.machine_ready = lambda: False
        router = CommandRouter(app)
        result, _ = router.dispatch(Upload(TOUCH_VP, (1,), 5), armed=True, linked=True, now=5.2)
        self.assertEqual(result, 7)
        app.jog.assert_not_called()
        result, _ = router.dispatch(Upload(TOUCH_VP, (14,), 5), armed=False, linked=True, now=5.2)
        self.assertEqual(result, 0)
        app.stop.assert_called_once()


if __name__ == "__main__":
    unittest.main()
