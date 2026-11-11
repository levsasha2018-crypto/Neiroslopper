"""Atomic composition of a few explicit, known caption-property edits.

Each callback returns a full reconstruction, so the last reconstruction alone
is the result. This helper neither renders intermediate plans nor executes a
preview or equipment command. Unknown clauses never produce a partial edit.
"""
from __future__ import annotations

from copy import deepcopy
import re
from typing import Mapping

from .planner import Plan


_QUOTES = re.compile(r'«[^«»]*»|"[^\"]*"|“[^“”]*”|„[^„“]*“')
_SEPARATOR = re.compile(r"\s+(?:и|затем|потом)\s+|(?<!\d),(?!\d)(?:\s*(?:и|затем|потом)\s+)?", re.I)
_WAITING_FIELDS = ("awaiting_text_field", "awaiting_text_request", "awaiting_text_target")
_ORDINAL = r"(?:перв\w*|втор\w*|трет\w*|четверт\w*|пят\w*|шест\w*|седьм\w*|восьм\w*|последн\w*)"
_MESSAGE = ("Не удалось разобрать все изменения надписи: ничего не применено, частичной правки нет. "
            "Укажите 2–4 полные известные правки текста/параметров, например: "
            "«Замени текст на «Марина» и высота букв 8 мм». Другие рисунки, рамки, "
            "общий масштаб и перемещения задавайте отдельной командой.")


def _masked(prompt):
    return _QUOTES.sub(lambda match: "_" * len(match[0]), prompt)


def _clauses(prompt, masked):
    # A named polite wrapper does not become a bogus first edit. Its comma
    # remains in the first clause for the existing single-edit parser.
    polite_end = 0
    for _ in range(3):
        polite = re.match(r"\s*(?:пожалуйста,?\s+|давай\s+|"
                          r"можешь(?:\s+ли)?(?:\s+ты)?(?:,\s*|\s+)|"
                          r"можете(?:\s+ли)?(?:\s+вы)?(?:,\s*|\s+)|"
                          r"(?:не\s+)?(?:мог|могла|могли)\s+бы(?:\s+(?:ты|вы))?(?:,\s*|\s+))",
                          masked[polite_end:], re.I)
        if polite is None:
            break
        polite_end += polite.end()
    spans, start = [], 0
    for separator in _SEPARATOR.finditer(masked):
        if separator.start() < polite_end:
            continue
        spans.append(prompt[start:separator.start()].strip())
        start = separator.end()
    spans.append(prompt[start:].strip())
    return spans


def _edit_like(clause):
    from .text_editing import _edit_request_text
    text = re.sub(r"\s+", " ", clause.casefold().replace("ё", "е")).strip()
    text = _edit_request_text(text)
    return bool(re.match(
        r"(?:замени|измени|поменяй|удали|убери|вставь|добавь)\s+"
        r"(?:" + _ORDINAL + r"\s+)?"
        r"(?:текст|надпись|слово|высоту|интервал|расстояние|строк\w*|фрагмент\w*|"
        r"подстрок\w*|часть|префикс\w*|суффикс\w*|символ\w*)\b|"
        r"вместо\b|текст\s*[:=_]|"
        r"высот[ау]\s+(?:букв|шрифта|надписи|перв\w*|втор\w*|трет\w*|последн\w*)\b|"
        r"(?:интервал|расстояние)\s+между\s+буквами\b|"
        r"(?:сделай|задай|установи|измени|поменяй)\s+"
        r"(?:буквы|шрифт|высоту|интервал|расстояние|отступ|выравнивание|межстрочный)\b|"
        r"(?:сделай|напиши|переведи)\s+.{0,80}(?:регистр|заглав\w*|строч\w*)\b|"
        r"(?:переведи\s+)?(?:надпись|текст|слово|перв\w*|втор\w*|трет\w*|последн\w*)"
        r"\s+.{0,80}\b(?:регистр|заглав\w*|строч\w*)\b|"
        r"(?:выравнивание|межстрочный\s+интервал)\b|"
        + _ORDINAL + r"\s+(?:надпись|текст|слово)\s+(?:текст|высот\w*|интервал)\b|"
        r"(?:добавь|вставь)\s+_+\s+в\s+(?:начало|конец)\s+(?:текста|надписи|слова)\b|"
        r"(?:добавь|вставь)\s+в\s+(?:начало|конец)\s+(?:текста|надписи|слова)\b|"
        r"в\s+(?:тексте|надписи|слове)\s+(?:замени|измени|поменяй|удали|убери)\b|"
        r"перенеси\s+(?:текст|надпись|слово)\b.{0,1000}\bна\s+(?:новую|следующую)\s+строку\b|"
        r"(?:объедини|соедини|склей)\s+(?:все\s+)?строки(?:\s+(?:текста|надписи))?\b|"
        r"сделай\s+(?:текст|надпись)\s+(?:одной\s+строкой|в\s+одну\s+строку)\b", text))


def _full_program(plan):
    if (not isinstance(plan, Plan) or not plan.requires_confirmation or plan.kind != "free_preview" or
            not isinstance(plan.parameters, dict) or set(plan.parameters) != {"operations"}):
        return None
    steps = plan.parameters["operations"]
    if not isinstance(steps, (list, tuple)) or not 1 <= len(steps) <= 8:
        return None
    # Schema validation only: no rendering, filesystem sample reads or bounds
    # execution. Full final geometry is the editor's responsibility.
    from .free_preview import _validate_operation
    try:
        validated = [_validate_operation(step) for step in steps]
    except (ValueError, TypeError, KeyError, OverflowError, RecursionError):
        return None
    captions = 0
    for kind, parameters in validated:
        if kind in {"print_text", "text", "ruler", "test_pattern"} or (
                kind in {"polyline", "ellipse"} and parameters["mode"] == "replace"):
            captions = 0
        if kind in {"print_text", "append_text", "text"}:
            captions += 1
    return deepcopy(list(steps)) if captions else None


def _question(question_factory):
    proposed = question_factory(_MESSAGE)
    # Clarification does not establish an implicit pending field/selection.
    # Even a factory that returns extra metadata cannot carry partial edits.
    explanation = proposed.explanation if isinstance(proposed, Plan) and isinstance(proposed.explanation, str) else _MESSAGE
    question = proposed.question if isinstance(proposed, Plan) and isinstance(proposed.question, str) and proposed.question else explanation
    return Plan("question", {}, explanation=explanation, question=question)


def compose(prompt, state, single_edit, question_factory) -> Plan | None:
    """Compose 2–4 full known edits, or ask one atomic clarification.

None leaves non-compound requests and ordinary creation/creative compositions
to their existing handlers. Every accepted clause needs its own explicit edit
wording, so a bare number cannot answer an older pending-field question.
"""
    if (not isinstance(prompt, str) or not 1 <= len(prompt) <= 1800 or
            not isinstance(state, Mapping)):
        return None
    masked = _masked(prompt)
    clauses = _clauses(prompt, masked)
    if len(clauses) < 2 or not any(_edit_like(_masked(clause)) for clause in clauses):
        return None
    if not 2 <= len(clauses) <= 4 or any(not clause for clause in clauses):
        return _question(question_factory)
    from .text_editing import _edit_request_text
    first = re.sub(r"\s+", " ", _masked(clauses[0]).casefold().replace("ё", "е")).strip()
    if _edit_request_text(first) != first:
        # A wrapper such as 'можешь заменить ... и задать ...' applies to the
        # coordinated verbs. Add the same named wrapper for the single parser;
        # literal content and numeric values in each original clause stay exact.
        clauses = [clauses[0], *("можешь " + clause for clause in clauses[1:])]
    # Matching paired quotes are the only way to make these markers data.
    # Unmatched quotes/newlines and code/hardware never get partially applied.
    if re.search(r"[«»\"“”„\r\n$`;]|\b(?:g\s*\d+|m\s*\d+|exec|eval|powershell|cmd\.exe|"
                 r"игнорируй|ignore|запусти|отправь|выполни|не)\b|"
                 r"мотор|двигател|головк|каретк|драйвер|arduino|grbl|скорост|подач|питан",
                 _edit_request_text(masked.casefold().strip()), re.I):
        return _question(question_factory)
    if any(not _edit_like(_masked(clause)) for clause in clauses):
        return _question(question_factory)
    current = dict(state)
    for field in _WAITING_FIELDS:
        current.pop(field, None)
    # Only small trusted reconstruction metadata is copied, not 50k SVG paths
    # or arbitrary editor/controller objects. The callback itself is read-only.
    for key in ("preview_draft", "preview_task"):
        if isinstance(current.get(key), Plan):
            current[key] = deepcopy(current[key])
    descriptions = []
    previous = None
    for index, clause in enumerate(clauses, 1):
        for field in _WAITING_FIELDS:
            current.pop(field, None)
        if previous is not None:
            current["preview_draft"] = deepcopy(previous)
        try:
            candidate = single_edit(clause, current)
            operations = _full_program(candidate)
        except (ValueError, TypeError, KeyError, OverflowError, RecursionError):
            return _question(question_factory)
        if operations is None:
            return _question(question_factory)
        previous = Plan("free_preview", {"operations": operations}, candidate.explanation)
        summary = candidate.explanation if isinstance(candidate.explanation, str) and candidate.explanation.strip() else clause
        if len(summary) > 1000:
            summary = summary[:999] + "…"
        descriptions.append(f"{index}. {summary}")
    explanation = (f"Составная правка надписи: {len(clauses)} изменения, атомарно — только все вместе.\n" +
                   "\n".join(descriptions) +
                   "\nЭто один полный пересчёт, не набор копий. Проверьте окончательный предпросмотр перед подтверждением.")
    return Plan("free_preview", deepcopy(previous.parameters), explanation)
