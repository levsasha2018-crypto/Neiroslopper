"""PC companion for a DWIN driven locally by the MKS (JD-DGUS/2).

One worker owns the TCP connection. The GUI only consumes snapshots and logs,
and can answer a pending board request. The CNC worker explicitly acquires a
short lease for work it will stream on the separate FluidNC command channel.
"""
from collections import deque
from dataclasses import dataclass
import queue
import struct
import threading
import time

from dgus_tunnel import MAGIC, ACK, HEARTBEAT, HELLO
from geometry import Axes
from network_transport import TcpWire, parse_tcp_endpoint


HELLO_TEXT = b"JD-DGUS/2"
RELEASE = 0x13
STATE, EVENT, PC_STATUS, PC_RESULT, CALIBRATE = range(0x20, 0x25)
PC_PROBE, PC_PONG = 0x25, 0x26
PANEL_QUERY, PANEL_ANSWER, PANEL_SHEET, PANEL_READY = range(0x27, 0x2B)
ASK, MIC_START, MIC_STOP, CANCEL, VIEW_SHEET = range(1, 6)
MAX_QUESTION = 120
MAX_ANSWER = 112
PREVIEW_WIDTH, PREVIEW_HEIGHT, MAX_SEGMENTS = 420, 278, 160
EMERGENCY_RELEASE, CLAIM, RENEW = range(3)
# MKS independently stops an owned job after 3 seconds without a heartbeat.
# Leave room for Wi-Fi latency while keeping the PC deadline below that limit.
STATE_TIMEOUT = 2.5
ACK_TIMEOUT = 2.2
RENEW_INTERVAL = 0.25
STATE_STRUCT = struct.Struct(">BBBiiiiIIIIIBB")
EVENT_STRUCT = struct.Struct(">IIBBH")
INCOMING_LENGTHS = {HELLO: len(HELLO_TEXT), STATE: STATE_STRUCT.size,
                    EVENT: EVENT_STRUCT.size, ACK: 2, PC_PROBE: 4}
REQUEST_KEYS = frozenset((12, 13, 15, 16, 17, 18))
RESULT_CODES = frozenset((0, 4, 7, 9))


@dataclass(frozen=True)
class NativeState:
    flags: int
    step_index: int
    speed_pct: int
    position_mm: tuple[float, float]
    origin_mm: tuple[float, float]
    width_mm: float
    height_mm: float
    generation: int
    boot_id: int
    request_id: int
    request_key: int
    result_code: int
    received: float

    @property
    def confirmed(self):
        return bool(self.flags & 1)

    @property
    def has_zero(self):
        return bool(self.flags & 2)

    @property
    def has_sheet(self):
        return bool(self.flags & 4)

    @property
    def pc_owned(self):
        return bool(self.flags & 8)

    @property
    def busy(self):
        return bool(self.flags & 16)

    @property
    def panel_online(self):
        return bool(self.flags & 32)

    @property
    def drawing_loaded(self):
        return bool(self.flags & 64)

    @property
    def axes(self):
        sign = -1 if self.flags & 128 else 1
        return Axes(right_sign=sign, down_sign=sign)

    @classmethod
    def decode(cls, payload, received):
        (flags, step, speed, x, y, origin_x, origin_y, width, height, generation,
         boot_id, request_id, key, result) = STATE_STRUCT.unpack(payload)
        if (step > 5 or not 1 <= speed <= 100 or
                (key not in REQUEST_KEYS if request_id else key != 0) or result > 13):
            raise OSError("Некорректное состояние DWIN от MKS")
        if flags & 4 and (not flags & 2 or not 1000 <= width <= 3_000_000 or
                          not 1000 <= height <= 3_000_000):
            raise OSError("Некорректные размеры листа от MKS")
        return cls(flags, step, speed, (x / 1000, y / 1000), (origin_x / 1000, origin_y / 1000),
                   width / 1000, height / 1000, generation, boot_id, request_id, key, result, received)


@dataclass(frozen=True)
class NativeEvent:
    seq: int
    uptime_ms: int
    key: int
    result_code: int
    vp: int


class _Parser:
    def __init__(self):
        self.buffer = bytearray()

    def feed(self, data):
        self.buffer.extend(data)
        if len(self.buffer) > 8192:
            raise OSError("Переполнен буфер JD-DGUS/2")
        while len(self.buffer) >= 6:
            kind = self.buffer[3]
            length = int.from_bytes(self.buffer[4:6], "big")
            valid = (5 <= length <= 5 + MAX_QUESTION if kind == PANEL_QUERY
                     else length == INCOMING_LENGTHS.get(kind))
            if self.buffer[:3] != MAGIC or not valid:
                raise OSError("Неизвестный пакет JD-DGUS/2")
            if len(self.buffer) < 6 + length:
                return
            payload = bytes(self.buffer[6:6 + length])
            del self.buffer[:6 + length]
            yield kind, payload


class NativeDwinClient:
    def __init__(self, endpoint, *, expected_peer=None, wire_factory=TcpWire, interactive=False):
        endpoint = parse_tcp_endpoint(endpoint)
        if endpoint.port != 8888:
            raise ValueError("DWIN на MKS использует порт TCP 8888")
        self.port = endpoint.url
        self._endpoint = endpoint
        self._expected_peer = expected_peer
        self._wire_factory = wire_factory
        self._condition = threading.Condition()
        self._halt = threading.Event()
        self.connected = threading.Event()
        self.notices = queue.SimpleQueue()
        self.thread = None
        self.error = None
        self.failure_reason = None
        self.peer_address = None
        self._state = None
        self._events = deque(maxlen=128)
        self._phase = "observer"
        self._last_ack = float("-inf")
        self._outcome = None
        self._responded = None
        self._calibration = None
        self._probe = self._probe_reply = None
        self._status = (False, 0)
        self.interactive = interactive
        self._panel_requests = deque(maxlen=16)
        self._panel_replies = deque(maxlen=16)
        self._panel_seen = deque(maxlen=64)
        self._panel_interrupted = False

    def start(self):
        with self._condition:
            if self.thread is not None or self._halt.is_set():
                raise RuntimeError("Для нового подключения DWIN создайте новый канал")
            self.thread = threading.Thread(target=self._run, name="DWIN-MKS", daemon=True)
            self.thread.start()

    def close(self):
        with self._condition:
            self._halt.set()
            self.connected.clear()
            self._panel_requests.clear()
            self._panel_replies.clear()
            self._condition.notify_all()

    def _fresh_state(self, now):
        if (self.connected.is_set() and not self._halt.is_set() and self._state is not None and
                now - self._state.received < STATE_TIMEOUT):
            return self._state
        return None

    def snapshot(self):
        with self._condition:
            return self._fresh_state(time.monotonic())

    def pending(self):
        with self._condition:
            events = list(self._events)
            self._events.clear()
        return events

    def panel_requests(self):
        with self._condition:
            state = self._fresh_state(time.monotonic())
            requests = list(self._panel_requests) if state is not None and state.panel_online else []
            self._panel_requests.clear()
        return requests

    def panel_interrupted(self):
        with self._condition:
            interrupted, self._panel_interrupted = self._panel_interrupted, False
            return interrupted

    def _reply_panel(self, kind, data):
        with self._condition:
            state = self._fresh_state(time.monotonic())
            if self.interactive and state is not None and state.panel_online:
                self._panel_replies.append((kind, data))

    def panel_answer(self, request_id, stage, text=""):
        payload = " ".join(text.split())[:MAX_ANSWER].encode("cp1251", errors="replace")
        self._reply_panel(PANEL_ANSWER, struct.pack(">IB", request_id, stage) + payload)

    def panel_sheet(self, request_id, bbox=None, segments=()):
        if bbox is None:
            self._reply_panel(PANEL_SHEET, struct.pack(">I", request_id))
            return
        lines = tuple(segments)
        if len(lines) > MAX_SEGMENTS:
            raise ValueError("Слишком много линий для DWIN")
        payload = bytearray(struct.pack(">I", request_id))
        for segment in (bbox, *lines):
            x0, y0, x1, y1 = segment
            if not (0 <= x0 < PREVIEW_WIDTH and 0 <= x1 < PREVIEW_WIDTH and
                    0 <= y0 < PREVIEW_HEIGHT and 0 <= y1 < PREVIEW_HEIGHT):
                raise ValueError("Линия за пределами предпросмотра DWIN")
            payload.extend(struct.pack(">4H", *segment))
        self._reply_panel(PANEL_SHEET, bytes(payload))

    def publish_status(self, loaded, progress):
        if type(loaded) is not bool or type(progress) is not int or not 0 <= progress <= 100:
            raise ValueError("Статус задания: наличие рисунка и прогресс 0–100")
        with self._condition:
            self._status = loaded, progress

    def answer_probe(self):
        """Called by the GUI pump so a frozen desktop cannot pass the test."""
        with self._condition:
            if self._fresh_state(time.monotonic()) is not None and self._probe is not None:
                self._probe_reply, self._probe = self._probe, None

    def respond(self, request_id, result_code):
        if (type(request_id) is not int or not 0 < request_id <= 0xFFFFFFFF or
                type(result_code) is not int or result_code not in RESULT_CODES):
            raise ValueError("Некорректный ответ на запрос MKS")
        with self._condition:
            state = self._fresh_state(time.monotonic())
            if (state is None or state.request_id != request_id or self._outcome is not None or
                    self._responded == (state.boot_id, request_id)):
                return False
            self._outcome = (state.boot_id, request_id, result_code)
            self._responded = state.boot_id, request_id
            return True

    def calibrate(self, value):
        if type(value) is not int or value not in (0, 1, 2):
            raise ValueError("Команда калибровки: 0 сброс, 1 первый угол, 2 второй")
        with self._condition:
            state = self._fresh_state(time.monotonic())
            if (state is None or not state.confirmed or state.busy or state.pc_owned or
                    self._phase != "observer" or self._calibration is not None):
                return False
            self._calibration = value
            return True

    def _await_phase(self, waiting, final, timeout):
        # Called with the condition held, only by the CNC worker. Timeout closes
        # the connection, so an unobserved late grant cannot be used or renewed.
        finished = self._condition.wait_for(
            lambda: self._phase != waiting or self._halt.is_set() or not self.connected.is_set(),
            timeout=timeout)
        if not finished:
            self.close()
        return finished and self._phase == final and self.connected.is_set()

    def claim(self, timeout=2.0):
        with self._condition:
            state = self._fresh_state(time.monotonic())
            if (state is None or not state.confirmed or state.busy or state.pc_owned or
                    self._phase != "observer" or self._calibration is not None):
                return False
            self._phase = "claiming"
            return self._await_phase("claiming", "owned", timeout)

    def release(self, timeout=2.0, *, graceful=True):
        with self._condition:
            if self._phase != "owned" or not self.connected.is_set():
                return False
            self._phase = "releasing" if graceful else "aborting"
            return self._await_phase(self._phase, "observer", timeout)

    def _lease_ready(self, now):
        state = self._fresh_state(now)
        return (state is not None and state.pc_owned and self._phase == "owned" and
                now - self._last_ack < ACK_TIMEOUT)

    def lease_ready(self):
        with self._condition:
            return self._lease_ready(time.monotonic())

    def diagnostic_state(self):
        with self._condition:
            now = time.monotonic()
            return {"tunnel_hello": self.connected.is_set(), "closed": self._halt.is_set(),
                    "ack_owned": self._phase == "owned",
                    "ack_age_s": round(now - self._last_ack, 3) if self._last_ack != float("-inf") else None,
                    "panel_age_s": round(now - self._state.received, 3) if self._state else None,
                    "reason": self.failure_reason, "lease_ready": self._lease_ready(now)}

    @staticmethod
    def _send(wire, kind, payload=b""):
        packet = MAGIC + bytes((kind,)) + len(payload).to_bytes(2, "big") + payload
        if wire.write(packet) != len(packet):
            raise OSError("Неполная запись JD-DGUS/2")

    def _on_state(self, payload):
        state = NativeState.decode(payload, time.monotonic())
        with self._condition:
            if self._state is not None:
                if self._state.boot_id != state.boot_id:
                    raise OSError("MKS перезапущена, прежние команды отменены")
                if self._state.panel_online and not state.panel_online:
                    self._panel_interrupted = True
            self._state = state
            if not state.panel_online:
                self._panel_requests.clear()
                self._panel_replies.clear()
            if not self._halt.is_set() and not self.connected.is_set():
                self.connected.set()
                self.notices.put(("connected", "События автономного экрана MKS"))

    def _on_ack(self, payload, operation):
        owned, reset = payload
        if owned not in (0, 1) or reset not in (0, 1) or not operation:
            raise OSError("Некорректное подтверждение владения MKS")
        with self._condition:
            now = time.monotonic()
            self._last_ack = now
            if operation == "claim":
                state = self._fresh_state(now)
                if owned and not reset and (state is None or not state.pc_owned):
                    raise OSError("MKS не подтвердила состояние после выдачи управления")
                self._phase = "owned" if owned and not reset else "observer"
            elif operation == "renew":
                if not owned or reset:
                    raise OSError("MKS отозвала управление, операция остановлена")
            elif operation == "release" and owned and not reset:
                # Firmware may still be settling after the final pen command.
                # Retry only RELEASE within the caller's original deadline.
                return
            elif owned or (operation == "release" and reset):
                raise OSError("MKS не подтвердила освобождение управления")
            else:
                state = self._fresh_state(now)
                if operation == "release" and (state is None or state.pc_owned):
                    raise OSError("После освобождения управления MKS продолжает считать ПК владельцем")
                self._phase = "observer"
            self._condition.notify_all()

    def _run(self):
        wire = None
        try:
            wire = self._wire_factory(self._endpoint, timeout=.05, write_timeout=.60)
            peer = getattr(wire, "peer_address", None)
            if self._expected_peer is not None and peer != self._expected_peer:
                raise OSError("Экран и канал движения подключились к разным адресам MKS")
            self.peer_address = peer
            self._exchange(wire)
        except (OSError, ValueError) as exc:
            if not self._halt.is_set():
                self.error = str(exc)
                if self.failure_reason is None:
                    self.failure_reason = type(exc).__name__
                self.notices.put(("error", self.error))
        finally:
            self.close()
            if wire is not None:
                wire.close()  # MKS stops a PC job if its owner disconnects.
            self.notices.put(("disconnected", "Канал событий MKS закрыт"))

    def _exchange(self, wire):
        parser = _Parser()
        hello = False
        connection_started = time.monotonic()
        last_renew = float("-inf")
        operation = None
        operation_sent = 0.0
        sent_status = None
        while not self._halt.is_set():
            for kind, payload in parser.feed(wire.read(4096)):
                if not hello:
                    if kind != HELLO or payload != HELLO_TEXT:
                        raise OSError("MKS не поддерживает автономный экран JD-DGUS/2")
                    hello = True
                elif kind == STATE:
                    self._on_state(payload)
                elif kind == EVENT:
                    with self._condition:
                        self._events.append(NativeEvent(*EVENT_STRUCT.unpack(payload)))
                elif kind == PC_PROBE:
                    with self._condition:
                        self._probe = payload
                elif kind == PANEL_QUERY and self.interactive:
                    request_id, action = struct.unpack_from(">IB", payload)
                    if (not request_id or action not in (ASK, MIC_START, MIC_STOP, CANCEL, VIEW_SHEET) or
                            bool(payload[5:]) != (action == ASK)):
                        raise OSError("Некорректный запрос с экрана")
                    prompt = payload[5:].decode("cp1251", errors="replace")
                    with self._condition:
                        state = self._fresh_state(time.monotonic())
                        key = (request_id, action)
                        if state is not None and state.panel_online and key not in self._panel_seen:
                            self._panel_seen.append(key)
                            self._panel_requests.append((request_id, action, prompt))
                elif kind == ACK:
                    self._on_ack(payload, operation)
                    operation = None
                else:
                    raise OSError("Неожиданное приветствие MKS")
            if self._halt.is_set():
                return
            now = time.monotonic()
            with self._condition:
                state = self._fresh_state(now)
                phase = self._phase
                status = self._status
                outcome, self._outcome = self._outcome, None
                calibration = self._calibration
                probe_reply, self._probe_reply = self._probe_reply, None
            if state is None:
                if self.connected.is_set() or now - connection_started > 3:
                    self.failure_reason = "state_timeout"
                    raise TimeoutError("Нет свежего состояния экрана MKS")
                continue
            if operation is not None and now - operation_sent >= ACK_TIMEOUT:
                self.failure_reason = operation + "_ack_timeout"
                raise TimeoutError("MKS не подтвердила управление")
            if phase == "owned" and not state.pc_owned:
                self.failure_reason = "ownership_revoked"
                raise OSError("MKS отозвала управление, операция остановлена")
            if operation is None:
                if phase == "claiming":
                    operation = "claim"
                    self._send(wire, HEARTBEAT, bytes((CLAIM,)))
                elif phase == "releasing" and now - operation_sent >= .12:
                    operation = "release"
                    self._send(wire, RELEASE)
                elif phase == "aborting":
                    operation = "abort"
                    self._send(wire, HEARTBEAT, bytes((EMERGENCY_RELEASE,)))
                elif phase == "owned" and now - last_renew >= RENEW_INTERVAL:
                    operation = "renew"
                    self._send(wire, HEARTBEAT, bytes((RENEW,)))
                if operation is not None:
                    operation_sent = last_renew = now
            if calibration is not None:
                self._send(wire, CALIBRATE, bytes((calibration,)))
                with self._condition:
                    self._calibration = None
            if probe_reply is not None:
                self._send(wire, PC_PONG, probe_reply)
            if outcome is not None and outcome[:2] == (state.boot_id, state.request_id):
                self._send(wire, PC_RESULT, struct.pack(">IB", outcome[1], outcome[2]))
            if status != sent_status:
                if sent_status is None and self.interactive:
                    self._send(wire, PANEL_READY)
                self._send(wire, PC_STATUS, bytes(status))
                sent_status = status
            with self._condition:
                reply = self._panel_replies.popleft() if self._panel_replies else None
            if reply is not None:
                self._send(wire, *reply)
