"""Original single-line print font: Latin, Cyrillic, digits and punctuation.

Font geometry was designed for this project; no downloaded fonts or model output
are used. The original vector data is provided under CC0-1.0. Each stroke is an
open or closed polyline, not the outline of a filled typeface. The caller must
check paper bounds and obtain confirmation; this module never moves equipment.

height_mm means nominal uppercase/capital height. Lowercase x-height is 0.7 of
that height; accents and descenders can increase the actual ink bounding box.
Disconnected strokes remain disconnected. Without a pen lift, their connecting
moves will also be drawn by the existing controller.
"""
from __future__ import annotations

from dataclasses import dataclass
import math
import unicodedata


FONT_NAME = "Plotter Print v1"
FONT_LICENSE = "CC0-1.0; original vector geometry created for this project"
MAX_CHARACTERS = 1000
MAX_LINES = 50
MAX_POINTS = 50_000


@dataclass(frozen=True)
class Glyph:
    advance: float
    strokes: tuple


@dataclass(frozen=True)
class TextLayout:
    paths: list
    bbox: tuple[float, float, float, float]
    width_mm: float
    height_mm: float
    advance_width_mm: float
    line_count: int
    character_count: int
    cap_height_mm: float
    font_name: str = FONT_NAME


def _glyph(advance, *strokes):
    return Glyph(advance, tuple(tuple((float(x), float(y)) for x, y in path) for path in strokes))


def _arc(cx, cy, rx, ry, start, end, steps=16):
    return tuple((cx + rx * math.cos(math.radians(start + (end - start) * i / steps)),
                  cy + ry * math.sin(math.radians(start + (end - start) * i / steps)))
                 for i in range(steps + 1))


def _oval(cx, cy, rx, ry):
    return _arc(cx, cy, rx, ry, 0, 360, 20)


def _lower_cap(glyph):
    """Actual Cyrillic small-cap-shaped print letters, not Latin substitution."""
    return _glyph(glyph.advance * 0.78,
                  *[[(x * 0.78, 0.3 + y * 0.7) for x, y in path] for path in glyph.strokes])


_CAPITALS = {
    "A": _glyph(.78, [(0, 1), (.35, 0), (.7, 1)], [(.14, .6), (.56, .6)]),
    "B": _glyph(.75, [(0, 1), (0, 0)], [(0, 0), (.46, 0), (.62, .12), (.64, .3), (.52, .47), (0, .48)],
                [(0, .48), (.48, .48), (.65, .6), (.65, .86), (.5, 1), (0, 1)]),
    "C": _glyph(.78, _arc(.35, .5, .35, .5, -45, -315)),
    "D": _glyph(.78, [(0, 1), (0, 0), (.36, 0), (.6, .12), (.7, .35), (.7, .65), (.6, .88), (.36, 1), (0, 1)]),
    "E": _glyph(.7, [(.62, 0), (0, 0), (0, 1), (.62, 1)], [(0, .48), (.5, .48)]),
    "F": _glyph(.7, [(0, 1), (0, 0), (.62, 0)], [(0, .48), (.5, .48)]),
    "G": _glyph(.82, _arc(.35, .5, .35, .5, -45, -315), [(.58, .86), (.7, .7), (.7, .52), (.4, .52)]),
    "H": _glyph(.78, [(0, 0), (0, 1)], [(.7, 0), (.7, 1)], [(0, .5), (.7, .5)]),
    "I": _glyph(.48, [(0, 0), (.4, 0)], [(.2, 0), (.2, 1)], [(0, 1), (.4, 1)]),
    "J": _glyph(.7, [(0, 0), (.62, 0), (.62, .76), (.55, .94), (.39, 1), (.16, 1), (0, .85)]),
    "K": _glyph(.78, [(0, 0), (0, 1)], [(.67, 0), (0, .55), (.7, 1)]),
    "L": _glyph(.68, [(0, 0), (0, 1), (.6, 1)]),
    "M": _glyph(.95, [(0, 1), (0, 0), (.43, .6), (.87, 0), (.87, 1)]),
    "N": _glyph(.78, [(0, 1), (0, 0), (.7, 1), (.7, 0)]),
    "O": _glyph(.78, _oval(.35, .5, .35, .5)),
    "P": _glyph(.75, [(0, 1), (0, 0), (.45, 0), (.63, .13), (.65, .35), (.52, .5), (0, .5)]),
    "Q": _glyph(.83, _oval(.35, .5, .35, .5), [(.4, .75), (.76, 1.1)]),
    "R": _glyph(.78, [(0, 1), (0, 0), (.45, 0), (.63, .13), (.65, .35), (.52, .5), (0, .5)], [(.32, .5), (.7, 1)]),
    "S": _glyph(.74, [(.63, .1), (.47, 0), (.21, 0), (.05, .13), (0, .3), (.12, .45), (.5, .57),
                     (.66, .73), (.62, .9), (.46, 1), (.19, 1), (.02, .9)]),
    "T": _glyph(.8, [(0, 0), (.72, 0)], [(.36, 0), (.36, 1)]),
    "U": _glyph(.78, [(0, 0), (0, .75), (.12, .94), (.35, 1), (.58, .94), (.7, .75), (.7, 0)]),
    "V": _glyph(.78, [(0, 0), (.35, 1), (.7, 0)]),
    "W": _glyph(1.08, [(0, 0), (.22, 1), (.5, .38), (.78, 1), (1, 0)]),
    "X": _glyph(.78, [(0, 0), (.7, 1)], [(.7, 0), (0, 1)]),
    "Y": _glyph(.78, [(0, 0), (.35, .5), (.7, 0)], [(.35, .5), (.35, 1)]),
    "Z": _glyph(.78, [(0, 0), (.7, 0), (0, 1), (.7, 1)]),
}

_LOWER = {
    "a": _glyph(.65, _oval(.275, .65, .275, .35), [(.55, .3), (.55, 1), (.6, 1)]),
    "b": _glyph(.64, [(0, 0), (0, 1)], _oval(.28, .65, .28, .35)),
    "c": _glyph(.62, _arc(.28, .65, .28, .35, -45, -315)),
    "d": _glyph(.64, _oval(.28, .65, .28, .35), [(.56, 0), (.56, 1)]),
    "e": _glyph(.64, [(0, .65), (.55, .65), (.52, .48), (.4, .34), (.22, .3), (.09, .38),
                     (.01, .53), (0, .7), (.06, .88), (.2, .99), (.38, 1), (.54, .92)]),
    "f": _glyph(.6, [(.25, 1), (.25, .18), (.31, .05), (.43, 0), (.52, .04)], [(0, .35), (.52, .35)]),
    "g": _glyph(.64, _oval(.28, .65, .28, .35), [(.56, .3), (.56, 1.08), (.46, 1.24), (.25, 1.28), (.05, 1.17)]),
    "h": _glyph(.64, [(0, 0), (0, 1)], [(0, .65), (.1, .38), (.29, .3), (.49, .38), (.56, .55), (.56, 1)]),
    "i": _glyph(.28, [(.1, .3), (.1, 1)], [(.1, .09), (.1, .14)]),
    "j": _glyph(.36, [(.25, .3), (.25, 1.08), (.16, 1.26), (.01, 1.26)], [(.25, .09), (.25, .14)]),
    "k": _glyph(.64, [(0, 0), (0, 1)], [(.53, .3), (0, .72), (.56, 1)]),
    "l": _glyph(.33, [(.05, 0), (.05, .9), (.13, 1), (.25, 1)]),
    "m": _glyph(.82, [(0, .3), (0, 1)], [(0, .63), (.07, .4), (.19, .3), (.31, .4), (.33, 1)],
                [(.33, .61), (.43, .4), (.58, .3), (.73, .42), (.74, 1)]),
    "n": _glyph(.64, [(0, .3), (0, 1)], [(0, .6), (.12, .38), (.3, .3), (.48, .4), (.56, .58), (.56, 1)]),
    "o": _glyph(.64, _oval(.28, .65, .28, .35)),
    "p": _glyph(.64, [(0, .3), (0, 1.28)], _oval(.28, .65, .28, .35)),
    "q": _glyph(.64, _oval(.28, .65, .28, .35), [(.56, .3), (.56, 1.28)]),
    "r": _glyph(.54, [(0, .3), (0, 1)], [(0, .62), (.11, .4), (.26, .3), (.46, .35)]),
    "s": _glyph(.58, [(.48, .39), (.36, .3), (.17, .3), (.05, .4), (.04, .53), (.15, .63),
                     (.37, .69), (.5, .81), (.47, .94), (.33, 1), (.14, 1), (.02, .92)]),
    "t": _glyph(.6, [(.25, .03), (.25, .83), (.31, .98), (.46, 1)], [(0, .3), (.52, .3)]),
    "u": _glyph(.64, [(0, .3), (0, .83), (.12, .97), (.29, 1), (.48, .88), (.56, .65)], [(.56, .3), (.56, 1)]),
    "v": _glyph(.64, [(0, .3), (.28, 1), (.56, .3)]),
    "w": _glyph(.88, [(0, .3), (.18, 1), (.4, .48), (.62, 1), (.8, .3)]),
    "x": _glyph(.64, [(0, .3), (.56, 1)], [(.56, .3), (0, 1)]),
    "y": _glyph(.64, [(0, .3), (.28, .86), (.56, .3)], [(.28, .86), (.17, 1.18), (.07, 1.26), (0, 1.23)]),
    "z": _glyph(.64, [(0, .3), (.56, .3), (0, 1), (.56, 1)]),
}

_CYRILLIC = {
    "А": _CAPITALS["A"],
    "Б": _glyph(.76, [(0, 1), (0, 0), (.65, 0)], [(0, .43), (.44, .43), (.63, .55), (.67, .8), (.53, .98), (0, 1)]),
    "В": _CAPITALS["B"], "Г": _glyph(.73, [(0, 1), (0, 0), (.65, 0)]),
    "Д": _glyph(.9, [(.12, 1), (.28, 0), (.56, 0), (.72, 1)], [(0, 1.2), (0, 1), (.82, 1), (.82, 1.2)]),
    "Е": _CAPITALS["E"],
    "Ё": _glyph(.7, *_CAPITALS["E"].strokes, [(.18, -.2), (.18, -.14)], [(.5, -.2), (.5, -.14)]),
    "Ж": _glyph(1.08, [(.5, 0), (.5, 1)], [(0, 0), (.5, .5), (1, 0)], [(0, 1), (.5, .5), (1, 1)]),
    "З": _glyph(.75, [(0, .12), (.17, 0), (.45, 0), (.63, .13), (.65, .33), (.5, .47), (.28, .5),
                     (.5, .53), (.67, .7), (.65, .9), (.48, 1), (.17, 1), (0, .88)]),
    "И": _glyph(.76, [(0, 0), (0, 1), (.68, 0), (.68, 1)]),
    "Й": _glyph(.76, [(0, 0), (0, 1), (.68, 0), (.68, 1)], [(.17, -.2), (.25, -.1), (.43, -.1), (.51, -.2)]),
    "К": _CAPITALS["K"],
    "Л": _glyph(.75, [(0, 1), (.15, .9), (.28, 0), (.67, 0), (.67, 1)]),
    "М": _CAPITALS["M"], "Н": _CAPITALS["H"], "О": _CAPITALS["O"],
    "П": _glyph(.76, [(0, 1), (0, 0), (.68, 0), (.68, 1)]),
    "Р": _CAPITALS["P"], "С": _CAPITALS["C"], "Т": _CAPITALS["T"],
    "У": _glyph(.76, [(0, 0), (.34, .6), (.68, 0)], [(.34, .6), (.2, .93), (.1, 1), (0, .97)]),
    "Ф": _glyph(.88, _oval(.4, .5, .4, .3), [(.4, 0), (.4, 1)]),
    "Х": _CAPITALS["X"],
    "Ц": _glyph(.85, [(0, 0), (0, 1), (.65, 1), (.65, 0)], [(.65, 1), (.77, 1), (.77, 1.2)]),
    "Ч": _glyph(.76, [(0, 0), (0, .35), (.1, .48), (.3, .5), (.68, .4)], [(.68, 0), (.68, 1)]),
    "Ш": _glyph(.88, [(0, 0), (0, 1), (.8, 1), (.8, 0)], [(.4, 0), (.4, 1)]),
    "Щ": _glyph(1.0, [(0, 0), (0, 1), (.8, 1), (.8, 0)], [(.4, 0), (.4, 1)], [(.8, 1), (.92, 1), (.92, 1.2)]),
    "Ъ": _glyph(.8, [(0, 0), (.22, 0), (.22, 1)], [(.22, .45), (.5, .45), (.68, .55), (.72, .8), (.59, .98), (.22, 1)]),
    "Ы": _glyph(.86, [(0, 0), (0, 1)], [(0, .45), (.3, .45), (.48, .57), (.52, .81), (.38, .97), (0, 1)], [(.78, 0), (.78, 1)]),
    "Ь": _glyph(.6, [(0, 0), (0, 1)], [(0, .45), (.3, .45), (.48, .57), (.52, .81), (.38, .97), (0, 1)]),
    "Э": _glyph(.76, [(.05, .1), (.2, 0), (.45, 0), (.62, .15), (.68, .5), (.62, .85), (.45, 1), (.2, 1), (.05, .9)], [(.25, .5), (.68, .5)]),
    "Ю": _glyph(1.08, [(0, 0), (0, 1)], [(0, .5), (.3, .5)], _oval(.65, .5, .35, .5)),
    "Я": _glyph(.76, [(.68, .5), (.2, .5), (.04, .4), (0, .18), (.15, 0), (.68, 0), (.68, 1)], [(.32, .5), (0, 1)]),
}

# Russian print lowercase letters with true x-height, plus genuine ascender and
# descender forms where needed. Shared shapes below are actually identical in
# these alphabets; Ж/Я/Ю/Ф/Щ/Ы are always drawn using Cyrillic geometry.
_CYRILLIC_LOWER = {letter.lower(): _lower_cap(glyph) for letter, glyph in _CYRILLIC.items()}
_CYRILLIC_LOWER.update({
    "а": _LOWER["a"], "б": _glyph(.64, [(.6, 0), (.3, .07), (.13, .18), (.02, .4), (0, .7)], _oval(.28, .68, .28, .32)),
    "е": _LOWER["e"], "ё": _glyph(.64, *_LOWER["e"].strokes, [(.15, .09), (.15, .15)], [(.43, .09), (.43, .15)]),
    "о": _LOWER["o"], "р": _LOWER["p"], "с": _LOWER["c"], "у": _LOWER["y"], "х": _LOWER["x"],
    "ф": _glyph(.8, _oval(.36, .65, .36, .35), [(.36, 0), (.36, 1.28)]),
})

_DIGITS = {
    "0": _glyph(.78, _oval(.35, .5, .35, .5), [(.14, .75), (.56, .25)]),
    "1": _glyph(.62, [(.1, .17), (.3, 0), (.3, 1)], [(.1, 1), (.54, 1)]),
    "2": _glyph(.76, [(0, .15), (.15, 0), (.44, 0), (.64, .13), (.67, .34), (.55, .52), (0, 1), (.68, 1)]),
    "3": _CYRILLIC["З"],
    "4": _glyph(.78, [(.55, 1), (.55, 0), (.05, .7), (.7, .7)]),
    "5": _glyph(.76, [(.65, 0), (0, 0), (0, .45), (.4, .45), (.62, .56), (.68, .75), (.6, .94), (.43, 1), (.17, 1), (0, .88)]),
    "6": _glyph(.7, [(.6, .05), (.4, 0), (.2, .1), (.06, .32), (0, .7)], _oval(.3, .7, .3, .3)),
    "7": _glyph(.78, [(0, 0), (.7, 0), (.18, 1)]),
    "8": _glyph(.78, _oval(.35, .25, .3, .25), _oval(.35, .75, .35, .25)),
    "9": _glyph(.7, _oval(.3, .3, .3, .3), [(.6, .3), (.6, .75), (.43, .97), (.22, 1), (.03, .9)]),
}

_PUNCTUATION = {
    ".": _glyph(.25, [(.07, .95), (.07, 1)]),
    ",": _glyph(.28, [(.14, .95), (.14, 1), (.04, 1.13)]),
    ":": _glyph(.25, [(.07, .4), (.07, .45)], [(.07, .95), (.07, 1)]),
    ";": _glyph(.28, [(.14, .4), (.14, .45)], [(.14, .95), (.14, 1), (.04, 1.13)]),
    "!": _glyph(.28, [(.1, 0), (.1, .72)], [(.1, .95), (.1, 1)]),
    "?": _glyph(.64, [(0, .15), (.14, .01), (.36, 0), (.54, .14), (.56, .33), (.4, .5), (.28, .58), (.28, .72)], [(.28, .95), (.28, 1)]),
    "-": _glyph(.52, [(0, .6), (.44, .6)]),
    "–": _glyph(.8, [(0, .6), (.72, .6)]),
    "—": _glyph(1.08, [(0, .6), (1, .6)]),
    "_": _glyph(.8, [(0, 1.08), (.72, 1.08)]),
    "'": _glyph(.23, [(.13, 0), (.05, .22)]),
    '"': _glyph(.43, [(.13, 0), (.05, .22)], [(.33, 0), (.25, .22)]),
    "«": _glyph(.66, [(.28, .3), (0, .6), (.28, .9)], [(.58, .3), (.3, .6), (.58, .9)]),
    "»": _glyph(.66, [(0, .3), (.28, .6), (0, .9)], [(.3, .3), (.58, .6), (.3, .9)]),
    "(": _glyph(.4, [(.3, 0), (.12, .2), (.02, .5), (.12, .8), (.3, 1)]),
    ")": _glyph(.4, [(0, 0), (.18, .2), (.28, .5), (.18, .8), (0, 1)]),
    "[": _glyph(.4, [(.3, 0), (0, 0), (0, 1), (.3, 1)]),
    "]": _glyph(.4, [(0, 0), (.3, 0), (.3, 1), (0, 1)]),
    "{": _glyph(.48, [(.4, 0), (.24, .08), (.24, .36), (.14, .47), (0, .5), (.14, .53), (.24, .64), (.24, .92), (.4, 1)]),
    "}": _glyph(.48, [(0, 0), (.16, .08), (.16, .36), (.26, .47), (.4, .5), (.26, .53), (.16, .64), (.16, .92), (0, 1)]),
    "/": _glyph(.6, [(0, 1.08), (.52, -.08)]),
    "\\": _glyph(.6, [(0, -.08), (.52, 1.08)]),
    "+": _glyph(.7, [(0, .6), (.62, .6)], [(.31, .29), (.31, .91)]),
    "=": _glyph(.7, [(0, .45), (.62, .45)], [(0, .75), (.62, .75)]),
    "%": _glyph(.84, _oval(.15, .18, .13, .18), [(.04, 1), (.72, 0)], _oval(.6, .82, .13, .18)),
    "#": _glyph(.82, [(.24, 0), (.08, 1)], [(.67, 0), (.51, 1)], [(0, .33), (.74, .33)], [(0, .7), (.74, .7)]),
    "*": _glyph(.7, [(.31, .15), (.31, .77)], [(0, .3), (.62, .63)], [(0, .63), (.62, .3)]),
    "&": _glyph(.86, [(.76, 1), (.12, .28), (.1, .12), (.24, 0), (.42, .02), (.5, .17), (.44, .32),
                     (.1, .6), (0, .78), (.08, .95), (.28, 1), (.48, .94), (.68, .74), (.76, .54)]),
    "@": _glyph(1.1, _oval(.5, .58, .23, .3), [(.73, .28), (.73, .82), (.88, .86), (1, .7),
                   (1, .34), (.85, .07), (.53, 0), (.19, .1), (0, .36), (0, .74), (.18, .96), (.51, 1), (.77, .95)]),
    "$": _glyph(.78, *_CAPITALS["S"].strokes, [(.33, -.12), (.33, 1.12)]),
    "№": _glyph(1.12, [(0, 1), (0, 0), (.55, 1), (.55, 0)], _oval(.87, .2, .16, .2), [(.71, .52), (1.03, .52)]),
    "…": _glyph(.7, [(.05, .95), (.05, 1)], [(.31, .95), (.31, 1)], [(.57, .95), (.57, 1)]),
}

_FONT = {**_CAPITALS, **_LOWER, **_CYRILLIC, **_CYRILLIC_LOWER, **_DIGITS, **_PUNCTUATION}
SUPPORTED_CHARACTERS = frozenset(_FONT) | {" ", "\n"}


def _finite(value, label):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{label}: нужно число.")
    try:
        result = float(value)
    except (OverflowError, ValueError):
        raise ValueError(f"{label}: число слишком большое.") from None
    if not math.isfinite(result):
        raise ValueError(f"{label}: число должно быть конечным.")
    return result


def generate_text_paths(text: str, height_mm: float = 5, *, origin=(0, 0),
                        letter_spacing_mm=None, line_spacing=1.6, monospace=False) -> TextLayout:
    """Construct a deterministic layout; no scaling-to-fit or glyph substitution.

    origin is the top-left of the actual ink bbox. Letter spacing is added to
    glyph advances, or defaults to 0.18 of capital height. line_spacing is the
    baseline pitch in capital heights (1.4..5). Proportional advances are used
    by default. Supported canonical decomposed Cyrillic Ё/Й normalize with NFC.
    Blank lines and spaces affect relative layout, but never produce fake lines.
    """
    if not isinstance(text, str) or not text or len(text) > MAX_CHARACTERS:
        raise ValueError(f"Нужен текст от 1 до {MAX_CHARACTERS} символов.")
    text = unicodedata.normalize("NFC", text.replace("\r\n", "\n").replace("\r", "\n"))
    unknown = sorted(set(text) - SUPPORTED_CHARACTERS)
    if unknown:
        shown = ", ".join(f"{char!r} (U+{ord(char):04X})" for char in unknown[:6])
        raise ValueError("В гарнитуре нет символов: " + shown + ". Поддерживаются Latin, кириллица, цифры и основная пунктуация.")
    lines = text.split("\n")
    if len(lines) > MAX_LINES:
        raise ValueError(f"Максимум {MAX_LINES} строк.")
    cap_height = _finite(height_mm, "Высота заглавных")
    if not 1 <= cap_height <= 100:
        raise ValueError("Высота заглавных должна быть от 1 до 100 мм.")
    spacing = cap_height * .18 if letter_spacing_mm is None else _finite(letter_spacing_mm, "Межбуквенный интервал")
    if not 0 <= spacing <= 100:
        raise ValueError("Межбуквенный интервал должен быть от 0 до 100 мм.")
    line_pitch = _finite(line_spacing, "Межстрочный интервал")
    if not 1.4 <= line_pitch <= 5:
        raise ValueError("Межстрочный интервал должен быть от 1,4 до 5 высот заглавных.")
    if not isinstance(monospace, bool):
        raise ValueError("monospace должен быть True или False.")
    if not isinstance(origin, (tuple, list)) or len(origin) != 2:
        raise ValueError("Начало текста — две неотрицательные координаты.")
    ox, oy = (_finite(item, "Координата начала") for item in origin)
    if min(ox, oy) < 0:
        raise ValueError("Начало текста не может быть отрицательным.")
    paths, point_count, advances = [], 0, []
    for line_index, line in enumerate(lines):
        pen_x = 0.0
        pen_y = line_index * cap_height * line_pitch
        for char in line:
            if char == " ":
                pen_x += (1.1 if monospace else .48) * cap_height + spacing
                continue
            glyph = _FONT[char]
            cell = 1.1 if monospace else glyph.advance
            padding = (cell - glyph.advance) * cap_height / 2 if monospace else 0.0
            for stroke in glyph.strokes:
                point_count += len(stroke)
                if point_count > MAX_POINTS:
                    raise ValueError(f"Слишком длинный текст: максимум {MAX_POINTS} точек.")
                paths.append([(pen_x + padding + x * cap_height, pen_y + y * cap_height) for x, y in stroke])
            pen_x += cell * cap_height + spacing
        advances.append(max(0, pen_x - spacing) if line else 0)
    if not paths:
        raise ValueError("В тексте нет печатаемых штрихов: одних пробелов и переносов недостаточно.")
    left = min(x for path in paths for x, _ in path)
    top = min(y for path in paths for _, y in path)
    native_width = max(x for path in paths for x, _ in path) - left
    native_height = max(y for path in paths for _, y in path) - top
    result = [[(ox + x - left, oy + y - top) for x, y in path] for path in paths]
    if any(not math.isfinite(value) for path in result for point in path for value in point):
        raise ValueError("Размеры текста слишком велики.")
    right = max(x for path in result for x, _ in path)
    bottom = max(y for path in result for _, y in path)
    # Extremely large origins can erase small differences through float rounding.
    if ((native_width > 0 and right - ox <= 0) or
            (native_height > 0 and bottom - oy <= 0)):
        raise ValueError("Координаты начала слишком велики для точного представления штрихов.")
    return TextLayout(result, (ox, oy, right, bottom), right - ox, bottom - oy,
                      max(advances), len(lines), len(text), cap_height)


def print_text_paths(text: str, height_mm: float, spacing_mm: float | None = None):
    """Planner convenience API: pure ink polylines with bbox top-left at (0, 0)."""
    return generate_text_paths(text, height_mm, letter_spacing_mm=spacing_mm).paths
