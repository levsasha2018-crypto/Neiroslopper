"""Camera workspace. Analysis is read-only; a scan route needs explicit approval."""
import json
import math
from pathlib import Path
import queue
import threading
import time
import tkinter as tk
from tkinter import ttk, filedialog, messagebox

import cv2
import numpy as np
from PIL import Image, ImageTk

from camera_client import CameraClient, capture, camera_control, read_image, endpoint
from camera_vision import (PlanarCalibration, calibrate_plane, calibrate_lens, detect_paper,
    paper_metrics, rectify_document, clean_document, frame_quality, save_scan, scan_plan, assemble_mosaic)
from editor_project import atomic_json


def scan_pen_notice(context):
    """One scan confirmation must not promise a powered pen stays up in a wait."""
    if context.get('pen_lift_enabled') and context.get('pen_power_raises') is True:
        return ("СНИМИТЕ РУЧКУ ИЛИ ЗАКРЕПИТЕ ЕЁ ПОДНЯТОЙ МЕХАНИЧЕСКИ. "
                "Питание поднимает её только на коротких переходах. Во время остановок "
                "на охлаждение и съёмку питание выключается, установленная ручка "
                "опускается и может оставлять точки на документе. ")
    if (context.get('pen_lift_ready') and context.get('pen_lift_enabled') and
            context.get('pen_power_raises') is False):
        return "Пульт отключит соленоид; ручку должен поднимать проверенный возвратный механизм. "
    return "СНИМИТЕ ИЛИ ПОДНИМИТЕ РУЧКУ ВРУЧНУЮ. "


class CameraWindow:
    def __init__(self, parent, get_context, start_scan=None, stop_motion=None):
        self.root = tk.Toplevel(parent)
        self.root.title("Камера плоттера — видео и сканирование")
        self.root.geometry("1160x880")
        self.root.minsize(950, 740)
        self.get_context = get_context
        self.start_motion_scan = start_scan
        self.stop_motion = stop_motion
        self.scan_active = False
        self.scan_token = None
        self.scan_events = queue.Queue()
        self.scan_count = 0
        self.closed = False
        self.client = None
        self.events = queue.Queue()
        self.token = None
        self.token_camera = False
        self.on_done = None
        self.busy = False
        self.timer = None
        self.photo = None
        self.raw_image = self.image = self.document = self.original_document = None
        self.document_dimensions = None
        self.image_position = None
        self.image_revision = None
        self.calibration = None
        self.document_calibration = None
        self.offline_area = None
        self.lens = None
        self.corners = []
        self.clicking = False
        self.frozen = True
        self.last_sequence = 0
        self.display_transform = None
        self.tiles = []
        self.tile_pixel_count = 0
        self.tile_revision = None
        self.tile_area = None  # Captured data survives a STOP; never a restored machine zero.
        self.incomplete_mosaic = False
        self.metric_report = None
        self.white_reference = None
        self.use_white_reference = tk.BooleanVar(value=False)
        self.stream_url = tk.StringVar(value="http://192.168.1.50:81/stream")
        self.capture_url = tk.StringVar(value="http://192.168.1.50/capture")
        self.telemetry_url = tk.StringVar(value="")
        self.width_mm = tk.StringVar(value="210")
        self.height_mm = tk.StringVar(value="297")
        self.ref_width = tk.StringVar(value="50")
        self.ref_height = tk.StringVar(value="50")
        self.offset_x = tk.StringVar(value="0")
        self.offset_y = tk.StringVar(value="0")
        self.dpi = tk.StringVar(value="300")
        self.overlap = tk.StringVar(value="25")
        self.mode = tk.StringVar(value="Цветной")
        self.strength = tk.DoubleVar(value=.7)
        self.status = tk.StringVar(value="Камера не подключена. Можно открыть изображение и проверить обработку.")
        self.telemetry = tk.StringVar(value="Телеметрия не подключена; её данные не управляют станком.")
        self.calibration_status = tk.StringVar(value="Калибровки нет: размеры в мм автоматически не определяются.")
        self.tile_status = tk.StringVar(value="Снимков прохода: 0. Автоматическое движение не включено.")
        self.build()
        self.root.protocol("WM_DELETE_WINDOW", self.close)
        self.timer = self.root.after(80, self.pump)

    def build(self):
        outer = ttk.Frame(self.root, padding=12)
        outer.pack(fill="both", expand=True)
        ttk.Label(outer, text="Видео, положение листа и скан", font=("Segoe UI", 17, "bold")).pack(anchor="w")
        ttk.Label(outer, text="Анализ фото не меняет ноль и не двигает станок. Автоматический проход "
                  "запускается отдельно с подтверждением; ручка должна быть поднята или снята.", wraplength=1100).pack(anchor="w", pady=(4, 8))
        connection = ttk.LabelFrame(outer, text="Камера ESP · подключение только по кнопке", padding=8)
        connection.pack(fill="x")
        for index, (label, variable) in enumerate((("Видео", self.stream_url), ("Фото", self.capture_url),
                                                  ("WebSocket (необязательно)", self.telemetry_url))):
            ttk.Label(connection, text=label).grid(row=index, column=0, sticky="w")
            ttk.Entry(connection, textvariable=variable, width=65).grid(row=index, column=1, sticky="ew", padx=6)
        connection.columnconfigure(1, weight=1)
        ttk.Button(connection, text="Подключить", command=self.connect).grid(row=0, column=2, padx=4)
        ttk.Button(connection, text="Отключить", command=self.disconnect).grid(row=1, column=2, padx=4)
        ttk.Button(connection, text="Видео 720p", command=lambda: self.set_camera(framesize=11)).grid(row=0, column=3, padx=4)
        ttk.Button(connection, text="Зафиксировать свет", command=lambda: self.set_camera(awb=0, aec=0, agc=0)).grid(row=1, column=3, padx=4)
        ttk.Button(connection, text="Автоэкспозиция / ББ", command=lambda: self.set_camera(awb=1, aec=1, agc=1)).grid(row=2, column=3, padx=4)
        ttk.Label(outer, textvariable=self.telemetry, wraplength=1100, font=("Segoe UI", 9)).pack(anchor="w", pady=4)
        body = ttk.Frame(outer)
        body.pack(fill="both", expand=True)
        tabs = ttk.Notebook(body, width=340)
        tabs.pack(side="left", fill="y", padx=(0, 10))
        scan = self.scroll_tab(tabs, "Документ")
        calibration = self.scroll_tab(tabs, "Калибровка")
        tiles = self.scroll_tab(tabs, "Проходы")
        for label, command in (("Открыть фото…", self.open_image), ("Зафиксировать свежий кадр", self.freeze),
                               ("Снимок по сети", self.snapshot), ("Вернуться к видео", self.resume),
                               ("Найти четыре угла листа", self.find_paper), ("Отметить 4 угла вручную", self.select_corners)):
            ttk.Button(scan, text=label, command=command).pack(fill="x", pady=2)
        ttk.Label(scan, text="Размер документа, мм · укажите вручную\nили получите из калиброванного изображения.",
                  wraplength=320).pack(anchor="w", pady=(8, 2))
        self.fields(scan, (("Ширина", self.width_mm), ("Высота", self.height_mm), ("DPI экспорта", self.dpi)))
        ttk.Label(scan, text="Режим очистки").pack(anchor="w", pady=(8, 2))
        ttk.Combobox(scan, textvariable=self.mode, values=("Оригинал", "Цветной", "Серый", "Ч/б текст"),
                     state="readonly", width=20).pack(fill="x")
        ttk.Label(scan, text="Сила очистки · 0 — без выравнивания фона").pack(anchor="w", pady=(6, 0))
        ttk.Scale(scan, variable=self.strength, from_=0, to=1).pack(fill="x")
        ttk.Button(scan, text="Открыть белый эталон…", command=self.open_white_reference).pack(fill="x", pady=2)
        ttk.Checkbutton(scan, text="Выравнивать по белому эталону", variable=self.use_white_reference).pack(anchor="w")
        ttk.Button(scan, text="Собрать / очистить документ", command=self.process_document).pack(fill="x", pady=6)
        ttk.Button(scan, text="Сохранить PNG / PDF…", command=self.export).pack(fill="x")
        ttk.Button(scan, text="Показать исходный скан", command=self.show_original).pack(fill="x", pady=4)
        ttk.Label(scan, text="Исходный скан сохраняется рядом с результатом.\nDPI не добавляет отсутствующие детали. "
                  "Очистка может ослаблять карандаш; проверьте оригинал.", wraplength=320).pack(anchor="w", pady=8)
        ttk.Label(calibration, textvariable=self.calibration_status, wraplength=320).pack(anchor="w", pady=4)
        ttk.Label(calibration, text="Эталонный прямоугольник должен лежать\nпараллельно направлениям вправо/вниз. "
                  "Отметьте его 4 угла на неподвижном кадре.", wraplength=320).pack(anchor="w", pady=6)
        self.fields(calibration, (("Ширина эталона, мм", self.ref_width), ("Высота эталона, мм", self.ref_height),
                                 ("Центр эталона от пера X, мм", self.offset_x), ("Центр эталона от пера Y, мм", self.offset_y)))
        ttk.Button(calibration, text="Отметить углы эталона", command=self.select_corners).pack(fill="x", pady=4)
        ttk.Button(calibration, text="Рассчитать плоскость / мм", command=self.calibrate).pack(fill="x", pady=4)
        ttk.Button(calibration, text="Калибровать объектив по фото…", command=self.calibrate_optics).pack(fill="x", pady=4)
        ttk.Label(calibration, text="Для объектива: ≥8 разных снимков шахматной доски 9×6 внутренних углов, "
                  "клетка 10 мм, одно разрешение. Затем заново калибруйте плоскость на исправленном кадре.",
                  wraplength=320).pack(anchor="w", pady=6)
        ttk.Button(calibration, text="Сохранить калибровку…", command=self.save_calibration).pack(fill="x", pady=4)
        ttk.Button(calibration, text="Открыть калибровку…", command=self.open_calibration).pack(fill="x", pady=4)
        ttk.Button(calibration, text="Сохранить углы / размеры JSON…", command=self.export_metrics).pack(fill="x", pady=4)
        ttk.Label(tiles, text="План рассчитан только внутри уже заданной области пульта. "
                  "Можно снимать вручную на остановках или отдельно подтвердить автоматический проход.", wraplength=320).pack(anchor="w", pady=6)
        self.fields(tiles, (("Перекрытие, %", self.overlap),))
        ttk.Button(tiles, text="Показать / сохранить план…", command=self.export_plan).pack(fill="x", pady=4)
        ttk.Button(tiles, text="Автоматический проход…", command=self.start_scan).pack(fill="x", pady=4)
        ttk.Button(tiles, text="СТОП прохода / ESC", command=self.stop_scan).pack(fill="x", pady=4)
        ttk.Button(tiles, text="Добавить свежий кадр на месте", command=self.add_tile).pack(fill="x", pady=4)
        ttk.Button(tiles, text="Убрать последний кадр", command=self.remove_tile).pack(fill="x", pady=4)
        ttk.Button(tiles, text="Склеить по координатам", command=self.mosaic).pack(fill="x", pady=4)
        ttk.Button(tiles, text="Сохранить исходные кадры…", command=self.save_tiles).pack(fill="x", pady=4)
        ttk.Button(tiles, text="Открыть сохранённый проход…", command=self.open_tiles).pack(fill="x", pady=4)
        ttk.Button(tiles, text="Очистить проход", command=self.clear_tiles_confirmed).pack(fill="x", pady=4)
        ttk.Label(tiles, textvariable=self.tile_status, wraplength=320).pack(anchor="w", pady=8)
        ttk.Label(tiles, text="Нужны калибровка, неизменный ноль и остановленная каретка. "
                  "Пропуски покрытия показываются пурпурным и блокируют экспорт полного скана.", wraplength=320).pack(anchor="w", pady=8)
        right = ttk.Frame(body)
        right.pack(side="left", fill="both", expand=True)
        self.canvas = tk.Canvas(right, background="#263445", highlightthickness=0)
        self.canvas.pack(fill="both", expand=True)
        self.canvas.bind("<Configure>", lambda _: self.render())
        self.canvas.bind("<Button-1>", self.click)
        self.root.bind("<Escape>", lambda _: self.stop_scan())
        ttk.Label(outer, textvariable=self.status, wraplength=1100, foreground="#225ea8").pack(anchor="w", pady=(8, 0))

    def fields(self, parent, fields):
        for label, variable in fields:
            row = ttk.Frame(parent)
            row.pack(fill="x", pady=2)
            ttk.Label(row, text=label).pack(side="left")
            ttk.Entry(row, textvariable=variable, width=9).pack(side="right")

    def scroll_tab(self, notebook, title):
        wrapper = ttk.Frame(notebook)
        notebook.add(wrapper, text=title)
        scrollbar = ttk.Scrollbar(wrapper, orient="vertical")
        scrollbar.pack(side="right", fill="y")
        canvas = tk.Canvas(wrapper, width=320, highlightthickness=0, yscrollcommand=scrollbar.set, background="#eef2f6")
        canvas.pack(side="left", fill="both", expand=True)
        scrollbar.configure(command=canvas.yview)
        content = ttk.Frame(canvas, padding=8)
        item = canvas.create_window(0, 0, window=content, anchor="nw")
        content.bind("<Configure>", lambda _: canvas.configure(scrollregion=canvas.bbox("all")))
        canvas.bind("<Configure>", lambda event: canvas.itemconfigure(item, width=event.width))
        def scroll(event):
            widget = event.widget
            while widget is not None:
                if widget is wrapper:
                    canvas.yview_scroll(-int(event.delta / 120), "units")
                    return "break"
                widget = getattr(widget, "master", None)
        self.root.bind("<MouseWheel>", scroll, add="+")
        return content

    def number(self, variable, lower, upper):
        try:
            value = float(variable.get().replace(",", "."))
        except (ValueError, AttributeError):
            raise ValueError("Введите числовые значения.") from None
        if not math.isfinite(value) or not lower <= value <= upper:
            raise ValueError(f"Число должно быть от {lower:g} до {upper:g}.")
        return value

    def work(self, function, done, *, camera=False):
        if self.closed:
            return
        if self.busy:
            self.status.set("Дождитесь завершения текущей обработки.")
            return
        token = self.token = object()
        self.on_done = done
        self.token_camera = camera
        events = self.events
        self.busy = True
        self.status.set("Обработка… интерфейс и СТОП в пульте остаются доступны.")
        def worker():
            try:
                events.put((token, function(), None))
            except Exception as exc:
                events.put((token, None, str(exc)))
        threading.Thread(target=worker, daemon=True).start()

    def fail(self, exc):
        self.status.set(str(exc))

    def connect(self):
        if self.busy:
            return
        try:
            endpoint(self.stream_url.get())
            if not self.replace_tiles_allowed():
                return
            client = CameraClient()
            client.start(self.stream_url.get(), self.telemetry_url.get().strip())
            self.disconnect()
            self.clear_tiles()
            self.last_sequence = 0
            self.client = client
            self.resume()
        except ValueError as exc:
            self.fail(exc)

    def disconnect(self):
        self.stop_scan()
        if self.token_camera:
            self.token = None
            self.on_done = None
            self.token_camera = False
            self.busy = False
        if self.client is not None:
            self.client.close()
            self.client = None
        self.status.set("Камера отключена. Открытие окна не подключает её автоматически.")
        self.telemetry.set("Телеметрия отключена; старые данные не используются.")

    def set_camera(self, **settings):
        if self.busy:
            return
        try:
            url = endpoint(self.capture_url.get())
            if not self.replace_tiles_allowed():
                return
            self.clear_tiles()
            self.work(lambda: camera_control(url, **settings),
                      lambda _: self.status.set("Настройки камеры изменены. Проверьте яркость; кадры старого прохода очищены."), camera=True)
        except ValueError as exc:
            self.fail(exc)

    def display_frame(self, raw, context=None):
        self.raw_image = raw.copy()
        if self.calibration is not None and (raw.shape[1], raw.shape[0]) == self.calibration.resolution:
            self.image = self.calibration.undistort(raw)
        elif self.lens is not None:
            camera, distortion, size = self.lens
            self.image = cv2.undistort(raw, camera, distortion) if (raw.shape[1], raw.shape[0]) == size else raw.copy()
        else:
            self.image = raw.copy()
        self.document = self.original_document = None
        self.document_dimensions = None
        self.metric_report = None
        self.incomplete_mosaic = False
        self.document_calibration = None
        self.corners = []
        self.image_position = context.get("position") if context is not None else None
        self.image_revision = context.get("revision") if context is not None else None
        self.render()

    def open_image(self):
        if self.busy:
            return
        path = filedialog.askopenfilename(parent=self.root, filetypes=[("Изображения", "*.png *.jpg *.jpeg")])
        if not path:
            return
        self.frozen = True
        self.work(lambda: read_image(path), lambda result: self.display_frame(result))

    def open_white_reference(self):
        if self.busy:
            return
        path = filedialog.askopenfilename(parent=self.root, title="Белый эталон той же геометрии, размера и освещения",
            filetypes=[("Изображения", "*.png *.jpg *.jpeg")])
        if not path:
            return
        def complete(image):
            self.white_reference = image
            self.use_white_reference.set(True)
            self.status.set("Белый эталон загружен. Он должен быть снят в тех же условиях и иметь размер исходного скана.")
        self.work(lambda: read_image(path), complete)

    def freeze(self):
        if self.busy:
            return
        try:
            if self.client is None:
                raise ValueError("Подключите камеру или откройте фото.")
            context = self.get_context()
            if context.get("busy"):
                raise ValueError("Дождитесь остановки каретки.")
            frame = self.client.get_frame()
            self.frozen = True
            self.display_frame(frame.image, context)
            quality = frame_quality(frame.image)
            self.status.set(f"Кадр {frame.sequence}: {frame.image.shape[1]}×{frame.image.shape[0]}; "
                            f"резкость {quality['sharpness']:.0f} (оценка, не гарантия).")
        except ValueError as exc:
            self.fail(exc)

    def snapshot(self):
        if self.busy:
            return
        try:
            context, url = self.get_context(), endpoint(self.scan_capture_url())
        except ValueError as exc:
            self.fail(exc)
            return
        if context.get("busy"):
            self.status.set("Дождитесь остановки каретки.")
            return
        self.frozen = True
        def complete(image):
            current = self.get_context()
            if current.get("busy") or current.get("revision") != context.get("revision") or current.get("position") != context.get("position"):
                raise ValueError("Положение изменилось во время съёмки. Кадр не привязан к координатам; повторите.")
            self.display_frame(image, context)
            self.status.set(f"Снимок получен: {image.shape[1]}×{image.shape[0]}. Проверяйте реальное разрешение, а не надпись 5 Мп.")
        self.work(lambda: capture(url), complete, camera=True)

    def scan_capture_url(self):
        from urllib.parse import urlsplit, urlunsplit, parse_qsl, urlencode
        parts = urlsplit(endpoint(self.capture_url.get()))
        query = dict(parse_qsl(parts.query))
        query["scan"] = "1"
        return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(query), ""))

    def resume(self):
        if self.busy:
            return
        self.frozen = False
        self.corners = []
        self.clicking = False
        self.status.set("Живое видео. При потере связи кадр помечается устаревшим.")

    def select_corners(self):
        if self.busy:
            return
        if not self.frozen:
            self.freeze()
            if not self.frozen:
                return
        if self.image is None:
            self.status.set("Сначала откройте или зафиксируйте изображение.")
            return
        self.clicking = True
        self.corners = []
        self.metric_report = None
        self.status.set("Отметьте четыре угла по порядку: верхний левый, верхний правый, нижний правый, нижний левый.")
        self.render()

    def click(self, event):
        if not self.clicking or self.display_transform is None or self.image is None or self.busy:
            return
        ox, oy, scale = self.display_transform
        point = ((event.x - ox) / scale, (event.y - oy) / scale)
        if 0 <= point[0] < self.image.shape[1] and 0 <= point[1] < self.image.shape[0]:
            self.corners.append(point)
            self.render()
            if len(self.corners) == 4:
                self.clicking = False
                self.status.set("Четыре угла отмечены. Выберите обработку документа или калибровку эталона.")

    def find_paper(self):
        if self.busy:
            return
        if not self.frozen:
            self.freeze()
            if not self.frozen:
                return
        if self.image is None:
            return
        image = self.image.copy()
        calibration = self.document_calibration or self.calibration
        def complete(corners):
            self.corners = corners.tolist()
            self.render()
            self.status.set("Найдены предполагаемые углы. Проверьте рамку; ноль станка не изменён.")
            if calibration is not None:
                calibration.check(image)
                self.metric_report = paper_metrics(corners, calibration, self.image_position or (0, 0))
                self.width_mm.set(f"{self.metric_report['width_mm']:.3f}")
                self.height_mm.set(f"{self.metric_report['height_mm']:.3f}")
                coordinate_note = "координаты от пера" if self.image_position is None else "координаты пульта"
                self.status.set(f"Лист ≈ {self.metric_report['width_mm']:.2f} × {self.metric_report['height_mm']:.2f} мм; "
                    f"угол {self.metric_report['rotation_deg']:.2f}°; {coordinate_note}. Это не новая калибровка станка.")
        self.work(lambda: detect_paper(image), complete)

    def calibrate(self):
        if self.busy:
            return
        try:
            if self.image is None or len(self.corners) != 4:
                raise ValueError("Отметьте четыре угла эталона.")
            dims = self.number(self.ref_width, 1, 3000), self.number(self.ref_height, 1, 3000)
            offset = self.number(self.offset_x, -3000, 3000), self.number(self.offset_y, -3000, 3000)
            lens = self.lens[:2] if self.lens is not None else (
                (self.calibration.camera_matrix, self.calibration.distortion) if self.calibration is not None else None)
            calibration = calibrate_plane(self.image, self.corners, dims, offset, lens)
            if not self.replace_tiles_allowed():
                return
            self.calibration = calibration
            self.metric_report = None
            self.document_calibration = None
            self.clear_tiles(force=True)
            self.calibration_status.set(f"Калибровка: {self.calibration.resolution}; объектив " +
                ("исправлен" if self.calibration.camera_matrix is not None else "не калиброван"))
            self.status.set("Плоскость калибрована. При изменении высоты, фокуса или крепления калибруйте заново.")
        except ValueError as exc:
            self.fail(exc)

    def calibrate_optics(self):
        if self.busy:
            return
        paths = filedialog.askopenfilenames(parent=self.root, title="≥8 фото шахматной доски 9×6 внутренних углов",
                                           filetypes=[("Изображения", "*.png *.jpg *.jpeg")])
        if not paths:
            return
        if len(paths) > 40:
            self.status.set("Выберите не более 40 снимков.")
            return
        if not self.replace_tiles_allowed():
            return
        def calculation():
            images, pixels = [], 0
            for path in paths:
                image = read_image(path)
                pixels += image.shape[0] * image.shape[1]
                if pixels > 100_000_000:
                    raise ValueError("Калибровочные снимки слишком большие; уменьшите их число.")
                images.append(image)
            return calibrate_lens(images)
        def complete(result):
            camera, distortion, error, count, size = result
            self.lens = camera, distortion, size
            self.calibration = None
            self.clear_tiles(force=True)
            self.calibration_status.set(f"Объектив: {count} снимков, ошибка {error:.3f} px; заново калибруйте плоскость.")
            if self.raw_image is not None:
                self.display_frame(self.raw_image)
        self.frozen = True
        self.work(calculation, complete)

    def save_calibration(self):
        if self.calibration is None or self.busy:
            return
        path = filedialog.asksaveasfilename(parent=self.root, defaultextension=".camera.json", filetypes=[("Калибровка", "*.camera.json")])
        if path:
            try:
                self.calibration.save(path)
                self.status.set("Калибровка сохранена. Она не сохраняет физический ноль станка.")
            except (OSError, ValueError) as exc:
                self.fail(exc)

    def open_calibration(self):
        if self.busy:
            return
        path = filedialog.askopenfilename(parent=self.root, filetypes=[("Калибровка", "*.camera.json")])
        if not path:
            return
        try:
            calibration = PlanarCalibration.load(path)
            if self.raw_image is not None:
                calibration.check(self.raw_image)
            if not self.replace_tiles_allowed():
                return
            self.calibration = calibration
            self.lens = None
            self.clear_tiles()
            self.calibration_status.set(f"Калибровка загружена: {calibration.resolution}. Проверьте высоту и крепление.")
            if self.raw_image is not None:
                self.display_frame(self.raw_image)
        except (OSError, ValueError, TypeError, KeyError) as exc:
            self.fail(exc)

    def process_document(self):
        if self.busy:
            return
        try:
            if self.image is None:
                raise ValueError("Нет изображения.")
            if self.incomplete_mosaic:
                raise ValueError("Неполное покрытие мозаики. Добавьте кадры и склейте заново.")
            if not self.frozen:
                self.freeze()
                if not self.frozen:
                    return
            dimensions = self.number(self.width_mm, 1, 3000), self.number(self.height_mm, 1, 3000)
            dpi = self.number(self.dpi, 50, 600)
            image, corners = self.image.copy(), list(self.corners)
            mode = {"Оригинал": "original", "Цветной": "color", "Серый": "gray", "Ч/б текст": "bw"}[self.mode.get()]
            strength = self.strength.get()
            white = self.white_reference.copy() if self.use_white_reference.get() and self.white_reference is not None else None
            document_ready = self.original_document.copy() if self.original_document is not None else None
            if len(corners) == 4:
                document_ready = None
            if document_ready is not None and dimensions != self.document_dimensions:
                raise ValueError("Размер подготовленного документа изменён. Вернитесь к исходному фото и подготовьте документ заново.")
            def calculation():
                original = document_ready if document_ready is not None else rectify_document(image, corners, dimensions, dpi)
                return original, clean_document(original, mode, strength, white), dimensions
            def complete(result):
                self.original_document, self.document, self.document_dimensions = result
                self.image = self.document
                self.image_position = self.image_revision = None
                self.metric_report = None
                height, width = self.image.shape[:2]
                self.document_calibration = PlanarCalibration((width, height),
                    np.array([[dimensions[0] / (width - 1), 0, 0],
                              [0, dimensions[1] / (height - 1), 0], [0, 0, 1]]), dimensions)
                self.corners = []
                self.render()
                self.status.set("Документ подготовлен. Сравните с исходным сканом; станок не двигался.")
            self.work(calculation, complete)
        except (ValueError, KeyError) as exc:
            self.fail(exc)

    def show_original(self):
        if self.original_document is not None and not self.busy:
            self.image = self.original_document
            self.render()
            self.status.set("Показан исходный скан до очистки. Экспорт по-прежнему сохраняет выбранную обработку.")

    def export(self):
        if self.document is None or self.busy:
            self.status.set("Сначала подготовьте документ.")
            return
        path = filedialog.asksaveasfilename(parent=self.root, title="Сохранить результат и исходный скан",
            defaultextension=".png", filetypes=[("PNG", "*.png"), ("PDF", "*.pdf")])
        if not path:
            return
        destination = Path(path)
        siblings = [destination, destination.with_name(destination.stem + ".original.png"),
                    destination.with_name(destination.stem + ".scan.json")]
        if any(p.exists() for p in siblings) and not messagebox.askokcancel("Перезаписать файлы?",
                "Будут перезаписаны результат, исходный скан и описание с таким именем.", parent=self.root):
            return
        image, original, dims = self.document.copy(), self.original_document.copy(), self.document_dimensions
        try:
            dpi = self.number(self.dpi, 50, 600)
        except ValueError as exc:
            self.fail(exc)
            return
        self.work(lambda: save_scan(path, image, original, dims, dpi),
                  lambda _: self.status.set("Сохранены результат, исходный скан и JSON-описание."))

    def export_metrics(self):
        if self.metric_report is None:
            self.status.set("Сначала найдите лист с калибровкой.")
            return
        path = filedialog.asksaveasfilename(parent=self.root, defaultextension=".json", filetypes=[("Углы / размеры", "*.json")])
        if path:
            try:
                atomic_json(path, self.metric_report)
            except (ValueError, OSError) as exc:
                self.fail(exc)

    def area(self):
        context = self.get_context()
        sheet = context.get("sheet")
        if sheet is None or context.get("busy") or not context.get("connected"):
            raise ValueError("Задайте оба угла в основном пульте и дождитесь остановки. Можно использовать DEMO.")
        if self.calibration is None:
            raise ValueError("Сначала калибруйте камеру.")
        return context, (sheet.width, sheet.height)

    def export_plan(self):
        try:
            context, area = self.area()
            if self.offline_area is not None:
                raise ValueError("Открыт сохранённый проход. Очистите его перед съёмкой нового.")
            positions = scan_plan(area, self.calibration, self.number(self.overlap, 10, 50) / 100)
            self.status.set(f"План: {len(positions)} кадров внутри области {area[0]:g}×{area[1]:g} мм. Движение не запускалось.")
            path = filedialog.asksaveasfilename(parent=self.root, title="План координат · не G-code", defaultextension=".json")
            if path:
                atomic_json(path, {"format": "plotter-scan-plan", "version": 1, "area_mm": list(area),
                    "pen_positions_mm": positions, "note": "Не исполнительная программа. Без концевиков нужен проверенный ручной ноль."})
        except (OSError, ValueError) as exc:
            self.fail(exc)

    def add_tile(self):
        if self.busy:
            return
        try:
            context, area = self.area()
            if self.offline_area is not None:
                raise ValueError("Открыт сохранённый проход. Очистите его перед съёмкой нового.")
            if context.get("position") is None:
                raise ValueError("Нет позиции каретки.")
            if not context['sheet'].contains(context['position']):
                raise ValueError("Каретка вне заданной области. Повторите установку углов.")
            if self.client is None:
                raise ValueError("Для прохода нужна подключённая камера; фото из файла не привязывается к позиции автоматически.")
            if self.tiles and self.tile_revision != context["revision"]:
                raise ValueError("Ноль, область или эскиз изменились. Очистите проход и начните заново.")
            # A direct capture after stopping avoids reusing a buffered preview frame.
            url = endpoint(self.scan_capture_url())
            def complete(image):
                current = self.get_context()
                if current.get("busy") or current.get("revision") != context["revision"] or current.get("position") != context["position"]:
                    raise ValueError("Каретка или ноль изменились во время съёмки. Кадр прохода отклонён.")
                self.calibration.check(image)
                pixels = image.shape[0] * image.shape[1]
                if len(self.tiles) >= 200 or self.tile_pixel_count + pixels > 80_000_000:
                    raise ValueError("Лимит прохода: 200 кадров / 80 Мп в памяти. Сохраните исходники.")
                self.tiles.append((image, tuple(context["position"])))
                self.tile_pixel_count += pixels
                self.tile_revision = context["revision"]
                self.tile_area = area
                self.tile_status.set(f"Кадров: {len(self.tiles)}; X={context['position'][0]:.2f}, Y={context['position'][1]:.2f} мм.")
                self.status.set("Снимок добавлен по программной позиции. Проверьте остановку и отсутствие пропусков шагов.")
            self.work(lambda: capture(url), complete, camera=True)
        except ValueError as exc:
            self.fail(exc)

    def start_scan(self):
        if self.busy:
            return
        try:
            context, area = self.area()
            client, calibration = self.client, self.calibration
            if self.start_motion_scan is None or client is None:
                raise ValueError("Для автоматического прохода нужны подключённые камера и пульт.")
            client.get_frame()
            if not context['sheet'].contains(context.get('position') or (math.inf, math.inf)):
                raise ValueError("Каретка вне проверенной области.")
            positions = scan_plan(area, calibration, self.number(self.overlap, 10, 50) / 100)
            url = endpoint(self.scan_capture_url())  # Reject malformed capture URLs before any G1.
            if context.get('pen_lift_enabled') and not context.get('pen_lift_ready'):
                raise ValueError("Проверьте настройки соленоида в пульте. Камера не измеряет положение ручки.")
            if len(positions) * calibration.resolution[0] * calibration.resolution[1] > 80_000_000:
                raise ValueError("Проход больше 80 Мп в памяти. Увеличьте полезное поле камеры или сканируйте меньшую область.")
            if not messagebox.askokcancel("Это запустит сканирующий проход",
                    f"{len(positions)} остановок внутри проверенной области {area[0]:g}×{area[1]:g} мм.\n\n" +
                    scan_pen_notice(context) + "Проверьте свободный ход камеры и кабеля, "
                    "крепление листа, калибровку камеры и физический ноль. Концевиков нет. "
                    "Старый проход в памяти будет заменён.\n\n" +
                    ("Пульт DEMO: реальные моторы не двигаются." if context.get('simulation') else
                     "Это будет движение реальных моторов. Первый проход — без ручки, на малой скорости."), parent=self.root):
                return
            current = self.get_context()
            if (current.get('busy') or not current.get('connected') or
                    current.get('revision') != context['revision'] or
                    current.get('position') != context['position']):
                raise ValueError("Состояние пульта изменилось во время подтверждения.")
            if self.client is not client or self.calibration is not calibration or self.scan_capture_url() != url:
                raise ValueError("Камера или её калибровка изменились во время подтверждения.")
            client.get_frame()
            events, token = self.scan_events, object()
            def take_photo(index, position):
                image = capture(url)
                calibration.check(image)
                events.put((token, index, image, position))
            if not self.start_motion_scan(positions, take_photo):
                raise ValueError("Пульт занят; проход не запущен.")
            self.clear_tiles()
            self.scan_token = token
            self.scan_active = self.busy = True
            self.scan_count = len(positions)
            self.tile_revision = context['revision']
            self.tile_area = area
            self.status.set("Проход запущен. СТОП в пульте или здесь немедленно отменяет дальнейшие движения.")
        except ValueError as exc:
            self.fail(exc)

    def stop_scan(self):
        if self.scan_active:
            self.scan_token = None
            self.scan_active = self.busy = False
            if self.stop_motion is not None:
                self.stop_motion()
            self.status.set("Проход остановлен. Ноль станка сброшен; сохраните полученные исходники отдельно.")

    def remove_tile(self):
        if self.tiles and not self.busy:
            image, _ = self.tiles.pop()
            self.tile_pixel_count -= image.shape[0] * image.shape[1]
            self.tile_status.set(f"Кадров: {len(self.tiles)}.")

    def clear_tiles(self, force=False):
        if self.busy and not force:
            return
        self.tiles.clear()
        self.tile_pixel_count = 0
        self.tile_revision = None
        self.tile_area = None
        self.offline_area = None
        self.tile_status.set("Кадров: 0. Координаты станка не изменены.")

    def replace_tiles_allowed(self):
        return not self.tiles or messagebox.askokcancel("Исходники прохода в памяти",
            "Эта операция заменит или очистит текущий проход. Сохраните исходные кадры, "
            "если они нужны. Продолжить?", parent=self.root)

    def clear_tiles_confirmed(self):
        if not self.busy and self.replace_tiles_allowed():
            self.clear_tiles()

    def mosaic(self):
        if self.busy:
            return
        try:
            context = self.get_context()
            area = self.offline_area
            if area is None:
                context, area = self.area()
            if self.offline_area is None and self.tile_revision != context["revision"]:
                raise ValueError("Калибровка пульта изменилась. Начните новый проход.")
            dpi = self.number(self.dpi, 50, 600)
            tiles, calibration = list(self.tiles), self.calibration
            revision = context["revision"]
            def complete(result):
                image, coverage = result
                if self.offline_area is None and self.get_context().get("revision") != revision:
                    raise ValueError("Область пульта изменилась во время склейки. Результат не принят.")
                self.frozen = True
                self.image = image
                self.image_position = None
                self.image_revision = None
                self.metric_report = None
                self.document_dimensions = None
                mh, mw = image.shape[:2]
                self.document_calibration = PlanarCalibration((mw, mh),
                    np.array([[area[0] / (mw - 1), 0, 0], [0, area[1] / (mh - 1), 0], [0, 0, 1]]), area)
                self.corners = []
                self.document = self.original_document = None
                self.incomplete_mosaic = coverage < .999
                if coverage >= .999:
                    self.original_document = self.document = image
                    self.document_dimensions = area
                    self.width_mm.set(f"{area[0]:g}")
                    self.height_mm.set(f"{area[1]:g}")
                self.render()
                self.status.set(f"Покрытие {coverage * 100:.2f}%. " + ("Можно очищать и экспортировать." if coverage >= .999 else
                    "Пропуски пурпурные; добавьте кадры. Экспорт полного скана заблокирован."))
            self.work(lambda: assemble_mosaic(tiles, calibration, area, dpi), complete)
        except ValueError as exc:
            self.fail(exc)

    def save_tiles(self):
        if not self.tiles or self.busy:
            return
        folder = filedialog.askdirectory(parent=self.root, title="Папка для новой подпапки исходных снимков")
        if not folder:
            return
        tiles, calibration = list(self.tiles), self.calibration
        try:
            area = self.offline_area or self.tile_area
            if area is None or calibration is None:
                raise ValueError("У прохода нет сохранённой области или калибровки камеры.")
        except ValueError as exc:
            self.fail(exc)
            return
        def calculation():
            import tempfile
            destination = Path(tempfile.mkdtemp(prefix="scan-", dir=folder))
            manifest = []
            for index, (image, position) in enumerate(tiles):
                name = f"tile-{index + 1:03d}.png"
                Image.fromarray(cv2.cvtColor(image, cv2.COLOR_BGR2RGB)).save(destination / name)
                manifest.append({"file": name, "pen_position_mm": list(position)})
            calibration.save(destination / "calibration.camera.json")
            atomic_json(destination / "tiles.json", {"format": "plotter-scan-tiles", "version": 1,
                "area_mm": list(area), "tiles": manifest})
            return destination
        self.work(calculation, lambda path: self.status.set("Исходные кадры сохранены: " + str(path)))

    def open_tiles(self):
        if self.busy:
            return
        path = filedialog.askopenfilename(parent=self.root, title="Открыть tiles.json · без восстановления нуля станка",
                                         filetypes=[("Проход камеры", "tiles.json")])
        if not path:
            return
        if not self.replace_tiles_allowed():
            return
        def calculation():
            from camera_vision import load_tiles
            return load_tiles(path)
        def complete(result):
            self.tiles, self.calibration, self.offline_area = result
            self.tile_area = self.offline_area
            self.tile_pixel_count = sum(image.shape[0] * image.shape[1] for image, _ in self.tiles)
            self.tile_revision = None
            self.frozen = True
            self.tile_status.set(f"Открыт сохранённый проход: {len(self.tiles)} кадров. Только обработка, не калибровка станка.")
        self.work(calculation, complete)

    def render(self):
        if self.closed:
            return
        self.canvas.delete("all")
        if self.image is None:
            return
        width, height = max(1, self.canvas.winfo_width()), max(1, self.canvas.winfo_height())
        ih, iw = self.image.shape[:2]
        scale = min(width / iw, height / ih)
        dw, dh = max(1, round(iw * scale)), max(1, round(ih * scale))
        resized = cv2.resize(self.image, (dw, dh), interpolation=cv2.INTER_AREA)
        self.photo = ImageTk.PhotoImage(Image.fromarray(cv2.cvtColor(resized, cv2.COLOR_BGR2RGB)), master=self.root)
        ox, oy = (width - dw) / 2, (height - dh) / 2
        self.display_transform = ox, oy, scale
        self.canvas.create_image(ox, oy, anchor="nw", image=self.photo)
        for index, point in enumerate(self.corners):
            x, y = ox + point[0] * scale, oy + point[1] * scale
            self.canvas.create_oval(x - 5, y - 5, x + 5, y + 5, outline="#00ff99", width=2)
            self.canvas.create_text(x + 10, y - 10, text=str(index + 1), fill="#00ff99", font=("Segoe UI", 12, "bold"))
        if len(self.corners) == 4:
            points = [v for p in [*self.corners, self.corners[0]] for v in (ox + p[0] * scale, oy + p[1] * scale)]
            self.canvas.create_line(*points, fill="#00ff99", width=2)

    def pump(self):
        if self.closed:
            return
        for _ in range(32):
            if self.client is None:
                break
            try:
                kind, data = self.client.events.get_nowait()
            except queue.Empty:
                break
            if kind == "telemetry":
                self.telemetry.set("Телеметрия (только показ): " + json.dumps(data, ensure_ascii=False)[:300])
            else:
                self.status.set(str(data))
        if self.client is not None and self.client.telemetry is not None:
            when, data = self.client.telemetry
            if time.monotonic() - when > 3:
                self.telemetry.set("Телеметрия УСТАРЕЛА: " + json.dumps(data, ensure_ascii=False)[:250])
        for _ in range(200):
            try:
                token, index, image, position = self.scan_events.get_nowait()
            except queue.Empty:
                break
            if token is not self.scan_token:
                continue
            self.tiles.append((image, position))
            self.tile_pixel_count += image.shape[0] * image.shape[1]
            self.tile_status.set(f"Автоматический проход: {index + 1}/{self.scan_count} кадров.")
        if self.scan_active and not self.get_context().get('busy'):
            self.scan_active = self.busy = False
            self.scan_token = None
            self.status.set("Проход завершён; можно склеивать кадры." if len(self.tiles) == self.scan_count else
                            "Проход прерван или камера не ответила. Полный скан не готов; исходники можно сохранить.")
        if self.client is not None and not self.frozen and not self.busy:
            try:
                frame = self.client.get_frame()
                if frame.sequence != self.last_sequence:
                    self.last_sequence = frame.sequence
                    self.display_frame(frame.image)
            except ValueError:
                self.canvas.delete("stale")
                self.canvas.create_text(15, 20, anchor="w", text="НЕТ СВЕЖЕГО КАДРА · не используйте изображение для измерений",
                                        fill="#ffcc66", tags="stale")
        for _ in range(8):
            try:
                token, result, error = self.events.get_nowait()
            except queue.Empty:
                break
            if token is not self.token:
                continue
            self.busy = False
            self.token = None
            self.token_camera = False
            done, self.on_done = self.on_done, None
            if error:
                self.status.set("Не выполнено: " + error)
            else:
                try:
                    done(result)
                except Exception as exc:
                    self.fail(exc)
        self.timer = self.root.after(80, self.pump)

    def close(self):
        if self.closed:
            return
        self.stop_scan()
        self.closed = True
        self.token = None
        self.on_done = None
        self.disconnect()
        if self.timer is not None:
            self.root.after_cancel(self.timer)
        self.root.destroy()
