"""Measured single-line-font wrapping. Never clips, substitutes or shrinks silently."""
from dataclasses import dataclass
import math
import re

from .text_paths import generate_text_paths, SUPPORTED_CHARACTERS


@dataclass(frozen=True)
class BlockLayout:
    paths: list
    lines: tuple
    used_height: float
    font_height: float


class BlockFitError(ValueError):
    pass


def number(value, lo, hi, label):
    if type(value) not in (int, float) or not math.isfinite(value) or not lo <= value <= hi:
        raise ValueError(f"{label}: допустимо {lo:g}–{hi:g}.")
    return float(value)


def _wrap(text, width, font, spacing):
    result = []
    def measure(line):
        leading = len(line)-len(line.lstrip(' '))
        indent = leading*(.48*font+(font*.18 if spacing is None else spacing))
        return indent+generate_text_paths(line, font, letter_spacing_mm=spacing).width_mm if line.strip() else 0
    for paragraph in text.split('\n'):
        # Spaces are preserved, including repeated spaces; only new wrap boundaries become newlines.
        if not paragraph.strip():
            result.append('')
            continue
        tokens = re.findall(r'\S+| +', paragraph)
        line = ''
        for token in tokens:
            if not token.strip():
                line += token
                continue
            if measure(token) > width + 1e-8:
                raise BlockFitError(f"Слово «{token}» шире блока. Нужна ширина минимум {measure(token):.2f} мм "
                                    "либо меньшие буквы; слово не разрывается и не обрезается.")
            if line.strip() and measure(line + token) > width + 1e-8:
                result.append(line.rstrip(' '))
                line = token
            else:
                line += token
        result.append(line.rstrip(' '))
    if len(result) > 50:
        raise BlockFitError("Получилось больше 50 строк. Увеличьте ширину блока или сократите текст.")
    return result


def _layout(text, width, height, font, spacing, line_spacing, align):
    lines = _wrap(text, width, font, spacing)
    paths, top, bottom = [], math.inf, -math.inf
    for index, line in enumerate(lines):
        if not line.strip():
            continue
        layout = generate_text_paths(line, font, letter_spacing_mm=spacing)
        indent = (len(line)-len(line.lstrip(' ')))*(.48*font+(font*.18 if spacing is None else spacing))
        dx = {'left': indent, 'center': (width - layout.width_mm) / 2, 'right': width - layout.width_mm}[align]
        if dx+layout.width_mm > width+1e-8:
            raise BlockFitError('Отступ и слово не помещаются по ширине блока.')
        dy = index * font * line_spacing
        for path in layout.paths:
            moved = [(x + dx, y + dy) for x, y in path]
            paths.append(moved)
            top = min(top, *(y for _, y in moved))
            bottom = max(bottom, *(y for _, y in moved))
    # Blank leading/trailing lines consume layout space, not invented pen strokes.
    used = max(bottom, (len(lines) - 1) * font * line_spacing + font)
    if used > height + 1e-8:
        raise BlockFitError(f"Нужно {used:.2f} мм высоты для {len(lines)} строк, доступно {height:g} мм.")
    if sum(len(path) for path in paths) > 50_000:
        raise ValueError("Больше 50 000 точек в блоке.")
    return BlockLayout(paths, tuple(lines), used, font)


def layout_block(text, width_mm, height_mm, font_height_mm=5, spacing_mm=None, line_spacing=1.6, align='left'):
    if not isinstance(text, str) or not 1 <= len(text) <= 800 or not text.strip():
        raise ValueError("Нужен непустой текст до 800 знаков.")
    text = text.replace('\r\n', '\n').replace('\r', '\n')
    import unicodedata
    text = unicodedata.normalize('NFC', text)
    unknown = set(text) - SUPPORTED_CHARACTERS
    if unknown:
        raise ValueError("Нет печатных символов: " + ', '.join(repr(c) for c in sorted(unknown)[:6]))
    width = number(width_mm, 1, 3000, 'Ширина блока, мм')
    height = number(height_mm, 1, 3000, 'Высота блока, мм')
    font = number(font_height_mm, 1, 100, 'Высота заглавных, мм')
    if spacing_mm is not None:
        spacing_mm = number(spacing_mm, 0, 100, 'Межбуквенный интервал, мм')
    pitch = number(line_spacing, 1.4, 5, 'Межстрочный интервал в высотах заглавных')
    if align not in {'left', 'center', 'right'}:
        raise ValueError("Выравнивание: left, center или right.")
    try:
        return _layout(text, width, height, font, spacing_mm, pitch, align)
    except BlockFitError as original:
        # Give an actually tested alternative, not an arbitrary guessed percentage.
        proposed = None
        lo, hi = 1.0, font
        try:
            _layout(text, width, height, lo, spacing_mm, pitch, align)
            for _ in range(15):
                mid = (lo + hi) / 2
                try:
                    _layout(text, width, height, mid, spacing_mm, pitch, align)
                    lo = mid
                except BlockFitError:
                    hi = mid
            proposed = math.floor(lo * 100) / 100
        except BlockFitError:
            pass
        suggestion = (f" Можно явно выбрать высоту заглавных {proposed:g} мм." if proposed else
                      " Даже 1 мм не помещается: увеличьте прямоугольник или сократите текст.")
        raise BlockFitError(str(original) + suggestion + " Автоматического уменьшения нет.") from None
