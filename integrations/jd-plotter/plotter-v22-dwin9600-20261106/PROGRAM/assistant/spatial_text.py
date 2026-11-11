"""Exact relative-to-paper placement for a new literal printed inscription.

Approximate words never become claimed user dimensions: the preview explicitly
proposes five percent of the corresponding sheet side. This module only returns
declarative preview operations; geometry and fresh confirmation remain the
editor's responsibility. It never moves the carriage or accesses the model.
"""
from __future__ import annotations

import re
from typing import Mapping

from .planner import Plan
from .project_answers import sheet_dimensions


DEFAULT_HEIGHT_MM = 5.0
SMALL_OFFSET_FRACTION = 0.05
_NUMBER = r"(?:\d+(?:[.,]\d+)?|[.,]\d+)"
_LITERAL = (r'«[^»\r\n]*»|"[^"\r\n]*"|“[^”\r\n]*”|'
            r"[A-Za-zА-Яа-яЁё]+(?:[-'][A-Za-zА-Яа-яЁё]+)*")
_MIDDLE = r"(?:середины|центра)(?:\s+листа)?"
_DIRECTION = r"левее|правее|выше|ниже"
_WRAPPER = (r"(?:пожалуйста,?\s+|давай\s+|можешь(?:\s+ли)?(?:\s+ты)?(?:,\s*|\s+)|"
            r"можете(?:\s+ли)?(?:\s+вы)?(?:,\s*|\s+)|"
            r"(?:не\s+)?(?:мог|могла|могли)\s+бы(?:\s+(?:ты|вы))?(?:,\s*|\s+))")
_PREFIX = (r"(?:" + _WRAPPER + r"{1,3}(?:напиши|напечатай|написать|напечатать)|"
           r"(?:напиши|напечатай))\s+(?:(?:слово|текст|надпись)\s+)?")
_START = _PREFIX + r"(?P<literal>" + _LITERAL + r")\s+"
_END = r"\s*[.!?]?\s*"
_PATTERNS = (
    re.compile(_START + r"(?P<approx>чуть|немного|слегка)\s+"
               r"(?P<direction>" + _DIRECTION + r")\s+" + _MIDDLE + _END, re.I),
    re.compile(_START + r"на\s*(?P<distance>" + _NUMBER + r")\s*(?:мм|миллиметр(?:а|ов)?)\s+"
               r"(?P<direction>" + _DIRECTION + r")\s+" + _MIDDLE + _END, re.I),
    re.compile(_START + r"(?P<direction>" + _DIRECTION + r")\s+" + _MIDDLE +
               r"\s+на\s*(?P<distance>" + _NUMBER + r")\s*(?:мм|миллиметр(?:а|ов)?)" + _END, re.I),
)
_DIRECTIONS = {
    "левее": (-1, 0), "правее": (1, 0), "выше": (0, -1), "ниже": (0, 1),
}


def plan(prompt: str, state: Mapping) -> Plan | None:
    """Recognize the entire supported request, without discarding any qualifier.

Unknown grammar returns None for the existing dispatcher/creative fallback.
A recognized command with unavailable editor prerequisites returns a question
Plan, so it cannot be turned into an unrelated action or an invented sheet.
"""
    if not isinstance(prompt, str) or not 0 < len(prompt) <= 1800:
        return None
    matched = next((found for pattern in _PATTERNS
                    if (found := pattern.fullmatch(prompt.strip())) is not None), None)
    if matched is None:
        return None
    if not isinstance(state, Mapping):
        return Plan("question", question="Неизвестно состояние редактора. Изменений нет.")
    if state.get("busy"):
        return Plan("question", question="Дождитесь завершения операции в пульте. Изменений нет.")
    sheet = sheet_dimensions(state)
    if sheet is None:
        return Plan("question", question="Сначала задайте два угла листа или выберите пробный лист. Изменений нет.")
    raw = matched["literal"]
    literal = raw[1:-1] if raw[:1] in {'«', '"', '“'} else raw
    if not literal.strip() or len(literal) > 800:
        return Plan("question", question="Нужен непустой текст надписи длиной до 800 символов. Изменений нет.")
    from .text_paths import print_text_paths
    try:
        # Preserve case and ё; reject unsupported glyphs rather than silently
        # replacing the requested text. This check produces no editor changes.
        print_text_paths(literal, DEFAULT_HEIGHT_MM, None)
    except (ValueError, TypeError, OverflowError):
        return Plan("question", question="Текст содержит неподдерживаемые печатные символы. Изменений нет.")
    direction = matched["direction"].casefold()
    sign_x, sign_y = _DIRECTIONS[direction]
    fields = matched.groupdict()
    if fields.get("distance") is not None:
        distance = float(fields["distance"].replace(",", "."))
        if not 0 < distance <= 3000:
            return Plan("question", question="Смещение должно быть больше 0 и не больше 3000 мм. Изменений нет.")
        choice = f"Смещение {distance:g} мм указано в просьбе."
    else:
        side = sheet[0] if sign_x else sheet[1]
        distance = side * SMALL_OFFSET_FRACTION
        qualifier = matched["approx"].casefold()
        dimension = "ширины" if sign_x else "высоты"
        choice = (f"Для «{qualifier}» предлагаю смещение на 5% {dimension} листа: {distance:g} мм. "
                  "Это предложение программы, точное расстояние вы не задавали.")
    operations = [
        {"kind": "print_text", "parameters": {"text": literal, "height_mm": DEFAULT_HEIGHT_MM,
                                               "spacing_mm": None, "anchor": "center", "margin_mm": 0.0}},
        {"kind": "move_preview", "parameters": {"dx_mm": sign_x * distance,
                                                  "dy_mm": sign_y * distance}},
    ]
    explanation = (f"Печатная надпись «{literal}» {direction} середины листа. "
                   f"Неуказанная высота заглавных — предложение программы {DEFAULT_HEIGHT_MM:g} мм; "
                   "интервал автоматический. " + choice +
                   " Сдвигается центр надписи относительно центра листа, не каретка.")
    return Plan("free_preview", {"operations": operations}, explanation)
