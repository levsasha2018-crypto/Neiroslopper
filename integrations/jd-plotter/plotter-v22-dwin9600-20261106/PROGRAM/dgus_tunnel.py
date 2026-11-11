"""Versioned DGUS/LAN tunnel with an explicit, short-lived control lease.

This is not a generic serial-over-IP adapter. Only the matching JD-DGUS/1
firmware may be used; a hello is verified before any panel byte is written.
No reconnect/replay. Native DGUS is never mixed with heartbeat bytes.
"""
import struct
import time

from network_transport import TcpWire, parse_tcp_endpoint

MAGIC = b"JD\x01"
MAX_PAYLOAD = 512
UART_TX, UART_RX, HEARTBEAT, ACK, HELLO = 1, 2, 0x10, 0x11, 0x7F
HELLO_TEXT = b"JD-DGUS/1"


def frame(kind, payload=b""):
    payload = bytes(payload)
    if kind not in (UART_TX, UART_RX, HEARTBEAT, ACK, HELLO) or len(payload) > MAX_PAYLOAD:
        raise ValueError("Invalid JD-DGUS frame")
    return MAGIC + bytes((kind,)) + struct.pack(">H", len(payload)) + payload


class TunnelParser:
    def __init__(self):
        self.buffer = bytearray()

    def feed(self, data):
        self.buffer.extend(data)
        if len(self.buffer) > 8192:
            raise OSError("Переполнен буфер JD-DGUS; соединение закрывается")
        result = []
        while len(self.buffer) >= 6:
            if self.buffer[:3] != MAGIC:
                raise OSError("Это не совместимый мост JD-DGUS/1")
            kind, length = self.buffer[3], int.from_bytes(self.buffer[4:6], "big")
            if kind not in (UART_TX, UART_RX, HEARTBEAT, ACK, HELLO) or length > MAX_PAYLOAD:
                raise OSError("Некорректный пакет JD-DGUS")
            if len(self.buffer) < 6 + length:
                break
            result.append((kind, bytes(self.buffer[6:6 + length])))
            del self.buffer[:6 + length]
        return result


class DgusTunnelWire:
    def __init__(self, endpoint, baudrate=9600, *, timeout=.05, write_timeout=.25,
                 lease_provider=lambda: False, wire=None, clock=time.monotonic,
                 hello_timeout=3.0):
        endpoint = parse_tcp_endpoint(endpoint)
        if endpoint.port != 8888:
            raise ValueError("Мост DWIN на MKS использует tcp://адрес-платы:8888, не порт GRBL 23")
        # This value describes the configured panel UART, not the USB/GRBL
        # console. The TCP tunnel cannot reconfigure a panel's baud rate.
        if baudrate not in (9600, 115200):
            raise ValueError("UART сетевого DWIN: 9600 или прежний профиль 115200 бод")
        self.port, self.baudrate = endpoint.url, baudrate
        self.clock, self.lease_provider = clock, lease_provider
        self.wire = wire if wire is not None else TcpWire(endpoint, timeout=timeout, write_timeout=write_timeout)
        self.parser = TunnelParser()
        self.buffer = bytearray()
        self.hello = False
        self.closed = False
        self.ack_owned = False
        self.last_ack = float("-inf")
        self.last_heartbeat = float("-inf")
        self.first_heartbeat = None
        self.last_owned = False
        try:
            deadline = self.clock() + hello_timeout
            while not self.hello and self.clock() < deadline:
                self._receive()
            if not self.hello:
                raise TimeoutError("MKS не подтвердил специальную прошивку JD-DGUS/1")
        except Exception:
            self.wire.close()
            self.closed = True
            raise

    def _send(self, kind, payload):
        data = frame(kind, payload)
        if self.wire.write(data) != len(data):
            raise OSError("Неполная сетевая запись JD-DGUS; повтора нет")

    def _heartbeat(self):
        if not self.hello or self.closed:
            return
        owned = bool(self.lease_provider())
        now = self.clock()
        if now - self.last_heartbeat >= .25 or owned != self.last_owned:
            self._send(HEARTBEAT, bytes((int(owned),)))
            if self.first_heartbeat is None:
                self.first_heartbeat = now
            self.last_heartbeat, self.last_owned = now, owned
        if owned and now - max(self.last_ack, self.first_heartbeat) > 1.5:
            raise TimeoutError("Нет подтверждений канала управления MKS; движение прекращается")

    def _receive(self):
        data = self.wire.read(4096)
        for kind, payload in self.parser.feed(data):
            if not self.hello:
                if kind != HELLO or payload != HELLO_TEXT:
                    raise OSError("Неверная версия сетевого моста дисплея")
                self.hello = True
                continue
            if kind == UART_RX:
                if not payload:
                    raise OSError("Пустой пакет UART в JD-DGUS")
                self.buffer.extend(payload)
                if len(self.buffer) > 8192:
                    raise OSError("Переполнен вход дисплея; соединение закрывается")
            elif kind == ACK:
                if len(payload) != 2 or payload[0] not in (0, 1) or payload[1] not in (0, 1):
                    raise OSError("Некорректное подтверждение управления MKS")
                self.ack_owned = bool(payload[0]) and payload[1] == 0
                self.last_ack = self.clock()
            else:
                raise OSError("Неожиданное сообщение моста JD-DGUS")

    def lease_ready(self):
        return (not self.closed and self.hello and self.ack_owned and self.last_owned and
                bool(self.lease_provider()) and self.clock() - self.last_ack < 1.0)

    def diagnostic_state(self):
        """Read-only snapshot; contains no addresses, payloads or commands."""
        from eventlog import age
        now = self.clock()
        return {"tunnel_hello": self.hello, "closed": self.closed,
                "ack_owned": self.ack_owned, "last_owned": self.last_owned,
                "ack_age_s": age(now, self.last_ack),
                "heartbeat_age_s": age(now, self.last_heartbeat),
                "lease_ready": self.lease_ready()}

    @property
    def peer_address(self):
        """Actual resolved socket peer, not the caller-supplied DNS name."""
        return getattr(self.wire, "peer_address", None)

    @property
    def in_waiting(self):
        return len(self.buffer)

    def read(self, count=1):
        if self.closed:
            raise OSError("Мост дисплея закрыт")
        self._heartbeat()
        if not self.buffer:
            self._receive()
        data = bytes(self.buffer[:count])
        del self.buffer[:count]
        return data

    def write(self, data):
        if self.closed:
            raise OSError("Мост дисплея закрыт")
        data = bytes(data)
        if not data or len(data) > MAX_PAYLOAD:
            raise ValueError("Слишком большой пакет дисплея")
        self._heartbeat()
        self._send(UART_TX, data)
        return len(data)

    def reset_input_buffer(self):
        # Do not discard protocol acknowledgements or hide a reset notice.
        self.buffer.clear()

    def close(self):
        if not self.closed:
            try:
                if self.hello:
                    self._send(HEARTBEAT, b"\0")
            except Exception:
                pass
            self.closed = True
            self.ack_owned = False
            self.wire.close()
