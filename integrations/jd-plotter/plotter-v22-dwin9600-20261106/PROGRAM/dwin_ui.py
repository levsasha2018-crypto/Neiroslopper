"""Explicitly enabled PC companion connection for the optional DWIN panel."""
import queue
import time
import tkinter as tk
from tkinter import ttk, messagebox

from dwin_bridge import (SerialBridge, CommandRouter, snapshot, link_state, write_words,
                         STATUS_VP, STEP_VP, SPEED_VP, RESULT_VP, LINK_VP, STEPS, TOUCH_VP)
from network_transport import parse_tcp_endpoint
from eventlog import record_event


def display_endpoint(main):
    """The display tunnel belongs to the same MKS as its motion channel."""
    endpoint = parse_tcp_endpoint(main)
    if endpoint.port != 23:
        raise ValueError("Канал станка MKS должен использовать TCP 23")
    host = f"[{endpoint.host}]" if ":" in endpoint.host else endpoint.host
    return f"tcp://{host}:8888"


def display_ports(port_infos, *, main_selected="", main_active=""):
    """Exclude explicit motor ports, not every board with an Arduino USB ID.

    A second Uno held in reset is a valid USB–UART adapter. Identical USB
    vendor/product IDs cannot distinguish that adapter from the motor Uno.
    """
    excluded = {str(value).casefold() for value in (main_selected, main_active) if value}
    seen = set()
    ports = []
    for info in port_infos:
        device = str(info.device)
        key = device.casefold()
        if key not in excluded and key not in seen:
            ports.append(device)
            seen.add(key)
    return ports


class DwinWindow:
    def __init__(self, app):
        self.app = app
        self.closed = False
        self.bridge = SerialBridge(eventlog=getattr(app, "eventlog", None))
        self.router = CommandRouter(app)
        self.last_status = 0
        self.last_result = 0
        self.armed_since = float("inf")
        self.last_selectors = None
        self.window = tk.Toplevel(app.root)
        self.window.title("DWIN · экран плоттера")
        self.window.geometry("700x580")
        self.window.minsize(640, 540)
        self.window.protocol("WM_DELETE_WINDOW", self.window.withdraw)
        self.port = tk.StringVar()
        self.baud = tk.StringVar(value="9600")
        self.armed = tk.BooleanVar(value=False)
        self.poll_mode = tk.BooleanVar(value=True)
        self.info = tk.StringVar(value="Экран не подключён. Управление выключено.")
        body = ttk.Frame(self.window, padding=16)
        body.pack(fill="both", expand=True)
        ttk.Label(body, text="DWIN 800 × 480", font=("Segoe UI", 18, "bold")).pack(anchor="w")
        row = ttk.Frame(body)
        row.pack(fill="x")
        self.ports = ttk.Combobox(row, textvariable=self.port, state="normal", width=28)
        self.ports.pack(side="left")
        ttk.Button(row, text="Обновить", command=self.refresh).pack(side="left", padx=6)
        ttk.Combobox(row, textvariable=self.baud, state="readonly", width=9,
                     values=("9600", "19200", "38400", "57600", "115200")).pack(side="left")
        self.button = ttk.Button(row, text="Подключить экран", command=self.toggle)
        self.button.pack(side="left", padx=6)
        ttk.Checkbutton(body, text="Разрешить команды с экрана", variable=self.armed,
                        command=self.arm).pack(anchor="w", pady=(14, 6))
        self.poll_button = ttk.Checkbutton(body, text="Опрашивать кнопки (рекомендуется без изменения CFG)",
                                           variable=self.poll_mode)
        self.poll_button.pack(anchor="w")
        ttk.Label(body, textvariable=self.info, wraplength=610, foreground="#225ea8").pack(anchor="w", pady=10)
        ttk.Separator(body).pack(fill="x", pady=8)
        ttk.Label(body, text="Рисование подтверждается в основном окне. Чтобы отключить экран, нажмите «Отключить экран».",
                  wraplength=610).pack(anchor="w")
        self.refresh()

    def refresh(self):
        from serial.tools import list_ports
        ports = display_ports(list_ports.comports(), main_selected=self.app.port.get(),
                              main_active=self.app.port_name)
        main = self.app.port_name or self.app.port.get()
        if str(main).lower().startswith("tcp://"):
            try:
                ports.insert(0, display_endpoint(main))
            except ValueError:
                pass
        self.ports["values"] = ports
        running = self.bridge.thread is not None and self.bridge.thread.is_alive()
        # Refresh must not change the displayed identity of an open port.
        # A conflicting new main selection is rejected by App.connect.
        if (not running and not self.port.get().lower().startswith("tcp://") and
                self.port.get().casefold() not in {p.casefold() for p in ports}):
            self.port.set(ports[0] if ports else "")

    def arm(self):
        if self.armed.get() and not self.bridge.connected.is_set():
            self.armed.set(False)
            self.info.set("Сначала подключите экран. Движения не выполнялись.")
        else:
            self.info.set("Управление экраном разрешено; действуют проверки обычного пульта." if self.armed.get()
                          else "Команды выключены; СТОП остаётся доступен.")
        self.armed_since = time.monotonic() if self.armed.get() else float("inf")
        self.last_status = 0

    def toggle(self):
        if self.bridge.thread is not None and self.bridge.thread.is_alive():
            self.armed.set(False)
            self.bridge.close()
            self.info.set("Отключение экрана…")
            return
        self.armed.set(False)
        try:
            if self.port.get().lower().startswith("tcp://"):
                main = self.app.port_name or self.app.port.get()
                if parse_tcp_endpoint(self.port.get()) != parse_tcp_endpoint(display_endpoint(main)):
                    raise ValueError("Экран и станок должны быть подключены к одной MKS: порты 8888 и 23")
                # The profile reports the real panel rate; USB/GRBL stays
                # independent. This only sets polling timing, not hardware.
                self.baud.set(str(self.app.settings.get("_dwin_uart_baud", 9600)))
            self.bridge.start(self.port.get(), forbidden_ports=(self.app.port.get(), self.app.port_name),
                              baudrate=int(self.baud.get()), touch_mode="poll" if self.poll_mode.get() else "upload")
        except (ValueError, OSError) as exc:
            messagebox.showerror("DWIN", str(exc), parent=self.window)
            return
        self.info.set("Подключаю экран. Управление останется выключено.")
        self.button.configure(text="Отключить экран")
        self.ports.configure(state="disabled")
        self.poll_button.configure(state="disabled")

    def pump(self):
        if self.closed:
            return
        while True:
            try:
                kind, text = self.bridge.notices.get_nowait()
            except queue.Empty:
                break
            record_event(self.app, "screen_connection_notice", kind=kind,
                         **self.bridge.diagnostic_state())
            if kind == "connected":
                self.info.set(f"{text}: порт открыт. Проверка ответа — кнопкой экрана; разрешите команды отдельно.")
                self.last_status = 0
                self.last_result = 0
                self.last_selectors = None
            elif kind in ("error", "disconnected"):
                self.armed.set(False)
                self.armed_since = float("inf")
                self.last_selectors = None
                self.last_result = 2
                self.button.configure(text="Подключить экран")
                self.ports.configure(state="normal")
                self.poll_button.configure(state="normal")
                if kind == "error":
                    self.info.set("Связь с экраном потеряна: " + text + ". Автопереподключение отключено.")
                    self.app.note("DWIN: " + text)
                elif self.bridge.error is None:
                    self.info.set("Экран отключён. Повторное подключение и разрешение — вручную.")
        for event in self.bridge.pending():
            if event.vp == TOUCH_VP and event.words in ((12,), (13,), (16,)):
                # Tk dialogs run a nested event loop. Exit this poll first so
                # incoming STOP and disconnect events are still processed.
                self.app.root.after_idle(self.dispatch_event, event)
            else:
                self.dispatch_event(event)
        now = time.monotonic()
        if self.bridge.connected.is_set() and now - self.last_status >= 0.4:
            self.last_status = now
            frames = [write_words(STATUS_VP, snapshot(self.app, self.armed.get())),
                      write_words(RESULT_VP, (self.last_result,
                                             link_state(self.app),
                                             int(self.armed.get())))]
            try:
                step = float(self.app.step.get().replace(",", "."))
                speed = round(float(self.app.feed.get().replace(",", ".")))
                selectors = (STEPS.index(step) if step in STEPS else None,
                             speed if 1 <= speed <= 100 else None)
                # Do not overwrite freshly touched panel selectors every
                # telemetry cycle before their poll response can reach us.
                previous = self.last_selectors
                if selectors[0] is not None and (previous is None or selectors[0] != previous[0]):
                    frames.append(write_words(STEP_VP, (selectors[0],)))
                if selectors[1] is not None and (previous is None or selectors[1] != previous[1]):
                    frames.append(write_words(SPEED_VP, (selectors[1],)))
                self.last_selectors = selectors
            except ValueError:
                pass
            self.bridge.publish(frames)

    def dispatch_event(self, event):
        if self.closed:
            return
        try:
            self.last_result, text = self.router.dispatch(event, armed=self.armed.get() and event.received >= self.armed_since,
                                                           linked=self.bridge.connected.is_set())
        except Exception as exc:
            self.last_result, text = 6, "DWIN: команда отклонена: " + str(exc)
        if text:
            self.info.set(text)
            self.app.note("DWIN: " + text)
        if event.vp in (STEP_VP, SPEED_VP) and self.last_result != 0:
            self.last_selectors = None  # Restore rejected selector values from the PC.
        self.last_status = 0

    def close(self):
        if self.closed:
            return
        self.closed = True
        self.armed.set(False)
        self.bridge.close()
        self.window.destroy()
        self.app.dwin_window = None


class NativeDwinWindow:
    """Observe the board; the panel UART and local controls belong to MKS."""
    native = True
    RETRY_INITIAL = .5
    RETRY_MAX = 5.0

    def __init__(self, app):
        self.app = app
        self.closed = False
        self.window = tk.Toplevel(app.root)
        self.window.title("DWIN · связь с MKS")
        self.window.protocol("WM_DELETE_WINDOW", self.window.withdraw)
        self.window.geometry("470x180")
        self.info = tk.StringVar(value="Подключаю журнал и задачи MKS…")
        ttk.Label(self.window, textvariable=self.info, wraplength=426).pack(anchor="w", padx=16, pady=16)
        ttk.Button(self.window, text="Переподключить", command=self.reconnect).pack(anchor="w", padx=16)
        self.bridge = None
        self.armed = tk.BooleanVar(value=False)
        self.panel_assistant = None
        self._motion_session = None
        self._retry_at = None
        self._retry_delay = self.RETRY_INITIAL
        self._manual_reconnect = False

    def start(self):
        from dwin_native import NativeDwinClient
        session = self.app.controller.session
        peer = getattr(getattr(session, 'wire', None), 'peer_address', None)
        if peer is None:
            return
        host = f"[{peer}]" if ":" in peer else peer
        interactive = getattr(session, "panel_assistant", False)
        self.bridge = NativeDwinClient(f"tcp://{host}:8888", expected_peer=peer, interactive=interactive)
        self._motion_session = session
        self.bridge.start()

    def reconnect(self):
        if self.closed or self.app.controller.busy or self._manual_reconnect or self.bridge is None:
            return
        self.close_assistant()
        self.bridge.close()
        self._manual_reconnect = True
        self._retry_delay = self.RETRY_INITIAL
        self._retry_at = time.monotonic()
        self.info.set("Закрываю старое подключение DWIN…")

    def close_assistant(self):
        if self.panel_assistant is not None:
            self.panel_assistant.close()
            self.panel_assistant = None

    def service_assistant(self, bridge):
        from dwin_assistant import ERROR, sheet_segments
        from dwin_native import ASK, MIC_START, MIC_STOP, CANCEL, VIEW_SHEET, PREVIEW_WIDTH, PREVIEW_HEIGHT, MAX_SEGMENTS

        assistant = self.panel_assistant
        for request_id, action, text in bridge.panel_requests():
            if action == CANCEL:
                assistant.cancel(request_id)
                continue
            if action == MIC_STOP:
                if not assistant.stop_voice(request_id):
                    bridge.panel_answer(request_id, ERROR, "Запись не началась. Повторите запрос.")
                continue
            snapshot = self.app.editor_snapshot()
            if action == VIEW_SHEET:
                try:
                    position = self.app.position
                    start = self.app.axes().paper(position) if position is not None and self.app.zero else None
                    box, lines = sheet_segments(snapshot, width_px=PREVIEW_WIDTH, height_px=PREVIEW_HEIGHT,
                                                max_segments=MAX_SEGMENTS, start_position=start)
                    bridge.panel_sheet(request_id, box, lines)
                    record_event(self.app, "native_panel_preview", words_count=len(lines), result_code=0)
                except (ValueError, TypeError):
                    bridge.panel_sheet(request_id)
                    record_event(self.app, "native_panel_preview", result_code=4)
            elif action == ASK:
                if not assistant.start(request_id, text, snapshot):
                    bridge.panel_answer(request_id, ERROR, "Помощник ещё занят. Повторите запрос.")
            elif action == MIC_START:
                voice_ui = getattr(getattr(self.app, "assistant_window", None), "voice", None)
                if voice_ui is not None and voice_ui.busy:
                    bridge.panel_answer(request_id, ERROR, "Микрофон уже используется на ПК.")
                elif not assistant.start_voice(request_id, snapshot):
                    bridge.panel_answer(request_id, ERROR, "Микрофон ещё занят. Повторите запрос.")
        assistant.pump()

    def pump(self):
        if self.closed or self.bridge is None:
            return
        bridge = self.bridge
        if (not self.app.connected or not self.app.native_panel or not self.app.network_controller() or
                self.app.controller.session is not self._motion_session):
            self.close()
            return
        while True:
            try:
                kind, text = bridge.notices.get_nowait()
            except queue.Empty:
                break
            self.info.set(text)
            self.app.note("MKS · " + text)
            record_event(self.app, "native_panel_link", kind=kind, **bridge.diagnostic_state())
        if not bridge.thread.is_alive():
            self.close_assistant()
            self.app.apply_panel_state(None)
            now = time.monotonic()
            if self._retry_at is None:
                self._retry_at = now + self._retry_delay
                self._retry_delay = min(self.RETRY_MAX, self._retry_delay * 2)
                self.info.set("Канал DWIN прервался · жду переподключения")
            if now >= self._retry_at and not self.app.controller.busy:
                self._retry_at = None
                self._manual_reconnect = False
                self.start()  # A new observer never inherits ownership or pending PC work.
            return
        for event in bridge.pending():
            record_event(self.app, "native_panel_event", vp=event.vp, key=event.key,
                         result_code=event.result_code, board_event_seq=event.seq,
                         firmware_uptime_ms=event.uptime_ms)
        board = bridge.snapshot()
        self.app.apply_panel_state(board)
        if board:
            self._retry_delay = self.RETRY_INITIAL
            bridge.answer_probe()
            if not board.confirmed:
                self.info.set("MKS не подтвердила профиль локального управления")
            elif not board.panel_online:
                self.info.set("MKS на связи · DWIN не отвечает по UART")
            else:
                self.info.set("MKS управляет экраном · ПК принимает журнал и задачи")
            bridge.publish_status(bool(self.app.paths), round(float(self.app.progress["value"])))
            if board.panel_online and bridge.interactive and self.panel_assistant is None:
                from dwin_assistant import PanelAssistant
                from assistant.runtime import Assistant
                self.panel_assistant = PanelAssistant(bridge.panel_answer, assistant=Assistant(self.app.data_dir))
            if self.panel_assistant is not None:
                if bridge.panel_interrupted():
                    self.panel_assistant.cancel()
                if board.panel_online:
                    self.service_assistant(bridge)
                else:
                    self.panel_assistant.cancel()
                    self.panel_assistant.pump()
        elif not bridge.connected.is_set():
            self.close_assistant()

    def close(self):
        if self.closed:
            return
        self.closed = True
        self.close_assistant()
        if self.bridge is not None:
            self.bridge.close()
        self.window.destroy()
        self.app.dwin_window = None
