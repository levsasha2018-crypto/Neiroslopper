"""Local Windows desktop sender for an XY GRBL 1.1 pen plotter."""
import math
from pathlib import Path
import queue
import threading
import time
import tkinter as tk
from tkinter import ttk, filedialog, messagebox
from controller import Controller, REQUIRED, MACHINE_MAX_FEED, MAX_JOG_DISTANCE
from eventlog import EventLog, record_event
from geometry import Axes, load_svg, test_square
from editor_history import EditHistory
from pen_lift import PenLiftSettings, load_settings, save_settings, connection_notice, firmware_profile
from machine_profiles import (UNO, DLC32, PROFILE_LABELS, profile_id, app_profile,
                              settings_ready, pen_matches, load_selected_profile,
                              save_selected_profile, pen_settings_directory)


class App:
    def __init__(self, root, *, data_dir=None):
        self.root = root
        self.closed = False
        self.pump_timer = None
        self.data_dir = data_dir
        self.eventlog = EventLog(data_dir) if data_dir is not None else None
        record_event(self, "app_start")
        from editor_tools.studio_store import Autosaves
        self.autosaves = Autosaves(data_dir) if data_dir is not None else None
        self.controller = Controller(network_interlock=self.network_lease_ready)
        self.connected = False
        self.zero = False
        self.sheet = None
        # Editor paper is not a machine origin. Keep it after STOP/USB loss,
        # while self.sheet/zero continue to represent physical calibration.
        self.draft_sheet = None
        self.relay_state_unknown = False
        self.position = None
        self.paths = []
        self.preview_revision = 0
        self.editor_window = None
        self.assistant_window = None
        self.editor_undo = None
        self.editor_history = EditHistory()
        self.editor_task = None
        self.editor_anchor = None
        self.camera_window = None
        self.dwin_window = None
        self.offered_panel_request = None
        self.last_motion_submit = None
        self.svg_dialog = False
        self.native_panel = False
        self.panel_calibration = None
        self.network_lease_seen = False
        self.dwin_pen_state = 0  # Commanded state only; no physical pen sensor.
        self.svg_events = queue.Queue()
        self.svg_loading = False
        self.svg_token = None
        self.svg_worker = None
        self.preview_key = None
        self.preview_paths = None
        self.preview_transform = None
        self.redraw_timer = None
        self.settings = {}
        self.port_name = ""
        self.simulation = False
        self.file_name = "Рисунок не выбран"
        self.port = tk.StringVar(value="DEMO")
        self.machine_profile = tk.StringVar(value=PROFILE_LABELS[load_selected_profile(data_dir)])
        self.step = tk.StringVar(value="1")
        self.feed = tk.StringVar(value="50")
        self.margin = tk.StringVar(value="3")
        self.hold = tk.BooleanVar(value=True)
        pen = self.stored_pen_settings()
        self.pen_enabled = tk.BooleanVar(value=pen.enabled)
        self.pen_active = tk.StringVar(value="Не проверено" if pen.active_low is None else "LOW" if pen.active_low else "HIGH")
        self.pen_confirmed = tk.BooleanVar(value=pen.confirmed)
        self.pen_power_raises = tk.BooleanVar(value=pen.power_raises)
        self.pen_continuous = tk.BooleanVar(value=pen.continuous)
        self.pen_effect = tk.StringVar(value="Поднимает" if pen.power_raises else "Опускает")
        self.pen_up_ms = tk.StringVar(value=str(pen.up_ms))
        self.pen_down_ms = tk.StringVar(value=str(pen.down_ms))
        self.pen_firmware = None
        self.pen_observer = None
        self.pen_status = tk.StringVar(value="")
        self.pen_summary = tk.StringVar(value="")
        self.pen_legend = tk.StringVar(value=connection_notice(pen.enabled, pen.power_raises, pen.continuous))
        # On this plotter the GRBL X channel drives the portal (screen up/down),
        # while the GRBL Y channel drives the pen carriage (screen left/right).
        self.swap = tk.BooleanVar(value=True)
        # The carriage motor is mounted so positive screen-right is negative GRBL Y.
        self.reverse_right = tk.BooleanVar(value=True)
        self.reverse_down = tk.BooleanVar()
        self.status = tk.StringVar(value="Не подключено · выберите DEMO для проверки без оборудования")
        self.coordinates = tk.StringVar(value="Положение: —")
        self.paper_text = tk.StringVar(value="Размер появится после сохранения двух углов")
        self.hint = tk.StringVar(value="1. Подключитесь.  2. Проверьте направления шагом 1 мм.  3. Сохраните два угла.")
        self.root.title("Плоттер — лист по двум углам")
        self.root.geometry("1180x850")
        self.root.minsize(1050, 790)
        self.root.configure(bg="#eef2f6")
        style = ttk.Style()
        style.theme_use("clam")
        style.configure("TFrame", background="#eef2f6")
        style.configure("TLabel", background="#eef2f6", font=("Segoe UI", 10))
        style.configure("TButton", font=("Segoe UI", 10), padding=6)
        style.configure("TLabelframe", background="#eef2f6")
        style.configure("TLabelframe.Label", background="#eef2f6", font=("Segoe UI", 10, "bold"))
        style.configure("TCheckbutton", background="#eef2f6", font=("Segoe UI", 9))
        self.build()
        self.refresh_ports()
        self.update_controls()
        self.root.bind("<Escape>", lambda event: self.stop())
        self.root.protocol("WM_DELETE_WINDOW", self.close)
        self.pump_timer = self.root.after(60, self.pump)

    def axes(self):
        panel = self.panel_client() if getattr(self, "native_panel", False) else None
        board = panel.snapshot() if panel else None
        if board is not None and board.confirmed:
            return board.axes
        return Axes(self.swap.get(), -1 if self.reverse_right.get() else 1,
                    -1 if self.reverse_down.get() else 1)

    def drawing_sheet(self):
        return self.sheet or self.draft_sheet

    def preserve_draft(self):
        if self.sheet is not None:
            self.draft_sheet = self.sheet

    def rebase_editor_history(self):
        # Pending proposals must be invalidated by connection/calibration
        # changes; committed editor undo/redo remains usable.
        if self.editor_history.past or self.editor_history.future:
            self.editor_undo = (self.editor_revision(), None)

    def draft_notice(self):
        sheet = self.drawing_sheet()
        if sheet is None:
            return "Размер появится после сохранения двух углов"
        return (f"Эскиз: {sheet.width:.2f} × {sheet.height:.2f} мм · "
                "ноль станка не задан, рисование недоступно")

    def build(self):
        outer = ttk.Frame(self.root, padding=16)
        outer.pack(fill="both", expand=True)
        ttk.Label(outer, text="Плоттер", font=("Segoe UI", 19, "bold")).pack(anchor="w", pady=(0, 8))
        self.settings_window = tk.Toplevel(self.root)
        self.settings_window.withdraw()
        self.settings_window.title("Настройки плоттера")
        self.settings_window.geometry("690x630")
        self.settings_window.protocol("WM_DELETE_WINDOW", self.settings_window.withdraw)
        self.settings_window.bind("<Escape>", lambda event: self.stop())
        settings_tabs = ttk.Notebook(self.settings_window)
        settings_tabs.pack(fill="both", expand=True, padx=12, pady=12)
        settings = ttk.Frame(settings_tabs, padding=12)
        service = ttk.Frame(settings_tabs, padding=12)
        diagnostics = ttk.Frame(settings_tabs, padding=12)
        settings_tabs.add(settings, text="Плата и движение")
        settings_tabs.add(service, text="Сервис ручки")
        settings_tabs.add(diagnostics, text="Журнал")
        profile_row = ttk.Frame(settings)
        profile_row.pack(fill="x", pady=(0, 7))
        ttk.Label(profile_row, text="Плата:").pack(side="left", padx=(0, 7))
        self.profile_input = ttk.Combobox(profile_row, textvariable=self.machine_profile,
                                        values=tuple(PROFILE_LABELS.values()), state="readonly", width=35)
        self.profile_input.pack(side="left")
        self.profile_input.bind("<<ComboboxSelected>>", self.change_machine_profile)
        top = ttk.Frame(outer)
        top.pack(fill="x", pady=(0, 10))
        self.ports = ttk.Combobox(top, textvariable=self.port, width=15, state="readonly")
        self.ports.configure(width=27)
        self.ports.pack(side="left")
        self.refresh_button = ttk.Button(settings, text="Обновить список портов", command=self.refresh_ports)
        self.refresh_button.pack(anchor="w", pady=(0, 8))
        self.connect_button = ttk.Button(top, text="Подключить", command=self.connect)
        self.connect_button.pack(side="left", padx=(6, 0))
        ttk.Button(top, text="Помощник", command=self.open_assistant).pack(side="left", padx=8)
        tools_button = ttk.Menubutton(top, text="Инструменты")
        tools_menu = tk.Menu(tools_button, tearoff=False)
        tools_menu.add_command(label="Редактор", command=self.open_editor)
        tools_menu.add_command(label="Камера и сканирование", command=self.open_camera)
        tools_menu.add_command(label="Подключение DWIN", command=self.open_dwin)
        tools_menu.add_command(label="Настройки", command=self.open_settings)
        tools_button.configure(menu=tools_menu)
        tools_button.pack(side="left")
        self.stop_button = tk.Button(top, text="СТОП / ESC", command=self.stop, bg="#b42335", fg="white",
                                     font=("Segoe UI", 11, "bold"), padx=18, pady=4)
        self.stop_button.pack(side="right")
        self.status_label = ttk.Label(outer, textvariable=self.status, foreground="#225ea8")
        self.status_label.pack(anchor="w", pady=(0, 8))
        self.panel_request_button = ttk.Button(outer, command=self.handle_panel_request)
        columns = ttk.Frame(outer)
        columns.pack(fill="both", expand=True)
        sidebar = ttk.Frame(columns, width=355)
        sidebar.pack(side="left", fill="y", padx=(0, 16))
        sidebar.pack_propagate(False)
        sidebar_scroll = ttk.Scrollbar(sidebar, orient="vertical")
        sidebar_scroll.pack(side="right", fill="y")
        self.sidebar_canvas = tk.Canvas(sidebar, width=335, bg="#eef2f6", highlightthickness=0,
                                        yscrollcommand=sidebar_scroll.set)
        self.sidebar_canvas.pack(side="left", fill="both", expand=True)
        sidebar_scroll.configure(command=self.sidebar_canvas.yview)
        left = ttk.Frame(self.sidebar_canvas)
        sidebar_window = self.sidebar_canvas.create_window(0, 0, window=left, anchor="nw", width=335)
        left.bind("<Configure>", lambda event: self.sidebar_canvas.configure(
            scrollregion=self.sidebar_canvas.bbox("all")))
        self.sidebar_canvas.bind("<Configure>", lambda event: self.sidebar_canvas.itemconfigure(
            sidebar_window, width=event.width))

        def scroll_sidebar(event):
            widget = event.widget
            while widget is not None:
                if widget is sidebar:
                    self.sidebar_canvas.yview_scroll(int(-event.delta / 120), "units")
                    return "break"
                widget = getattr(widget, "master", None)

        self.root.bind("<MouseWheel>", scroll_sidebar)
        right = ttk.Frame(columns)
        right.pack(side="left", fill="both", expand=True)

        setup = ttk.LabelFrame(settings, text="Конфигурация", padding=10)
        setup.pack(fill="x", pady=(0, 8))
        self.configure_button = ttk.Button(setup, text="Настроить без концевиков", command=self.configure)
        self.configure_button.pack(fill="x")
        self.unlock_button = ttk.Button(setup, text="Снять аварию MKS…", command=self.unlock_alarm)
        self.unlock_button.pack(fill="x", pady=(4, 0))
        self.hold_button = ttk.Checkbutton(setup, text="Удерживать моторы в простое", variable=self.hold,
                                          command=self.change_hold)
        self.hold_button.pack(anchor="w", pady=(5, 2))
        self.axis_widgets = []
        for text, variable in (("Каретка ←/→, портал ↑/↓ (X ↔ Y)", self.swap),
                               ("Развернуть направление вправо", self.reverse_right),
                               ("Развернуть направление вниз", self.reverse_down)):
            widget = ttk.Checkbutton(setup, text=text, variable=variable, command=self.redraw)
            widget.pack(anchor="w", pady=2)
            self.axis_widgets.append(widget)
        ttk.Label(setup, text="По умолчанию: ←/→ каретка; ↑/↓ портал. Проверьте до первого угла.",
                  font=("Segoe UI", 9), wraplength=315).pack(anchor="w")

        pen_box = ttk.LabelFrame(service, text="Автоматический подъём", padding=10)
        pen_box.pack(fill="x", pady=(0,8))
        self.pen_widgets = []
        enabled = ttk.Checkbutton(pen_box, text="Автоматически поднимать ручку", variable=self.pen_enabled, command=self.apply_pen_settings)
        enabled.pack(anchor="w")
        ttk.Label(pen_box,textvariable=self.pen_summary,wraplength=305,font=("Segoe UI",9)).pack(anchor="w",pady=(3,4))
        row = ttk.Frame(pen_box);row.pack(fill="x")
        self.pen_up_button = ttk.Button(row,text="Проверить подъём",command=lambda:self.submit("pen_up"))
        self.pen_up_button.pack(side="left",fill="x",expand=True)
        self.pen_settings_button = ttk.Button(row,text="Настройки…",command=self.toggle_pen_settings)
        self.pen_settings_button.pack(side="left",padx=(4,0))
        ttk.Label(pen_box,textvariable=self.pen_status,wraplength=305,font=("Segoe UI",9),foreground="#8f2939").pack(anchor="w",pady=(4,0))
        self.pen_advanced = ttk.Frame(pen_box)
        row = ttk.Frame(self.pen_advanced); row.pack(fill="x",pady=(8,4))
        ttk.Label(row,text="Выход платы активен по").pack(side="left")
        self.pen_polarity = ttk.Combobox(row,textvariable=self.pen_active,values=("Не проверено","HIGH","LOW"),state="readonly",width=13)
        self.pen_polarity.pack(side="right")
        self.pen_polarity.bind("<<ComboboxSelected>>",self.pen_polarity_changed)
        row = ttk.Frame(self.pen_advanced);row.pack(fill="x",pady=2)
        ttk.Label(row,text="Питание ручку").pack(side="left")
        self.pen_effect_combo = ttk.Combobox(row,textvariable=self.pen_effect,values=("Поднимает","Опускает"),state="readonly",width=13)
        self.pen_effect_combo.pack(side="right")
        self.pen_effect_combo.bind("<<ComboboxSelected>>",self.pen_effect_changed)
        verified = ttk.Checkbutton(self.pen_advanced,text="Действие питания и полярность проверены",variable=self.pen_confirmed,command=self.apply_pen_settings)
        verified.pack(anchor="w")
        continuous = ttk.Checkbutton(self.pen_advanced,text="Без ограничения времени соленоида",variable=self.pen_continuous,command=self.apply_pen_settings)
        continuous.pack(anchor="w")
        for caption,variable in (("Подъём, мс",self.pen_up_ms),("Опускание, мс",self.pen_down_ms)):
            row = ttk.Frame(self.pen_advanced); row.pack(fill="x",pady=2)
            ttk.Label(row,text=caption+" (50–500)").pack(side="left")
            field = ttk.Entry(row,textvariable=variable,width=8);field.pack(side="right")
            self.pen_widgets.append(field)
        save = ttk.Button(self.pen_advanced,text="Сохранить настройки",command=self.apply_pen_settings)
        save.pack(fill="x",pady=3)
        help_button = ttk.Button(self.pen_advanced,text="Определить HIGH / LOW…",command=self.open_pen_observer)
        help_button.pack(fill="x",pady=3)
        self.pen_pulse_button = ttk.Button(self.pen_advanced,text="Короткий тест ручки",command=self.pen_pulse)
        self.pen_pulse_button.pack(fill="x",pady=3)
        self.pen_widgets.extend((enabled,verified,continuous,save,help_button,self.pen_settings_button))
        ttk.Label(self.pen_advanced,text="Тест кратковременный, оси не движутся. Непрерывный режим требует соответствующей прошивки; температура не измеряется.",wraplength=305,font=("Segoe UI",9)).pack(anchor="w")

        jog = ttk.LabelFrame(left, text="Перемещение", padding=10)
        jog.pack(fill="x", pady=(0, 8))
        row = ttk.Frame(jog)
        row.pack(fill="x")
        ttk.Label(row, text="Ход за нажатие, мм").pack(side="left")
        self.step_input = ttk.Combobox(row, textvariable=self.step,
                                      values=("0.1", "1", "5", "10", "20", "50", "100"), width=8, state="normal")
        self.step_input.pack(side="left", padx=6)
        speed_row = ttk.Frame(jog)
        speed_row.pack(fill="x", pady=(4, 0))
        ttk.Label(speed_row, text="Скорость, %").pack(side="left")
        self.feed_input = ttk.Entry(speed_row, textvariable=self.feed, width=6)
        self.feed_input.pack(side="left", padx=(6, 0))
        cross = ttk.Frame(jog)
        cross.pack()
        self.jog_buttons = []
        for label, dx, dy, r, c in (("↑", 0, -1, 0, 1), ("←", -1, 0, 1, 0),
                                    ("→", 1, 0, 1, 2), ("↓", 0, 1, 2, 1)):
            button = ttk.Button(cross, text=label, width=5, command=lambda x=dx, y=dy: self.jog(x, y))
            button.grid(row=r, column=c, padx=3, pady=2)
            self.jog_buttons.append(button)
        ttk.Label(jog, textvariable=self.coordinates).pack(anchor="w", pady=(5, 0))

        corners = ttk.LabelFrame(left, text="Задать лист по двум углам", padding=10)
        corners.pack(fill="x", pady=(0, 8))
        self.first_button = ttk.Button(corners, text="1. Верхний левый угол", command=self.set_zero)
        self.first_button.pack(fill="x", pady=(0, 5))
        self.second_button = ttk.Button(corners, text="2. Нижний правый угол", command=self.set_corner)
        self.second_button.pack(fill="x")
        ttk.Label(corners, textvariable=self.paper_text, wraplength=315).pack(anchor="w", pady=7)
        self.clear_button = ttk.Button(corners, text="Новый лист", command=lambda: self.submit("clear"))
        self.clear_button.pack(fill="x")

        drawing = ttk.LabelFrame(right, text="Рисунок", padding=10)
        drawing.pack(fill="both", expand=True)
        self.canvas = tk.Canvas(drawing, bg="#dde5ee", highlightthickness=0)
        self.canvas.pack(fill="both", expand=True)
        self.canvas.bind("<Configure>", lambda event: self.request_redraw())
        ttk.Label(service, textvariable=self.pen_legend, wraplength=580,
                  foreground="#9b4d08", font=("Segoe UI", 9)).pack(anchor="w", pady=(6, 0))
        file_row = ttk.Frame(right)
        file_row.pack(fill="x", pady=8)
        self.load_button = ttk.Button(file_row, text="Открыть SVG", command=self.open_svg)
        self.load_button.pack(side="left")
        svg_settings = ttk.LabelFrame(settings, text="SVG", padding=10)
        svg_settings.pack(fill="x")
        ttk.Label(svg_settings, text="Поле SVG, мм").pack(side="left")
        self.margin_input = ttk.Entry(svg_settings, textvariable=self.margin, width=5)
        self.margin_input.pack(side="left", padx=6)
        self.test_button = ttk.Button(svg_settings, text="Тест ≤10 мм", command=self.load_test)
        self.test_button.pack(side="right")
        self.file_label = ttk.Label(right, text=self.file_name, wraplength=700)
        self.file_label.pack(anchor="w")
        actions = ttk.Frame(right)
        actions.pack(fill="x", pady=(8, 0))
        self.startpoint_button = ttk.Button(actions, text="К началу рисунка", command=self.go_start)
        self.startpoint_button.pack(side="left", padx=(0, 8))
        self.draw_button = ttk.Button(actions, text="Начать рисование", command=self.draw)
        self.draw_button.pack(side="left")
        self.progress = ttk.Progressbar(right, maximum=100)
        self.progress.pack(fill="x", pady=8)
        ttk.Label(right, textvariable=self.hint, wraplength=700, foreground="#225ea8").pack(anchor="w")
        ttk.Label(diagnostics, text="Без концевиков программа не видит упоры и потерянные шаги. Не двигайте оси рукой после настройки.\n"
                  "СТОП — программный сброс, не аварийное отключение питания. При сбое USB моторы могут продолжить движение.",
                  wraplength=580, foreground="#8f2939", font=("Segoe UI", 9)).pack(anchor="w")
        self.log = tk.Text(diagnostics, height=10, wrap="word", font=("Consolas", 9),
                           state="disabled", bg="#f8fafc", relief="flat")
        self.log.pack(fill="both", expand=True, pady=(6, 0))

    def open_settings(self):
        self.settings_window.deiconify()
        self.settings_window.lift()

    def note(self, text):
        self.log.configure(state="normal")
        self.log.insert("end", text + "\n")
        if int(self.log.index("end-1c").split(".")[0]) > 100:
            self.log.delete("1.0", "2.0")
        self.log.see("end")
        self.log.configure(state="disabled")

    def open_editor(self):
        from editor_tools.window import EditorWindow
        if self.editor_window is not None and not self.editor_window.closed:
            self.editor_window.root.lift()
            return
        self.editor_window = EditorWindow(self)

    def open_assistant(self):
        from assistant.window import AssistantWindow
        if self.assistant_window is not None and not self.assistant_window.closed:
            self.assistant_window.root.lift()
            return
        self.assistant_window = AssistantWindow(
            self.root, self.editor_snapshot, self.apply_assistant_preview,
            self.undo_editor_preview, self.redo_editor_preview, self.load_editor_project,
            data_dir=self.data_dir, autosave_store=self.autosaves, editor_history=self.studio_history)

    def studio_history(self):
        current = (self.paths,self.file_name,self.editor_task,self.editor_anchor)
        return list(self.editor_history.past)+[current]+list(reversed(self.editor_history.future)),len(self.editor_history.past)

    def open_dwin(self, *, show=True):
        native = self.native_panel and self.network_controller()
        if self.native_panel and not native:
            if show:
                messagebox.showinfo("Связь с DWIN", "Эта прошивка MKS передаёт состояние экрана по Wi-Fi. USB доступен для диагностики.", parent=self.root)
            return
        if native and self.dwin_window is not None and not getattr(self.dwin_window, "native", False):
            self.dwin_window.close()
        if self.dwin_window is None or self.dwin_window.closed:
            from dwin_ui import DwinWindow, NativeDwinWindow
            self.dwin_window = NativeDwinWindow(self) if native else DwinWindow(self)
            if native:
                self.dwin_window.start()
        if show:
            self.dwin_window.window.deiconify()
            self.dwin_window.window.lift()
        else:
            self.dwin_window.window.withdraw()

    def panel_client(self):
        window = getattr(self, "dwin_window", None)
        return window.bridge if getattr(window, "native", False) and not window.closed else None

    def apply_panel_state(self, board):
        from geometry import Sheet
        previous_position = self.position
        identity = ((board.boot_id, board.generation, board.has_zero, board.has_sheet)
                    if board and board.confirmed else None)
        changed = identity != self.panel_calibration
        if changed:
            self.preserve_draft()
            self.panel_calibration = identity
            self.preview_revision += 1
            self.cancel_svg()
            self.zero = bool(identity and board.has_zero)
            self.sheet = Sheet(board.width_mm, board.height_mm) if identity and board.has_sheet else None
            if self.sheet:
                self.draft_sheet = self.sheet
                self.paper_text.set(f"Лист MKS: {self.sheet.width:.2f} × {self.sheet.height:.2f} мм")
            else:
                self.paper_text.set("Первый угол сохранён на MKS" if self.zero else self.draft_notice())
            self.rebase_editor_history()
            self.invalidate_editor()
        if identity:
            origin = board.origin_mm if board.has_zero else (0.0, 0.0)
            self.position = tuple(a - b for a, b in zip(board.position_mm, origin))
            right, down = board.axes.paper(self.position)
            self.coordinates.set(f"Вправо: {right:.2f} · вниз: {down:.2f} мм")
        else:
            self.position = None
            self.coordinates.set("Положение: —")
        names = {12: "К началу", 13: "Рисовать", 15: "Тестовый квадрат",
                 16: "Открыть SVG", 17: "Редактор", 18: "Камера"}
        if board and board.request_id and board.request_key in names:
            self.panel_request_button.configure(text="Действие с экрана · " + names[board.request_key],
                                                state="disabled" if board.busy or self.controller.busy or self.svg_loading else "normal")
            if not self.panel_request_button.winfo_manager():
                self.panel_request_button.pack(after=self.status_label, anchor="w", pady=(0, 8))
            request = (board.boot_id, board.generation, board.request_id, board.request_key)
            if (request != self.offered_panel_request and not board.busy and not self.controller.busy and
                    not self.svg_loading):
                self.offered_panel_request = request
                self.root.after_idle(self.handle_panel_request, request)
        else:
            self.offered_panel_request = None
            self.panel_request_button.pack_forget()
        if changed or self.position != previous_position:
            self.request_redraw()

    def reject_panel_request(self, request):
        panel = self.panel_client()
        state = panel.snapshot() if panel else None
        if state and request == (state.boot_id, state.generation, state.request_id, state.request_key):
            panel.respond(request[2], 7)

    def handle_panel_request(self, expected=None):
        if self.closed:
            return
        panel = self.panel_client()
        board = panel.snapshot() if panel else None
        if (not board or not board.confirmed or not board.request_id or board.busy or self.controller.busy or
                self.svg_loading):
            return
        request = (board.boot_id, board.generation, board.request_id, board.request_key)
        if expected is not None and request != expected:
            return
        if board.request_key in (12, 13):
            if not board.has_sheet or not self.paths:
                self.note("DWIN: задайте два угла и загрузите рисунок, затем повторите запрос.")
                panel.respond(board.request_id, 4)
            else:
                self.feed.set(str(board.speed_pct))
                if board.request_key == 12:
                    self.go_start(panel_request=request)
                else:
                    self.draw(panel_request=request)
            return
        actions = {15: self.load_test, 16: self.open_svg, 17: self.open_editor, 18: self.open_camera}
        callback = actions.get(board.request_key)
        if callback is None:
            return
        if board.request_key in (15, 16) and not self.drawing_sheet():
            panel.respond(board.request_id, 4)
        elif panel.respond(board.request_id, 9):
            callback()

    def open_camera(self):
        try:
            from camera_ui import CameraWindow
            if self.camera_window is not None and not self.camera_window.closed:
                self.camera_window.root.lift()
                return
            self.camera_window = CameraWindow(self.root, self.camera_context, self.start_camera_scan, self.stop)
        except ImportError:
            messagebox.showerror("Камера", "Установите компоненты камеры через INSTALL_CAMERA.cmd.", parent=self.root)

    def camera_context(self):
        return {"busy": self.controller.busy or self.svg_loading,
                "position": self.axes().paper(self.position) if self.position is not None else None,
                "revision": self.editor_revision(), "simulation": self.simulation,
                "connected": self.connected, "sheet": self.sheet,
                "pen_lift_enabled": self.pen_enabled.get(), "pen_lift_ready": self.pen_ready(),
                "pen_power_raises": self.pen_power_raises.get(),
                "pen_continuous": self.pen_continuous.get()}

    def start_camera_scan(self, points, take_photo):
        return self.submit("scan", points=points, capture=take_photo, feed=self.get_feed())

    def clear_editor_history(self):
        self.editor_history.clear()
        self.editor_task = self.editor_anchor = self.editor_undo = None

    def editor_snapshot(self):
        from editor_tools.preview import snapshot
        sheet = self.drawing_sheet()
        try:
            preview_feed = self.get_feed()
        except ValueError:
            preview_feed = None
        return snapshot({"sandbox": False, "connected": self.connected,
                         "machine_profile": self.effective_machine_profile(),
                         "board_revision": self.settings.get("_board_revision"),
                         "controller_config_verified": self.connected and not self.simulation and self.machine_ready(),
                         "display_model": "DMG80480C043_01WTC",
                         "native_panel": getattr(self, "native_panel", False),
                         "panel_assistant": bool(getattr(self.controller.session, "panel_assistant", False)),
                         "simulation": self.simulation, "file_name": self.file_name,
                         "busy": self.controller.busy or self.svg_loading,
                         "feed_mm_min": preview_feed,
                         "pen_lift_enabled": self.pen_enabled.get(), "pen_lift_ready": self.pen_ready(),
                         "pen_power_raises": self.pen_power_raises.get(),
                         "pen_continuous": self.pen_continuous.get(),
                          "sheet_width": sheet.width if sheet else None,
                          "sheet_height": sheet.height if sheet else None,
                          "machine_calibrated": bool(self.zero and self.sheet),
                         "preview_task": self.editor_task, "preview_anchor": self.editor_anchor},
                        self.paths, self.editor_revision())

    def editor_revision(self):
        return ("editor", self.preview_revision, id(self.paths), self.drawing_sheet(),
                self.zero, self.axes(), self.connected, self.port_name, self.effective_machine_profile())

    def invalidate_editor(self):
        if self.editor_window is not None and not self.editor_window.closed:
            self.editor_window.editor_changed()
        assistant = getattr(self, "assistant_window", None)
        if assistant is not None and not assistant.closed:
            assistant.editor_changed()

    def apply_editor_preview(self, before, plan):
        from editor_tools import draft_context, planner
        return self._apply_editor_plan(before, plan, planner, draft_context)

    def apply_assistant_preview(self, before, plan):
        from assistant import draft_context, planner
        return self._apply_editor_plan(before, plan, planner, draft_context)

    def _apply_editor_plan(self, before, plan, planner, draft_context):
        sheet = self.drawing_sheet()
        if (self.controller.busy or self.svg_loading or sheet is None or
                before.revision != self.editor_revision()):
            raise ValueError("Эскиз, калибровка или состояние пульта изменились. Повторите команду.")
        if plan.kind not in planner.ACTION_KINDS:
            raise ValueError("Разрешены только изменения эскиза; команды станку недоступны.")
        # Recalculate against the current editor and validate again at commit time.
        # This method does not call Controller.submit, connect, jog or any serial method.
        result = planner.transform_paths(self.paths, plan, sheet.width, sheet.height)
        task = draft_context.applied_task(plan, self.editor_task)
        anchor = draft_context.plan_anchor(plan, self.editor_anchor)
        previous = (self.paths, self.file_name, self.editor_task, self.editor_anchor)
        self.editor_history.push(previous)
        self.paths = result
        self.file_name = "Эскиз редактора · " + plan.explanation
        self.editor_task, self.editor_anchor = task, anchor
        self.loaded(preserve_history=True)
        self.editor_undo = (self.editor_revision(), previous)
        self.note("Редактор изменил только эскиз. Движение не запускалось.")
        return self.editor_snapshot()

    def undo_editor_preview(self):
        if self.controller.busy or self.svg_loading:
            raise ValueError("Сначала дождитесь завершения операции в пульте.")
        if self.editor_undo is None or self.editor_undo[0] != self.editor_revision():
            raise ValueError("Нет последней операции редактора для отмены; эскиз или калибровка могли измениться.")
        self.paths, self.file_name, self.editor_task, self.editor_anchor = self.editor_history.undo(
            (self.paths, self.file_name, self.editor_task, self.editor_anchor))
        self.loaded(preserve_history=True)
        self.editor_undo = (self.editor_revision(), None)
        return self.editor_snapshot()

    def redo_editor_preview(self):
        if self.controller.busy or self.svg_loading or self.editor_undo is None or self.editor_undo[0] != self.editor_revision():
            raise ValueError("Эскиз или состояние изменились; повтор недоступен.")
        self.paths, self.file_name, self.editor_task, self.editor_anchor = self.editor_history.redo(
            (self.paths, self.file_name, self.editor_task, self.editor_anchor))
        self.loaded(preserve_history=True)
        self.editor_undo = (self.editor_revision(), None)
        return self.editor_snapshot()

    def load_editor_project(self, project):
        sheet = self.drawing_sheet()
        if self.controller.busy or self.svg_loading:
            raise ValueError("Дождитесь завершения операции. Проект не восстанавливает ноль станка.")
        adopt_sheet = sheet is None and not self.zero
        if adopt_sheet:
            sheet = project.sheet
        if sheet is None:
            raise ValueError("Сначала завершите установку двух углов или сбросьте калибровку.")
        if abs(sheet.width - project.sheet.width) > 0.01 or abs(sheet.height - project.sheet.height) > 0.01:
            raise ValueError("Размер проекта отличается от области пульта. Откройте его на пробном листе.")
        from geometry import validate_paths
        validate_paths(project.paths, sheet)
        if adopt_sheet:
            self.draft_sheet = sheet
            self.paper_text.set(self.draft_notice())
        self.editor_history.push((self.paths, self.file_name, self.editor_task, self.editor_anchor))
        self.paths, self.file_name = project.paths, project.name
        self.editor_task, self.editor_anchor = project.task, project.anchor
        self.loaded(preserve_history=True)
        self.editor_undo = (self.editor_revision(), None)
        return self.editor_snapshot()

    def refresh_ports(self):
        from serial.tools import list_ports
        current = self.port.get()
        network = [current] if app_profile(self) == DLC32 and current.startswith("tcp://") else []
        choices = ["DEMO"] + network + [port.device for port in list_ports.comports()]
        self.ports["values"] = choices
        if self.port.get() not in choices:
            self.port.set("DEMO")

    def submit(self, action, **params):
        if self.closed:
            return False
        if getattr(self, "native_panel", False) and action not in ("connect", "disconnect", "configure"):
            panel = self.panel_client()
            if action in ("zero", "corner", "clear"):
                return bool(panel and panel.calibrate({"clear": 0, "zero": 1, "corner": 2}[action]))
            params["panel_client"] = panel
            if "panel_context" not in params:
                params["panel_context"] = self.panel_calibration[:2] if self.panel_calibration else None
        params.setdefault("axes", self.axes())
        try:
            if action not in ("disconnect","configure","hold","clear","unlock"):
                params.setdefault("pen", self.read_pen_settings().as_dict())
        except ValueError as exc:
            messagebox.showerror("Настройки ручки",str(exc),parent=self.root)
            return False
        if self.controller.submit(action, **params):
            self.update_controls()
            return True
        return False

    def connect(self):
        if (not self.connected and self.dwin_window is not None and
                self.dwin_window.bridge.thread is not None and self.dwin_window.bridge.thread.is_alive() and
                self.port.get().casefold() == str(self.dwin_window.bridge.port).casefold()):
            messagebox.showerror("Порт занят экраном", "Для Arduino и DWIN нужны разные COM-порты.", parent=self.root)
            return
        if self.pen_observer is not None and not self.pen_observer.closed:
            messagebox.showerror("Проверка полярности","Сначала закройте окно наблюдения A3: оно может занимать USB-порт.",parent=self.root)
            return
        if self.connected:
            self.submit("disconnect")
            return
        selected = app_profile(self)
        if self.port.get().lower().startswith("tcp:"):
            try:
                if selected != DLC32:
                    raise ValueError("Wi-Fi поддерживается только профилем MKS DLC32 / FluidNC.")
                from network_transport import parse_tcp_endpoint
                self.port.set(parse_tcp_endpoint(self.port.get()).url)
            except ValueError as exc:
                messagebox.showerror("Адрес платы", str(exc), parent=self.root)
                return
        requirement = ("Нужна подготовленная FluidNC и конфигурация для вашей версии MKS DLC32. "
                       "Пульт прочитает конфигурацию, но не будет её переписывать. "
                       if selected == DLC32 else "Нужна прошивка GRBL 1.1. ")
        first_connection = ("Первую проверку новой платы выполните БЕЗ питания моторов И соленоида. "
                            "Для работы по Wi-Fi затем потребуется сетевой DGUS-мост этой же платы (порт 8888). "
                            "При разрыве сети команды могут не дойти; программный СТОП не заменяет выключение БП.\n\n"
                            if self.port.get().startswith("tcp://") else
                            "Первое подключение выполните БЕЗ питания моторов И соленоида (оба БП 12 В отключены): "
                            "открытие USB может перезапустить плату, а старые стартовые команды могут запустить движение.\n\n")
        if self.port.get() != "DEMO" and not messagebox.askokcancel("Перед подключением",
                requirement + "Закройте другие программы и сетевые клиенты управления платой.\n\n" + first_connection +
                "Продолжить подключение?", parent=self.root):
            return
        self.status.set("Подключение…")
        self.submit("connect", port=self.port.get(), hold=self.hold.get(), machine_profile=selected)

    def change_machine_profile(self, event=None):
        if self.connected or self.controller.busy:
            return
        # Relay pin and logic voltage differ even when the mechanical lift is
        # unchanged. Confirmation on the Uno does not verify a rewired DLC32.
        pen = self.stored_pen_settings()
        self.pen_enabled.set(pen.enabled)
        self.pen_active.set("Не проверено" if pen.active_low is None else "LOW" if pen.active_low else "HIGH")
        self.pen_confirmed.set(False)
        self.pen_power_raises.set(pen.power_raises)
        self.pen_continuous.set(pen.continuous)
        self.pen_up_ms.set(str(pen.up_ms))
        self.pen_down_ms.set(str(pen.down_ms))
        try:
            save_selected_profile(self.data_dir, app_profile(self))
        except OSError as exc:
            messagebox.showerror("Сохранение профиля", str(exc), parent=self.root)
        self.pen_firmware = None
        self.settings = {}
        self.hint.set("MKS по Wi-Fi: в поле порта введите tcp://IP-платы:23. ПК и плата должны быть в одной локальной сети. "
                      "Адрес не выбирается автоматически." if app_profile(self) == DLC32 else
                      "Для Uno выберите его COM-порт. DEMO не подключает оборудование.")
        self.update_controls()

    def stored_pen_settings(self):
        selected = app_profile(self)
        directory = pen_settings_directory(self.data_dir, selected)
        if selected == DLC32 and (directory is None or not (directory / "pen-lift-settings.json").exists()):
            return PenLiftSettings(True, False, False, 250, 250, True, True)
        return load_settings(directory)

    def effective_machine_profile(self):
        return UNO if self.simulation and self.connected else app_profile(self)

    def machine_ready(self):
        if getattr(self, "native_panel", False):
            panel = self.panel_client()
            board = panel.snapshot() if panel else None
            return bool(board and board.confirmed and not board.busy and
                        settings_ready(self.settings, self.effective_machine_profile()))
        return (settings_ready(self.settings, self.effective_machine_profile()) and
                (not self.network_controller() or self.network_lease_ready()))

    def network_controller(self):
        session = getattr(self.controller, "session", None)
        return bool(session is not None and session.machine_profile == DLC32 and
                    str(getattr(session.wire, "port", "")).lower().startswith("tcp:"))

    def network_lease_ready(self):
        """Worker-safe: immutable strings and the bridge's locked lease API only.

        Never read Tk variables or cause UI work from the controller thread.
        """
        session = getattr(self.controller, "session", None)
        window = getattr(self, "dwin_window", None)
        bridge = getattr(window, "bridge", None)
        if session is None or bridge is None:
            return False
        try:
            from network_transport import parse_tcp_endpoint
            motion = parse_tcp_endpoint(str(getattr(session.wire, "port", "")))
            display = parse_tcp_endpoint(str(getattr(bridge, "port", "")))
            # A matching entered hostname does not prove a matching board:
            # DNS/mDNS can change between the two independent TCP connects.
            # Missing numeric-peer evidence is not sufficient for movement.
            motion_peer = getattr(session.wire, "peer_address", None)
            native = getattr(session, "native_panel", False)
            if native:
                if not getattr(window, "native", False) or window.closed:
                    return False
                display_peer = getattr(bridge, "peer_address", None)
            else:
                display_peer = getattr(getattr(bridge, "network_wire", None), "peer_address", None)
            ready = getattr(bridge, "lease_ready", None)
            return bool((native or motion.host.casefold() == display.host.casefold()) and motion.port == 23 and
                        display.port == 8888 and motion_peer is not None and
                        motion_peer == display_peer and callable(ready) and ready())
        except (ValueError, OSError):
            return False

    def update_network_lease(self):
        if getattr(self, "native_panel", False):
            return
        window = self.dwin_window
        bridge = getattr(window, "bridge", None)
        wanted = bool(self.connected and not self.simulation and self.network_controller() and
                      not self.controller.cancelled.is_set())
        setter = getattr(bridge, "set_control_lease", None)
        if callable(setter):
            setter(wanted)
        ready = wanted and self.network_lease_ready()
        if self.network_lease_seen and wanted and not ready:
            try:
                detail = bridge.diagnostic_state()
            except Exception:
                detail = {}
            record_event(self, "network_lease_first_loss", wanted=wanted, ready=ready, **detail)
            self.network_lease_seen = False
            self.stop()
            self.status.set("Сетевая связь контроля MKS потеряна: СТОП, углы сброшены. Повторного запуска нет.")
        else:
            self.network_lease_seen = bool(ready)

    def read_pen_settings(self):
        try:
            up,down = int(self.pen_up_ms.get()),int(self.pen_down_ms.get())
        except ValueError:
            raise ValueError("Задержки ручки задаются целыми миллисекундами: 50–500.")
        active = {"Не проверено":None,"HIGH":False,"LOW":True}.get(self.pen_active.get())
        return PenLiftSettings(self.pen_enabled.get(),active,self.pen_confirmed.get(),up,down,
                               self.pen_power_raises.get(),self.pen_continuous.get()).validate()

    def pen_ready(self):
        try:
            pen = self.read_pen_settings()
            if not pen.enabled:return True
            if self.simulation and self.connected:return True
            if not self.connected:return False
            pen.validate(require_ready=True)
            return pen_matches(self.pen_firmware, pen, self.effective_machine_profile())
        except ValueError:return False

    def pen_test_ready(self):
        try:
            pen = self.read_pen_settings()
            if not pen.enabled or not self.connected:return False
            if self.simulation:return True
            return pen.active_low is not None and pen_matches(self.pen_firmware, pen, self.effective_machine_profile())
        except ValueError:return False

    def pen_polarity_changed(self,event=None):
        self.pen_confirmed.set(False)
        self.apply_pen_settings()

    def pen_effect_changed(self,event=None):
        self.pen_power_raises.set(self.pen_effect.get() == "Поднимает")
        self.pen_confirmed.set(False)
        self.apply_pen_settings()

    def toggle_pen_settings(self):
        if self.pen_advanced.winfo_manager():
            self.pen_advanced.pack_forget()
            self.pen_settings_button.configure(text="Настройки…")
        else:
            self.pen_advanced.pack(fill="x")
            self.pen_settings_button.configure(text="Свернуть")

    def apply_pen_settings(self):
        if self.controller.busy:return
        try:
            pen = self.read_pen_settings()
            save_settings(pen_settings_directory(self.data_dir, app_profile(self)),pen)
        except (ValueError,OSError) as exc:
            messagebox.showerror("Настройки ручки",str(exc),parent=self.root)
            return
        self.pen_legend.set(connection_notice(pen.enabled, pen.power_raises, pen.continuous))
        self.preview_revision += 1
        self.invalidate_editor()
        self.update_controls()

    def pen_pulse(self):
        # This explicit button is the actuation request; no repeated modal per test.
        # The controller verifies the firmware variant; the explicit test is short.
        self.submit("pen_pulse")

    def open_pen_observer(self):
        if app_profile(self) == DLC32:
            messagebox.showinfo("MKS DLC32", "Окно наблюдения A3 предназначено только для Uno. "
                                "Для MKS используются GPIO4, M8/M9 и проверка config.yaml.", parent=self.root)
            return
        if self.connected or self.controller.busy:
            messagebox.showerror("Проверка полярности","Сначала отключите пульт от USB. БП моторов и соленоида должны быть выключены.",parent=self.root)
            return
        if self.pen_observer is not None and not self.pen_observer.closed:
            self.pen_observer.root.lift();return
        from relay_polarity import RelayPolarityWindow
        def choice(profile):
            self.pen_active.set('LOW' if profile.active_low else 'HIGH')
            self.pen_enabled.set(True);self.pen_confirmed.set(False)
            self.apply_pen_settings()
        self.pen_observer=RelayPolarityWindow(self.root,data_dir=self.data_dir,on_choice=choice)

    def change_hold(self):
        if self.effective_machine_profile() == DLC32:
            self.hold.set(self.settings.get(1) == 255)
            messagebox.showinfo("Удержание FluidNC", "Удержание задаётся stepping/idle_ms в config.yaml. "
                                "Пульт не переписывает конфигурацию FluidNC.", parent=self.root)
            return
        if self.connected:
            self.submit("hold", hold=self.hold.get())

    def configure(self):
        if self.effective_machine_profile() == DLC32:
            if messagebox.askokcancel("Проверить FluidNC",
                    "Пульт повторно прочитает config.yaml, проверит оси и выход реле. "
                    "Настройки платы не изменятся; углы листа будут сброшены. "
                    "При Alarm автоматическая разблокировка не выполняется.", parent=self.root):
                self.submit("configure", hold=self.hold.get())
            return
        if messagebox.askokcancel("Настройка без концевиков",
                "Будут сохранены: $20=0, $21=0, $22=0 (отключить лимиты и homing), "
                "$10=0 и $13=0 (рабочие координаты в мм), а также максимум скорости "
                "$110=$111=3000 мм/мин и ускорение $120=$121=150 мм/с².\n\n"
                "Удержание моторов — по галочке «Удерживать моторы в простое».\n\n"
                "При Alarm также отправится $X — разблокировка. Убедитесь, что механика свободна, "
                "причина аварии устранена и концевиков действительно нет.\n\n"
                "Шаги/мм и ток драйверов не настраиваются. Обе точки листа будут сброшены. "
                "Первую проверку 100% выполняйте без ручки.", parent=self.root):
            self.submit("configure", hold=self.hold.get())

    def unlock_alarm(self):
        if (self.effective_machine_profile() != DLC32 or self.simulation or not self.connected or
                self.controller.busy or self.controller.cancelled.is_set()):
            messagebox.showinfo("Снятие аварии MKS",
                                "Сначала подключите MKS и дождитесь окончания текущей операции.", parent=self.root)
            return
        if not self.machine_ready():
            messagebox.showinfo("Снятие аварии MKS",
                                "Нужны проверенная конфигурация MKS и подтверждённый сетевой канал управления.",
                                parent=self.root)
            return
        if not messagebox.askokcancel("Снять аварию MKS?",
                "Сначала физически устраните причину аварии и освободите оба концевика. "
                "Не пытайтесь отъехать от нажатого концевика этой кнопкой. Если требуется ручной отвод, "
                "сначала выключите питание моторов.\n\n"
                "Подтверждаете, что причина устранена, механика свободна и концевики освобождены? "
                "Пульт прочитает свежие входы Pn и только при Alarm отправит $X. "
                "Это НЕ движение и НЕ базирование; реле не включается. "
                "Оба угла листа будут сброшены, рисунок сохранится.", parent=self.root):
            return
        self.preserve_draft()
        self.submit("unlock", confirmed=True)

    def get_feed(self):
        percent = float(self.feed.get().replace(",", "."))
        if not math.isfinite(percent) or not 1 <= percent <= 100:
            raise ValueError("Введите скорость от 1 до 100%.")
        configured_max = min(float(self.settings.get(110, MACHINE_MAX_FEED)),
                             float(self.settings.get(111, MACHINE_MAX_FEED)))
        return configured_max * percent / 100.0

    def get_step(self):
        step = float(self.step.get().replace(",", "."))
        if not math.isfinite(step) or not 0.01 <= step <= MAX_JOG_DISTANCE:
            raise ValueError(f"Введите ход за нажатие от 0,01 до {MAX_JOG_DISTANCE:g} мм.")
        return step

    def jog(self, dx, dy):
        try:
            step = self.get_step()
            self.submit("jog", delta=(dx * step, dy * step), feed=self.get_feed())
        except ValueError as exc:
            messagebox.showerror("Проверьте ввод", str(exc), parent=self.root)

    def set_zero(self):
        self.submit("zero")

    def set_corner(self):
        self.submit("corner")

    def load_test(self):
        sheet = self.drawing_sheet()
        if self.controller.busy or self.svg_loading or sheet is None:
            return False
        try:
            self.paths = test_square(sheet)
            self.file_name = "Тестовый квадрат: сторона ≤10 мм, отступ 1 мм"
            self.loaded()
            return True
        except ValueError as exc:
            messagebox.showerror("Тест", str(exc), parent=self.root)
            return False

    def open_svg(self):
        if self.svg_dialog or self.controller.busy or self.svg_loading or self.drawing_sheet() is None:
            return
        before = self.editor_revision()
        self.svg_dialog = True
        try:
            filename = filedialog.askopenfilename(filetypes=[("Векторный рисунок SVG", "*.svg")], parent=self.root)
        finally:
            self.svg_dialog = False
        if filename and not self.closed:
            if before == self.editor_revision():
                self.load_file(filename)
            else:
                self.note("Пока выбирался SVG, эскиз или калибровка изменились. Откройте файл заново.")

    def cancel_svg(self):
        self.svg_token = None
        self.svg_loading = False

    def load_file(self, filename):
        if self.controller.busy or self.svg_loading or self.drawing_sheet() is None:
            return
        if self.svg_worker is not None and self.svg_worker.is_alive():
            self.note("Предыдущее чтение SVG ещё завершается. Повторите открытие файла чуть позже.")
            return
        try:
            margin = float(self.margin.get().replace(",", "."))
        except Exception as exc:
            messagebox.showerror("SVG не загружен", str(exc), parent=self.root)
            return
        sheet = self.drawing_sheet()
        token = self.svg_token = object()
        self.svg_loading = True
        self.hint.set("Загружаю рисунок…")
        self.update_controls()
        event_queue = self.svg_events

        def read_svg():
            try:
                result = load_svg(filename, sheet, margin)
            except Exception as exc:
                result = exc
            # Only the Tk thread handles widgets; obsolete imports are ignored.
            event_queue.put((token, filename, margin, result))

        self.svg_worker = threading.Thread(target=read_svg, daemon=True)
        try:
            self.svg_worker.start()
        except RuntimeError:
            self.cancel_svg()
            self.update_controls()
            self.note("Не удалось запустить чтение SVG. Повторите открытие файла.")

    def loaded(self, preserve_history=False):
        if not preserve_history:
            self.editor_history.clear()
            self.editor_undo = None
            self.editor_task = self.editor_anchor = None
        self.preview_revision += 1
        self.invalidate_editor()
        self.file_label.configure(text=self.file_name)
        self.progress["value"] = 0
        self.hint.set("Проверьте контуры и переходы. В проверенном режиме соленоида переходы выполняются с поднятой ручкой; иначе поднимайте её вручную.")
        self.redraw()
        self.update_controls()
        if self.autosaves and self.drawing_sheet() is not None and self.paths:
            enabled = all(window.autosave_enabled.get() for window in
                          (self.editor_window, getattr(self, "assistant_window", None))
                          if window is not None and not window.closed)
            if enabled:
                from editor_project import SketchProject
                self.autosaves.schedule('Эскиз плоттера',SketchProject(self.drawing_sheet(),self.paths,self.file_name[:500],self.editor_task,self.editor_anchor))

    def go_start(self, *, panel_request=None):
        return self.request_job("startpoint", panel_request=panel_request)

    def draw(self, *, panel_request=None):
        return self.request_job("draw", panel_request=panel_request)

    def request_job(self, action, *, panel_request=None):
        if self.closed or self.controller.busy or self.svg_loading:
            return False
        last = self.last_motion_submit
        if last is not None and last[0] == action and time.monotonic() - last[1] < .35:
            return False
        panel_context = self.panel_calibration[:2] if self.panel_calibration else None
        accepted = self.start_job(action, panel_request=panel_request, panel_context=panel_context)
        if accepted:
            self.last_motion_submit = action, time.monotonic()
        elif panel_request is not None:
            self.reject_panel_request(panel_request)
        return accepted

    def start_job(self, action, *, panel_request=None, panel_context=None):
        try:
            if not self.connected or not self.zero or self.sheet is None:
                raise ValueError("Эскиз сохранён. Подключитесь и заново задайте оба угла перед движением.")
            from geometry import validate_paths
            validate_paths(self.paths, self.sheet)
            accepted = self.submit(action, paths=[list(path) for path in self.paths], feed=self.get_feed(),
                                   panel_request=panel_request, panel_context=panel_context)
            if accepted:
                self.progress["value"] = 0
            return accepted
        except ValueError as exc:
            messagebox.showerror("Проверьте ввод", str(exc), parent=self.root)
            return False

    def stop(self):
        try:
            import sys
            caller = sys._getframe(1).f_code.co_name
            reason = ("network_lease_lost" if caller == "update_network_lease" else
                      "screen_stop" if caller == "_dispatch" else "requested_stop")
            record_event(self, "stop_requested", caller=caller, reason=reason)
        except Exception:
            pass
        self.dwin_pen_state = 0
        self.preserve_draft()
        self.preview_revision += 1
        self.cancel_svg()
        self.controller.stop()
        self.zero, self.sheet = False, None
        self.position = None
        self.relay_state_unknown = True
        self.rebase_editor_history()
        self.paper_text.set(self.draft_notice())
        self.status.set("СТОП: эскиз сохранён. Для продолжения подключитесь и задайте два угла заново.")
        self.invalidate_editor()
        self.update_controls()
        self.redraw()

    def update_controls(self):
        busy = self.controller.busy or self.svg_loading
        available = self.connected and not busy and not self.controller.cancelled.is_set()
        ready = available and self.machine_ready()
        motion_ready = ready and self.pen_ready()
        drawing_ready = motion_ready and self.zero and self.sheet is not None
        power_raises = self.pen_power_raises.get()
        self.pen_effect.set("Поднимает" if power_raises else "Опускает")
        self.pen_summary.set(("Питание поднимает" if power_raises else "Питание опускает") +
                             (" · выход " + self.pen_active.get() if self.pen_active.get() != "Не проверено" else " · полярность не задана") +
                             (" · непрерывный ход" if self.pen_continuous.get() else ""))
        self.pen_legend.set(connection_notice(self.pen_enabled.get(), power_raises, self.pen_continuous.get()))
        if self.relay_state_unknown:
            self.pen_status.set("Состояние реле неизвестно: подтверждение отключения не получено. При необходимости выключите БП соленоида вручную.")
        elif not self.pen_enabled.get():
            self.pen_status.set("Режим без подъёма. Соленоидный БП отключите; переходы оставляют линии.")
        elif self.simulation and self.connected:
            self.pen_status.set("DEMO: виртуальный подъём, реле и USB не используются.")
        elif not self.connected:
            self.pen_status.set("Подключите Arduino. Настройки ручки сохраняются между запусками.")
        elif not busy and not self.pen_ready():
            self.pen_status.set("В настройках один раз подтвердите действие питания и полярность." if self.pen_test_ready() else
                                "Профиль реле или таймера не совпадает с прошивкой. Нужна прошивка выбранного режима.")
        elif not busy and self.pen_ready() and self.pen_status.get().startswith(('Профиль','В настройках','Подключите','Режим без подъёма','DEMO:')):
            self.pen_status.set("Подъём включается только на время переходов и теста. После остановки ручка внизу." if power_raises else
                                "Автоподъём готов. Тест кратковременный, оси не движутся.")
        self.connect_button.configure(text="Отключить" if self.connected else "Подключить")
        for widget, enabled in (
                (self.connect_button, not busy), (self.refresh_button, not busy and not self.connected),
                (self.configure_button, available), (self.first_button, motion_ready and not self.zero),
                (self.unlock_button, ready and self.effective_machine_profile() == DLC32 and not self.simulation),
                (self.hold_button, not busy and self.effective_machine_profile() == UNO),
                (self.second_button, motion_ready and self.zero and self.sheet is None),
                (self.clear_button, available and self.zero), (self.load_button, not busy and self.drawing_sheet() is not None),
                (self.test_button, not busy and self.drawing_sheet() is not None),
                (self.startpoint_button, drawing_ready and bool(self.paths)), (self.draw_button, drawing_ready and bool(self.paths)),
                (self.feed_input, not busy), (self.margin_input, not busy)):
            widget.configure(state="normal" if enabled else "disabled")
        for widget in self.jog_buttons:
            widget.configure(state="normal" if motion_ready else "disabled")
        for widget in self.pen_widgets:
            widget.configure(state="normal" if not busy else "disabled")
        self.pen_polarity.configure(state="readonly" if not busy else "disabled")
        self.pen_effect_combo.configure(state="readonly" if not busy else "disabled")
        for widget in (self.pen_up_button,self.pen_pulse_button):
            widget.configure(state="normal" if ready and self.pen_test_ready() else "disabled")
        for widget in self.axis_widgets:
            widget.configure(state="normal" if not busy and not self.zero and not getattr(self, "native_panel", False)
                             else "disabled")
        self.ports.configure(state=("normal" if self.effective_machine_profile() == DLC32 else "readonly")
                             if not busy and not self.connected else "disabled")
        self.profile_input.configure(state="readonly" if not busy and not self.connected else "disabled")
        self.configure_button.configure(text="Проверить конфигурацию FluidNC" if self.effective_machine_profile() == DLC32
                                        else "Настроить без концевиков")
        self.step_input.configure(state="normal" if not busy else "disabled")

    def request_redraw(self):
        if not self.closed and self.redraw_timer is None:
            self.redraw_timer = self.root.after(40, self.redraw)

    def redraw(self):
        if self.redraw_timer is not None:
            self.root.after_cancel(self.redraw_timer)
            self.redraw_timer = None
        canvas = self.canvas
        w, h = max(10, canvas.winfo_width()), max(10, canvas.winfo_height())
        sheet = self.drawing_sheet()
        key = (w, h, sheet, self.zero, self.sheet is not None, self.axes())
        if key == self.preview_key and self.preview_paths is self.paths:
            self.update_head()
            return
        self.preview_key = key
        self.preview_paths = self.paths
        self.preview_transform = None
        canvas.delete("all")
        if not sheet:
            title = "Верхний левый угол сохранён" if self.zero else "Задайте два угла листа"
            text = "Подведите головку вправо и вниз,\nзатем сохраните нижний правый угол." if self.zero else \
                "① Верхний левый — начало координат\n② Нижний правый — ширина и высота\n\nЛист положите параллельно осям станка."
            canvas.create_text(w / 2, h / 2 - 50, text=title, font=("Segoe UI", 17, "bold"), fill="#213651")
            canvas.create_text(w / 2, h / 2 + 10, text=text, font=("Segoe UI", 12), fill="#52647b", justify="center")
            return
        scale = min((w - 90) / sheet.width, (h - 80) / sheet.height)
        if scale <= 0:
            return
        left, top = (w - scale * sheet.width) / 2, (h - scale * sheet.height) / 2
        self.preview_transform = (left, top, scale)
        def xy(point):
            return left + point[0] * scale, top + point[1] * scale
        x2, y2 = xy((sheet.width, sheet.height))
        canvas.create_rectangle(left, top, x2, y2, fill="white", outline="#a8b6c8")
        grid = 10 if max(sheet.width, sheet.height) < 400 else 100
        for u in range(grid, int(sheet.width), grid):
            canvas.create_line(*xy((u, 0)), *xy((u, sheet.height)), fill="#edf1f5")
        for v in range(grid, int(sheet.height), grid):
            canvas.create_line(*xy((0, v)), *xy((sheet.width, v)), fill="#edf1f5")
        canvas.create_text(left, top - 16, text=("① 0,0 · верхний левый" if self.zero and self.sheet else "Эскиз сохранён · перед рисованием задайте два угла"), anchor="w", fill="#344b65")
        canvas.create_text(x2, y2 + 17, text=f"② {sheet.width:.1f} × {sheet.height:.1f} мм", anchor="e", fill="#344b65")
        previous = None
        for path in self.paths:
            if not path:
                continue
            if previous is not None and math.dist(previous, path[0]) > 0.0005:
                canvas.create_line(*xy(previous), *xy(path[0]), fill="#df790d", width=2, dash=(5, 3))
            # Keep every point, but avoid one Tcl/Canvas object per segment.
            # Blocks overlap at their endpoints to preserve the exact polyline.
            for offset in range(0, len(path) - 1, 512):
                points = path[offset:offset + 513]
                coordinates = [value for point in points for value in xy(point)]
                canvas.create_line(*coordinates, fill="#2563a6", width=2, tags="contour")
            previous = path[-1]
        if self.paths:
            sx, sy = xy(self.paths[0][0])
            canvas.create_oval(sx - 4, sy - 4, sx + 4, sy + 4, fill="#218657", outline="")
        canvas.create_line(0, 0, 0, 0, fill="#df790d", width=2, dash=(5, 3), tags="start_link")
        canvas.create_line(0, 0, 0, 0, fill="#ba2439", width=2, tags="head_horizontal")
        canvas.create_line(0, 0, 0, 0, fill="#ba2439", width=2, tags="head_vertical")
        self.update_head()

    def update_head(self):
        if self.preview_transform is None:
            return
        if self.position is None or self.sheet is None:
            for tag in ("head_horizontal", "head_vertical", "start_link"):
                self.canvas.itemconfigure(tag, state="hidden")
            return
        for tag in ("head_horizontal", "head_vertical"):
            self.canvas.itemconfigure(tag, state="normal")
        left, top, scale = self.preview_transform
        current = self.axes().paper(self.position) if self.position is not None else (0, 0)
        cx, cy = left + current[0] * scale, top + current[1] * scale
        self.canvas.coords("head_horizontal", cx - 7, cy, cx + 7, cy)
        self.canvas.coords("head_vertical", cx, cy - 7, cx, cy + 7)
        if self.paths and math.dist(current, self.paths[0][0]) > 0.0005:
            start = self.paths[0][0]
            self.canvas.coords("start_link", cx, cy, left + start[0] * scale, top + start[1] * scale)
            self.canvas.itemconfigure("start_link", state="normal")
        else:
            self.canvas.itemconfigure("start_link", state="hidden")

    def pump(self):
        if self.closed:
            return
        redraw = False
        latest_position = latest_progress = None
        for _ in range(500):
            try:
                name, value = self.controller.events.get_nowait()
            except queue.Empty:
                break
            if name == "board_runtime":
                record_event(self, "board_runtime", **value)
            elif name == "native_panel":
                self.native_panel = value
            elif name == "connected":
                record_event(self, "motion_connection_notice", kind=name, connected=True)
                self.connected = True
                self.port_name = value
                self.simulation = value == "DEMO"
                self.rebase_editor_history()
                self.status.set("DEMO · симуляция, USB не используется" if self.simulation else
                                f"{value} · {PROFILE_LABELS[self.effective_machine_profile()]} подключён")
                self.note("Подключено: " + value + ". Ноль нужно выставить заново.")
                if self.native_panel and self.network_controller():
                    self.open_dwin(show=False)
                elif self.native_panel:
                    self.status.set("MKS по USB · диагностика. Для управления из пульта подключитесь по Wi-Fi.")
            elif name == "disconnected":
                if self.panel_client() is not None:
                    self.dwin_window.close()
                elif self.dwin_window is not None:
                    self.dwin_window.armed.set(False)
                    self.dwin_window.arm()
                self.native_panel = False
                self.panel_calibration = None
                self.panel_request_button.pack_forget()
                record_event(self, "motion_connection_notice", kind=name, connected=False)
                self.dwin_pen_state = 0
                self.preserve_draft()
                self.preview_revision += 1
                self.cancel_svg()
                latest_position = latest_progress = None
                self.connected = self.zero = False
                self.last_motion_submit = None
                self.port_name = ""
                self.simulation = False
                self.settings = {}
                self.pen_firmware = None
                self.sheet = self.position = None
                self.relay_state_unknown = True
                self.rebase_editor_history()
                self.invalidate_editor()
                self.paper_text.set(self.draft_notice())
                self.coordinates.set("Положение: —")
                self.status.set("Не подключено · эскиз сохранён; для движения задайте оба угла заново")
                self.hint.set("Рисунок и история правок сохранены. Можно редактировать и сохранить проект; автоматического продолжения движения нет.")
                redraw = True
            elif name == "settings":
                self.settings = value
                if value.get("_board_revision") == "2.2" and not self.zero:
                    self.swap.set(False)
                    self.reverse_right.set(False)
                    self.reverse_down.set(False)
                    self.note("MKS V2.2: X — каретка, Y — портал. Концевики в текущем профиле выключены.")
                if 1 in value:
                    self.hold.set(value[1] == 255)
                self.note("Шаги/мм: X=" + str(value.get(100, "?")) + "; Y=" + str(value.get(101, "?"))
                          + "; 100% скорости=" + str(min(value.get(110, 0), value.get(111, 0))) + " мм/мин")
            elif name == "pen_firmware":
                self.pen_firmware = value
                if self.settings.get("_board_revision") == "2.2" and value:
                    self.pen_active.set("LOW" if value.get("active_low") else "HIGH")
                self.note("Прошивка ручки: "+str(value) if value else "Профиль выхода ручки не подтверждён; автоматический режим недоступен.")
            elif name == "pen":
                self.dwin_pen_state = {"up": 1, "down": 2}.get(value, 0)
                self.relay_state_unknown = value == "unknown"
                power_raises = self.pen_power_raises.get()
                text = {"up":"Команда подъёма: питание " + ("включено." if power_raises else "выключено."),
                        "down":"Команда опускания: питание " + ("выключено." if power_raises else "включено."),
                        "cooling":"Питание выключено: пауза охлаждения. " + ("Ручка внизу." if power_raises else "Ожидается подъём ручки."),
                        "unknown":"Состояние реле неизвестно: доставка сброса не подтверждена. При обрыве связи отключите силовое питание вручную."}.get(value,str(value))
                self.pen_status.set(text + " Положение датчиком не измеряется.")
            elif name == "position":
                if self.native_panel and self.panel_client():
                    continue
                state, self.position = value
                latest_position = value
                redraw = True
            elif name == "calibration":
                if self.native_panel:
                    continue
                self.preserve_draft()
                self.preview_revision += 1
                self.cancel_svg()
                self.zero, self.sheet = value
                self.rebase_editor_history()
                self.invalidate_editor()
                if self.sheet:
                    self.draft_sheet = self.sheet
                    self.paper_text.set(f"Лист: {self.sheet.width:.2f} × {self.sheet.height:.2f} мм")
                    if self.paths:
                        from geometry import validate_paths
                        try:
                            validate_paths(self.paths, self.sheet)
                        except ValueError:
                            self.hint.set("Эскиз сохранён без изменения масштаба, но не помещается в новую область. Измените его размер перед рисованием.")
                        else:
                            self.hint.set("Область задана. Сохранённый эскиз не изменён; проверьте размещение и запустите рисование отдельно.")
                    else:
                        self.hint.set("Область задана. Выберите тестовый квадрат или загрузите SVG.")
                else:
                    self.paper_text.set("Первая точка сохранена. Эскиз сохранён; задайте нижний правый угол." if self.zero
                                        else self.draft_notice())
                redraw = True
            elif name == "progress":
                latest_progress = value
            elif name == "finished":
                latest_progress = None
                self.progress["value"] = 100
                text = "Головка у начала. Установите ручку, не сдвигая оси, и нажмите «Начать рисование»." if value == "startpoint" else \
                    "Рисование завершено: контроллер подтвердил остановку. Автоматического возврата нет."
                if value == "scan":
                    text = "Сканирующий проход завершён. Кадры находятся в окне камеры; головка остаётся на последней точке."
                if value in ("pen_up","pen_pulse"):
                    text = ("Краткий тест завершён: питание выключено, ручка возвращается вниз." if self.pen_power_raises.get() else
                            "Краткий тест завершён: питание выключено. Проверьте, что ручка вернулась вверх.")
                    self.pen_status.set(text)
                elif self.pen_enabled.get():
                    text += (" Питание соленоида отключено, ручка остаётся внизу." if self.pen_power_raises.get() else
                             " Питание соленоида отключено; ожидается подъём ручки.")
                self.hint.set(text)
                self.note(text)
            elif name == "link_wait":
                if not self.connected:
                    self.native_panel = False
                    self.settings = {}
                    self.pen_firmware = None
                    self.status.set("Ожидание связи с MKS")
                detail = value.get("message", str(value)) if isinstance(value, dict) else str(value)
                self.note("Связь с MKS: " + detail)
            elif name == "error":
                if isinstance(value, dict):
                    text = str(value.get("message", value.get("error", "Операция прервана")))
                    log_path = value.get("diagnostic_log", value.get("log_path"))
                    if log_path:
                        text += "\n\nДиагностический журнал: " + str(log_path)
                else:
                    text = str(value)
                if "ClearCommError" in text or "PermissionError(13" in text:
                    text = ("Windows прервала доступ к USB-порту. Рисунок сохранён; перед новым запуском восстановите связь и задайте оба угла.\n\n"
                            "Технические подробности: " + text)
                self.note("ОШИБКА: " + text)
                messagebox.showerror("Операция остановлена", text, parent=self.root)
            elif name == "log":
                self.note(value)
        if latest_position is not None and self.position is not None:
            state, position = latest_position
            u, v = self.axes().paper(position)
            self.coordinates.set(f"Вправо: {u:.2f} · вниз: {v:.2f} мм · {state}")
        if latest_progress is not None:
            sent, total = latest_progress
            self.progress["value"] = 100 * sent / max(1, total)
            self.hint.set(f"По данным контроллера выполнено {sent:.1f}/{total:.1f} мм штрихов." if isinstance(total,float) else
                          f"В контроллер передано {sent}/{total} участков. Ожидаю завершения движения…")
        while not self.svg_events.empty():
            token, filename, margin, result = self.svg_events.get_nowait()
            if token is not self.svg_token:
                continue
            self.cancel_svg()
            if isinstance(result, Exception):
                self.hint.set("Не удалось загрузить рисунок. Выберите другой SVG.")
                messagebox.showerror("SVG не загружен", str(result), parent=self.root)
            else:
                self.paths = result
                self.file_name = Path(filename).name + f" · {len(result)} контур(ов) · поле {margin:g} мм"
                if getattr(result, "actual_size", False):
                    self.file_name += " · размер 1:1, без растягивания"
                self.loaded()
                redraw = False
        if redraw:
            self.redraw()
        self.update_network_lease()
        self.update_controls()
        if self.dwin_window is not None:
            self.dwin_window.pump()
        self.pump_timer = self.root.after(60, self.pump)

    def close(self):
        if self.closed:
            return
        if self.controller.busy and not messagebox.askokcancel("Закрыть программу?",
                "Операция выполняется. Будет отправлен сброс; ноль потеряется. Закрыть?", parent=self.root):
            return
        self.closed = True
        # Auxiliary windows and autosaves can wait for workers. Stop motion
        # before any of those waits, as soon as closing is accepted.
        self.controller.shutdown()
        if self.dwin_window is not None:
            self.dwin_window.close()
        for name in ('pump_timer', 'redraw_timer'):
            identifier = getattr(self, name)
            if identifier is not None:
                self.root.after_cancel(identifier)
                setattr(self, name, None)
        self.cancel_svg()
        if self.editor_window is not None:
            self.editor_window.close()
        if self.assistant_window is not None:
            self.assistant_window.close()
        if self.camera_window is not None:
            self.camera_window.close()
        if self.pen_observer is not None:
            self.pen_observer.stop.set()
        if self.autosaves:
            self.autosaves.close()
        self.root.destroy()


if __name__ == "__main__":
    App(tk.Tk(),data_dir=Path(__file__).parent/'data').root.mainloop()
