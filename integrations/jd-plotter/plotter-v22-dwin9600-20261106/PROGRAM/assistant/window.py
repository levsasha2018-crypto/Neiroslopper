"""Threaded assistant UI. Suggestions affect only an editor after an explicit click."""
from __future__ import annotations

from dataclasses import replace
import queue
import re
import threading
import tkinter as tk
from tkinter import filedialog, messagebox, simpledialog, ttk
from tkinter.scrolledtext import ScrolledText

from .planner import transform_paths
from .preview import Sandbox
from .runtime import Assistant, EXAMPLES
from .voice_ui import VoiceControls


class AssistantWindow:
    def __init__(self, parent, get_snapshot, commit_preview, undo_preview, redo_preview=None, load_project=None,
                 *, data_dir=None, autosave_store=None, editor_history=None):
        self.root = tk.Toplevel(parent)
        self.root.title("Помощник плоттера — команды и эскиз")
        screen_width, screen_height = self.root.winfo_screenwidth(), self.root.winfo_screenheight()
        self.root.geometry(f"{min(1100, screen_width - 80)}x{min(760, screen_height - 120)}")
        self.root.minsize(min(720, screen_width - 80), min(480, screen_height - 120))
        self.root.configure(bg="#eef2f6")
        self.get_editor = get_snapshot
        self.commit_editor = commit_preview
        self.undo_editor = undo_preview
        self.redo_editor = redo_preview
        self.load_editor_project = load_project
        self.get_editor_history = editor_history
        self.data_dir = data_dir
        from .task_memory import TaskMemory
        self.task_memories = {mode: TaskMemory() for mode in ('Пробный лист', 'Эскиз плоттера')}
        from .studio_store import Autosaves, Templates
        self.autosaves = autosave_store or (Autosaves(data_dir) if data_dir is not None else None)
        self.owns_autosaves = autosave_store is None
        self.templates = Templates(data_dir) if data_dir is not None else None
        self.selected_objects = {}
        self.studio_retry = None
        self.studio_panel = None
        self.reference_panel = None
        self.reference_request = None
        self.variant_cache = None
        self.last_templates = {}
        self.display_transform = None
        from editor_history import EditHistory
        self.context_histories = {mode: EditHistory() for mode in ("Пробный лист", "Эскиз плоттера")}
        self.history_revisions = {}
        self.mode_contexts = {}
        self.sandbox = Sandbox()
        self.assistant = Assistant(data_dir)
        self.events = queue.Queue()
        self.pending = None
        self.applied_anchor = None
        self.context_proposal = None
        self.preview_task = None
        self.undo_task = None
        self.context_scope = None
        self.last_preview_request = ""
        self.awaiting_text_field = ""
        self.awaiting_text_edit = {}
        self.chat_history = []
        self.retry_base = None
        self.busy = False
        self.closed = False
        self.streamed = ""
        self.token = None
        self.cancel = threading.Event()
        self.timer = None
        current = get_snapshot()
        self.mode = tk.StringVar(value="Эскиз плоттера" if current.state.get("sheet_width") else "Пробный лист")
        self.autosave_enabled = tk.BooleanVar(value=self.autosaves is not None)
        self.object_label = tk.StringVar(value='Вся композиция')
        self.status = tk.StringVar(value="Готов · Codex")
        self.proposal_title = tk.StringVar(value="Текущий эскиз · изменений ещё нет")
        self.source_info = tk.StringVar()
        self.build()
        self.refresh()
        self.restore_mode_context()
        self.render_current()
        self.root.protocol("WM_DELETE_WINDOW", self.close)
        self.entry.focus_set()
        self.timer = self.root.after(60, self.pump)

    def build(self):
        from .layout import ScrollablePage

        outer = ttk.Frame(self.root, padding=10)
        outer.pack(fill="both", expand=True)
        outer.columnconfigure(0, weight=1)
        # Only the middle grows/shrinks. The input is a separate, fixed-height row.
        outer.rowconfigure(2, weight=1)
        ttk.Label(outer, text="Помощник плоттера", font=("Segoe UI", 15, "bold")).grid(
            row=0, column=0, sticky='w')
        row = ttk.Frame(outer)
        row.grid(row=1, column=0, sticky='ew', pady=(4, 8))
        ttk.Label(row, text="Работаем с:").pack(side="left")
        self.mode_input = ttk.Combobox(row, textvariable=self.mode,
                                      values=("Пробный лист", "Эскиз плоттера"), state="readonly", width=20)
        self.mode_input.pack(side="left", padx=6)
        self.mode_input.bind("<<ComboboxSelected>>", lambda _: self.change_mode())
        ttk.Button(row, text="Обновить эскиз", command=self.change_mode).pack(side="right")
        ttk.Button(row, text='Картинка…', command=self.open_reference).pack(side='right', padx=5)
        if self.data_dir is not None:
            ttk.Button(row, text='Чат Codex…', command=self.configure_codex_chat).pack(side='right')

        self.tabs = ttk.Notebook(outer)
        self.tabs.grid(row=2, column=0, sticky='nsew')
        self.dialog_tab = ttk.Frame(self.tabs, padding=8)
        self.tabs.add(self.dialog_tab, text='Диалог и эскиз')
        self.project_tab = ScrollablePage(self.tabs)
        self.voice_tab = ScrollablePage(self.tabs)
        for page, title in ((self.project_tab, 'Проект'), (self.voice_tab, 'Голос')):
            self.tabs.add(page, text=title)

        columns = ttk.Panedwindow(self.dialog_tab, orient='horizontal')
        columns.pack(fill='both', expand=True)
        left = ttk.Frame(columns)
        right = ttk.Frame(columns)
        columns.add(left, weight=3)
        columns.add(right, weight=2)
        right.columnconfigure(0, weight=1)
        right.rowconfigure(1, weight=1)
        self.transcript = ScrolledText(left, width=38, height=8, wrap="word", font=("Segoe UI", 10), state="disabled")
        self.transcript.pack(fill="both", expand=True)
        proposal_label = ttk.Label(right, textvariable=self.proposal_title, wraplength=360)
        proposal_label.grid(row=0, column=0, sticky='ew', pady=(0, 4))
        self.canvas = tk.Canvas(right, width=340, height=220, bg="#dde5ee", highlightthickness=0)
        self.canvas.grid(row=1, column=0, sticky='nsew')
        self.canvas.bind("<Configure>", lambda _: self.render_current())
        self.canvas.bind('<Button-1>', self.select_at)
        selection_row = ttk.Frame(right)
        selection_row.grid(row=2, column=0, sticky='ew', pady=4)
        self.object_input = ttk.Combobox(selection_row, textvariable=self.object_label, state='readonly', width=16)
        self.object_input.pack(side='left', fill='x', expand=True)
        self.object_input.bind('<<ComboboxSelected>>', self.select_from_list)
        ttk.Button(selection_row, text='Инструменты…', command=self.open_studio).pack(side='right')
        self.pen_legend = tk.StringVar(value="Оранжевый — переходы. Их след зависит от проверенного режима подъёма в пульте.")
        legend = ttk.Label(right, textvariable=self.pen_legend, wraplength=360,
                           font=("Segoe UI", 9), foreground="#984b00")
        legend.grid(row=3, column=0, sticky='ew', pady=(0, 4))
        right.bind('<Configure>', lambda event: [label.configure(wraplength=max(160, event.width))
                                                for label in (proposal_label, legend)])
        actions = ttk.Frame(right)
        actions.grid(row=4, column=0, sticky='ew')
        actions.columnconfigure((0, 1), weight=1)
        self.apply_button = ttk.Button(actions, text="Применить к эскизу", command=self.apply, state="disabled")
        self.apply_button.grid(row=0, column=0, columnspan=2, sticky='ew')
        self.undo_button = ttk.Button(actions, text="Отменить правку", command=self.undo)
        self.undo_button.grid(row=1, column=0, sticky='ew', pady=(4, 0), padx=(0, 3))
        self.redo_button = ttk.Button(actions, text="Повторить правку", command=self.redo)
        self.redo_button.grid(row=1, column=1, sticky='ew', pady=(4, 0))

        project = self.project_tab.content
        ttk.Label(project, textvariable=self.source_info, wraplength=650, foreground="#984b00").pack(
            anchor='w', fill='x', pady=(0, 8))
        ttk.Label(project, text="Синий — контуры; оранжевый — переходы. Без подъёма они тоже рисуются.\n"
                  "Начальный переход от головки проверяйте в основном пульте.\n"
                  "Пробный лист не задаёт координаты станка.", wraplength=650).pack(anchor='w', pady=(0, 8))
        project_actions = ttk.Frame(project)
        project_actions.pack(fill="x", pady=5)
        ttk.Button(project_actions, text="Сохранить проект", command=self.save_project).pack(side="left")
        ttk.Button(project_actions, text="Открыть проект", command=self.open_project).pack(side="left", padx=5)
        self.restore_button = ttk.Button(project, text="Вернуть последний корректный вариант предложения",
                                         command=self.restore_proposal, state="disabled")
        self.restore_button.pack(anchor="w", pady=(5, 0))
        ttk.Button(project, text="Сохранить текущий эскиз в SVG", command=self.export_svg).pack(anchor="w", pady=5)
        auto_row = ttk.Frame(project)
        auto_row.pack(fill='x')
        ttk.Checkbutton(auto_row, text='Автосохранение', variable=self.autosave_enabled,
                        command=self.schedule_autosave, state='normal' if self.autosaves else 'disabled').pack(side='left')
        ttk.Button(auto_row, text='Восстановить автокопию…', command=self.recover_autosave,
                   state='normal' if self.autosaves else 'disabled').pack(side='left', padx=8)
        sample = ttk.LabelFrame(project, text='Примеры команд', padding=8)
        sample.pack(fill="x", pady=(10, 4))
        self.examples = ttk.Combobox(sample, values=EXAMPLES, state="readonly", width=48)
        self.examples.pack(side="left", fill='x', expand=True, padx=(0, 6))
        self.examples.set(EXAMPLES[0])
        ttk.Button(sample, text="Вставить", command=self.insert_example).pack(side="left")

        self.composer = ttk.LabelFrame(outer, text='Ваш запрос — напишите здесь', padding=8)
        self.composer.grid(row=3, column=0, sticky='ew', pady=(8, 0))
        self.composer.columnconfigure(0, weight=1)
        self.entry = ScrolledText(self.composer, width=20, height=3, wrap="word", font=("Segoe UI", 11),
                                  background='white', highlightthickness=1, highlightbackground='#90a4be',
                                  highlightcolor='#2563a6')
        self.entry.grid(row=0, column=0, sticky='ew', padx=(0, 8))
        self.entry.bind("<Return>", self.send)
        self.entry.bind("<Shift-Return>", lambda _: None)
        submit = ttk.Frame(self.composer)
        submit.grid(row=0, column=1, sticky='ns')
        self.send_button = ttk.Button(submit, text="Отправить ↵", command=self.send)
        self.send_button.pack(fill='x')
        self.cancel_button = ttk.Button(submit, text="Отменить запрос", command=self.cancel_request)
        self.cancel_button.pack(fill='x', pady=(4, 0))
        shortcuts = ttk.Frame(self.composer)
        shortcuts.grid(row=1, column=0, columnspan=2, sticky='ew', pady=(6, 0))
        ttk.Label(shortcuts, text='Enter — отправить · Shift+Enter — новая строка', font=('Segoe UI', 9)).pack(side='left')
        ttk.Button(shortcuts, text='Голос…', command=self.show_voice).pack(side='right')
        ttk.Button(shortcuts, text='Печатный текст…', command=self.open_text_form).pack(side='right', padx=5)
        # A long diagnostic message must not make the whole composer taller.
        self.status_label = tk.Label(self.composer, textvariable=self.status, height=2,
                                     font=('Segoe UI', 9), anchor='w', justify='left',
                                     background='#eef2f6', foreground='#52647a', wraplength=800)
        self.status_label.grid(row=2, column=0, columnspan=2, sticky='ew', pady=(3, 0))
        self.status_label.bind('<Configure>', lambda event: self.status_label.configure(wraplength=max(160, event.width)))
        self.root.bind('<Control-l>', self.focus_query)

        self.voice = VoiceControls(self.voice_tab.content, self.entry, on_recording=self.voice_started,
                                   on_busy=lambda _: self.update_send_button(),
                                   can_record=lambda: not self.busy)
        self.voice.pack(fill="x")
        for page in (self.project_tab, self.voice_tab):
            page.enable_wheel()
        self.append("Попробуйте «Нарисуй розу» или «Сдвинь надпись левее на 5 мм».\n")
        if self.autosaves and self.autosaves.available():
            self.append('Есть автокопия предыдущей работы. «Восстановить автокопию…» откроет её только на пробном листе.\n')

    def focus_query(self, _event=None):
        self.tabs.select(self.dialog_tab)
        self.entry.focus_set()
        return 'break'

    def configure_codex_chat(self):
        if self.busy or self.voice.busy:
            self.status.set('Сначала завершите текущий запрос или запись.')
            return
        from .saved_chat import load, save
        try:
            current = load(self.data_dir) or ''
        except (ValueError, OSError):
            current = ''
        value = simpledialog.askstring('Чат Codex', 'ID локального чата Codex или ссылка codex://threads/…\n'
            'Пустое поле отключит привязку. В привязанном чате вопросы используют очень высокое рассуждение.',
            initialvalue=current, parent=self.root)
        if value is None:
            return
        try:
            save(self.data_dir, value)
            self.status.set('Чат Codex привязан · Очень высокая' if value.strip() else 'Привязка отключена')
        except (ValueError, OSError) as exc:
            messagebox.showerror('Не удалось сохранить чат', str(exc), parent=self.root)

    def show_voice(self):
        self.tabs.select(self.voice_tab)

    def open_reference(self, query=None, prompt=None):
        if self.busy or self.voice.busy or self.snapshot().state.get('busy'):
            self.status.set('Сначала завершите текущий запрос, запись или операцию станка.')
            return None
        try:
            from .reference_ui import ReferencePanel
            if self.reference_panel is None or self.reference_panel.closed:
                self.reference_panel = ReferencePanel(self)
            panel = self.reference_panel
            panel.root.deiconify()
            panel.root.lift()
            if query:
                panel.set_query(query, prompt)
            return panel
        except ImportError:
            self.status.set('Для обводки нужны зависимости из requirements-camera.txt: OpenCV и Pillow.')
            return None

    def snapshot(self):
        return self.sandbox.snapshot() if self.mode.get() == "Пробный лист" else self.get_editor()

    def append(self, text):
        self.transcript.configure(state="normal")
        self.transcript.insert("end", text)
        self.transcript.see("end")
        self.transcript.configure(state="disabled")

    def insert_example(self):
        self.entry.delete("1.0", "end")
        self.entry.insert("1.0", self.examples.get())
        self.focus_query()

    def open_text_form(self):
        if self.busy or self.voice.busy:
            self.status.set("Сначала дождитесь ответа или отмените текущий запрос.")
            return
        from .text_dialog import open_text_dialog

        def submit(command):
            if self.busy or self.voice.busy:
                raise ValueError("Сначала дождитесь завершения текущего запроса.")
            self.entry.delete("1.0", "end")
            self.entry.insert("1.0", command)
            self.send()

        open_text_dialog(self.root, submit)

    def refresh(self, *, keep_task=False):
        self.pending = None
        self.context_proposal = None
        self.last_preview_request = ""
        self.awaiting_text_field = ""
        self.awaiting_text_edit = {}
        self.context_scope = None
        self.retry_base = None
        self.restore_button.configure(state="disabled")
        if not keep_task:
            self.preview_task = None
            self.undo_task = None
            self.applied_anchor = None
        self.apply_button.configure(state="disabled")
        self.proposal_title.set("Текущий эскиз · изменений ещё нет")
        self.render_current()

    def change_mode(self):
        self.cancel_request()
        self.reference_request = None
        self.studio_retry = self.variant_cache = None
        self.refresh()
        self.restore_mode_context()

    def restore_mode_context(self):
        mode, current = self.mode.get(), self.snapshot()
        stored = self.mode_contexts.get(mode)
        if stored is not None and stored[0] == current.revision:
            task, anchor = stored[1]
        elif mode == "Эскиз плоттера":
            task, anchor = current.state.get("preview_task"), current.state.get("preview_anchor")
        else:
            return
        self.preview_task = (mode, current.revision, task) if task is not None else None
        self.applied_anchor = (mode, current.revision, anchor) if anchor is not None else None
        self.context_scope = (mode, current.revision)

    def update_send_button(self):
        if not self.closed:
            voice_busy = hasattr(self, "voice") and self.voice.busy
            self.send_button.configure(state="disabled" if self.busy or voice_busy else "normal")

    def voice_started(self):
        # A new recording invalidates any older proposed drawing, but never
        # changes an editor or sends the eventual transcript by itself.
        self.cancel_request(cancel_voice=False)
        self.proposal_title.set("Голосовой ввод · старое предложение отменено")
        self.render_current()
        self.status.set("Запишите просьбу, затем проверьте текст. Отправка — отдельной кнопкой.")

    def editor_changed(self):
        if not self.closed and self.mode.get() == "Эскиз плоттера":
            self.cancel_request()
            self.refresh()
            self.restore_mode_context()
            self.status.set("Эскиз или калибровка изменились. Старое предложение отменено.")

    def current_scene(self, *, proposed=False):
        from .scene import from_plan
        current = self.snapshot()
        task = self.history_context()[0]
        paths = current.paths
        if proposed and self.context_proposal and self.context_proposal[0].revision==current.revision:
            task,paths = self.context_proposal[1:3]
        if not paths or current.state.get('sheet_width') is None:
            return None
        return from_plan(task,paths,current.state['sheet_width'],current.state['sheet_height'])

    def update_objects(self):
        try:
            scene = self.current_scene()
        except (ValueError,TypeError,KeyError):
            scene = None
        self.object_ids = [None]+([o['id'] for o in scene['objects']] if scene else [])
        labels = ['Вся композиция']+([f'{i+1}. {o["name"][:40]}' for i,o in enumerate(scene['objects'])] if scene else [])
        self.object_input['values'] = labels
        selected = self.selected_objects.get(self.mode.get())
        if selected not in self.object_ids:
            selected = None
            self.selected_objects[self.mode.get()] = None
        self.object_label.set(labels[self.object_ids.index(selected)])
        return scene

    def select_object(self, identifier):
        if self.busy or self.voice.busy or self.snapshot().state.get('busy'):
            self.status.set('Сначала завершите текущую операцию.')
            self.update_objects()
            return
        if self.pending or self.context_proposal:
            self.status.set('Перед сменой объекта примените или отмените предложение.')
            self.update_objects()
            return
        scene = self.current_scene()
        if identifier is not None and (scene is None or identifier not in {o['id'] for o in scene['objects']}):
            raise ValueError('Объект больше не существует.')
        self.cancel_request()
        self.studio_retry = None
        self.selected_objects[self.mode.get()] = identifier
        self.render_current()
        self.status.set('Выбран отдельный объект; остальные сохраняются.' if identifier else 'Выбрана вся композиция.')

    def select_from_list(self,event=None):
        index = self.object_input.current()
        if index>=0:
            self.select_object(self.object_ids[index])

    def select_at(self,event):
        if self.display_transform is None:
            return
        from .scene import hit_test
        scene = self.current_scene()
        if scene:
            ox,oy,scale = self.display_transform
            current = self.snapshot()
            self.select_object(hit_test(scene,current.state['sheet_width'],current.state['sheet_height'],
                ((event.x-ox)/scale,(event.y-oy)/scale),6/scale))

    def open_studio(self):
        from .studio_ui import StudioPanel
        if self.studio_panel is None or not self.studio_panel.root.winfo_exists():
            self.studio_panel = StudioPanel(self)
        else:
            self.studio_panel.refresh_history()
            self.studio_panel.root.lift()
        return self.studio_panel

    def stage_plan(self,before,plan,mode=None):
        mode = mode or self.mode.get()
        current = self.snapshot()
        if self.busy or self.voice.busy or current.state.get('busy') or mode!=self.mode.get() or before.revision!=current.revision:
            raise ValueError('Эскиз или состояние изменились. Постройте предложение заново.')
        paths = transform_paths(before.paths,plan,current.state['sheet_width'],current.state['sheet_height'])
        self.cancel_request()
        self.pending = self.context_proposal = (before,plan,paths,mode)
        self.context_scope = (mode,before.revision)
        self.apply_button.configure(state='normal')
        self.proposal_title.set('Предложение · ещё НЕ применено')
        self.render_current()
        memory = getattr(self, 'task_memories', {}).get(mode)
        if memory is not None:
            memory.note((mode, before.revision), 'proposed', plan.explanation[:500], selected=self.selected_objects.get(mode))
        return paths

    def schedule_autosave(self):
        if not self.autosaves or not self.autosave_enabled.get():
            return
        current = self.snapshot()
        if not current.paths or current.state.get('busy'):
            return
        # Main editor has its own save hook, including imports outside this window.
        if not self.owns_autosaves and self.mode.get()=='Эскиз плоттера':
            return
        from editor_project import SketchProject
        from geometry import Sheet
        task,anchor = self.history_context()
        self.autosaves.schedule(self.mode.get(),SketchProject(Sheet(current.state['sheet_width'],current.state['sheet_height']),
            current.paths,current.state.get('file_name','Эскиз')[:500],task,anchor))

    def recover_autosave(self):
        if not self.autosaves or self.busy or self.voice.busy or self.get_editor().state.get('busy'):
            return
        available = self.autosaves.available()
        if not available:
            self.status.set('Автокопий пока нет.')
            return
        choice = available[0] if len(available)==1 else simpledialog.askstring('Выбор автокопии',
            'Введите «Пробный лист» или «Эскиз плоттера». Обе копии откроются только на пробном листе.',parent=self.root)
        if choice not in available:
            return
        try:
            project = self.autosaves.restore(choice)
            mode,revision = self.mode.get(),self.snapshot().revision
            if not messagebox.askokcancel('Восстановить автокопию?',
                'Будет заменён пробный лист. Ноль станка, COM и реальные координаты не восстанавливаются.',parent=self.root):
                return
            if self.busy or self.voice.busy or self.get_editor().state.get('busy') or self.mode.get()!=mode or self.snapshot().revision!=revision:
                raise ValueError('Состояние изменилось во время подтверждения.')
            self.mode.set('Пробный лист')
            self.change_mode()
            before = self.snapshot()
            self.prepare_history(self.mode.get(),before.revision)
            self.context_histories[self.mode.get()].push(self.history_context())
            self.sandbox.load_project(project)
            self.restore_history_context((project.task,project.anchor))
            self.status.set('Автокопия восстановлена на пробном листе; станок не изменён.')
        except (OSError,ValueError) as exc:
            self.status.set(str(exc))

    def send(self, event=None):
        if getattr(self, 'closed', False):
            return "break"
        if self.voice.busy:
            self.status.set("Сначала завершите запись и проверку голосового текста.")
            return "break"
        prompt = self.entry.get("1.0", "end").strip()
        if not prompt:
            return "break"
        from .runtime import preview_cancel
        cancellation = preview_cancel(prompt)
        if cancellation is not None:
            # Works even while a model request is busy; its late result is stale.
            self.cancel_request(cancel_voice=False)
            self.entry.delete('1.0', 'end')
            self.append('\nВы: ' + prompt + '\n\nПомощник: ' + cancellation + '\n')
            self.proposal_title.set('Текущий эскиз · предложение отменено')
            self.render_current()
            return 'break'
        if self.busy:
            return 'break'
        if re.fullmatch(r'(?:покажи|отобрази)\s+(?:(?:этот|последний)\s+)?'
                        r'(?:его|рисунок|эскиз|вариант)(?:\s+в\s+(?:окне|предпросмотре))?[.!]?', prompt, re.I):
            if self.restore_proposal():
                self.entry.delete('1.0', 'end')
                self.append('\nВы: ' + prompt + '\n\nПомощник: Открыл последнее предложение.\n')
                return 'break'
        if len(prompt) > 1800:
            self.status.set("Сократите сообщение до 1800 знаков.")
            return "break"
        from .reference_images import drawing_clause, drawing_reference_request, self_draw_request
        previous = (getattr(self, 'reference_request', None) or
                    (self.chat_history[-1][0] if self.chat_history else None))
        original_drawing = self_draw_request(prompt, previous)
        if original_drawing == '':
            self.status.set('Что нарисовать? Назовите предмет или опишите рисунок.')
            return 'break'
        reference = drawing_reference_request(prompt, previous) if original_drawing is None else None
        self.reference_request = reference.prompt if reference is not None else None
        panel = getattr(self, 'reference_panel', None)
        if panel is not None and not panel.closed and panel.busy:
            panel.stop()
        if original_drawing and panel is not None and not panel.closed:
            panel.root.withdraw()
        self.tabs.select(self.dialog_tab)
        before = self.snapshot()
        mode = self.mode.get()
        state = dict(before.state)
        state["editor_paths"] = before.paths
        if reference is not None:
            state['web_drawing'] = reference
        if self.context_proposal is not None:
            if self.context_proposal[0].revision == before.revision and self.context_proposal[3] == mode:
                state["preview_draft"] = self.context_proposal[1]
            else:
                self.context_proposal = None
        if self.preview_task is not None:
            if self.preview_task[:2] == (mode, before.revision):
                state["preview_task"] = self.preview_task[2]
            else:
                self.preview_task = None
        if self.context_scope == (mode, before.revision):
            state["last_preview_request"] = self.last_preview_request
            state["awaiting_text_field"] = self.awaiting_text_field
            state.update(self.awaiting_text_edit)
        if self.retry_base is not None and self.retry_base[:2] == (mode, before.revision):
            state["retry_draft"] = self.retry_base[2]
        state["chat_history"] = tuple(self.chat_history)
        if self.applied_anchor is not None and self.applied_anchor[:2] == (mode, before.revision):
            state["preview_anchor"] = self.applied_anchor[2]
        try:
            state['studio_scene'] = self.current_scene(proposed=True)
            state['studio_selected'] = self.selected_objects.get(mode)
            if self.studio_retry and self.studio_retry[:2]==(mode,before.revision):
                state['studio_scale_base'] = self.studio_retry[2]
            if self.templates:
                followup = re.fullmatch(r'(?:сделай|создай)\s+(?:такую|такой|такую же|такой же)(?:\s+открытку|\s+шаблон)?\s+для\s+.+',prompt,re.I)
                if followup and self.last_templates.get(mode):
                    state['studio_template'] = self.templates.load(self.last_templates[mode])
                for name in self.templates.names():
                    if re.fullmatch(r'(?:шаблон\s+)?[«"]?'+re.escape(name)+r'[»"]?(?:\s+для\s+.+)?',prompt,re.I):
                        state['studio_template'] = self.templates.load(name)
                        break
        except (OSError,ValueError,TypeError,KeyError) as exc:
            self.status.set('Не удалось прочитать объекты: '+str(exc))
            return 'break'
        memory = getattr(self, 'task_memories', {}).get(mode)
        if memory is not None:
            state['task_memory'] = memory.snapshot((mode, before.revision), selected=self.selected_objects.get(mode))
        token = self.token = object()
        self.cancel = threading.Event()
        cancel = self.cancel
        self.pending = None
        self.busy = True
        self.streamed = ""
        self.apply_button.configure(state="disabled")
        self.restore_button.configure(state="disabled")
        self.send_button.configure(state="disabled")
        self.entry.delete("1.0", "end")
        self.append("\nВы: " + prompt + "\n\nПомощник: ")
        effective_prompt = original_drawing or (reference.prompt if reference is not None else drawing_clause(prompt))
        if effective_prompt != prompt:
            state['original_user_prompt'] = prompt
        self.status.set('Codex ищет образец и строит эскиз…' if reference is not None else 'Разбираю команду…')
        engine = self.assistant
        event_queue = self.events

        def worker():
            try:
                # No Tk window/variable references in a worker, including late
                # replies after closing: Tk objects must be released on its thread.
                reply = engine.answer(effective_prompt, state, use_llm=True, allow_creative=True, cancel=cancel,
                                      on_chunk=lambda text: event_queue.put((token, "chunk", text)))
                paths = None
                if reply.plan is not None:
                    width, height = before.state.get("sheet_width"), before.state.get("sheet_height")
                    if width is None or height is None:
                        reply = replace(reply, text="Сначала задайте два угла в пульте или выберите "
                                        "«Пробный лист».", plan=None)
                    elif before.state.get("busy"):
                        reply = replace(reply, text="В пульте выполняется операция или загрузка SVG. "
                                        "Дождитесь завершения и повторите команду.", plan=None)
                    else:
                        paths = transform_paths(before.paths, reply.plan, width, height)
                event_queue.put((token, "done", (reply, paths, before, mode, effective_prompt, state)))
            except Exception as exc:
                event_queue.put((token, "error", str(exc)))

        threading.Thread(target=worker, daemon=True).start()
        return "break"

    def cancel_request(self, *, cancel_voice=True):
        panel = getattr(self, 'reference_panel', None)
        if panel is not None and not panel.closed and panel.busy:
            panel.stop()
        if getattr(self, 'task_memories', None) and (getattr(self, 'busy', False) or getattr(self, 'pending', None) is not None):
            mode = self.mode.get()
            memory = getattr(self, 'task_memories', {}).get(mode)
            if memory is not None:
                current = self.snapshot()
                memory.note((mode, current.revision), 'cancelled', self.last_preview_request)
        self.cancel.set()
        self.token = None
        self.busy = False
        self.pending = None
        self.context_proposal = None
        self.context_scope = None
        self.last_preview_request = ""
        self.awaiting_text_field = ""
        self.awaiting_text_edit = {}
        self.retry_base = None
        self.studio_retry = None
        if hasattr(self, "restore_button"):
            self.restore_button.configure(state="disabled")
        if cancel_voice and hasattr(self, "voice") and self.voice.busy:
            self.voice.cancel()
        self.update_send_button()
        self.apply_button.configure(state="disabled")
        self.status.set("Запрос отменён. Движения станка не отправлялись.")

    def pump(self):
        if self.closed:
            return
        if self.autosaves:
            try:
                error=self.autosaves.errors.get_nowait()
                self.append('Автокопия не записана: '+error+'\nСохраните проект вручную после устранения причины.\n')
                self.status.set('Ошибка автосохранения — подробности в чате.')
            except queue.Empty:
                pass
        for _ in range(100):
            try:
                token, kind, data = self.events.get_nowait()
            except queue.Empty:
                break
            if token is not self.token:
                continue
            if kind == "chunk":
                # A model's text remains plain text; never parsed as an action.
                self.streamed += data
                self.append(data)
                self.status.set("Codex отвечает…")
            elif kind == "error":
                self.busy = False
                self.update_send_button()
                self.append("Не получилось подготовить эскиз: " + data + "\n")
                self.status.set("Операция не применена.")
                current = self.snapshot()
                retained = (self.context_proposal is not None and self.context_proposal[0].revision == current.revision and
                            self.context_proposal[3] == self.mode.get() and not current.state.get("busy"))
                self.restore_button.configure(state="normal" if retained else "disabled")
                self.render_current()
            else:
                reply, paths, before, mode, *request_prompt = data
                self.busy = False
                self.update_send_button()
                current = self.snapshot()
                stale = (mode != self.mode.get() or before.revision != current.revision or current.state.get("busy"))
                if reply.plan is not None and stale:
                    reply = replace(reply, text="Эскиз, калибровка или состояние пульта изменились. "
                                    "Старое предложение отменено; повторите команду.", plan=None)
                    paths = None
                if stale:
                    self.context_proposal = None
                    self.context_scope = None
                    self.last_preview_request = self.awaiting_text_field = ""
                    self.awaiting_text_edit = {}
                    self.retry_base = None
                    self.studio_retry = None
                else:
                    self.context_scope = (mode, current.revision)
                    self.last_preview_request = reply.context_request
                    self.awaiting_text_field = reply.waiting_field
                    self.awaiting_text_edit = dict(reply.waiting_edit)
                    if reply.context_request:
                        from .planner import parse
                        source_state = request_prompt[1] if len(request_prompt) > 1 else {}
                        known = parse(reply.context_request, {**source_state, "has_paths": True})
                        if known.kind == "scale":
                            base = (source_state.get("retry_draft") if reply.corrects_previous else None) or (
                                source_state.get("preview_draft") or source_state.get("preview_task"))
                            self.retry_base = (mode, current.revision, base) if base is not None else None
                        elif reply.source != "Контекст редактора · без догадок":
                            self.retry_base = None
                    if reply.studio_base is not None:
                        self.studio_retry = (mode,current.revision,reply.studio_base)
                    elif reply.plan is not None:
                        self.studio_retry = None
                    if reply.selection_id is not None or reply.select_all:
                        self.selected_objects[mode] = None if reply.select_all else reply.selection_id
                    if reply.template_name:
                        self.last_templates[mode] = reply.template_name
                    if reply.variants:
                        self.variant_cache = (before,mode,reply.variants)
                        self.open_studio().show_variants(before,mode,reply.variants)
                if request_prompt:
                    self.chat_history.append((request_prompt[0][:600], reply.text[:700]))
                    self.chat_history = self.chat_history[-4:]
                memory = getattr(self, 'task_memories', {}).get(mode)
                if memory is not None and not stale and request_prompt:
                    outcome = ('rejected' if reply.validation_error else 'proposed' if reply.plan is not None else
                               'needs_data' if reply.waiting_field or reply.waiting_edit else 'answered')
                    # A question about memory is not a new editor task.
                    if reply.source != 'Память задачи · данные редактора':
                        memory.note((mode, current.revision), outcome, request_prompt[0],
                                    selected=self.selected_objects.get(mode), awaiting=reply.waiting_field,
                                    error=reply.validation_error)
                if self.streamed and reply.text.startswith(self.streamed):
                    self.append(reply.text[len(self.streamed):] + "\n")
                else:
                    self.append(("\n" if self.streamed else "") + reply.text + "\n")
                depth = {"low": "Быстро", "medium": "Обычно", "high": "Подробно", "xhigh": "Очень высокая"}
                metric = reply.llm_metrics[-1] if reply.llm_metrics else {}
                level = depth.get(metric.get("reasoning_effort"))
                self.status.set(f"{reply.source} · {level}" if level else reply.source)
                if reply.plan is not None and paths is not None:
                    self.pending = (before, reply.plan, paths, mode)
                    self.context_proposal = self.pending
                    self.proposal_title.set("Предложение · ещё НЕ применено")
                    self.restore_button.configure(state="disabled")
                    self.apply_button.configure(state="normal")
                    self.render_current()
                else:
                    retained = self.context_proposal is not None and not stale
                    self.proposal_title.set("Предыдущий вариант сохранён для уточнения · НЕ применён" if retained else
                                            "Текущий эскиз · нового предложения нет")
                    self.restore_button.configure(state="normal" if retained else "disabled")
                    self.render_current()
        self.timer = self.root.after(60, self.pump)

    def export_svg(self):
        if self.busy or self.voice.busy or self.pending is not None:
            self.status.set("Сначала примените или отмените предложение. Сохраняется только текущий эскиз.")
            return
        current = self.snapshot()
        if current.state.get("busy") or not current.paths:
            self.status.set("Нет готового эскиза для сохранения, или пульт занят.")
            return
        destination = filedialog.asksaveasfilename(parent=self.root, title="Сохранить эскиз без запуска станка",
            defaultextension=".svg", filetypes=[("SVG — контуры 1:1", "*.svg")], initialfile="plotter_text.svg")
        if not destination:
            return
        from .svg_export import save_svg
        try:
            save_svg(destination, current.paths, current.state["sheet_width"], current.state["sheet_height"],
                     current.state.get("file_name", "Эскиз плоттера"))
            self.status.set("SVG сохранён: размер 1:1. При импорте пульт располагает его у выбранного поля.")
        except (OSError, ValueError) as exc:
            self.status.set("Не удалось сохранить SVG: " + str(exc))

    def render_current(self):
        if self.closed:
            return
        current = self.snapshot()
        scene = self.update_objects()
        self.display_transform = None
        if self.pending is not None and self.pending[0].revision != current.revision:
            self.pending = None
            self.apply_button.configure(state="disabled")
            self.proposal_title.set("Эскиз изменился · отправьте команду заново")
        data = current.state
        from pen_lift import connection_notice
        self.pen_legend.set(connection_notice(data.get('pen_lift_enabled') is True,
                                             data.get('pen_power_raises') is True,
                                             data.get('pen_continuous') is True))
        if self.mode.get() == "Пробный лист":
            self.source_info.set(f"ПРОБНЫЙ ЛИСТ {data['sheet_width']:g} × {data['sheet_height']:g} мм · только экран. "
                                 "Изменения сюда не попадают в пульт и не двигают станок.")
        else:
            self.source_info.set("ЭСКИЗ ПЛОТТЕРА · подтверждение меняет рисунок в основном пульте. "
                                 "Рисование запускается отдельной обычной кнопкой.")
        contextual = self.context_proposal
        if contextual is not None and (contextual[0].revision != current.revision or contextual[3] != self.mode.get()):
            self.context_proposal = contextual = None
            self.context_scope = None
            self.last_preview_request = self.awaiting_text_field = ""
            self.awaiting_text_edit = {}
        paths = self.pending[2] if self.pending else contextual[2] if contextual is not None else current.paths
        canvas = self.canvas
        canvas.delete("all")
        width, height = data.get("sheet_width"), data.get("sheet_height")
        w, h = max(10, canvas.winfo_width()), max(10, canvas.winfo_height())
        if width is None or height is None:
            canvas.create_text(w / 2, h / 2, text="Задайте два угла в пульте\nили выберите пробный лист.",
                               font=("Segoe UI", 12), justify="center")
            return
        scale = min((w - 44) / width, (h - 44) / height)
        if scale <= 0:
            return
        ox, oy = (w - scale * width) / 2, (h - scale * height) / 2
        self.display_transform = (ox,oy,scale)
        def xy(point):
            return ox + point[0] * scale, oy + point[1] * scale
        canvas.create_rectangle(ox, oy, ox + width * scale, oy + height * scale,
                                fill="white", outline="#a8b6c8")
        last = None
        for path in paths:
            if len(path) < 2:
                continue
            if last is not None and last != path[0]:
                canvas.create_line(*xy(last), *xy(path[0]), fill="#df790d", dash=(5, 3))
            for offset in range(0, len(path) - 1, 512):
                coordinates = [v for point in path[offset:offset + 513] for v in xy(point)]
                canvas.create_line(*coordinates, fill="#2563a6", width=2)
            last = path[-1]
        selected = self.selected_objects.get(self.mode.get())
        if selected and scene:
            from .scene import checked_scene,bounds
            try:
                shown = self.current_scene(proposed=True)
                _,groups = checked_scene(shown,width,height)
                for obj,group in zip(shown['objects'],groups):
                    if obj['id']==selected:
                        left,top,right,bottom = bounds(group)
                        canvas.create_rectangle(ox+left*scale-4,oy+top*scale-4,ox+right*scale+4,oy+bottom*scale+4,
                            outline='#13a083',width=2,dash=(3,2))
            except (ValueError,TypeError,KeyError):
                pass

    def apply(self):
        if self.pending is None or self.busy or self.voice.busy:
            return
        proposal = self.pending
        before, plan, paths, mode = proposal
        current = self.snapshot()
        if mode != self.mode.get() or before.revision != current.revision or current.state.get("busy"):
            self.refresh()
            self.append("Эскиз, калибровка или состояние пульта изменились. Отправьте команду заново.\n")
            return
        if not messagebox.askokcancel("Применить к эскизу?", plan.explanation +
                "\n\nИзменится только рисунок на экране. Команды движения и настройки контроллера "
                "не отправляются. Режим подъёма ручки проверяется отдельно в основном пульте.", parent=self.root):
            return
        current = self.snapshot()
        if (self.pending is not proposal or mode != self.mode.get() or before.revision != current.revision or
                current.state.get("busy") or self.busy or self.voice.busy):
            self.refresh()
            self.append("Предложение или состояние изменились во время подтверждения. Ничего не применено.\n")
            return
        try:
            from .draft_context import plan_anchor, applied_task
            previous_anchor = (self.applied_anchor[2] if self.applied_anchor is not None and
                               self.applied_anchor[:2] == (mode, before.revision) else None)
            previous_task = (self.preview_task[2] if self.preview_task is not None and
                             self.preview_task[:2] == (mode, before.revision) else None)
            previous_request, previous_retry = self.last_preview_request, self.retry_base
            previous_studio_retry = self.studio_retry
            context_before = self.history_context()
            self.prepare_history(mode, before.revision)
            if mode == "Пробный лист":
                self.sandbox.commit(before, plan)
            else:
                self.commit_editor(before, plan)
            self.context_histories[mode].push(context_before)
            self.history_revisions[mode] = self.snapshot().revision
            self.applied_anchor = (mode, self.snapshot().revision, plan_anchor(plan, previous_anchor))
            task = applied_task(plan, previous_task)
            self.preview_task = (mode, self.snapshot().revision, task) if task is not None else None
            self.mode_contexts[mode] = (self.snapshot().revision, (task, self.applied_anchor[2]))
            self.undo_task = previous_task
            self.refresh(keep_task=True)
            self.context_scope = (mode, self.snapshot().revision)
            self.last_preview_request = previous_request
            if previous_retry is not None:
                self.retry_base = (mode, self.snapshot().revision, previous_retry[2])
            self.studio_retry = (mode,self.snapshot().revision,previous_studio_retry[2]) if previous_studio_retry else None
            self.schedule_autosave()
            memory = getattr(self, 'task_memories', {}).get(mode)
            if memory is not None:
                memory.applied((mode, before.revision), (mode, self.snapshot().revision))
            self.append("Изменение применено только к эскизу. Станок не запускался.\n")
        except ValueError as exc:
            self.refresh()
            self.append("Не применено: " + str(exc) + "\n")

    def restore_proposal(self):
        if self.busy or self.voice.busy or self.context_proposal is None:
            return False
        before, plan, paths, mode = self.context_proposal
        current = self.snapshot()
        if mode != self.mode.get() or before.revision != current.revision or current.state.get("busy"):
            self.refresh()
            return False
        self.pending = self.context_proposal
        self.apply_button.configure(state="normal")
        self.restore_button.configure(state="disabled")
        self.proposal_title.set("Восстановлен последний корректный вариант · НЕ применён")
        self.render_current()
        return True

    def undo(self):
        if self.busy or self.voice.busy:
            return
        try:
            mode, current = self.mode.get(), self.snapshot()
            if current.state.get("busy"):
                raise ValueError("Сначала дождитесь остановки станка.")
            self.prepare_history(mode, current.revision)
            context_current = self.history_context()
            if self.mode.get() == "Пробный лист":
                self.sandbox.undo()
            else:
                self.undo_editor()
            context = self.context_histories[mode].undo(context_current) if self.context_histories[mode].past else (
                self.snapshot().state.get('preview_task'),self.snapshot().state.get('preview_anchor'))
            self.restore_history_context(context)
            self.studio_retry = None
            self.schedule_autosave()
            self.append("Последнее изменение эскиза отменено.\n")
        except ValueError as exc:
            self.append(str(exc) + "\n")

    def history_context(self):
        key = (self.mode.get(), self.snapshot().revision)
        task = self.preview_task[2] if self.preview_task is not None and self.preview_task[:2] == key else None
        anchor = self.applied_anchor[2] if self.applied_anchor is not None and self.applied_anchor[:2] == key else None
        if task is None and self.mode.get()=='Эскиз плоттера':
            task,anchor = self.snapshot().state.get('preview_task'),self.snapshot().state.get('preview_anchor')
        return task, anchor

    def prepare_history(self, mode, revision):
        if self.history_revisions.get(mode) != revision:
            self.context_histories[mode].clear()
            self.history_revisions[mode] = revision

    def restore_history_context(self, context):
        mode, revision = self.mode.get(), self.snapshot().revision
        task, anchor = context
        self.refresh()
        self.preview_task = (mode, revision, task) if task is not None else None
        self.applied_anchor = (mode, revision, anchor) if anchor is not None else None
        self.context_scope = (mode, revision)
        self.history_revisions[mode] = revision
        self.mode_contexts[mode] = (revision, context)

    def redo(self):
        if self.busy or self.voice.busy:
            return
        try:
            mode, current = self.mode.get(), self.snapshot()
            if current.state.get("busy"):
                raise ValueError("Сначала дождитесь остановки станка.")
            self.prepare_history(mode, current.revision)
            context_current = self.history_context()
            if mode == "Пробный лист":
                self.sandbox.redo()
            elif self.redo_editor is not None:
                self.redo_editor()
            else:
                raise ValueError("Повтор недоступен в этом редакторе.")
            context = self.context_histories[mode].redo(context_current) if self.context_histories[mode].future else (
                self.snapshot().state.get('preview_task'),self.snapshot().state.get('preview_anchor'))
            self.restore_history_context(context)
            self.studio_retry = None
            self.schedule_autosave()
            self.append("Изменение повторено только в эскизе.\n")
        except ValueError as exc:
            self.append(str(exc) + "\n")

    def save_project(self):
        if self.busy or self.voice.busy or self.pending is not None or self.snapshot().state.get("busy"):
            self.status.set("Сначала завершите запрос и примените или отмените предложение.")
            return
        current = self.snapshot()
        if not current.paths:
            self.status.set("Нет эскиза для сохранения.")
            return
        destination = filedialog.asksaveasfilename(parent=self.root, title="Сохранить проект эскиза",
            defaultextension=".plotter.json", filetypes=[("Проект плоттера", "*.plotter.json")])
        if not destination:
            return
        from editor_project import save_project
        from geometry import Sheet
        try:
            task, anchor = self.history_context()
            save_project(destination, Sheet(current.state['sheet_width'], current.state['sheet_height']),
                         current.paths, current.state.get('file_name', 'Эскиз')[:500], task, anchor)
            self.status.set("Проект сохранён. Координаты станка, COM-порт и история разговора в файл не входят.")
        except (OSError, ValueError) as exc:
            self.status.set("Проект не сохранён: " + str(exc))

    def open_project(self):
        if self.busy or self.voice.busy or self.snapshot().state.get("busy"):
            self.status.set("Сначала дождитесь завершения текущей операции.")
            return
        source = filedialog.askopenfilename(parent=self.root, title="Открыть проект эскиза",
            filetypes=[("Проект плоттера", "*.plotter.json")])
        if not source:
            return
        requested_mode, requested_revision = self.mode.get(), self.snapshot().revision
        from editor_project import load_project
        try:
            project = load_project(source)
            if not messagebox.askokcancel("Открыть проект?", "Текущий эскиз будет заменён. "
                    "Ноль станка не восстанавливается, движения не запускаются.", parent=self.root):
                return
            if self.busy or self.voice.busy or self.snapshot().state.get("busy"):
                raise ValueError("Состояние изменилось во время подтверждения.")
            if self.mode.get() != requested_mode or self.snapshot().revision != requested_revision:
                raise ValueError("Эскиз или режим изменились во время подтверждения. Проект не применён.")
            mode, before = self.mode.get(), self.snapshot()
            self.prepare_history(mode, before.revision)
            context = self.history_context()
            if mode == "Пробный лист":
                self.sandbox.load_project(project)
            elif self.load_editor_project is not None:
                self.load_editor_project(project)
            else:
                raise ValueError("Загрузка в этот редактор недоступна.")
            self.context_histories[mode].push(context)
            self.restore_history_context((project.task, project.anchor))
            self.studio_retry = None
            self.schedule_autosave()
            self.status.set("Проект открыт; текстовый контекст восстановлен." if project.task else
                            "Проект открыт как контуры; достоверного текстового контекста нет.")
        except (OSError, ValueError) as exc:
            self.status.set("Проект не открыт: " + str(exc))

    def close(self):
        if self.closed:
            return
        self.closed = True
        self.cancel.set()
        self.token = None
        self.voice.close()
        if self.studio_panel is not None:
            self.studio_panel.close()
        if self.reference_panel is not None:
            self.reference_panel.close()
        if self.autosaves and self.owns_autosaves:
            self.autosaves.close()
        if self.timer is not None:
            self.root.after_cancel(self.timer)
        self.root.destroy()
