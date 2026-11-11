"""Explicit human review; neither a thumbs-up nor a model response is training."""
from datetime import datetime
from tkinter import filedialog, messagebox, ttk
import tkinter as tk
from tkinter.scrolledtext import ScrolledText

from . import corrections


class CorrectionDialog:
    def __init__(self, parent):
        self.root = tk.Toplevel(parent)
        self.root.title('Проверка исправлений — обучение отдельно')
        self.root.geometry('900x650')
        self.rows = []
        frame = ttk.Frame(self.root, padding=12); frame.pack(fill='both', expand=True)
        ttk.Label(frame, text='Утверждайте только проверенный эталон. Нажатие не обучает модель и не меняет веса.', wraplength=850).pack(anchor='w')
        self.list = tk.Listbox(frame, height=6); self.list.pack(fill='x', pady=8)
        self.list.bind('<<ListboxSelect>>', lambda _: self.select())
        self.original = ScrolledText(frame, height=10, wrap='word', state='disabled'); self.original.pack(fill='both', expand=True)
        ttk.Label(frame, text='Полноценный исправленный ответ, а не описание ошибки:').pack(anchor='w')
        self.reference = ScrolledText(frame, height=6, wrap='word'); self.reference.pack(fill='both', expand=True)
        buttons = ttk.Frame(frame); buttons.pack(fill='x', pady=8)
        ttk.Button(buttons, text='Утвердить эталон', command=lambda: self.review(True)).pack(side='left')
        ttk.Button(buttons, text='Исключить из обучения', command=lambda: self.review(False)).pack(side='left', padx=8)
        ttk.Button(buttons, text='Экспорт train / dev…', command=self.export).pack(side='right')
        self.status = tk.StringVar(); ttk.Label(frame, textvariable=self.status, wraplength=850).pack(anchor='w')
        self.refresh()

    def refresh(self):
        try:
            self.rows = corrections.reviewed_records()
            self.list.delete(0, 'end')
            for record, decision in self.rows:
                label = 'утверждён' if decision and decision.get('approved') else 'исключён' if decision else 'не проверен'
                self.list.insert('end', '['+label+'] '+str(record.get('prompt', ''))[:100])
            self.status.set('Примеров: '+str(len(self.rows))+'. Ничего не обучено автоматически.')
        except (OSError, ValueError) as exc:
            self.status.set(str(exc))

    def select(self):
        selected = self.list.curselection()
        if not selected:
            return
        record, decision = self.rows[selected[0]]
        import json
        self.original.configure(state='normal'); self.original.delete('1.0', 'end')
        self.original.insert('1.0', 'Вопрос: '+str(record.get('prompt', ''))+'\n\nОтвет: '+str(record.get('answer', ''))+
                             '\n\nКонтекст: '+json.dumps(record.get('editor_context', {}), ensure_ascii=False))
        self.original.configure(state='disabled')
        self.reference.delete('1.0', 'end')
        self.reference.insert('1.0', decision.get('reference', '') if decision else record.get('human_correction', ''))

    def review(self, approved):
        selected = self.list.curselection()
        if not selected:
            self.status.set('Выберите пример.'); return
        if approved and not messagebox.askokcancel('Подтвердить проверку?',
            'Вы проверили правильность всего эталонного ответа и его соответствие вопросу/контексту? '
            'Он будет допущен в отдельный набор обучения.', parent=self.root):
            return
        try:
            corrections.review(corrections.digest(self.rows[selected[0]][0]), self.reference.get('1.0', 'end').strip(),
                               approved=approved, consent=True)
            self.refresh()
        except (OSError, ValueError) as exc:
            self.status.set(str(exc))

    def export(self):
        directory = filedialog.askdirectory(parent=self.root, title='Каталог для нового набора проверенных исправлений')
        if not directory:
            return
        from pathlib import Path
        try:
            target = Path(directory)/('reviewed-'+datetime.now().strftime('%Y%m%d-%H%M%S-%f'))
            result = corrections.export(target)
            self.status.set(f"Экспорт: {result['train_count']} train, {result['dev_count']} dev. Веса не менялись. {target}")
        except (OSError, ValueError) as exc:
            self.status.set(str(exc))
