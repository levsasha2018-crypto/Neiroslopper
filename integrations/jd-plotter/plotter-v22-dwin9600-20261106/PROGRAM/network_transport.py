"""Small serial-shaped LAN TCP transport. Never reconnects or replays a write."""
from dataclasses import dataclass
import ipaddress
import queue
import re
import select
import socket
import threading
import time
from urllib.parse import urlsplit


@dataclass(frozen=True)
class Endpoint:
    host: str
    port: int
    url: str


def _lan_address(value):
    try:
        address = ipaddress.ip_address(value.split("%", 1)[0])
    except ValueError:
        return False
    if address.is_loopback or address.is_link_local:
        return True
    networks = ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16") if address.version == 4 else ("fc00::/7",)
    return any(address in ipaddress.ip_network(network) for network in networks)


def parse_tcp_endpoint(value):
    if not isinstance(value, str) or value != value.strip() or len(value) > 300:
        raise ValueError("Адрес Wi-Fi: tcp://адрес-платы:порт, без пробелов.")
    try:
        parsed = urlsplit(value)
        host, port = parsed.hostname, parsed.port
    except ValueError as exc:
        raise ValueError("Некорректный адрес TCP или порт.") from exc
    if (parsed.scheme != "tcp" or not host or port is None or not 1 <= port <= 65535 or
            parsed.username is not None or parsed.password is not None or
            parsed.path or parsed.query or parsed.fragment or "%" in host):
        raise ValueError("Укажите только tcp://адрес-платы:порт; логины, пароли и пути недопустимы.")
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        if not re.fullmatch(r"(?=.{1,253}\Z)[A-Za-z0-9](?:[A-Za-z0-9.-]*[A-Za-z0-9])?", host) or any(
                not 1 <= len(label) <= 63 or label.startswith("-") or label.endswith("-")
                for label in host.split(".")):
            raise ValueError("Некорректное имя платы в локальной сети.")
        # A typo in a numeric address must not become a DNS query.
        if re.fullmatch(r"[\d.]+", host):
            raise ValueError("Некорректный IP-адрес платы.")
    else:
        if not _lan_address(str(address)):
            raise ValueError("Разрешён только адрес локальной сети, не публичный сервер.")
    canonical = f"tcp://[{host}]:{port}" if ":" in host else f"tcp://{host}:{port}"
    return Endpoint(host, port, canonical)


def _resolve(endpoint, timeout):
    """Bound hostname lookup; connect only to the numeric addresses checked here."""
    results = queue.Queue(maxsize=1)
    def work():
        try:
            results.put(socket.getaddrinfo(endpoint.host, endpoint.port, type=socket.SOCK_STREAM))
        except Exception as exc:
            results.put(exc)
    threading.Thread(target=work, daemon=True).start()
    try:
        addresses = results.get(timeout=timeout)
    except queue.Empty as exc:
        raise TimeoutError("Имя платы не найдено за отведённое время; попробуйте её IP-адрес.") from exc
    if isinstance(addresses, Exception):
        raise OSError("Не удалось найти плату в локальной сети.") from addresses
    addresses = [entry for entry in addresses if _lan_address(entry[4][0])]
    if not addresses:
        raise ValueError("Имя платы не указывает на разрешённый адрес локальной сети.")
    return addresses


class TcpWire:
    def __init__(self, endpoint, *, timeout=.05, write_timeout=.5, connect_timeout=3):
        endpoint = parse_tcp_endpoint(endpoint) if isinstance(endpoint, str) else endpoint
        self.port = endpoint.url
        self.baudrate = None
        self.timeout = max(.001, min(float(timeout), 3))
        self.write_timeout = max(.001, min(float(write_timeout), 3))
        self.closed = False
        self.buffer = bytearray()
        self.socket = None
        # This is the actual numeric peer, not the supplied DNS/mDNS name.
        # Independent connections to one hostname can resolve to two boards.
        self.peer_address = None
        deadline = time.monotonic() + min(max(float(connect_timeout), .1), 5)
        addresses = _resolve(endpoint, max(.001, deadline - time.monotonic()))
        last_error = None
        for family, kind, protocol, _, address in addresses:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            wire = socket.socket(family, kind, protocol)
            try:
                wire.settimeout(remaining)
                wire.connect(address)
                peer_address = str(ipaddress.ip_address(wire.getpeername()[0]))
                if not _lan_address(peer_address):
                    raise OSError("Соединение не указывает на разрешённый локальный адрес.")
                wire.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
                wire.setblocking(False)
            except OSError as exc:
                last_error = exc
                wire.close()
                continue
            self.socket = wire
            self.peer_address = peer_address
            break
        if self.socket is None:
            raise ConnectionError("Плата недоступна по Wi-Fi; проверьте адрес, порт и сеть. Автоповтора нет.") from last_error

    def _check(self):
        if self.closed or self.socket is None:
            raise OSError("Соединение TCP закрыто.")

    def _receive(self, timeout):
        self._check()
        readable, _, _ = select.select([self.socket], [], [], timeout)
        if readable:
            try:
                data = self.socket.recv(65536)
            except BlockingIOError:
                return
            if not data:
                raise ConnectionError("Связь с платой по Wi-Fi потеряна; автоматического продолжения нет.")
            self.buffer.extend(data)
            if len(self.buffer) > 131072:
                raise OSError("Входной TCP-буфер переполнен.")

    @property
    def in_waiting(self):
        self._receive(0)
        return len(self.buffer)

    def read(self, size=1):
        self._check()
        if not self.buffer:
            self._receive(self.timeout)
        result = bytes(self.buffer[:size])
        del self.buffer[:size]
        return result

    def write(self, data):
        self._check()
        sent = 0
        deadline = time.monotonic() + self.write_timeout
        while sent < len(data):
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("Нет подтверждения записи TCP; команда не повторялась.")
            _, writable, _ = select.select([], [self.socket], [], remaining)
            if not writable:
                raise TimeoutError("Тайм-аут записи TCP; команда не повторялась.")
            try:
                count = self.socket.send(data[sent:])
            except BlockingIOError:
                continue
            if count <= 0:
                raise ConnectionError("Связь оборвалась при записи TCP; команда не повторялась.")
            sent += count
        return sent

    def reset_input_buffer(self):
        for _ in range(32):
            self._receive(0)
            if not self.buffer:
                break
            self.buffer.clear()

    def close(self):
        self.closed = True
        if self.socket is not None:
            self.socket.close()
            self.socket = None


def open_transport(port, baudrate=115200, **kwargs):
    """Explicit caller-chosen endpoint; no discovery, fallback or reconnect."""
    if isinstance(port, str) and port.lower().startswith("tcp:"):
        return TcpWire(port, **kwargs)
    import serial
    return serial.Serial(port, baudrate, **kwargs)
