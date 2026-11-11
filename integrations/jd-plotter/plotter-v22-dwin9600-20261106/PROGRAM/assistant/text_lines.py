"""Exact line edits to one known caption, with no devices or model calls.

The caller masks quoted literals as ``§0§`` etc. and supplies its closed
caption-target pattern. Target selection, font/geometry validation and fresh
confirmation remain the caller's responsibility. These helpers only rewrite
the literal string and never infer text from imported SVG paths.
"""
from __future__ import annotations

import re


MAX_CHARACTERS = 800
MAX_LINES = 50
_TOKEN = r"§[0-9]{1,3}§"
_INDEX = r"[+-]?[0-9]{1,50}(?:[.,][0-9]{1,50})?"
_LINE_BREAK = re.compile(r"\r\n|\r|\n")
_ANY_LINE_BREAK = re.compile(r"[\r\n\v\f\x85\u2028\u2029]")
_FIELDS = {
    "add_line": {"domain", "operation", "text", "position", "message"},
    "break_after": {"domain", "operation", "fragment", "message"},
    "join_lines": {"domain", "operation", "message"},
    "replace_line": {"domain", "operation", "line_number", "text", "message"},
    "delete_line": {"domain", "operation", "line_number", "message"},
}


def _single_line(text, *, fragment=False):
    label = "Фрагмент для переноса" if fragment else "Новая строка"
    agreement = "должен" if fragment else "должна"
    if not isinstance(text, str) or not text.strip():
        raise ValueError(label + f" {agreement} содержать непустой текст.")
    if len(text) > MAX_CHARACTERS:
        raise ValueError(f"Максимум {MAX_CHARACTERS} символов в надписи.")
    if _ANY_LINE_BREAK.search(text):
        raise ValueError(label + f" {agreement} быть одной строкой без встроенных переносов.")
    return text


def _literal(token, literals, *, fragment=False):
    match = re.fullmatch(r"§([0-9]{1,3})§", token)
    if (match is None or not isinstance(literals, (list, tuple)) or
            int(match[1]) >= len(literals)):
        raise ValueError("Не найден буквальный текст в кавычках. Повторите команду с кавычками.")
    return _single_line(literals[int(match[1])], fragment=fragment)


def _line_number(value):
    if isinstance(value, str) and re.fullmatch(r"[+-]?[0-9]{1,50}", value):
        value = int(value)
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= MAX_LINES:
        raise ValueError(f"Номер строки должен быть целым числом от 1 до {MAX_LINES}.")
    return value


def _message(value):
    operation = value["operation"]
    if operation == "add_line":
        position = "в начало" if value["position"] == "start" else "в конец"
        return (f"Добавить отдельную строку «{value['text']}» {position} надписи. "
                "Существующий текст, регистр, Ё и пробелы сохраняются.")
    if operation == "break_after":
        return (f"Вставить перенос после единственного точного вхождения «{value['fragment']}». "
                "Регистр, Ё и все существующие пробелы сохраняются; повторяющийся фрагмент требует уточнения.")
    if operation == "join_lines":
        return ("Объединить строки в одну: убрать пробелы по краям отдельных строк и пустые строки, "
                "границы непустых строк заменить одним пробелом. Регистр, Ё и пробелы внутри строк сохраняются.")
    if operation == "replace_line":
        return (f"Заменить строку номер {value['line_number']} на «{value['text']}». "
                "Все остальные строки, регистр, Ё, пробелы и разделители сохраняются.")
    return (f"Удалить строку номер {value['line_number']} вместе с одним соседним переносом. "
            "Остальной текст сохраняется; пустую надпись не создаю.")


def _value(operation, **parameters):
    result = {"domain": "lines", "operation": operation, **parameters}
    result["message"] = _message(result)
    return result


def _target_pattern(target_pattern):
    if not isinstance(target_pattern, str) or not 1 <= len(target_pattern) <= 2000:
        raise ValueError("Некорректный шаблон выбора надписи.")
    return r"(?P<target>(?:" + target_pattern + r"))"


def parse_request(masked_text, literals, target_pattern):
    """Return ``('content', target, lines_value)`` for one complete command.

    The masked text must already be normalized by the caller. Invalid literal
    payloads / explicit numeric indices raise a human-readable ValueError;
    unrelated or incomplete commands return None for scoped clarification.
    """
    if not isinstance(masked_text, str) or not 1 <= len(masked_text) <= 1800:
        return None
    if "\n" in masked_text or "\r" in masked_text:
        return None
    target = _target_pattern(target_pattern)
    literal = r"(?P<literal>" + _TOKEN + r")"
    where = r"(?P<where>конец|начало)"
    suffix = r"(?:\s+(?:в|у)\s+" + target + r")?"
    # Quote-first and position-first variants retain an exact optional selector.
    add_patterns = (
        r"(?:добавь|вставь)\s+(?:новую\s+)?строку\s+" + literal + r"\s+в\s+" + where + r"(?:\s+" + target + r")?",
        r"(?:добавь|вставь)\s+в\s+" + where + r"(?:\s+" + target + r")?\s+(?:новую\s+)?строку\s+" + literal,
        r"(?:добавь|вставь)\s+(?:новую\s+)?строку\s+в\s+" + where + r"(?:\s+" + target + r")?\s+" + literal,
    )
    for pattern in add_patterns:
        match = re.fullmatch(pattern, masked_text)
        if match:
            value = _value("add_line", text=_literal(match["literal"], literals),
                           position="start" if match["where"] == "начало" else "end")
            return "content", match["target"], value
    after = r"после\s+(?:(?:слова|фрагмента)\s+)?" + literal
    break_patterns = (
        r"(?:(?:сделай|добавь|вставь)\s+)?перенос(?:\s+строки)?\s+" + after + suffix,
        r"(?:сделай|добавь|вставь)\s+перенос(?:\s+строки)?\s+(?:в|у)\s+" + target + r"\s+" + after,
        r"перенеси\s+" + target + r"\s+" + after + r"\s+на\s+новую\s+строку",
    )
    for pattern in break_patterns:
        match = re.fullmatch(pattern, masked_text)
        if match:
            return "content", match["target"], _value(
                "break_after", fragment=_literal(match["literal"], literals, fragment=True))
    join_patterns = (
        r"(?:убери|удали)\s+переносы(?:\s+строк)?" + suffix,
        r"объедини\s+строки(?:\s+" + target + r")?\s+в\s+одну(?:\s+строку)?",
        r"объедини\s+строки\s+в\s+одну(?:\s+строку)?(?:\s+(?:в|у)\s+" + target + r")?",
        r"сделай\s+" + target + r"\s+(?:в\s+одну\s+строку|одной\s+строкой)",
    )
    for pattern in join_patterns:
        match = re.fullmatch(pattern, masked_text)
        if match:
            return "content", match["target"], _value("join_lines")
    index = r"(?:номер\s+)?(?P<index>" + _INDEX + r")"
    replace_patterns = (
        r"(?:замени|поменяй)\s+строку\s+" + index + r"(?:\s+(?:в|у)\s+" + target + r")?\s+на\s+" + literal,
        r"(?:замени|поменяй)\s+строку\s+" + index + r"\s+на\s+" + literal + suffix,
    )
    for pattern in replace_patterns:
        match = re.fullmatch(pattern, masked_text)
        if match:
            return "content", match["target"], _value(
                "replace_line", line_number=_line_number(match["index"]), text=_literal(match["literal"], literals))
    match = re.fullmatch(r"(?:удали|убери)\s+строку\s+" + index + suffix, masked_text)
    if match:
        return "content", match["target"], _value("delete_line", line_number=_line_number(match["index"]))
    return None


def looks_like_request(masked_text):
    """Recognize the bounded line-edit domain, not any usable edit authority."""
    if not isinstance(masked_text, str) or not 1 <= len(masked_text) <= 1800:
        return False
    ordinary = bool(re.match(
        r"(?:(?:добавь|вставь|замени|поменяй|удали|убери)\s+(?:новую\s+)?(?:строку|переносы?)\b|"
        r"(?:добавь|вставь)\s+в\s+(?:начало|конец)\b|"
        r"(?:сделай\s+)?перенос\b|объедини\s+строки\b|"
        r"сделай\s+.*\b(?:одной строкой|в одну строку)\b)", masked_text))
    move_to_new_line = bool(re.match(r"перенеси\s+", masked_text) and
                            re.search(r"\b(?:текст|надпись|надписи|слово)\b", masked_text) and
                            re.search(r"\b(?:после|новую)\b", masked_text))
    return ordinary or move_to_new_line


def _checked_value(value):
    if (not isinstance(value, dict) or value.get("domain") != "lines" or
            not isinstance(value.get("operation"), str) or value["operation"] not in _FIELDS):
        raise ValueError("Неизвестное изменение строк.")
    if set(value) != _FIELDS[value["operation"]]:
        raise ValueError("Неверные или лишние параметры изменения строк.")
    if value["operation"] in {"add_line", "replace_line"}:
        _single_line(value["text"])
    if value["operation"] == "break_after":
        _single_line(value["fragment"], fragment=True)
    if value["operation"] == "add_line" and (
            not isinstance(value["position"], str) or value["position"] not in {"start", "end"}):
        raise ValueError("Новую строку можно добавить только в начало или конец.")
    if value["operation"] in {"replace_line", "delete_line"}:
        if isinstance(value["line_number"], bool) or not isinstance(value["line_number"], int):
            raise ValueError(f"Номер строки должен быть целым числом от 1 до {MAX_LINES}.")
        _line_number(value["line_number"])
    if value["message"] != _message(value):
        raise ValueError("Описание изменения не соответствует его параметрам.")
    return value


def _checked_text(text):
    if not isinstance(text, str) or not text.strip():
        raise ValueError("Надпись не может быть пустой.")
    if len(text) > MAX_CHARACTERS:
        raise ValueError(f"Максимум {MAX_CHARACTERS} символов в надписи.")
    if len(_LINE_BREAK.split(text)) > MAX_LINES:
        raise ValueError(f"Максимум {MAX_LINES} строк в надписи.")
    return text


def _restore(lines, separators):
    return "".join(line + (separators[index] if index < len(separators) else "")
                   for index, line in enumerate(lines))


def rewrite(current_literal, value):
    """Return a new literal; input strings/dictionaries are never mutated."""
    current = _checked_text(current_literal)
    value = _checked_value(value)
    operation = value["operation"]
    lines = _LINE_BREAK.split(current)
    separators = _LINE_BREAK.findall(current)
    if operation == "add_line":
        if value["position"] == "start":
            result = value["text"] + (separators[0] if separators else "\n") + current
        else:
            result = current + (separators[-1] if separators else "\n") + value["text"]
    elif operation == "join_lines":
        if not separators:
            raise ValueError("В надписи уже одна строка: переносов для удаления нет.")
        result = " ".join(line.strip() for line in lines if line.strip())
    elif operation == "break_after":
        fragment, matches, search = value["fragment"], [], 0
        while True:
            found = current.find(fragment, search)
            if found < 0:
                break
            matches.append(found)
            if len(matches) > 1:
                raise ValueError("Фрагмент встречается несколько раз. Укажите более длинный уникальный фрагмент; первое вхождение не выбираю.")
            search = found + 1  # Overlapping occurrences are ambiguous too.
        if not matches:
            raise ValueError("Такого точного фрагмента в надписи нет. Проверьте регистр, Ё и пробелы.")
        end = matches[0] + len(fragment)
        suffix = current[end:]
        if re.match(r"[^\S\r\n]*(?:\r\n|\r|\n)", suffix):
            raise ValueError("После этого фрагмента уже есть перенос строки.")
        if not suffix.strip():
            raise ValueError("После фрагмента нет продолжения текста: пустую строку не добавляю.")
        delimiter = separators[0] if separators else "\n"
        result = current[:end] + delimiter + suffix
    else:
        number = value["line_number"]
        if number > len(lines):
            raise ValueError(f"В надписи {len(lines)} строк; строки номер {number} нет.")
        index = number - 1
        if operation == "replace_line":
            lines[index] = value["text"]
        else:
            if len(lines) == 1:
                raise ValueError("Нельзя удалить единственную строку: надпись станет пустой.")
            lines.pop(index)
            separators.pop(index if index < len(separators) else index - 1)
        result = _restore(lines, separators)
    return _checked_text(result)
