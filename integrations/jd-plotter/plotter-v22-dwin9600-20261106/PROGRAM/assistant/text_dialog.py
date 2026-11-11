"""Human text form; submits one preview request, never motion or G-code."""
import math
from decimal import Decimal
import tkinter as tk
from tkinter import ttk
from tkinter.scrolledtext import ScrolledText


def make_print_command(text, height, spacing=""):
    if not isinstance(text, str) or not text.strip() or len(text) > 800:
        raise ValueError("Введите от 1 до 800 символов текста.")
    if "«" in text or "»" in text:
        raise ValueError("Уберите внутренние «ёлочки»: в этой форме они отделяют текст команды.")
    try:
        height_mm = float(str(height).replace(",", "."))
        spacing_mm = None if str(spacing).strip() == "" else float(str(spacing).replace(",", "."))
    except (ValueError, OverflowError):
        raise ValueError("Высоту и интервал задайте числами в миллиметрах.") from None
    if not math.isfinite(height_mm) or not 1 <= height_mm <= 100:
        raise ValueError("Высота заглавных должна быть от 1 до 100 мм.")
    if spacing_mm is not None and (not math.isfinite(spacing_mm) or not 0 <= spacing_mm <= 100):
        raise ValueError("Интервал должен быть от 0 до 100 мм, либо оставьте поле пустым.")
    # The parser accepts decimal notation, not scientific notation. General
    # float formatting also rounds to six significant digits by default.
    height_text = format(Decimal(str(height_mm)), "f")
    command = f"Напиши печатными «{text}» высотой {height_text} мм"
    if spacing_mm is not None:
        spacing_text = format(Decimal(str(spacing_mm)), "f")
        command += f" с интервалом {spacing_text} мм"
    return command


def open_text_dialog(parent, submit):
    root = tk.Toplevel(parent)
    root.title("Печатная надпись — русский и латиница")
    root.geometry("600x390")
    root.transient(parent)
    box = ttk.Frame(root, padding=14)
    box.pack(fill="both", expand=True)
    ttk.Label(box, text="Текст — можно несколько строк:").pack(anchor="w")
    text = ScrolledText(box, height=6, font=("Segoe UI", 12), wrap="word")
    text.pack(fill="both", expand=True, pady=6)
    text.insert("1.0", "Привет, мир!\nHello, world!")
    settings = ttk.Frame(box)
    settings.pack(fill="x")
    height = tk.StringVar(value="5")
    spacing = tk.StringVar(value="")
    ttk.Label(settings, text="Высота заглавных, мм:").grid(row=0, column=0, sticky="w")
    ttk.Entry(settings, textvariable=height, width=9).grid(row=0, column=1, padx=8)
    ttk.Label(settings, text="Интервал, мм (пусто — авто):").grid(row=1, column=0, sticky="w", pady=5)
    ttk.Entry(settings, textvariable=spacing, width=9).grid(row=1, column=1, padx=8)
    ttk.Label(box, text="Сначала будет предпросмотр. Без подъёма ручки соединения между "
              "буквами и штрихами тоже рисуются. Высота строчных меньше; выносные элементы "
              "могут выступать за номинальную высоту.", wraplength=560).pack(anchor="w", pady=8)
    status = tk.StringVar()
    ttk.Label(box, textvariable=status, wraplength=560, foreground="#9b370e").pack(anchor="w")

    def preview():
        try:
            command = make_print_command(text.get("1.0", "end-1c"), height.get(), spacing.get())
            submit(command)
            root.destroy()
        except ValueError as exc:
            status.set(str(exc))

    ttk.Button(box, text="Подготовить предпросмотр", command=preview).pack(anchor="e", pady=5)
    text.focus_set()
    return root
