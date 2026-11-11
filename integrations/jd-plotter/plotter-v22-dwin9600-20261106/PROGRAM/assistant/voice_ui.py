"""Explicit, reviewed voice dictation for Tk editors; never submits a command."""
from __future__ import annotations

import queue
import threading
import tkinter as tk
from tkinter import messagebox, ttk
from tkinter.scrolledtext import ScrolledText


MAX_INPUT_CHARS = 1800
PROFILES = {"Баланс": "balanced", "Точнее": "quality"}


def review_insertion(existing: str, recognised: str, mode: str) -> str:
    """Keep recognised numbers, units, spelling and case exactly as reviewed."""
    text = recognised.strip()
    if not text:
        raise ValueError("Текст пуст. Повторите запись или впишите команду.")
    if mode == "replace":
        result = text
    elif mode == "append":
        before = existing.rstrip()
        result = before + ("\n" if before else "") + text
    else:
        raise ValueError("Неизвестный способ вставки текста.")
    if len(result) > MAX_INPUT_CHARS:
        raise ValueError(f"Сократите итоговый текст до {MAX_INPUT_CHARS} знаков.")
    return result


class VoiceControls(ttk.Frame):
    """Reusable recorder controls with a draft-review boundary.

    ``entry`` is a Tk Text-compatible editor. All callbacks and editor changes
    run on the Tk thread. Device discovery only enumerates; the constructor
    never starts a microphone, recogniser or command. ``on_busy`` lets an owner
    block ordinary submission while a recording or its review is open.
    """

    def __init__(self, parent, entry, *, on_recording=None, on_busy=None,
                 can_record=None, service_factory=None):
        super().__init__(parent, padding=(0, 6))
        if service_factory is None:
            from .voice import VoiceService
            service_factory = VoiceService
        self.entry = entry
        self.on_recording = on_recording
        self.on_busy = on_busy
        self.can_record = can_record
        self.events = queue.Queue()
        self.device_events = queue.Queue()
        self.service = service_factory(self.events)
        self.closed = False
        self._operation_active = False
        self._cancelled = False
        self._last_busy = False
        self._before = ""
        self._review = None
        self._device_epoch = 0
        self._device_map = {"Системный микрофон": None}
        self._timer = None
        self.device_name = tk.StringVar(value="Системный микрофон")
        self.profile_name = tk.StringVar(value="Точнее")
        self.status = tk.StringVar(value="Запись только по кнопке · до 30 с · текст проверяется перед отправкой.")
        self.level = tk.DoubleVar(value=0)
        line = ttk.Frame(self)
        line.pack(fill="x")
        ttk.Label(line, text="Микрофон:").pack(side="left")
        self.devices_input = ttk.Combobox(line, textvariable=self.device_name, state="readonly",
                                        values=tuple(self._device_map), width=27)
        self.devices_input.pack(side="left", padx=(5, 4))
        self.refresh_button = ttk.Button(line, text="Обновить", command=self.refresh_devices)
        self.refresh_button.pack(side="left")
        ttk.Label(line, text="Режим:").pack(side="left", padx=(9, 0))
        self.profile_input = ttk.Combobox(line, textvariable=self.profile_name, state="readonly",
                                        values=tuple(PROFILES), width=12)
        self.profile_input.pack(side="left", padx=5)
        buttons = ttk.Frame(self)
        buttons.pack(fill="x", pady=(5, 0))
        self.record_button = ttk.Button(buttons, text="Записать голос", command=self.toggle_record)
        self.record_button.pack(side="left")
        self.cancel_button = ttk.Button(buttons, text="Отменить запись", command=self.cancel, state="disabled")
        self.cancel_button.pack(side="left", padx=5)
        ttk.Label(buttons, text="Ctrl + пробел — запись / стоп · Esc — отмена").pack(side="left", padx=5)
        meter = ttk.Frame(self)
        meter.pack(fill="x", pady=(4, 0))
        ttk.Progressbar(meter, variable=self.level, maximum=1.0, length=90).pack(side="left", padx=(0, 7))
        self.status_label = ttk.Label(meter, textvariable=self.status, wraplength=650)
        self.status_label.pack(side="left", fill="x", expand=True)
        meter.bind("<Configure>", lambda event: self.status_label.configure(wraplength=max(220, event.width - 105)))
        self.refresh_devices()
        self._timer = self.after(60, self._pump)
        self._key_owner = self.winfo_toplevel()
        self._record_binding = self._key_owner.bind("<Control-space>", self._record_shortcut, add="+")
        self._cancel_binding = self._key_owner.bind("<Escape>", self._cancel_shortcut, add="+")

    @property
    def busy(self):
        return not self.closed and (self._operation_active or self._review is not None)

    def _notify_busy(self):
        value = self.busy
        if value != self._last_busy:
            self._last_busy = value
            if self.on_busy is not None:
                self.on_busy(value)

    def _record_shortcut(self, event=None):
        self.toggle_record()
        return "break"

    def _cancel_shortcut(self, event=None):
        if self.busy:
            self.cancel()
            return "break"
        return None

    def refresh_devices(self):
        if self.closed or self.busy:
            return
        self._device_epoch += 1
        epoch = self._device_epoch
        service, events = self.service, self.device_events
        self.refresh_button.configure(state="disabled")
        self.status.set("Ищу доступные микрофоны… Запись ещё не включена.")

        def worker():
            try:
                result = ("devices", service.devices())
            except Exception as exc:
                result = ("error", str(exc))
            events.put((epoch, *result))

        threading.Thread(target=worker, daemon=True).start()

    def toggle_record(self):
        if self.closed:
            return
        if self._operation_active:
            if self.service.recording and not self._cancelled:
                self.service.stop()
                self.status.set("Запись завершена. Распознаю речь…")
                self.record_button.configure(state="disabled")
            return
        if self._review is not None:
            self._review.lift()
            return
        if self.can_record is not None and not self.can_record():
            self.status.set("Сначала дождитесь ответа помощника или отмените запрос.")
            return
        self._before = self.entry.get("1.0", "end-1c")
        self._cancelled = False
        self._operation_active = True
        self._notify_busy()
        if self.on_recording is not None:
            self.on_recording()
        try:
            self.service.start(device=self._device_map.get(self.device_name.get()),
                               profile=PROFILES[self.profile_name.get()])
        except Exception as exc:
            self._operation_active = False
            self.status.set("Не удалось начать запись: " + str(exc))
            self._notify_busy()
        self._refresh_buttons()

    def cancel(self):
        if self.closed:
            return
        self._cancelled = True
        self.service.cancel()
        self._close_review()
        self.level.set(0)
        self.status.set("Голосовой ввод отменён. Текст не отправлен и не заменён.")
        # Do not admit another recording until ``finished``: a cancelled
        # worker can still be releasing its device or model in the background.
        self._refresh_buttons()
        self._notify_busy()

    def _refresh_buttons(self):
        if self.closed:
            return
        active = self._operation_active
        recording = active and not self._cancelled and self.service.recording
        self.record_button.configure(text="Закончить запись" if recording else
                                     "Распознаётся…" if active else "Записать голос",
                                     state="normal" if recording or not self.busy else "disabled")
        self.cancel_button.configure(state="normal" if self.busy else "disabled")
        self.devices_input.configure(state="disabled" if self.busy else "readonly")
        self.profile_input.configure(state="disabled" if self.busy else "readonly")
        self.refresh_button.configure(state="disabled" if self.busy else "normal")

    def _show_review(self, transcript):
        if self.closed or self._cancelled or not self._operation_active:
            return
        self._close_review()
        dialog = self._review = tk.Toplevel(self)
        dialog.title("Проверьте распознанную команду")
        dialog.transient(self.winfo_toplevel())
        dialog.geometry("660x330")
        dialog.minsize(520, 280)
        outer = ttk.Frame(dialog, padding=12)
        outer.pack(fill="both", expand=True)
        ttk.Label(outer, text="Проверьте особенно числа, единицы, названия и слово, которое нужно написать.",
                  wraplength=620).pack(anchor="w")
        warnings = tuple(getattr(transcript, "warnings", ()))
        if warnings:
            ttk.Label(outer, text="\n".join(str(value) for value in warnings),
                      foreground="#984b00", wraplength=620).pack(anchor="w", pady=5)
        recognised = ScrolledText(outer, height=5, wrap="word", font=("Segoe UI", 11))
        recognised.pack(fill="both", expand=True, pady=8)
        recognised.insert("1.0", transcript.text)
        ttk.Label(outer, text="Вставка меняет только поле ввода. После проверки нажмите обычную кнопку «Отправить».",
                  wraplength=620, foreground="#984b00").pack(anchor="w")
        row = ttk.Frame(outer)
        row.pack(fill="x", pady=(8, 0))

        def insert(mode):
            if self.closed or self._cancelled or self._review is not dialog:
                return
            current = self.entry.get("1.0", "end-1c")
            if mode == "replace" and current != self._before:
                if not messagebox.askokcancel("Поле ввода изменилось",
                        "Пока шло распознавание, вы изменили текст. Заменить его проверенной записью?\n"
                        "Можно отменить замену и выбрать «Добавить к тексту».", parent=dialog):
                    return
            try:
                result = review_insertion(current, recognised.get("1.0", "end-1c"), mode)
            except ValueError as exc:
                messagebox.showwarning("Текст не вставлен", str(exc), parent=dialog)
                return
            self.entry.delete("1.0", "end")
            self.entry.insert("1.0", result)
            self.entry.focus_set()
            self.status.set("Текст вставлен. Проверьте его и нажмите «Отправить», когда будете готовы.")
            self._close_review()
            self._notify_busy()
            self._refresh_buttons()

        ttk.Button(row, text="Заменить поле ввода", command=lambda: insert("replace")).pack(side="left")
        ttk.Button(row, text="Добавить к тексту", command=lambda: insert("append")).pack(side="left", padx=6)
        ttk.Button(row, text="Не использовать", command=self.cancel).pack(side="right")
        dialog.protocol("WM_DELETE_WINDOW", self.cancel)
        # The review is a separate Toplevel; the main window's Escape binding
        # is not part of its bindtags, so bind cancellation here as well.
        dialog.bind("<Escape>", self._cancel_shortcut)
        recognised.focus_set()
        self.status.set("Речь распознана. Проверьте текст в отдельном окне; отправки ещё не было.")
        self._notify_busy()

    def _close_review(self):
        dialog, self._review = self._review, None
        if dialog is not None:
            try:
                dialog.destroy()
            except tk.TclError:
                pass

    def _pump(self):
        if self.closed:
            return
        for _ in range(30):
            try:
                epoch, kind, payload = self.device_events.get_nowait()
            except queue.Empty:
                break
            if epoch != self._device_epoch:
                continue
            if kind == "devices":
                choices = {"Системный микрофон": None}
                for device in payload:
                    label = f"{device.index}: {device.name}" + (" • по умолчанию" if device.is_default else "")
                    choices[label] = device.index
                self._device_map = choices
                self.devices_input.configure(values=tuple(choices))
                if self.device_name.get() not in choices:
                    self.device_name.set("Системный микрофон")
                if not self.busy:
                    self.status.set("Голос готов. Нажмите «Записать голос», затем «Закончить запись».")
            elif not self.busy:
                self.status.set("Не удалось найти микрофоны: " + payload)
            self.refresh_button.configure(state="disabled" if self.busy else "normal")
        for _ in range(100):
            try:
                kind, payload = self.events.get_nowait()
            except queue.Empty:
                break
            if not self._operation_active:
                continue
            if kind == "finished":
                self._operation_active = False
                self.level.set(0)
                self._notify_busy()
            elif self._cancelled:
                continue
            elif kind == "state":
                self.status.set(str(payload))
            elif kind == "level":
                try:
                    self.level.set(max(0.0, min(1.0, float(payload))))
                except (TypeError, ValueError):
                    pass
            elif kind == "result":
                self._show_review(payload)
            elif kind == "error":
                self.status.set("Голосовой ввод не завершён: " + str(payload))
        self._refresh_buttons()
        self._timer = self.after(60, self._pump)

    def close(self):
        if self.closed:
            return
        self.closed = True
        self._cancelled = True
        self._operation_active = False
        self._device_epoch += 1
        if self._timer is not None:
            try:
                self.after_cancel(self._timer)
            except tk.TclError:
                pass
        self._close_review()
        self.service.close()
        for key, binding in (("<Control-space>", self._record_binding), ("<Escape>", self._cancel_binding)):
            if binding:
                try:
                    self._key_owner.unbind(key, binding)
                except tk.TclError:
                    pass
