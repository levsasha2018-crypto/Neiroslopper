"""Reference selection and local tracing. Stages suggestions, never commits/moves."""
from dataclasses import replace
from copy import deepcopy
import io
import json
from pathlib import Path
import queue
import re
import threading
import tkinter as tk
from tkinter import ttk, filedialog
from tkinter import messagebox
import webbrowser

from PIL import Image, ImageTk

from geometry import Sheet
from .image_trace import Options, Asset, asset_from_bytes, read_asset, decode_rgb, trace
from .layout import ScrollablePage
from .reference_images import search, download, save_source, ReferenceCancelled
from .scene import scene_plan, checked_scene
from .window import draw

KINDS = {'Автоматически': 'auto', 'Линейный рисунок': 'line', 'Фотография': 'photo', 'Контуры SVG': 'vector'}
ANCHORS = {'В центре': 'center', 'Сверху слева': 'top_left', 'Сверху справа': 'top_right',
           'Снизу слева': 'bottom_left', 'Снизу справа': 'bottom_right'}


def search_gallery(query, cancel):
    rows = search(query, cancel=cancel)
    result = []
    for row in rows:
        if cancel.is_set():
            raise ReferenceCancelled('Поиск отменён.')
        try:
            image = decode_rgb(download(row.thumbnail, limit=2_000_000, cancel=cancel))
        except ReferenceCancelled:
            raise
        except ValueError:
            image = None
        result.append((row, image))
    return result


def selected_plan(result, scene=None, selected=None):
    if selected is None:
        return result.plan
    w, h = result.sheet
    scene, _ = checked_scene(scene, w, h)
    item = next((item for item in scene['objects'] if item['id'] == selected), None)
    if item is None:
        raise ValueError('Выбранный объект больше не существует.')
    source = result.plan.parameters['scene']['objects'][0]
    item['name'], item['source'], item['matrix'] = source['name'], deepcopy(source['source']), [1, 0, 0, 1, 0, 0]
    scene['route'] = None
    checked_scene(scene, w, h)
    return scene_plan(scene, result.plan.explanation + ' Заменяется только выбранный объект; остальные сохранены.')


class ReferencePanel:
    def __init__(self, window):
        self.window = window
        self.root = tk.Toplevel(window.root)
        self.root.title('Рисунок по образцу — поиск и обводка')
        sw, sh = self.root.winfo_screenwidth(), self.root.winfo_screenheight()
        self.root.geometry(f'{min(1120, sw-80)}x{min(780, sh-120)}')
        self.root.minsize(min(720, sw-80), min(480, sh-120))
        self.closed = self.busy = False
        self.cancel = threading.Event()
        self.events = queue.Queue()
        self.epoch = 0
        self.callback = None
        self.timer = None
        self.asset = self.result = self.before = None
        self.source_candidate = None
        self.original_candidate = None
        self.subject = None
        self.versions = {}
        self.review_report = self.review_key = None
        self.review_count = 0
        self.review_cache = {}
        self.profile = tk.StringVar(value='Детальный')
        self.high_detail = tk.BooleanVar(value=True)
        self.working_side = tk.IntVar(value=2048)
        self.local_contrast = tk.BooleanVar(value=True)
        self.weak_edges = tk.BooleanVar(value=True)
        self.background_detail = tk.IntVar(value=25)
        self.hatching = tk.BooleanVar(value=False)
        self.hatch_spacing = tk.DoubleVar(value=.8)
        self.hatch_strength = tk.DoubleVar(value=.65)
        self.auto_fit = tk.BooleanVar(value=True)
        self.stroke_limit = tk.IntVar(value=12000)
        self.point_limit = tk.IntVar(value=45000)
        self.fit_note = tk.StringVar(value='Автоподгонка сложности включена; изменения будут указаны после обработки.')
        self.selection_mode = tk.StringVar(value='Главный объект')
        self.version_choice = tk.StringVar(value='Первый вариант')
        self.gallery_photos = []
        self.original_photo = None
        self.original_transform = None
        self.crop = None
        self.crop_start = None
        self.query = tk.StringVar(value='Porsche 911 side view')
        self.status = tk.StringVar(value='Откройте образец. Обводка выполняется локальными алгоритмами.')
        self.source_note = tk.StringVar(value='Источник ещё не выбран.')
        self.kind = tk.StringVar(value='Автоматически')
        self.detail = tk.IntVar(value=90)
        self.smoothing = tk.DoubleVar(value=.04)
        self.min_detail = tk.DoubleVar(value=.1)
        current = window.snapshot().state
        self.width = tk.DoubleVar(value=min(160, max(1, (current.get('sheet_width') or 210)-6)))
        self.margin = tk.DoubleVar(value=min(3, min(current.get('sheet_width') or 210,
                                                    current.get('sheet_height') or 297)/6))
        self.anchor = tk.StringVar(value='В центре')
        self.remove_background = tk.BooleanVar(value=False)
        self.optimize = tk.BooleanVar(value=True)
        self.build()
        for variable in (self.kind, self.detail, self.smoothing, self.min_detail, self.width,
                         self.margin, self.anchor, self.remove_background, self.optimize, self.high_detail,
                         self.working_side, self.local_contrast, self.weak_edges, self.background_detail,
                         self.hatching, self.hatch_spacing, self.hatch_strength, self.auto_fit,
                         self.stroke_limit, self.point_limit):
            variable.trace_add('write', self.invalidate)
        self.root.protocol('WM_DELETE_WINDOW', self.close)
        self.timer = self.root.after(60, self.pump)

    def build(self):
        outer = ttk.Frame(self.root, padding=10)
        outer.pack(fill='both', expand=True)
        outer.columnconfigure(0, weight=1)
        outer.rowconfigure(1, weight=1)
        top = ttk.Frame(outer)
        top.grid(row=0, column=0, sticky='ew', pady=(0, 8))
        ttk.Button(top, text='Открыть PNG / JPG / SVG…', command=self.open_file).pack(side='left')
        self.original_button = ttk.Button(top, text='Загрузить оригинал', command=self.load_original, state='disabled')
        self.original_button.pack(side='left', padx=8)
        self.tabs = ttk.Notebook(outer)
        self.tabs.grid(row=1, column=0, sticky='nsew')
        self.search_tab = ttk.Frame(self.tabs, padding=8)
        self.trace_tab = ttk.Frame(self.tabs, padding=8)
        self.tabs.add(self.search_tab, text='Найти образец')
        self.tabs.add(self.trace_tab, text='Сравнить и обвести')
        self.search_tab.columnconfigure(0, weight=1)
        self.search_tab.rowconfigure(2, weight=1)
        line = ttk.Frame(self.search_tab)
        line.grid(row=0, column=0, sticky='ew')
        self.query_input = ttk.Entry(line, textvariable=self.query)
        self.query_input.pack(side='left', fill='x', expand=True, padx=(0, 8))
        self.query_input.bind('<Return>', lambda _event: self.find())
        self.search_button = ttk.Button(line, text='Искать в интернете', command=self.find)
        self.search_button.pack(side='right')
        ttk.Label(self.search_tab, text='Wikimedia Commons · без API-ключа. Передаётся только строка поиска. '
                  'SVG и линейные рисунки предлагаются первыми; ракурс и сходство выбираете вы.', wraplength=850).grid(
                      row=1, column=0, sticky='ew', pady=6)
        self.gallery = ScrollablePage(self.search_tab)
        self.gallery.grid(row=2, column=0, sticky='nsew')
        self.search_help = ttk.Label(self.gallery.content, text='Например: Porsche 911 side view, rocket drawing, cat line drawing.', wraplength=600)
        self.search_help.pack(anchor='w')

        panes = ttk.Panedwindow(self.trace_tab, orient='horizontal')
        panes.pack(fill='both', expand=True)
        controls = ScrollablePage(panes)
        panes.add(controls, weight=0)
        preview = ttk.Frame(panes)
        panes.add(preview, weight=1)
        fields = controls.content
        fields.columnconfigure(0, weight=1)
        fields.columnconfigure(1, weight=1)
        ttk.Label(fields, text='Тип образца').grid(row=0, column=0, sticky='w')
        self.kind_input = ttk.Combobox(fields, textvariable=self.kind, values=tuple(KINDS), width=18, state='readonly')
        self.kind_input.grid(row=1, column=0, columnspan=2, sticky='ew', pady=(2, 8))
        ttk.Label(fields, text='Детализация, %').grid(row=2, column=0, sticky='w')
        ttk.Label(fields, textvariable=self.detail).grid(row=2, column=1, sticky='e')
        ttk.Scale(fields, from_=0, to=100, variable=self.detail).grid(row=3, column=0, columnspan=2, sticky='ew', pady=(2, 8))
        for row, title, variable, start, end, step in ((4, 'Сглаживание, мм', self.smoothing, 0, 2, .05),
                 (5, 'Минимум детали, мм', self.min_detail, 0, 10, .1),
                 (6, 'Макс. ширина, мм', self.width, 1, 3000, 5),
                 (7, 'Поля листа, мм', self.margin, 0, 500, 1)):
            ttk.Label(fields, text=title).grid(row=row, column=0, sticky='w', pady=3)
            ttk.Spinbox(fields, textvariable=variable, from_=start, to=end, increment=step, width=8).grid(
                row=row, column=1, sticky='e')
        ttk.Label(fields, text='Размещение').grid(row=8, column=0, sticky='w', pady=(8, 2))
        ttk.Combobox(fields, textvariable=self.anchor, values=tuple(ANCHORS), state='readonly', width=18).grid(
            row=9, column=0, columnspan=2, sticky='ew')
        ttk.Checkbutton(fields, text='Убрать фон фотографии', variable=self.remove_background).grid(
            row=10, column=0, columnspan=2, sticky='w', pady=(8, 2))
        ttk.Checkbutton(fields, text='Сократить соединения', variable=self.optimize).grid(
            row=11, column=0, columnspan=2, sticky='w')
        ttk.Label(fields, text='Рамка мышью: главный объект или обрезка. Фон вне главного объекта сохраняется.',
                  wraplength=230).grid(row=12, column=0, columnspan=2, sticky='ew', pady=8)
        ttk.Button(fields, text='Сбросить обрезку', command=self.reset_crop).grid(row=13, column=0, columnspan=2, sticky='ew')
        ttk.Button(fields, text='Открыть страницу источника', command=self.open_source).grid(
            row=14, column=0, columnspan=2, sticky='ew', pady=8)
        ttk.Label(fields, textvariable=self.source_note, wraplength=235).grid(row=15, column=0, columnspan=2, sticky='ew')
        profile = ttk.Combobox(fields, textvariable=self.profile, values=('Быстрый', 'Детальный', 'Контуры + штриховка'),
                               state='readonly', width=20)
        profile.grid(row=16, column=0, columnspan=2, sticky='ew', pady=6)
        profile.bind('<<ComboboxSelected>>', self.set_profile)
        ttk.Checkbutton(fields, text='Повышенное разрешение', variable=self.high_detail).grid(row=17, column=0, columnspan=2, sticky='w')
        ttk.Label(fields, text='Рабочая сторона, px').grid(row=18, column=0, sticky='w')
        ttk.Spinbox(fields, textvariable=self.working_side, from_=512, to=4096, increment=256, width=8).grid(row=18, column=1, sticky='e')
        ttk.Checkbutton(fields, text='Местный контраст', variable=self.local_contrast).grid(row=19, column=0, columnspan=2, sticky='w')
        ttk.Checkbutton(fields, text='Слабые контуры', variable=self.weak_edges).grid(row=20, column=0, columnspan=2, sticky='w')
        ttk.Label(fields, text='Детализация фона, %').grid(row=21, column=0, sticky='w')
        ttk.Spinbox(fields, textvariable=self.background_detail, from_=0, to=100, width=8).grid(row=21, column=1, sticky='e')
        ttk.Combobox(fields, textvariable=self.selection_mode, values=('Главный объект', 'Обрезка'), state='readonly', width=20).grid(
            row=22, column=0, columnspan=2, sticky='ew', pady=6)
        ttk.Button(fields, text='Сбросить главный объект', command=self.reset_subject).grid(row=23, column=0, columnspan=2, sticky='ew')
        ttk.Checkbutton(fields, text='Тональная штриховка', variable=self.hatching).grid(row=24, column=0, columnspan=2, sticky='w', pady=6)
        for row, title, var, low, high, step in ((25, 'Шаг штрихов, мм', self.hatch_spacing, .2, 5, .1),
                                               (26, 'Насыщенность, 0–1', self.hatch_strength, 0, 1, .05)):
            ttk.Label(fields, text=title).grid(row=row, column=0, sticky='w')
            ttk.Spinbox(fields, textvariable=var, from_=low, to=high, increment=step, width=8).grid(row=row, column=1, sticky='e')
        ttk.Label(fields, text='Высокая детализация и штриховка увеличивают время и переходы. '
                  'Исходник не увеличивается искусственно; для мелких деталей загрузите оригинал.',
                  wraplength=235).grid(row=27, column=0, columnspan=2, sticky='ew', pady=8)
        ttk.Checkbutton(fields, text='Автоподгонка сложности', variable=self.auto_fit).grid(row=28, column=0, columnspan=2, sticky='w')
        for row, title, var, lo, hi in ((29, 'Лимит штрихов', self.stroke_limit, 100, 20000),
                                      (30, 'Лимит точек', self.point_limit, 1000, 48000)):
            ttk.Label(fields, text=title).grid(row=row, column=0, sticky='w')
            ttk.Spinbox(fields, textvariable=var, from_=lo, to=hi, increment=1000, width=8).grid(row=row, column=1, sticky='e')
        # Put the preset at the top rather than after long source attribution.
        for child in fields.winfo_children():
            info = child.grid_info()
            if child is not profile and info and int(info['row']) <= 15:
                child.grid_configure(row=int(info['row'])+1)
        profile.grid_configure(row=0)
        controls.enable_wheel()
        preview.columnconfigure((0, 1), weight=1, uniform='preview')
        preview.rowconfigure(1, weight=1)
        ttk.Label(preview, text='Образец (SVG — его контуры)').grid(row=0, column=0, sticky='w')
        ttk.Label(preview, text='Траектория на листе').grid(row=0, column=1, sticky='w')
        self.original_canvas = tk.Canvas(preview, background='#dde5ee', highlightthickness=0, width=230, height=220)
        self.original_canvas.grid(row=1, column=0, sticky='nsew', padx=(0, 5))
        self.path_canvas = tk.Canvas(preview, background='#dde5ee', highlightthickness=0, width=230, height=220)
        self.path_canvas.grid(row=1, column=1, sticky='nsew')
        self.original_canvas.bind('<Configure>', lambda _event: self.render_original())
        self.path_canvas.bind('<Configure>', lambda _event: self.render_result())
        self.original_canvas.bind('<Button-1>', self.begin_crop)
        self.original_canvas.bind('<B1-Motion>', self.move_crop)
        self.original_canvas.bind('<ButtonRelease-1>', self.end_crop)
        ttk.Label(preview, text='Оранжевые переходы тоже рисуются без подъёма ручки. Начальный подвод проверяйте в пульте.',
                  wraplength=550, foreground='#984b00').grid(row=2, column=0, columnspan=2, sticky='ew', pady=5)
        self.version_input = ttk.Combobox(preview, textvariable=self.version_choice,
                                         values=('Первый вариант',), state='readonly')
        self.version_input.grid(row=3, column=0, columnspan=2, sticky='ew')
        self.version_input.bind('<<ComboboxSelected>>', self.select_version)
        self.fit_note_label = tk.Label(preview, textvariable=self.fit_note, wraplength=550, foreground='#984b00',
                                      height=2, anchor='w', justify='left', font=('Segoe UI', 9))
        self.fit_note_label.grid(row=4, column=0, columnspan=2, sticky='ew', pady=4)
        self.fit_note_label.bind('<Configure>', lambda event: self.fit_note_label.configure(wraplength=max(100, event.width)))
        self.status_label = tk.Label(outer, textvariable=self.status, anchor='w', justify='left', height=2,
                                      wraplength=1000, foreground='#52647a', font=('Segoe UI', 9))
        self.status_label.grid(row=2, column=0, sticky='ew', pady=6)
        self.status_label.bind('<Configure>', lambda event: self.status_label.configure(wraplength=max(200, event.width)))
        buttons = ttk.Frame(outer)
        buttons.grid(row=3, column=0, sticky='ew')
        buttons.columnconfigure(2, weight=1)
        self.trace_button = ttk.Button(buttons, text='Построить обводку', command=self.build_trace)
        self.trace_button.grid(row=0, column=0, sticky='w')
        ttk.Button(buttons, text='Отменить', command=self.stop).grid(row=0, column=1, padx=5, sticky='w')
        self.stage_button = ttk.Button(buttons, text='Предложить в эскизе', command=self.stage, state='disabled')
        self.stage_button.grid(row=0, column=2, sticky='e')
        self.export_button = ttk.Button(buttons, text='Сохранить обводку в SVG…', command=self.export, state='disabled')
        self.export_button.grid(row=1, column=0, columnspan=2, sticky='w', pady=(4, 0))

    def set_query(self, query, prompt=None):
        self.invalidate()
        self.asset = self.source_candidate = self.original_candidate = None
        self.crop = self.subject = None
        self.original_button.configure(state='disabled')
        self.source_note.set('Источник ещё не выбран.')
        self.render_original()
        self.root.deiconify()
        self.root.lift()
        self.query.set(query)
        if prompt:
            from .planner import _CORNERS
            text = prompt.casefold()
            anchor = next((value for corner, value in _CORNERS.items()
                           if re.search(r'\b(?:в|на)\s+' + corner + r'\s+углу\b', text)), 'center')
            self.anchor.set(next(label for label, value in ANCHORS.items() if value == anchor))
            width = re.search(r'\bширин(?:ой|а)\s+(\d+(?:[.,]\d+)?)\s*мм\b', text)
            if width:
                self.width.set(float(width[1].replace(',', '.')))
            if re.search(r'\bдетализированно\b', text):
                self.detail.set(90)
        self.status.set('Проверьте название и настройки. Поиск начинается только по кнопке; файлы не отправляются.')
        self.tabs.select(self.search_tab)
        self.query_input.focus_set()

    def invalidate(self, *_args):
        self.result = self.before = None
        self.versions.clear()
        self.review_report = self.review_key = None
        self.review_count = 0
        self.fit_note.set('Изменены исходник/настройки. Постройте новый вариант; старый результат неприменим.')
        self.version_input.configure(values=('Нет готового варианта',))
        self.version_choice.set('Нет готового варианта')
        self.set_review_text('Исходник или настройки изменились. Старая проверка неприменима; постройте новый вариант. '
                             'Постройте новую локальную обводку.')
        self.stage_button.configure(state='disabled')
        self.export_button.configure(state='disabled')
        self.render_result()
        if self.busy:
            self.stop()

    def run(self, job, callback, status):
        self.cancel.set()
        self.epoch += 1
        epoch = self.epoch
        self.cancel = threading.Event()
        cancel, events = self.cancel, self.events
        self.callback = callback
        self.busy = True
        self.trace_button.configure(state='disabled')
        self.search_button.configure(state='disabled')
        self.stage_button.configure(state='disabled')
        self.export_button.configure(state='disabled')
        self.status.set(status)
        def worker():
            try:
                value = job(cancel)
                events.put((epoch, 'done', value))
            except ReferenceCancelled:
                events.put((epoch, 'cancelled', None))
            except Exception as exc:
                # No URLs, HTML or source text are interpreted as instructions.
                events.put((epoch, 'error', str(exc)[:400]))
        threading.Thread(target=worker, daemon=True).start()

    def stop(self):
        self.cancel.set()
        self.epoch += 1
        self.busy = False
        self.callback = None
        self.trace_button.configure(state='normal')
        self.search_button.configure(state='normal')
        self.stage_button.configure(state='normal' if self.result else 'disabled')
        self.export_button.configure(state='normal' if self.result else 'disabled')
        self.status.set('Отменено. Эскиз не изменён.')
        self.refresh_buttons()

    def pump(self):
        if self.closed:
            return
        while True:
            try:
                epoch, kind, value = self.events.get_nowait()
            except queue.Empty:
                break
            if epoch != self.epoch:
                continue
            callback = self.callback
            self.callback = None
            self.busy = False
            self.trace_button.configure(state='normal')
            self.search_button.configure(state='normal')
            try:
                if kind == 'done':
                    callback(value)
                else:
                    self.status.set('Операция отменена.' if kind == 'cancelled' else value)
            except (ValueError, OSError, tk.TclError) as exc:
                self.status.set(str(exc))
            self.refresh_buttons()
        self.timer = self.root.after(60, self.pump)

    def find(self):
        query = self.query.get().strip()
        self.invalidate()
        self.run(lambda cancel: search_gallery(query, cancel), self.show_gallery,
                 'Ищу образцы в Wikimedia Commons… В интернет передана только строка поиска.')

    def show_gallery(self, rows):
        self.gallery_photos.clear()
        for child in self.gallery.content.winfo_children():
            child.destroy()
        for col in range(3):
            self.gallery.content.columnconfigure(col, weight=1, uniform='cards')
        for i, (candidate, image) in enumerate(rows):
            card = ttk.Frame(self.gallery.content, padding=6)
            card.grid(row=i//3, column=i%3, sticky='nsew')
            photo = None
            if image is not None:
                thumb = Image.fromarray(image)
                thumb.thumbnail((200, 125), Image.Resampling.LANCZOS)
                photo = ImageTk.PhotoImage(thumb, master=self.root)
                self.gallery_photos.append(photo)
            button = ttk.Button(card, text='Выбрать образец', command=lambda item=candidate, bitmap=image: self.choose(item, bitmap))
            if photo:
                button.configure(image=photo, compound='top')
            button.pack(fill='x')
            if candidate.mime == 'image/svg+xml' and image is not None:
                ttk.Button(card, text='Обвести растровую версию',
                           command=lambda item=candidate, bitmap=image: self.use_thumbnail(item, bitmap)).pack(fill='x', pady=3)
            ttk.Label(card, text=candidate.title, wraplength=210).pack(anchor='w', pady=4)
            ttk.Label(card, text=('SVG · ' if candidate.mime == 'image/svg+xml' else '') + candidate.license,
                      wraplength=210, foreground='#52647a').pack(anchor='w')
            if image is None:
                ttk.Label(card, text='Превью не загрузилось. Проверьте образец на странице источника.',
                          wraplength=210, foreground='#984b00').pack(anchor='w', pady=3)
            ttk.Button(card, text='Страница источника',
                       command=lambda item=candidate: webbrowser.open(item.page)).pack(fill='x', pady=3)
        self.gallery.enable_wheel()
        self.status.set(('Выберите образец; поколение и ракурс проверьте по картинке.' +
                         (' Некоторые превью недоступны; можно открыть страницу источника.'
                          if any(image is None for _, image in rows) else '')) if rows else
                        'Не найдено подходящих изображений. Измените запрос, попробуйте английское название или откройте локальную картинку.')

    def use_thumbnail(self, candidate, image):
        self.invalidate()
        if image is None:
            self.status.set('Изображение не загружено; выберите другой образец.')
            return
        buffer = io.BytesIO()
        Image.fromarray(image).save(buffer, format='PNG')
        raster_source = replace(candidate, mime='image/png', url=candidate.thumbnail)
        try:
            save_source(self.window.data_dir, buffer.getvalue(), raster_source)
            self.use_asset(Asset(candidate.title + ' (растровый образец)', image.copy()), raster_source)
            self.original_candidate = candidate
            self.original_button.configure(state='normal')
        except (OSError, ValueError) as exc:
            self.status.set(str(exc))

    def choose(self, candidate, image=None):
        if candidate.mime != 'image/svg+xml' and image is not None:
            # Commons supplies a bounded ~1200 px rendition. Do not fetch a
            # huge original photograph when it exceeds useful pen resolution.
            self.use_thumbnail(candidate, image)
            return
        self.invalidate()
        directory = self.window.data_dir
        def load(cancel):
            raw = download(candidate.url, cancel=cancel)
            asset = asset_from_bytes(raw, candidate.title, mime=candidate.mime)
            if cancel.is_set():
                raise ReferenceCancelled('Загрузка отменена.')
            save_source(directory, raw, candidate)
            return asset
        self.run(load, lambda asset: self.use_asset(asset, candidate), 'Загружаю выбранный образец…')

    def open_file(self, path=None):
        path = path or filedialog.askopenfilename(parent=self.root, title='Открыть образец для обводки',
            filetypes=[('Картинки и SVG', '*.png *.jpg *.jpeg *.webp *.gif *.bmp *.tif *.tiff *.svg')])
        if not path:
            return
        self.invalidate()
        self.run(lambda cancel: read_asset(path), lambda asset: self.use_asset(asset), 'Читаю локальный образец… Файл никуда не отправляется.')

    def use_asset(self, asset, candidate=None):
        self.invalidate()
        self.asset = asset
        self.source_candidate = candidate
        self.original_candidate = candidate
        self.original_button.configure(state='normal' if candidate else 'disabled')
        self.crop = self.subject = None
        self.result = None
        self.source_note.set((f'{candidate.title}\nАвтор: {candidate.author}\nЛицензия: {candidate.license}\n{candidate.page}'
                              if candidate else f'Локальный файл: {asset.name}. Само открытие и обводка ничего не отправляют.'))
        self.tabs.select(self.trace_tab)
        self.status.set('SVG: используются готовые контуры. Проверьте размер и нажмите «Построить обводку».' if asset.vector_paths else
                        'Образец загружен. Выберите тип, детализацию и постройте обводку.')
        self.render_original()
        self.render_result()

    def options(self):
        try:
            return Options(KINDS[self.kind.get()], self.detail.get(), self.smoothing.get(), self.min_detail.get(),
                           self.width.get(), self.margin.get(), self.remove_background.get(), self.optimize.get(),
                           self.crop, ANCHORS[self.anchor.get()], high_detail=self.high_detail.get(),
                           working_side=self.working_side.get(), local_contrast=self.local_contrast.get(),
                           weak_edges=self.weak_edges.get(), subject_box=self.subject,
                           background_detail=self.background_detail.get(), hatching=self.hatching.get(),
                           hatch_spacing_mm=self.hatch_spacing.get(), hatch_strength=self.hatch_strength.get(),
                           auto_fit=self.auto_fit.get(), stroke_limit=self.stroke_limit.get(), point_limit=self.point_limit.get())
        except (tk.TclError, KeyError):
            raise ValueError('В параметрах обводки нужны числа и значения из списка.') from None

    def context_key(self):
        window = self.window
        mode = window.mode.get()
        return mode, window.snapshot().revision, id(window.context_proposal), window.selected_objects.get(mode)

    def load_original(self):
        if self.original_candidate is not None and not self.busy:
            self.choose(self.original_candidate)

    def set_profile(self, _event=None):
        fast = self.profile.get() == 'Быстрый'
        self.high_detail.set(not fast)
        self.local_contrast.set(not fast)
        self.weak_edges.set(not fast)
        self.hatching.set(self.profile.get() == 'Контуры + штриховка')
        self.detail.set(70 if fast else 90)
        self.smoothing.set(.12 if fast else .04)
        self.min_detail.set(.6 if fast else .1)
        self.status.set('Профиль выбран. Настройки можно изменить вручную; постройте новый вариант.')

    def reset_subject(self):
        self.subject = None
        self.invalidate()
        self.render_original()

    def set_review_text(self, text):
        self.local_processing_summary = text

    def refresh_buttons(self):
        valid = self.result is not None and not self.busy
        if valid:
            valid = (self.result_key == self.context_key() and not self.window.busy and
                     not False and not self.window.snapshot().state.get('busy'))
        for button in (self.stage_button, self.export_button):
            button.configure(state='normal' if valid else 'disabled')

    def select_version(self, _event=None):
        if self.busy:
            return
        result = self.versions.get(self.version_choice.get())
        if result is not None:
            self.result = result
            self.show_adaptation(result)
            self.render_result()
            self.refresh_buttons()


    def show_adaptation(self, result):
        metrics = result.metrics
        if metrics.get('auto_fit_applied'):
            self.fit_note.set(f'Автоподгонка: исключено {metrics.get("budget_removed_paths", 0)} штрихов; '
                f'сглаживание {metrics.get("effective_smoothing_mm", result.options.smoothing_mm):g} мм; '
                f'шаг тона {result.options.hatch_spacing_mm:g} мм. Сравните результат с исходником.')
        else:
            self.fit_note.set('Результат поместился в лимиты без дополнительного упрощения сложности.')




    def build_trace(self):
        if self.asset is None:
            self.status.set('Сначала выберите образец или откройте файл.')
            return
        window = self.window
        if window.busy or False or window.snapshot().state.get('busy'):
            self.status.set('Сначала завершите текущий запрос, запись или операцию станка.')
            return
        try:
            options = self.options()
            options.check()
            before = window.snapshot()
            sheet = Sheet(before.state['sheet_width'], before.state['sheet_height'])
            asset = self.asset
            key = self.context_key()
            self.proposal_guard = window.context_proposal  # Keep its identity alive until staging/cancellation.
            scene = window.current_scene()
            selected = key[3]
            if selected and scene:
                from .scene import checked_scene
                checked, groups = checked_scene(scene, sheet.width, sheet.height)
                remaining = 50000-sum(len(path) for obj, group in zip(checked['objects'], groups)
                                     if obj['id'] != selected for path in group)
                if remaining < 1000:
                    raise ValueError('Соседние объекты уже заняли почти весь бюджет 50 000 точек. '
                                     'Упростите композицию или выберите замену всей композиции.')
                options = replace(options, point_limit=min(options.point_limit, remaining))
            self.invalidate()
            def ready(result):
                if key != self.context_key():
                    raise ValueError('Область, объект или предложение изменились. Постройте обводку заново.')
                self.result, self.before, self.result_key, self.result_scene = result, before, key, scene
                self.versions = {'Первый вариант': result}
                self.version_input.configure(values=tuple(self.versions))
                self.version_choice.set('Первый вариант')
                self.stage_button.configure(state='normal')
                self.export_button.configure(state='normal')
                self.refresh_buttons()
                metrics = result.metrics
                self.status.set(f'{metrics["path_count"]} штрихов · {metrics["point_count"]} точек · '
                    f'переходы {metrics["connection_length_mm"]:.1f} мм. ' + ' '.join(result.warnings))
                self.render_result()
                self.show_adaptation(result)
                self.set_review_text('Локальная обработка:\n\n'+'\n\n'.join(result.warnings)+
                                     '\n\nЕсли были упрощения, сравните результат с исходником.')
            self.run(lambda cancel: trace(asset, sheet, options, cancel=cancel), ready, 'Строю обводку локально…')
        except (ValueError, KeyError, TypeError) as exc:
            self.status.set(str(exc))

    def stage(self):
        if self.result is None or self.busy:
            return
        try:
            if self.result_key != self.context_key():
                raise ValueError('Область, объект или предложение изменились. Постройте обводку заново.')
            plan = selected_plan(self.result, self.result_scene, self.result_key[3])
            self.window.stage_plan(self.before, plan, self.result_key[0])
            source = ('\nИсточник: ' + self.source_candidate.page + '\nАвтор: ' + self.source_candidate.author +
                      '\nЛицензия: ' + self.source_candidate.license) if self.source_candidate else '\nИсточник: локальная картинка.'
            self.window.append('\nОбводка: ' + plan.explanation + source + '\n' + '\n'.join(self.result.warnings) +
                               '\nПроверьте предложение и нажмите «Применить к эскизу».\n')
            self.window.tabs.select(self.window.dialog_tab)
            self.window.root.lift()
            self.root.withdraw()
        except ValueError as exc:
            self.status.set(str(exc))

    def export(self):
        if self.result is None or self.busy:
            return
        path = filedialog.asksaveasfilename(parent=self.root, title='Сохранить обводку, не запуская станок',
                defaultextension='.svg', filetypes=[('SVG', '*.svg')], initialfile='reference_trace.svg')
        if not path:
            return
        try:
            from .svg_export import render_svg
            from html import escape
            result = self.result
            svg = render_svg(result.paths, *result.sheet, self.asset.name)
            source = self.source_candidate
            if source:
                description = json.dumps({'source': source.page, 'author': source.author, 'license': source.license}, ensure_ascii=False)
                svg = svg.replace('</svg>', '<desc>' + escape(description) + '</desc>\n</svg>')
            if len(svg.encode('utf-8')) > 3_000_000:
                raise ValueError('SVG слишком большой; упростите обводку.')
            Path(path).write_text(svg, encoding='utf-8')
            self.status.set('Обводка сохранена в SVG 1:1; источник и лицензия добавлены, если они известны.')
        except (OSError, ValueError) as exc:
            self.status.set(str(exc))

    def open_source(self):
        if self.source_candidate:
            webbrowser.open(self.source_candidate.page)

    def render_original(self):
        canvas = self.original_canvas
        canvas.delete('all')
        self.original_transform = None
        if self.asset is None or self.closed:
            return
        h, w = self.asset.image.shape[:2]
        cw, ch = max(10, canvas.winfo_width()), max(10, canvas.winfo_height())
        scale = min((cw-12)/w, (ch-12)/h)
        if scale <= 0:
            return
        size = max(1, round(w*scale)), max(1, round(h*scale))
        image = Image.fromarray(self.asset.image).resize(size, Image.Resampling.LANCZOS)
        self.original_photo = ImageTk.PhotoImage(image, master=self.root)
        ox, oy = (cw-size[0])/2, (ch-size[1])/2
        canvas.create_image(ox, oy, image=self.original_photo, anchor='nw')
        self.original_transform = ox, oy, scale, w, h
        if self.crop:
            x0, y0, x1, y1 = self.crop
            canvas.create_rectangle(ox+x0*scale, oy+y0*scale, ox+x1*scale, oy+y1*scale, outline='#13a083', width=2, tags='crop')
        if self.subject:
            x0, y0, x1, y1 = self.subject
            canvas.create_rectangle(ox+x0*scale, oy+y0*scale, ox+x1*scale, oy+y1*scale, outline='#9453cc', width=2, tags='subject')

    def crop_point(self, event):
        if not self.original_transform:
            return None
        ox, oy, scale, w, h = self.original_transform
        return min(w, max(0, round((event.x-ox)/scale))), min(h, max(0, round((event.y-oy)/scale)))

    def begin_crop(self, event):
        self.crop_start = self.crop_point(event)

    def move_crop(self, event):
        end = self.crop_point(event)
        if not self.crop_start or not end:
            return
        x0, y0 = self.crop_start
        x1, y1 = end
        box = min(x0, x1), min(y0, y1), max(x0, x1), max(y0, y1)
        if self.selection_mode.get() == 'Главный объект':
            self.subject = box
        else:
            self.crop = box
        self.render_original()

    def end_crop(self, event):
        if self.crop_start:
            self.move_crop(event)
            self.crop_start = None
            self.invalidate()
            self.status.set('Рамка выбрана. Для SVG переключитесь на растровый режим; постройте обводку заново.')

    def reset_crop(self):
        self.crop = self.crop_start = None
        self.invalidate()
        self.render_original()

    def render_result(self):
        if self.closed:
            return
        if self.result:
            draw(self.path_canvas, self.result.paths, *self.result.sheet)
        else:
            self.path_canvas.delete('all')
            self.path_canvas.create_text(max(20, self.path_canvas.winfo_width())/2,
                max(20, self.path_canvas.winfo_height())/2, text='Постройте обводку\nи сравните с образцом.', justify='center')

    def close(self):
        if self.closed:
            return
        self.closed = True
        self.cancel.set()
        self.callback = None
        if self.timer:
            self.root.after_cancel(self.timer)
        self.root.destroy()
