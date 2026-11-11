import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from setup_check import board_address, panel_status, remember, verify_wifi
from dgus_tunnel import HELLO, frame
from dwin_native import STATE, STATE_STRUCT


IDENTITY = [
    '[VER:4.1 FluidNC v4.1.1-JD-DGUS2.1-qa1 (esp32-wifi) :]',
    '[MSG:Machine: JD_Plotter_DLC32_V22]',
    '[MSG:Mode=STA:SSID=Test Hotspot:Status=Connected:IP=10.42.42.169:MAC=02-00-00-00-00-01]',
    '[MSG:[JD-DGUS:2,PORT=8888,UART=1,TX=23,RX=33,BAUD=9600,LEASE_MS=3000,UI=1]]',
]


class FakeWire:
    def __init__(self, data):
        self.data = data
        self.closed = False

    def read(self, size):
        size = min(3, size)
        chunk, self.data = self.data[:size], self.data[size:]
        return chunk

    def close(self):
        self.closed = True


class SetupCheckTests(unittest.TestCase):
    def test_usb_identity_checks_profile_and_connected_address(self):
        self.assertEqual(board_address(IDENTITY), ('10.42.42.169', 'STA'))
        ap = IDENTITY[:2] + ['[MSG:Mode=AP:SSID=FluidNC:IP=192.168.0.1:MAC=02-00-00-00-00-02]']
        self.assertEqual(board_address(ap), ('192.168.0.1', 'AP'))
        invalid_reports = (
            IDENTITY[:2] + [IDENTITY[2].replace('02-00', 'XX-00')],
            IDENTITY[:2] + [IDENTITY[2].replace('Connected', 'Disconnected')],
            [IDENTITY[0], '[MSG:Machine: Another_Plotter]', IDENTITY[2]],
        )
        for report in invalid_reports:
            with self.assertRaises(ValueError):
                board_address(report)

    def test_native_hello_and_uart_status_are_separate(self):
        for flags in (0, 32):
            payload = STATE_STRUCT.pack(flags, 0, 50, 0, 0, 0, 0, 0, 0, 0, 1, 0, 0, 0)
            packet = b'JD\x01' + bytes([STATE]) + len(payload).to_bytes(2, 'big') + payload
            wire = FakeWire(frame(HELLO, b'JD-DGUS/2') + packet)
            with patch('setup_check.TcpWire', return_value=wire):
                self.assertEqual(panel_status('10.42.42.169', 2), bool(flags))
            self.assertTrue(wire.closed)

    def test_wrong_peer_is_rejected_before_loading_settings(self):
        wire = FakeWire(b'')
        with patch('setup_check.TcpWire', return_value=wire), patch('setup_check.GrblSession') as session:
            session.return_value.command.return_value = IDENTITY
            with self.assertRaises(ValueError):
                verify_wifi('10.42.42.170')
            session.return_value.command.assert_called_once_with('$I')
            session.return_value.refresh_settings.assert_not_called()
        self.assertTrue(wire.closed)

    def test_remember_updates_both_connections(self):
        with tempfile.TemporaryDirectory() as folder:
            target = Path(folder) / 'connection.json'
            target.write_text('{"other": 7}', encoding='utf-8')
            remember(target, '10.42.42.169')
            result = target.read_text(encoding='utf-8')
            self.assertIn('tcp://10.42.42.169:23', result)
            self.assertIn('tcp://10.42.42.169:8888', result)
            self.assertIn('"other": 7', result)
