"""Optional DGUS II companion display; never opens the plotter's GRBL port.

All wire access belongs to one worker. Commands are finite button-release
events, not G-code, and are dispatched by the Tk pump through App methods.
No automatic connection, arming, retry, job continuation or firmware writes.
"""
from dataclasses import dataclass
import math
import queue
import struct
import threading
import time
from eventlog import record_event, age


TOUCH_VP = 0x1000
STATUS_VP = 0x1100
STEP_VP = 0x1200
SPEED_VP = 0x1201
RESULT_VP = 0x1110
LINK_VP = 0x1111
ARMED_VP = 0x1112
CURRENT_PAGE_VP = 0x0014  # DGUS II PIC_Now (read-only), not page-switch args 0x0085.
HOME_PAGE = 1  # Page 0 is the native two-second startup image.
STEPS = (0.1, 1, 5, 10, 50, 100)
COMMANDS = {
    1: "left", 2: "right", 3: "up", 4: "down",
    10: "top_left", 11: "bottom_right", 12: "startpoint",
    13: "draw", 14: "stop", 15: "test", 16: "open_svg",
    17: "editor", 18: "camera", 19: "pen_up_hold", 20: "pen_down_hold", 21: "toggle_hold",
}


def write_words(vp, words):
    """DGUS II 0x82 write frame, without optional CRC (panel CFG must agree)."""
    if not isinstance(vp, int) or not 0 <= vp <= 0xFFFF:
        raise ValueError("Invalid VP address")
    words = tuple(words)
    if not 1 <= len(words) <= 125 or any(type(w) is not int or not 0 <= w <= 0xFFFF for w in words):
        raise ValueError("DGUS write needs 1–125 unsigned words")
    payload = b"\x82" + struct.pack(">H", vp) + struct.pack(">" + "H" * len(words), *words)
    return b"\x5a\xa5" + bytes((len(payload),)) + payload


def read_words(vp, count=1):
    if type(vp) is not int or not 0 <= vp <= 0xFFFF or type(count) is not int or not 1 <= count <= 125:
        raise ValueError("Invalid DGUS read")
    return b"\x5a\xa5\x04\x83" + struct.pack(">HB", vp, count)


def show_page(page=0):
    """DGUS II system VP 0x0084: 5A01 + the zero-based picture/page ID."""
    if type(page) is not int or not 0 <= page <= 0xFFFF:
        raise ValueError("Invalid DGUS page")
    return write_words(0x0084, (0x5A01, page))


@dataclass(frozen=True)
class Upload:
    vp: int
    words: tuple
    received: float


class DGUSParser:
    """Bounded incremental parser. Ignores ACKs, writes and malformed uploads."""
    def __init__(self):
        self.buffer = bytearray()

    def feed(self, data, now=None):
        now = time.monotonic() if now is None else now
        events = []
        # Serial reads are bounded by the worker; slicing also bounds callers
        # supplying huge noise chunks while retaining the newest complete data.
        self.buffer.extend(bytes(data)[-4096:])
        while True:
            start = self.buffer.find(b"\x5a\xa5")
            if start < 0:
                self.buffer[:] = self.buffer[-1:] if self.buffer[-1:] == b"\x5a" else b""
                break
            if start:
                del self.buffer[:start]
            if len(self.buffer) < 3:
                break
            size = self.buffer[2]
            if size < 3:
                del self.buffer[0]
                continue
            if len(self.buffer) >= 4:
                command = self.buffer[3]
                # Reject impossible upload headers before waiting for their
                # advertised length. Otherwise a corrupted 0xFE/0xFF length
                # can hide a valid following STOP for many UART cycles.
                if command not in (0x82, 0x83) or (command == 0x83 and (size < 6 or size % 2)):
                    del self.buffer[0]
                    continue
                if command == 0x83 and len(self.buffer) >= 7:
                    count = self.buffer[6]
                    if not 1 <= count <= 125 or size != 4 + 2 * count:
                        del self.buffer[0]
                        continue
            if len(self.buffer) < size + 3:
                break
            frame = bytes(self.buffer[3:size + 3])
            del self.buffer[:size + 3]
            if frame[0] != 0x83 or len(frame) < 6:
                continue
            count = frame[3]
            if count < 1 or len(frame) != 4 + 2 * count:
                continue
            events.append(Upload(int.from_bytes(frame[1:3], "big"),
                                 struct.unpack(">" + "H" * count, frame[4:]), now))
        return events


def _scaled_words(value):
    try:
        value = float(value)
    except (TypeError, ValueError, OverflowError):
        value = 0.0
    if not math.isfinite(value):
        value = 0.0
    value = max(-21474836.48, min(21474836.47, value))
    number = round(value * 100)
    raw = number & 0xFFFFFFFF
    return raw >> 16, raw & 0xFFFF


def link_state(app, linked=True):
    """Keep a DEMO connection distinct from a live physical GRBL link."""
    if not linked:
        return 0
    return 2 if (app.connected and not app.simulation and
                 not app.controller.cancelled.is_set()) else 1


def snapshot(app, armed, linked=True):
    """Sixteen VP words (1100–110F); screen coordinates use right/down axes."""
    sheet = app.sheet
    flags = (int(link_state(app, linked) == 2) | int(bool(app.zero)) << 1 |
             int(sheet is not None) << 2 | int(bool(armed and linked)) << 3 |
             int(bool(app.simulation)) << 4 | int(bool(app.paths)) << 5 |
             int(bool(app.pen_enabled.get())) << 6 | int(bool(linked)) << 7)
    point = app.axes().paper(app.position) if app.position is not None else (0, 0)
    try:
        progress = float(app.progress["value"])
        progress = max(0, min(100, round(progress))) if math.isfinite(progress) else 0
    except (TypeError, ValueError, OverflowError):
        progress = 0
    pen_state = getattr(app, "dwin_pen_state", 0) if app.connected else 0
    return (flags, 1, *_scaled_words(point[0]), *_scaled_words(point[1]),
            *_scaled_words(sheet.width if sheet else 0),
            *_scaled_words(sheet.height if sheet else 0),
            int(bool(app.controller.busy or app.svg_loading)), progress,
            2 if sheet is not None and app.zero else 1 if app.zero else 0,
            pen_state, int(bool(app.hold.get())), int(bool(app.paths)))


class CommandRouter:
    """Tk-thread-only adapter. Reuses the desktop's guards and axis mapping."""
    def __init__(self, app):
        self.app = app

    def dispatch(self, event, *, armed, linked, now=None):
        observed = time.monotonic() if now is None else now
        fields = {"vp": event.vp, "words_count": len(event.words),
                  "armed": armed, "linked": linked, "age_s": age(observed, event.received)}
        if event.vp == TOUCH_VP and len(event.words) == 1:
            fields.update(key=event.words[0], key_name=COMMANDS.get(event.words[0], "unknown"))
        record_event(self.app, "screen_dispatch_begin", **fields)
        try:
            result = self._dispatch(event, armed=armed, linked=linked, now=now)
        except Exception as exc:
            record_event(self.app, "screen_dispatch_error", **fields, error_type=type(exc).__name__)
            raise
        record_event(self.app, "screen_dispatch_result", **fields, result_code=result[0])
        return result

    def _dispatch(self, event, *, armed, linked, now=None):
        app = self.app
        now = time.monotonic() if now is None else now
        if app.closed:
            return 2, "Экран не подключён"
        if len(event.words) != 1:
            return 6, "Ожидается одно значение кнопки"
        value = event.words[0]
        # STOP remains available without remote arming. It is not an
        # independent emergency circuit: a live display/USB/PC link is needed.
        if event.vp == TOUCH_VP and value == 14:
            app.stop()
            return 0, "СТОП: нужно заново задать углы"
        if not linked:
            return 2, "Экран не подключён"
        if not 0 <= now - event.received <= 1.0:
            return 8, "Запоздалая команда отброшена"
        if event.vp == TOUCH_VP and value == 0:
            return 0, ""
        if not armed:
            return 1, "Разрешите управление экраном в пульте"
        if app.controller.busy or app.svg_loading or getattr(app, "motion_prompt", False):
            return 3, "Операция выполняется; команда не поставлена в очередь"
        if event.vp == STEP_VP:
            if value >= len(STEPS):
                return 6, "Некорректный шаг"
            app.step.set(f"{STEPS[value]:g}")
            return 0, f"Шаг {STEPS[value]:g} мм"
        if event.vp == SPEED_VP:
            if not 1 <= value <= 100:
                return 6, "Скорость должна быть 1–100%"
            app.feed.set(str(value))
            return 0, f"Скорость {value}%"
        if event.vp != TOUCH_VP or value not in COMMANDS:
            return 5, "Неизвестная кнопка"
        # Non-motion actions open existing PC windows, never infer a job.
        non_motion = {16: app.open_svg, 17: app.open_editor, 18: app.open_camera}
        if value in non_motion:
            if value == 16 and app.drawing_sheet() is None:
                return 4, "Сначала задайте размер листа"
            non_motion[value]()
            return 9, "Окно открыто на компьютере"
        if value == 15:
            if app.drawing_sheet() is None:
                return 4, "Сначала задайте размер листа"
            if app.load_test() is False:
                return 7, "Тестовый рисунок не загружен. Проверьте размеры листа в пульте"
            return 0, "Тестовый рисунок загружен, движения нет"
        if value == 21:
            from machine_profiles import DLC32, app_profile
            if not app.simulation and app_profile(app) == DLC32:
                return 7, "Удержание MKS задаётся stepping/idle_ms в config.yaml на ПК"
            app.hold.set(not app.hold.get())
            app.change_hold()
            return 0, ("Удержание включено" if app.hold.get() else "Удержание выключено") + (
                "; задайте углы заново" if app.connected else "; применится при подключении")
        if not app.connected or app.controller.cancelled.is_set():
            return 2, "Подключите станок или DEMO в пульте"
        from machine_profiles import UNO, app_profile, settings_ready
        selected = UNO if app.simulation else app_profile(app)
        if not settings_ready(app.settings, selected) or not app.pen_ready():
            return 7, "Проверьте настройку станка и подъёма ручки в пульте"
        ready = getattr(app, "machine_ready", None)
        if callable(ready) and not ready():
            return 7, "Дождитесь подтверждения канала управления MKS в пульте"
        if value in (11, 12, 13) and not app.zero:
            return 4, "Сначала задайте верхний левый угол"
        if value in (12, 13) and (app.sheet is None or not app.paths):
            return 4, "Нужны два угла и рисунок"
        if value in (1, 2, 3, 4):
            # Desktop jog fields also allow custom values. The panel has six
            # finite presets, so never move by an unrepresentable PC value
            # while its screen still shows an older, different preset.
            try:
                step = float(app.step.get().replace(",", "."))
            except (TypeError, ValueError):
                step = None
            if step not in STEPS:
                return 6, "Выберите шаг на экране: текущий шаг ПК не входит в его набор значений"
            app.jog(*{1: (-1, 0), 2: (1, 0), 3: (0, -1), 4: (0, 1)}[value])
        elif value == 10:
            app.set_zero()
        elif value == 11:
            app.set_corner()
        elif value == 12:
            if app.go_start() is False:
                return 7, "Переход к началу не запущен"
            return 9, "Подтвердите переход в окне компьютера"
        elif value == 13:
            if app.draw() is False:
                return 7, "Рисование не запущено"
            return 9, "Подтвердите рисование в окне компьютера"
        elif value in (19, 20):
            if not app.pen_enabled.get():
                return 7, "Сначала включите управление подъёмом ручки в пульте"
            if value == 19 and not app.pen_continuous.get():
                return 7, "Для постоянного подъёма включите проверенный непрерывный профиль"
            app.submit("pen_up_hold" if value == 19 else "pen_down_hold")
        return 0, "Команда передана обычному пульту"


class SerialBridge:
    """One explicitly started connection; bounded events, coalesced telemetry."""
    def __init__(self, wire_factory=None, clock=time.monotonic, preflight_duration=None, eventlog=None):
        self.eventlog = eventlog
        self.wire_factory = wire_factory
        self.clock = clock
        # Opening a real Uno normally resets its MCU. Read its boot output
        # before sending binary DGUS bytes: some of those bytes have realtime
        # meanings to GRBL. Injected test transports skip the delay unless a
        # preflight duration is explicitly supplied.
        if preflight_duration is None:
            preflight_duration = 1.5 if wire_factory is None else 0.0
        if not math.isfinite(preflight_duration) or not 0 <= preflight_duration <= 10:
            raise ValueError("Некорректное время пассивной проверки порта")
        self.preflight_duration = float(preflight_duration)
        self.events = queue.Queue(maxsize=64)
        self.notices = queue.SimpleQueue()
        self.stop_requested = threading.Event()
        self.shutdown = threading.Event()
        self.connected = threading.Event()
        self.thread = None
        self.lock = threading.Lock()
        self.latest = None
        self.port = None
        self.error = None
        self.network_wire = None
        self.control_lease = False
        self.control_lease_at = float("-inf")
        self.last_panel_response = float("-inf")

    def start(self, port, *, forbidden_ports=(), baudrate=115200, touch_mode="poll"):
        if self.thread is not None and self.thread.is_alive():
            raise ValueError("Сначала отключите текущий экран")
        if not port or str(port).casefold() in {str(p).casefold() for p in forbidden_ports if p}:
            raise ValueError("Для DWIN нужен отдельный COM-порт, не порт Arduino/GRBL")
        if str(port).lower().startswith("tcp:"):
            from network_transport import parse_tcp_endpoint
            endpoint = parse_tcp_endpoint(port)
            if endpoint.port != 8888 or baudrate not in (9600, 115200):
                raise ValueError("Сетевой DWIN: tcp://адрес-MKS:8888, UART 9600 (прежний профиль: 115200)")
            port = endpoint.url
        if baudrate not in (9600, 19200, 38400, 57600, 115200):
            raise ValueError("Неподдерживаемая скорость UART")
        if touch_mode not in ("upload", "poll"):
            raise ValueError("Неизвестный режим событий")
        self.shutdown.clear()
        self.stop_requested.clear()
        self.connected.clear()
        self.error = None
        self.network_wire = None
        self.control_lease = False
        self.control_lease_at = float("-inf")
        self.last_panel_response = float("-inf")
        while not self.events.empty():
            self.events.get_nowait()
        while not self.notices.empty():
            self.notices.get_nowait()
        self.latest = None
        self.port = str(port)
        self.thread = threading.Thread(target=self._run, args=(self.port, baudrate, touch_mode),
                                       name="DWIN UART", daemon=True)
        self.thread.start()

    def close(self):
        self.shutdown.set()
        self.connected.clear()
        self.stop_requested.clear()
        with self.lock:
            self.latest = None
            self.control_lease = False

    def set_control_lease(self, enabled):
        """Called by the live PC UI pump, independently of HMI remote arming."""
        with self.lock:
            self.control_lease = bool(enabled)
            self.control_lease_at = self.clock()

    def _lease_wanted(self):
        with self.lock:
            return (not self.shutdown.is_set() and self.control_lease and
                    self.clock() - self.control_lease_at < 2.0 and
                    self.clock() - self.last_panel_response < 2.0)

    def _panel_responded(self):
        with self.lock:
            self.last_panel_response = self.clock()

    def diagnostic_state(self):
        now = self.clock()
        with self.lock:
            state = {"connected": self.connected.is_set(), "control_lease": self.control_lease,
                     "panel_age_s": age(now, self.last_panel_response),
                     "ui_age_s": age(now, self.control_lease_at)}
        # Never hold self.lock while invoking the tunnel's lease_provider.
        wire_state = getattr(self.network_wire, "diagnostic_state", None)
        if callable(wire_state):
            try:
                state.update(wire_state())
            except Exception:
                pass
        return state

    def lease_ready(self):
        wire = self.network_wire
        return bool(self.connected.is_set() and wire is not None and wire.lease_ready())

    def publish(self, frames):
        with self.lock:
            if self.connected.is_set() and not self.shutdown.is_set():
                self.latest = tuple(frames)

    def pending(self):
        if self.stop_requested.is_set():
            self.stop_requested.clear()
            while not self.events.empty():
                self.events.get_nowait()
            return [Upload(TOUCH_VP, (14,), self.clock())]
        items = []
        for _ in range(16):
            try:
                items.append(self.events.get_nowait())
            except queue.Empty:
                break
        # The UART worker may have received STOP while this batch was being
        # drained. Do not return the ordinary commands ahead of it.
        if self.stop_requested.is_set():
            return self.pending()
        return items

    def _passive_preflight(self, wire):
        """Reject a recognizable GRBL port without writing anything to it.

        Silence does not prove that a port belongs to the display. Explicit
        motor-port exclusion and correct physical identification remain
        mandatory, including when a board does not emit a startup banner.
        """
        deadline = time.monotonic() + self.preflight_duration
        received = bytearray()
        markers = (b"grbl ", b"[pen:", b"[ver:", b"[msg:", b"['$' for help]")
        while not self.shutdown.is_set() and time.monotonic() < deadline:
            received.extend(wire.read(256))
            received[:] = received[-2048:]
            lowered = bytes(received).lower()
            if any(marker in lowered for marker in markers):
                raise OSError("На выбранном COM обнаружена прошивка GRBL. Это порт станка, а не дисплея; данные DWIN не отправлялись")

    def _run(self, port, baudrate, touch_mode):
        wire = None
        initialized = False
        try:
            factory = self.wire_factory
            if factory is None:
                if str(port).lower().startswith("tcp://"):
                    from dgus_tunnel import DgusTunnelWire
                    factory = lambda p, b, **kwargs: DgusTunnelWire(
                        p, b, lease_provider=self._lease_wanted, **kwargs)
                else:
                    import serial
                    factory = serial.Serial
            wire = factory(port, baudrate, timeout=0.05, write_timeout=0.25)
            if hasattr(wire, "lease_ready"):
                self.network_wire = wire
            if self.shutdown.is_set():
                return
            if self.network_wire is None:
                self._passive_preflight(wire)
            if self.shutdown.is_set():
                return

            def send(frame):
                count = wire.write(frame)
                if count != len(frame):
                    raise OSError("Неполная запись DWIN; повтор команды отключён")

            # Discard retained buttons. Never replay a DRAW/JOG on reconnect.
            if hasattr(wire, "reset_input_buffer"):
                wire.reset_input_buffer()
            send(write_words(TOUCH_VP, (0,)))
            # A previous panel session may have retained an armed badge.
            # Keep the current page: the native splash animation switches
            # itself to Home, and reconnecting must not interrupt navigation.
            send(write_words(RESULT_VP, (0, 1, 0)))
            initialized = True
            page_probe_at = self.clock() + 2.0
            self.connected.set()
            self.notices.put(("connected", port))
            record_event(self, "screen_wire_connected", **self.diagnostic_state())
            parser = DGUSParser()
            next_poll = self.clock()
            poll_latched = None
            poll_pending = False
            selectors_pending = False
            previous_selectors = None
            last_key = None
            last_key_at = -1.0
            page_probe_sent = page_probe_pending = False
            while not self.shutdown.is_set():
                with self.lock:
                    frames, self.latest = self.latest, None
                for frame in frames or ():
                    send(frame)
                if page_probe_pending and self.clock() > page_probe_deadline:
                    page_probe_pending = False
                    page_probe_at = self.clock() + .2
                if not page_probe_pending and self.clock() >= page_probe_at:
                    send(read_words(CURRENT_PAGE_VP))
                    page_probe_sent = page_probe_pending = True
                    page_probe_deadline = self.clock() + 1.0
                if touch_mode == "poll" and self.clock() >= next_poll:
                    # Read selectors first: a freshly selected 100 mm step
                    # must reach the Tk router before a following JOG key.
                    send(read_words(STEP_VP, 2))
                    send(read_words(TOUCH_VP))
                    poll_pending = selectors_pending = True
                    next_poll = self.clock() + max(0.05, 960 / baudrate)
                data = wire.read(256)
                uploads = parser.feed(data, self.clock())
                # Read an entire serial chunk before filling the ordinary
                # event queue. A STOP later in that chunk beats its earlier
                # JOGs even if the queue was already full.
                if any(e.vp == TOUCH_VP and e.words == (14,) for e in uploads):
                    self.stop_requested.set()
                for event in uploads:
                    if self.stop_requested.is_set() and not (event.vp == TOUCH_VP and event.words == (14,)):
                        continue
                    if event.vp == CURRENT_PAGE_VP:
                        if page_probe_pending and len(event.words) == 1:
                            page_probe_pending = False
                            page_probe_at = self.clock() + .8
                            if event.received <= page_probe_deadline:
                                self._panel_responded()
                            # Recover only an old retained page outside this
                            # package. Leave splash timing to its native
                            # animation, and never reset an open section.
                            if event.received <= page_probe_deadline and event.words[0] > 8:
                                send(show_page(HOME_PAGE))
                        continue
                    if touch_mode == "poll" and event.vp == STEP_VP and len(event.words) == 2:
                        if not selectors_pending:
                            continue
                        selectors_pending = False
                        values = event.words
                        if values[0] < len(STEPS) and 1 <= values[1] <= 100:
                            self._panel_responded()
                        if previous_selectors is not None:
                            for index, vp in enumerate((STEP_VP, SPEED_VP)):
                                if values[index] != previous_selectors[index]:
                                    try:
                                        self.events.put_nowait(Upload(vp, (values[index],), event.received))
                                    except queue.Full:
                                        raise OSError("Слишком много событий DWIN; связь закрыта")
                        previous_selectors = values
                        continue
                    if touch_mode == "poll" and event.vp in (STEP_VP, SPEED_VP):
                        continue
                    if event.vp == TOUCH_VP and len(event.words) == 1:
                        key = event.words[0]
                        # STOP is actionable as soon as it has been received.
                        # It must not be swallowed by a prior latched JOG, by
                        # duplicate filtering, or by a failure to clear VP.
                        if key == 14:
                            self.stop_requested.set()
                            send(write_words(TOUCH_VP, (0,)))
                            continue
                        if touch_mode == "poll" and key != 14:
                            if not poll_pending:
                                continue
                            poll_pending = False
                        if key == 0:
                            poll_latched = None
                            continue
                        send(write_words(TOUCH_VP, (0,)))
                        if touch_mode == "poll":
                            if poll_latched == key:
                                continue
                            poll_latched = key
                        elif key == last_key and event.received - last_key_at < 0.12:
                            continue
                        last_key, last_key_at = key, event.received
                    if event.vp == TOUCH_VP and event.words == (14,):
                        self.stop_requested.set()
                    elif event.vp in (TOUCH_VP, STEP_VP, SPEED_VP):
                        try:
                            self.events.put_nowait(event)
                        except queue.Full:
                            raise OSError("Слишком много событий DWIN; связь закрыта")
        except Exception as exc:
            record_event(self, "screen_wire_error", error_type=type(exc).__name__,
                         errno=getattr(exc, "errno", None), winerror=getattr(exc, "winerror", None),
                         **self.diagnostic_state())
            self.error = str(exc)
            self.notices.put(("error", self.error))
        finally:
            self.connected.clear()
            if wire is not None:
                # Best effort on deliberate close only. An abrupt cable loss
                # can leave a stale panel label; it is not a safety heartbeat.
                if initialized and self.shutdown.is_set() and self.error is None:
                    try:
                        wire.write(write_words(STATUS_VP, (0,) * 16))
                        wire.write(write_words(RESULT_VP, (0,)))
                        wire.write(write_words(LINK_VP, (0, 0)))
                    except Exception:
                        pass
                try:
                    wire.close()
                except Exception:
                    pass
            self.notices.put(("disconnected", port))
            record_event(self, "screen_wire_disconnected", **self.diagnostic_state())
