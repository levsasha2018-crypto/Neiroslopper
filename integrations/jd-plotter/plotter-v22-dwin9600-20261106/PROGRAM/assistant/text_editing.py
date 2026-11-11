"""Typed caption rewrites from trusted plans, not guessed SVG typography.

Returns a full replacement Plan against the original editor paths. The caller
must validate geometry and obtain fresh confirmation; this module cannot move
equipment, interpret assistant prose, or change the editor by itself.
"""
from __future__ import annotations

from copy import deepcopy
import math
import re
from typing import Mapping

from .planner import ACTION_KINDS, Plan, _VALUE, _parsed_number, _request_text


_QUOTES = re.compile(r'«([^«»]*)»|"([^"\n]*(?:\n[^"\n]*)*)"|“([^“”]*)”|„([^„“]*)“')
_ORDER = {
    "перв": 0, "втор": 1, "трет": 2, "четверт": 3,
    "пят": 4, "шест": 5, "седьм": 6, "восьм": 7,
}
_ORDINAL = (r"(?:перв(?:ая|ую|ой|ый|ого)|втор(?:ая|ую|ой|ого)|"
            r"трет(?:ья|ью|ьей|ий|ьего)|четверт(?:ая|ую|ой|ый|ого)|"
            r"пят(?:ая|ую|ой|ый|ого)|шест(?:ая|ую|ой|ой|ого)|"
            r"седьм(?:ая|ую|ой|ой|ого)|восьм(?:ая|ую|ой|ой|ого)|"
            r"последн(?:яя|юю|ей|ий|его))")
_CAPTION = r"(?:надпись|надписи|текст|текста|слово|слова)"
_LITERAL = r"§\d+§"
_TARGET = (r"(?:" + _ORDINAL + r"\s+" + _CAPTION + r"|" + _CAPTION +
           r"\s+(?:" + _LITERAL + r"|номер\s+[1-8])|" + _CAPTION + r")")
_MM = r"(?:мм|миллиметр(?:а|ов)?)"
_COMMAND_TYPOS = {"увиличь": "увеличь", "увеличь": "увеличь", "уменьшы": "уменьши",
                  "сдивнь": "сдвинь", "перемисти": "перемести", "замини": "замени",
                  "измениь": "измени"}


def _question(text, awaiting=None, *, context_query=False, waiting_request=None, waiting_target=None):
    parameters = {"awaiting_text_field": awaiting} if awaiting else {}
    if waiting_request:
        parameters["awaiting_text_request"] = waiting_request
    if waiting_target is not None:
        parameters["awaiting_text_target"] = waiting_target
    if context_query:
        parameters["context_query"] = True
    return Plan("question", parameters, explanation=text, question=text)


def _correct_command_typos(text):
    return re.sub(r"\b(?:" + "|".join(_COMMAND_TYPOS) + r")\b",
                  lambda match: _COMMAND_TYPOS[match[0].casefold()], text, flags=re.IGNORECASE)


def _edit_request_text(text):
    """Normalize named request wrappers outside literal placeholders only."""
    normalized = _request_text(text)
    if normalized != text:
        first, separator, rest = normalized.partition(" ")
        # Caption-specific infinitives complement the shared polite grammar.
        first = {"заменить": "замени", "изменить": "измени", "поменять": "поменяй",
                 "перевести": "переведи", "удалить": "удали", "убрать": "убери",
                 "вставить": "вставь", "дописать": "допиши"}.get(first, first)
        normalized = first + separator + rest
    return _correct_command_typos(normalized)


def _tokens(prompt):
    literals = []
    pieces = []
    previous = 0
    for match in _QUOTES.finditer(prompt):
        outside = prompt[previous:match.start()]
        if "§" in outside or "\n" in outside or "\r" in outside:
            raise ValueError("Текст с переносами заключите целиком в кавычки.")
        pieces.append(outside)
        literal = next(group for group in match.groups() if group is not None)
        literals.append(literal)
        pieces.append(f"§{len(literals) - 1}§")
        previous = match.end()
    outside = prompt[previous:]
    if "§" in outside or "\n" in outside or "\r" in outside:
        raise ValueError("Текст с переносами заключите целиком в кавычки.")
    pieces.append(outside)
    text = re.sub(r"\s+", " ", "".join(pieces).casefold().replace("ё", "е")).strip()
    text = re.sub(r"[.!?]+$", "", text).strip()
    return _edit_request_text(text), literals


def _literal(token, literals):
    match = re.fullmatch(r"§(\d+)§", token)
    if not match or int(match[1]) >= len(literals):
        raise ValueError("Нужен буквальный текст в кавычках.")
    return literals[int(match[1])]


def _operations(plan):
    if not isinstance(plan, Plan) or not isinstance(plan.kind, str) or plan.kind not in ACTION_KINDS:
        return None
    if plan.kind == "free_preview":
        if not isinstance(plan.parameters, dict) or set(plan.parameters) != {"operations"}:
            return None
        operations = plan.parameters["operations"]
        if not isinstance(operations, list) or not 1 <= len(operations) <= 8:
            return None
    else:
        operations = [{"kind": plan.kind, "parameters": plan.parameters}]
    if any(not isinstance(item, dict) or set(item) != {"kind", "parameters"} or
           not isinstance(item["kind"], str) or not isinstance(item["parameters"], dict)
           for item in operations):
        return None
    # Stored plans are normally validated by the editor. Recheck the registry
    # and all retained caption fields anyway before generating edit authority
    # or reporting their parameters as trusted contextual facts.
    try:
        from .free_preview import _validate_operation
        from .text_paths import print_text_paths
        for item in operations:
            _validate_operation(item)
            parameters = item["parameters"]
            if item["kind"] in {"print_text", "append_text"}:
                height = parameters["height_mm"]
                spacing = parameters["spacing_mm"]
                if not 1 <= height <= 100 or (spacing is not None and not 0 <= spacing <= 100):
                    return None
                print_text_paths(parameters["text"], height, spacing)
            elif item["kind"] == "text" and (
                    parameters["text"] != "привет" or parameters["height_mm"] not in {1.5, 2, 3}):
                return None
    except (ValueError, TypeError, KeyError, OverflowError, RecursionError):
        return None
    return deepcopy(operations)


def _captions(operations):
    """Only captions still present after the last replacing generator count."""
    result = []
    for index, item in enumerate(operations):
        kind, parameters = item["kind"], item["parameters"]
        if kind in {"print_text", "text", "ruler", "test_pattern"} or (
                kind in {"polyline", "ellipse"} and parameters.get("mode") == "replace"):
            result = []
        if kind in {"print_text", "append_text", "text"} and isinstance(parameters.get("text"), str):
            result.append(index)
    return result


def _choose(target, captions, operations, literals):
    if not target or re.fullmatch(_CAPTION, target):
        return captions[0] if len(captions) == 1 else None
    quoted = re.search(_LITERAL, target)
    if quoted:
        literal = _literal(quoted[0], literals)
        choices = [index for index in captions if operations[index]["parameters"]["text"] == literal]
        return choices[0] if len(choices) == 1 else None
    if "последн" in target:
        return captions[-1]
    number = re.search(r"номер\s+([1-8])", target)
    if number:
        ordinal = int(number[1]) - 1
    else:
        ordinal = next((value for stem, value in _ORDER.items()
                        if re.search(r"\b" + stem + r"\w*", target)), None)
    return captions[ordinal] if ordinal is not None and ordinal < len(captions) else None


def _choices(captions, operations):
    return "; ".join(f"{order}. «{operations[index]['parameters']['text']}»"
                     for order, index in enumerate(captions, 1))


def _dimension(value, field):
    number = _parsed_number(value)
    lower = 1 if field == "height_mm" else 0
    if not math.isfinite(number) or not lower <= number <= 100:
        raise ValueError("Высота заглавных — 1–100 мм, интервал — 0–100 мм.")
    return number


def _request(text, literals, awaiting):
    """Return operation/target/value, or None. Entire grammar must match."""
    if awaiting == "text" and re.fullmatch(_LITERAL, text):
        return "text", None, _literal(text, literals)
    if awaiting in {"height_mm", "spacing_mm"}:
        match = re.fullmatch(r"(" + _VALUE + r")\s*" + _MM, text)
        if match:
            return awaiting, None, _dimension(match[1], awaiting)
    match = re.fullmatch(r"вместо\s+(" + _LITERAL + r")\s+(?:напиши|напечатай|поставь)\s+(" + _LITERAL + r")", text)
    if match:
        return "text", "надпись " + match[1], _literal(match[2], literals)
    match = re.fullmatch(r"(?:замени|измени|поменяй)\s+(" + _TARGET + r")(?:\s+на|\s*:)\s*(" + _LITERAL + r")", text)
    if match:
        return "text", match[1], _literal(match[2], literals)
    match = re.fullmatch(r"(?:(" + _TARGET + r")\s+)?текст\s*:?[ ]*(" + _LITERAL + r")", text)
    if match:
        return "text", match[1], _literal(match[2], literals)
    match = re.fullmatch(r"(?:сделай|напиши|переведи)\s+(" + _TARGET + r")\s+"
                        r"(?:в\s+)?(заглавными(?: буквами)?|строчными(?: буквами)?|верхний регистр|нижний регистр)", text)
    if match:
        return ("upper" if match[2].startswith(("заглав", "верхний")) else "lower"), match[1], None
    match = re.fullmatch(r"(?:сделай\s+)?(?:все\s+)?буквы\s+(заглавными|строчными)", text)
    if match:
        return ("upper" if match[1] == "заглавными" else "lower"), None, None
    match = re.fullmatch(r"(?:переведи\s+)?(" + _TARGET + r")\s+в\s+(верхний|нижний)\s+регистр", text)
    if match:
        return ("upper" if match[2] == "верхний" else "lower"), match[1], None
    patterns = [
        ("height_mm", r"(?:высота|высоту)\s+(?:букв|шрифта|(" + _TARGET + r"))\s*(?:на\s+|[:=]\s*)?(" + _VALUE + r")\s*" + _MM),
        ("height_mm", r"(?:сделай|задай|установи|измени|поменяй)\s+высоту\s+(?:букв|шрифта|(" + _TARGET + r"))\s+(?:на\s+)?(" + _VALUE + r")\s*" + _MM),
        ("height_mm", r"(?:сделай|задай|установи|измени)\s+(?:буквы|шрифт|(" + _TARGET + r"))\s+(?:высотой|высоту|высота|размером)\s+(" + _VALUE + r")\s*" + _MM),
        ("spacing_mm", r"(?:интервал|расстояние)\s+между\s+буквами(?:\s+(?:у\s+)?(" + _TARGET + r"))?\s*(?:[:=]\s*)?(" + _VALUE + r")\s*" + _MM),
        ("spacing_mm", r"(?:сделай|задай|установи|измени)\s+(?:интервал|расстояние)\s+между\s+буквами(?:\s+(?:у\s+)?(" + _TARGET + r"))?\s+(?:на\s+)?(" + _VALUE + r")\s*" + _MM),
    ]
    for field, pattern in patterns:
        match = re.fullmatch(pattern, text)
        if match:
            return field, match[1], _dimension(match[2], field)
    incomplete = [
        ("text", r"(?:замени|измени|поменяй)\s+(" + _TARGET + r")"),
        ("height_mm", r"(?:измени|поменяй|задай|установи)\s+высоту\s+(?:букв|шрифта|(" + _TARGET + r"))"),
        ("spacing_mm", r"(?:измени|поменяй|задай|установи)\s+(?:интервал|расстояние)\s+между\s+буквами(?:\s+(?:у\s+)?(" + _TARGET + r"))?"),
    ]
    for field, pattern in incomplete:
        match = re.fullmatch(pattern, text)
        if match:
            return "await", match[1], field
    if re.fullmatch(r"(?:что за текст|какой текст|что написано|какая надпись)(?: сейчас)?", text):
        return "query", None, "text"
    if re.fullmatch(r"(?:какая|какой)\s+(?:сейчас\s+)?(?:высота букв|высота надписи|размер шрифта)", text):
        return "query", None, "height_mm"
    if re.fullmatch(r"(?:какой|какое)\s+(?:сейчас\s+)?(?:интервал|расстояние)\s+между\s+буквами", text):
        return "query", None, "spacing_mm"
    return None


def _looks_like_edit(text):
    from . import text_lines, text_mutations
    return text_lines.looks_like_request(text) or text_mutations.looks_like_request(text) or bool(re.match(r"(?:замени|измени|поменяй)\s+(?:текст|надпись|слово|высоту|интервал|расстояние)|"
                         r"вместо\b|текст\s*[:§«\"“„]|высот[ау]\s+(?:букв|шрифта|надписи)|"
                         r"(?:интервал|расстояние)\s+между\s+буквами|"
                         r"(?:сделай|задай|установи)\s+(?:буквы|шрифт|высоту|интервал|расстояние)|"
                         r"(?:сделай|переведи)\s+(?:текст|надпись|слово).*(?:регистр|заглав|строч|пропис|большими буквами|маленькими буквами)", text))


def _replaces_source(operations):
    return any(item["kind"] in {"print_text", "text", "ruler", "test_pattern"} or
               item["kind"] in {"polyline", "ellipse"} and item["parameters"].get("mode") == "replace"
               for item in operations)


def _context(state):
    draft, applied = state.get("preview_draft"), state.get("preview_task")
    operations = _operations(draft if isinstance(draft, Plan) else applied)
    if (operations is not None and isinstance(draft, Plan) and isinstance(applied, Plan) and
            not _replaces_source(operations)):
        if _operations(applied) is None:
            return None, []
        # A delta draft retains the applied generator. This is metadata only:
        # do not replay the delta or infer glyphs from editor line geometry.
        from .draft_context import applied_task
        operations = _operations(applied_task(draft, applied))
    return operations, _captions(operations) if operations is not None else []


def _single_field_hint(text, state, awaiting=None):
    if re.search(r"\b(?:и|затем|потом)\b|,\s*[а-яa-z]", text):
        return None
    operations, captions = _context(state)
    saved_target = state.get("awaiting_text_target")
    selected = (captions[saved_target - 1] if isinstance(saved_target, int) and not isinstance(saved_target, bool) and
                1 <= saved_target <= len(captions) else captions[0] if len(captions) == 1 else None)
    if selected is None or operations[selected]["kind"] == "text":
        return None
    fields = []
    if (re.search(r"\b(?:замени|измени|поменяй)\s+" + _TARGET + r".*" + _LITERAL, text) or
            re.search(r"\bтекст\s*:?[ ]*" + _LITERAL, text) or
            re.search(r"\bвместо\s+" + _LITERAL, text)):
        fields.append("text")
    if re.search(r"\b(?:высота|высоту|высотой|шрифта)\b", text):
        fields.append("height_mm")
    if re.search(r"\b(?:интервал|расстояние)\b.*\bбуквами\b", text):
        fields.append("spacing_mm")
    if not fields and awaiting:
        return awaiting
    return fields[0] if len(fields) == 1 else None


def _short_field_answer(text, awaiting):
    # Missing units/wrong field are still clarification attempts, not a new
    # free model task. A full new command/question remains free to change topic.
    return bool(awaiting and (
        re.fullmatch(_LITERAL, text) or
        re.fullmatch(_VALUE + r"(?:\s*[а-яa-z]+)?", text) or
        awaiting == "text" and re.fullmatch(r"[а-яa-z][а-яa-z0-9-]{0,79}", text)))


def _selection_target(text):
    text = re.sub(r"^(?:\d{1,2}[.)]\s*|(?:выбери|измени|давай|это)\s+)", "", text)
    text = re.sub(r"\s+из\s+них$", "", text)
    if re.fullmatch(r"[1-8]", text):
        return "надпись номер " + text
    if re.fullmatch(_ORDINAL, text):
        return text + " надпись"
    if re.fullmatch(_LITERAL, text):
        return "надпись " + text
    if re.fullmatch(_TARGET, text):
        return text
    return None


def _field_question(message, state, field):
    target = state.get("awaiting_text_target")
    _, captions = _context(state)
    if not field or not isinstance(target, int) or isinstance(target, bool) or not 1 <= target <= len(captions):
        target = None
    return _question(message, field, waiting_target=target)


def _request_to_resume(text, literals, state):
    """Resume only a program's exact pending edit, never a chat transcript."""
    previous = state.get("awaiting_text_request")
    target = _selection_target(text)
    if (target is None or not isinstance(previous, str) or not 1 <= len(previous) <= 1800):
        return None
    previous_text, previous_literals = _tokens(previous)
    request = _typed_request(previous_text, previous_literals, None)
    if request is None or request[0] == "query":
        return None
    action, _, value = request
    return action, target, value


def _typed_request(text, literals, awaiting):
    basic = _request(text, literals, awaiting)
    # A quoted whole-word search is a content edit, whereas 'замени слово на'
    # remains replacement of the entire caption. Never confuse the two.
    inner_word = bool(re.match(r"(?:замени|поменяй)\s+(?:все\s+)?(?:слово|слова)\s+" + _LITERAL, text))
    if basic is not None and not inner_word:
        return basic
    from . import text_lines, text_mutations
    for helper in (text_lines, text_mutations):
        request = helper.parse_request(text, literals, _TARGET)
        if request is not None:
            return request
    return basic


def edit_single(prompt: str, state: Mapping) -> Plan | None:
    """Rewrite a trusted caption plan; None leaves unrelated commands alone.

    awaiting_text_field must come from a prior program question, not model prose.
    Questions never require confirmation and contain no usable drawing action.
    Bounds/point count are intentionally checked by the caller's renderer.
    """
    if not isinstance(prompt, str) or not 1 <= len(prompt.strip()) <= 1800 or not isinstance(state, Mapping):
        return None
    awaiting = state.get("awaiting_text_field")
    if not isinstance(awaiting, str) or awaiting not in {"text", "height_mm", "spacing_mm"}:
        awaiting = None
    try:
        text, literals = _tokens(prompt)
    except ValueError as exc:
        return _question(str(exc)) if _looks_like_edit(prompt.casefold()) or awaiting else None
    # Quoted content is data. Every code/hardware marker outside is rejected.
    if re.search(r"[$`;]|\b(?:g\s*\d+|m\s*\d+|exec|eval|powershell|cmd\.exe|игнорируй|ignore)\b|"
                 r"мотор|двигател|головк|каретк|драйвер|arduino|grbl|скорост|подач|питан|"
                 r"\bне\b|\b(?:запусти|отправь|выполни)\b", text):
        return _question("Редактирую только надпись в эскизе. Код и команды оборудованию не выполняются.") if _looks_like_edit(text) or awaiting else None
    try:
        request = _request_to_resume(text, literals, state) or _typed_request(text, literals, awaiting)
    except (ValueError, OverflowError) as exc:
        return _field_question(str(exc), state, _single_field_hint(text, state, awaiting))
    if request is None:
        if _looks_like_edit(text) and re.search(r"\bпрописн\w*\b|\b(?:большими|маленькими)\s+буквами\b", text):
            return _question("Вы имеете в виду заглавные/строчные печатные буквы или размер/рукописный стиль? "
                             "Для регистра: «Сделай текст заглавными»; для размера: «Высота букв 8 мм». "
                             "Произвольного рукописного шрифта сейчас нет.")
        if _looks_like_edit(text):
            return _question("Уточните одно изменение: «Замени текст на «Анна»», «Высота букв 8 мм» "
                             "или «Интервал между буквами 2 мм». Текст с переносами — целиком в кавычках.",
                             _single_field_hint(text, state, awaiting))
        if _short_field_answer(text, awaiting):
            expected = ("Пришлите новый текст целиком в кавычках." if awaiting == "text" else
                        "Пришлите одно число с единицей мм: например «8 мм». Не меняю другие поля.")
            return _field_question(expected, state, _single_field_hint(text, state, awaiting))
        return None
    action, target, value = request
    saved_target = state.get("awaiting_text_target")
    if (target is None and awaiting and _short_field_answer(text, awaiting) and
            isinstance(saved_target, int) and not isinstance(saved_target, bool) and 1 <= saved_target <= 8):
        target = "надпись номер " + str(saved_target)
    operations, captions = _context(state)
    if not captions:
        return _question("Нет сохранённой текстовой надписи, которую можно изменить. "
                         "По линиям импортированного SVG я не определяю текст, шрифт или высоту. "
                         "Сначала создайте надпись, например: «Напиши «Марина» высотой 5 мм».")
    if action == "query":
        lines = []
        for order, index in enumerate(captions, 1):
            parameters = operations[index]["parameters"]
            extra = (f"рукописный готовый образец; исходная основная высота букв {parameters['height_mm']:g} мм"
                     if operations[index]["kind"] == "text" else
                     f"исходная высота заглавных {parameters.get('height_mm')} мм; "
                     f"интервал {'автоматический' if parameters.get('spacing_mm') is None else str(parameters['spacing_mm']) + ' мм'}")
            lines.append(f"{order}. «{parameters['text']}» — {extra}.")
        return _question("Сохранённые параметры, не измерение станка:\n" + "\n".join(lines) +
                         "\nОбщий масштаб/вписывание может менять итоговый размер линий.", context_query=True)
    if isinstance(state.get("preview_task"), Plan) and not _replaces_source(operations):
        return _question("Исходные линии до добавления надписи не сохранены. Нельзя полностью пересчитать "
                         "эту правку без повторного добавления уже применённых букв или преобразований. "
                         "Создайте надпись отдельным рисунком с нужным текстом и параметрами. Изменений нет.")
    index = _choose(target, captions, operations, literals)
    if index is None:
        return _question("Уточните, какую надпись изменить: " + _choices(captions, operations) +
                         ". Назовите первую/вторую/последнюю надпись или её точный текст в кавычках.",
                         waiting_request=state.get("awaiting_text_request") if _selection_target(text) and
                                         state.get("awaiting_text_request") else prompt)
    if action == "await":
        question = {"text": "Какой новый текст? Пришлите его целиком в кавычках.",
                    "height_mm": "Какую высоту заглавных задать? Пришлите число с мм, например «8 мм».",
                    "spacing_mm": "Какой интервал между буквами? Пришлите число с мм, например «2 мм»."}[value]
        return _question(question, value, waiting_target=captions.index(index) + 1)
    kind, parameters = operations[index]["kind"], operations[index]["parameters"]
    if kind == "text":
        return _question("Это готовый рукописный образец «привет», не редактируемый печатный шрифт. "
                         "Для другого слова создайте печатную надпись; не заменяю рукописный стиль молча.")
    required = {"text", "height_mm", "spacing_mm"}
    if set(parameters) not in (required, required | {"anchor", "margin_mm"}):
        return _question("Сохранённые параметры надписи некорректны. Создайте её заново.")
    if action in {"text", "upper", "lower", "content"}:
        if action == "content":
            from . import text_lines, text_mutations
            helper = text_lines if value.get("domain") == "lines" else text_mutations
            try:
                literal = helper.rewrite(parameters["text"], value)
            except (ValueError, TypeError, KeyError, OverflowError) as exc:
                return _question(str(exc) + " Последний корректный текст сохранён.")
        else:
            literal = value if action == "text" else getattr(parameters["text"], action)()
        if not isinstance(literal, str) or not 1 <= len(literal) <= 800 or not literal.strip():
            return _question("Нужен непустой текст до 800 символов, целиком в кавычках.")
        parameters["text"] = literal
        explanation = f"Изменить надпись {captions.index(index) + 1} на «{literal}». Сохранить её размеры и последующие преобразования."
        if action == "content":
            if helper is text_lines:
                explanation = value["message"] + "\n" + explanation
            elif value["operation"] in {"prepend", "append"}:
                explanation += " Слова склеиваются одним пробелом, если разделитель ещё не задан; краевые обычные пробелы нормализуются."
            else:
                explanation += " Поиск точный с учётом регистра и Ё; остальные пробелы и знаки сохраняются."
    else:
        # Absolute height cannot keep a subsequent global size multiplier.
        # Removing a shared scale would silently resize other captions.
        after = operations[index + 1:]
        if action == "height_mm" and any(item["kind"] in {"resize_width", "margin"} for item in after):
            return _question("После надписи задано общее вписывание или ширина. Чтобы высота была точно заданной, "
                             "сначала отмените это преобразование или создайте надпись с нужной высотой заново.")
        if action == "height_mm" and any(item["kind"] == "scale" for item in after):
            if len(captions) > 1 or any(item["kind"] in {"polyline", "ellipse", "ruler", "test_pattern"}
                                        for item in operations):
                return _question("Масштаб общий для нескольких надписей или фигур. Нельзя убрать его для одной надписи, "
                                 "не меняя остальные. Создайте выбранную надпись с нужной высотой отдельно.")
            operations = operations[:index + 1] + [item for item in after if item["kind"] != "scale"]
        parameters[action] = value
        label = "высоту заглавных" if action == "height_mm" else "межбуквенный интервал исходной надписи"
        explanation = f"Задать {label} {value:g} мм у надписи «{parameters['text']}». Положение, поворот и сдвиги сохраняются."
        if action == "spacing_mm" and any(item["kind"] in {"scale", "resize_width", "margin"} for item in after):
            explanation += " Последующее общее масштабирование также изменяет интервал."
    # Font validity is deterministic; fit is separately validated against the
    # complete reconstructed program and real editor snapshot by the caller.
    try:
        from .text_paths import print_text_paths
        print_text_paths(parameters["text"], parameters["height_mm"], parameters["spacing_mm"])
    except (ValueError, TypeError, OverflowError) as exc:
        return _field_question("Не удалось подготовить надпись: " + str(exc), state,
                               awaiting if _short_field_answer(text, awaiting) else None)
    return Plan("free_preview", {"operations": operations}, explanation +
                "\nЭто полный пересчёт сохранённой надписи, не добавление копии. Проверьте предпросмотр перед подтверждением.")


def edit(prompt: str, state: Mapping) -> Plan | None:
    from .text_compound import compose
    result = compose(prompt, state, edit_single, _question)
    return result if result is not None else edit_single(prompt, state)
