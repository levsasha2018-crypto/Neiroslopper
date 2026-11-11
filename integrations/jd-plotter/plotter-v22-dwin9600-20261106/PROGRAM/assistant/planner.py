"""A closed command vocabulary for preview edits, never machine movement.

Language models may explain a Plan but cannot extend this module's permissions.
Every accepted action must still be confirmed by the user in the GUI. Coordinates
are paper millimetres, with the origin at its top-left and positive y downward.
"""
from __future__ import annotations

from dataclasses import dataclass, field
import math
from pathlib import Path
import re
from typing import Mapping


ACTION_KINDS = frozenset({"center", "rotate", "scale", "resize_width", "margin",
                          "mirror", "move_preview", "place_preview", "text", "print_text",
                          "frame", "ruler", "test_pattern", "free_preview", "object_scene", "text_block"})
MAX_POINTS = 50_000
_NUMBER = r"[+-]?(?:\d+(?:[.,]\d+)?|[.,]\d+)"
_ART = r"(?:(?:рисунок|рисунка|изображение|изображения|эскиз|эскиза)\s+)?"
_SCALE_ART = r"(?:(?:рисунок|рисунка|изображение|изображения|эскиз|эскиза|надпись|надписи|текст|текста|слово|слова)\s+)?"
_MM = r"(?:мм|миллиметр(?:а|ов)?)"
_ONES = {"один": 1, "одна": 1, "одно": 1, "одну": 1, "два": 2, "две": 2,
         "три": 3, "четыре": 4, "пять": 5, "шесть": 6, "семь": 7,
         "восемь": 8, "девять": 9}
_SMALL = {"ноль": 0, "нуль": 0, **_ONES, "десять": 10, "одиннадцать": 11,
          "двенадцать": 12, "тринадцать": 13, "четырнадцать": 14,
          "пятнадцать": 15, "шестнадцать": 16, "семнадцать": 17,
          "восемнадцать": 18, "девятнадцать": 19}
_TENS = {"двадцать": 20, "тридцать": 30, "сорок": 40, "пятьдесят": 50,
         "шестьдесят": 60, "семьдесят": 70, "восемьдесят": 80, "девяносто": 90}
_HUNDREDS = {"сто": 100, "двести": 200, "триста": 300, "четыреста": 400,
             "пятьсот": 500, "шестьсот": 600, "семьсот": 700,
             "восемьсот": 800, "девятьсот": 900}


def _choice(values):
    return "(?:" + "|".join(sorted(values, key=len, reverse=True)) + ")"


_UNDER100 = r"(?:" + _choice(_TENS) + r"(?:\s+" + _choice(_ONES) + r")?|" + _choice(_SMALL) + r")"
_CARDINAL = r"(?:" + _choice(_HUNDREDS) + r"(?:\s+" + _UNDER100 + r")?|" + _UNDER100 + r")"
_THOUSANDS = (r"(?:тысяча|тысячу|(?:одна|одну)\s+тысяч(?:а|у)|"
              r"(?:две|три|четыре)\s+тысячи|(?:пять|шесть|семь|восемь|девять|десять)\s+тысяч)")
_WHOLE_WORDS = r"(?:" + _THOUSANDS + r"(?:\s+" + _CARDINAL + r")?|" + _CARDINAL + r")"
_WORD_NUMBER = r"(?:(?:минус|плюс)\s+)?(?:полтора|полторы|" + _WHOLE_WORDS + r"(?:\s+с\s+половиной)?)"
_VALUE = r"(?:" + _NUMBER + r"|" + _WORD_NUMBER + r")"
_SAMPLES = {1.5: "handwriting_1_5mm.svg", 2.0: "handwriting_2mm.svg",
            3.0: "handwriting_3mm.svg"}
PLACEMENT_ANCHORS = frozenset({"top_left", "top_right", "bottom_left", "bottom_right", "center"})
_CORNERS = {"левом верхнем": "top_left", "верхнем левом": "top_left",
            "правом верхнем": "top_right", "верхнем правом": "top_right",
            "левом нижнем": "bottom_left", "нижнем левом": "bottom_left",
            "правом нижнем": "bottom_right", "нижнем правом": "bottom_right"}
_PLACEMENT = (r"(?:(?:в|на)\s+(?:" + "|".join(_CORNERS) + r")\s+углу(?:\s+листа)?|"
              r"по\s+центру(?:\s+листа)?|в\s+центре(?:\s+листа)?)")
_ANCHOR_LABELS = {"top_left": "в левом верхнем углу", "top_right": "в правом верхнем углу",
                  "bottom_left": "в левом нижнем углу", "bottom_right": "в правом нижнем углу",
                  "center": "по центру"}


@dataclass(frozen=True)
class Plan:
    kind: str
    parameters: dict = field(default_factory=dict)
    explanation: str = ""
    question: str = ""

    @property
    def requires_confirmation(self):
        return self.kind in ACTION_KINDS


def _number(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError("Ожидается число, а не строка или логическое значение.")
    try:
        result = float(value)
    except (OverflowError, ValueError):
        raise ValueError("Число слишком большое.") from None
    if not math.isfinite(result):
        raise ValueError("Все числа должны быть конечными.")
    return result


def _parsed_number(value):
    if re.fullmatch(_NUMBER, value):
        result = float(value.replace(",", "."))
    else:
        # Deliberately small exact grammar: no approximate amounts, fractions
        # other than halves, arbitrary prose, or model-supplied arithmetic.
        if not re.fullmatch(_WORD_NUMBER, value):
            raise ValueError("Неоднозначное число словами.")
        words = value.split()
        sign = -1 if words[0] == "минус" else 1
        if words[0] in {"минус", "плюс"}:
            words = words[1:]
        if words in (["полтора"], ["полторы"]):
            return sign * 1.5
        fraction = 0.0
        if words[-2:] == ["с", "половиной"]:
            fraction = 0.5
            words = words[:-2]
        result = 0.0
        for index, word in enumerate(words):
            if word.startswith("тысяч"):
                multiplier = sum(_SMALL[w] for w in words[:index]) if index else 1
                result = multiplier * 1000
                words = words[index + 1:]
                break
        values = {**_SMALL, **_TENS, **_HUNDREDS}
        result = sign * (result + sum(values[word] for word in words) + fraction)
    if not math.isfinite(result):
        raise ValueError("Число должно быть конечным.")
    return result


def _request_text(text):
    """Remove only named polite wrappers, never general negation or prose."""
    polite = False
    for _ in range(3):
        match = re.match(r"^(?:пожалуйста,?\s+|давай\s+|"
                         r"можешь(?:\s+ли)?(?:\s+ты)?(?:,\s*|\s+)|"
                         r"можете(?:\s+ли)?(?:\s+вы)?(?:,\s*|\s+)|"
                         r"(?:не\s+)?(?:мог|могла|могли)\s+бы(?:\s+(?:ты|вы))?(?:,\s*|\s+))", text)
        if not match:
            break
        polite = True
        text = text[match.end():]
    text = re.sub(r",?\s+пожалуйста$", "", text).strip()
    if polite:
        verbs = {"повернуть": "поверни", "разместить": "размести", "поместить": "помести",
                 "поставить": "поставь", "выровнять": "выровняй", "перенести": "перенеси",
                 "сдвинуть": "сдвинь", "переместить": "перемести", "отразить": "отрази",
                 "вписать": "впиши", "сделать": "сделай", "задать": "задай",
                 "установить": "установи", "увеличить": "увеличь", "уменьшить": "уменьши",
                 "написать": "напиши", "напечатать": "напечатай",
                 "подготовить": "подготовь", "создать": "создай", "добавить": "добавь",
                 "объяснить": "объясни"}
        first, separator, rest = text.partition(" ")
        text = verbs.get(first, first) + separator + rest
    typos = {"повирни": "поверни", "поворни": "поверни", "рисуннок": "рисунок",
             "рисуок": "рисунок", "эскизз": "эскиз", "перимитру": "периметру",
             "милеметр": "миллиметр", "милеметра": "миллиметра", "милеметров": "миллиметров",
             "милиметр": "миллиметр", "милиметра": "миллиметра", "милиметров": "миллиметров"}
    return re.sub(r"\b(?:" + "|".join(typos) + r")\b", lambda match: typos[match[0]], text)


def _sheet(width, height):
    width, height = _number(width), _number(height)
    if not (1 <= width <= 3000 and 1 <= height <= 3000):
        raise ValueError("Размеры листа должны быть от 1 до 3000 мм.")
    return width, height


def _question(text):
    return Plan("question", explanation="Изменений нет.", question=text)


def _ready(plan, state):
    if plan.kind not in ACTION_KINDS:
        return plan
    try:
        width, height = _sheet(state.get("sheet_width"), state.get("sheet_height"))
    except (TypeError, ValueError):
        return _question("Сначала задай два угла листа кнопками программы. Какой размер получился?")
    if plan.kind not in {"text", "print_text", "ruler", "test_pattern"} and not state.get("has_paths", False):
        return _question("Сначала открой рисунок: пока нечего изменять в предпросмотре.")
    if plan.kind == "margin" and 2 * plan.parameters["margin_mm"] >= min(width, height):
        return _question("Поле должно быть меньше половины короткой стороны листа. Какое поле выбрать?")
    if plan.kind == "resize_width" and plan.parameters["width_mm"] > width:
        return _question("Такая ширина больше листа. Какую меньшую ширину выбрать?")
    if plan.kind in {"place_preview", "print_text"} and "margin_mm" in plan.parameters:
        if 2 * plan.parameters["margin_mm"] > min(width, height):
            return _question("Такой отступ не оставляет места на листе. Какой меньший отступ выбрать?")
    return plan


def _placement_anchor(phrase):
    for corner, anchor in _CORNERS.items():
        if re.search(r"\b" + corner + r"\b", phrase):
            return anchor
    return "center"


def _placed_text_command(command):
    """Extract only a fully delimited quoted payload or one literal word.

    No text outside the payload is dropped. The returned payload span is also
    used by the security gate; instruction-like glyphs remain literal data.
    """
    quoted = re.fullmatch(r'([^«»"]*)(?:«([^«»]+)»|"([^"]+)")([^«»"]*)', command)
    if quoted is not None:
        group = 2 if quoted[2] is not None else 3
        literal = quoted[group]
        start, end = quoted.span(group)
        skeleton = command[:start - 1] + "__printed_text__" + command[end + 1:]
    else:
        word = re.fullmatch(r"(?i)(\s*(?:напиши|напечатай)\s+)([a-zа-яё0-9]+)(\s+.+)", command)
        front = re.fullmatch(r"(?i)(\s*(?:напиши|напечатай)\s+)(" + _PLACEMENT +
            r")\s+(?:слово\s+)?([a-zа-яё0-9]+)(\s*.*)", command)
        if front is not None:
            literal, (start, end) = front[3], front.span(3)
            skeleton = front[1] + "__printed_text__ " + front[2] + front[4]
        else:
            if word is None:
                return None
            literal, (start, end) = word[2], word.span(2)
            skeleton = word[1] + "__printed_text__" + word[3]
    skeleton = _request_text(re.sub(r"\s+", " ", skeleton.casefold().replace("ё", "е")).strip(" .!?"))
    front = re.fullmatch(r"((?:напиши(?:\s+печатными(?:\s+буквами)?)?|напечатай)\s+)(" +
        _PLACEMENT + r")\s+(?:слово\s+)?__printed_text__(.*)", skeleton)
    if front is not None:
        skeleton = front[1] + "__printed_text__ " + front[2] + front[3]
    match = re.fullmatch(
        r"(?:напиши(?:\s+печатными(?:\s+буквами)?)?|напечатай|подготовь\s+печатный\s+текст)\s+"
        r"__printed_text__\s+(?:(?:высотой|размером)\s+(" + _VALUE + r")\s*" + _MM + r"\s+)?"
        r"(" + _PLACEMENT + r")"
        r"(?:\s+(?:высотой|размером)\s+(" + _VALUE + r")\s*" + _MM + r")?"
        r"(?:\s+с\s+отступом\s+(" + _VALUE + r")\s*" + _MM + r")?", skeleton)
    if match is None or (match[1] is not None and match[3] is not None):
        return None
    return literal, (start, end), match


def _placed_printed_text_plan(command, state):
    parsed = _placed_text_command(command)
    if parsed is None:
        return None
    literal, _, match = parsed
    try:
        height = _parsed_number(match[1] or match[3]) if (match[1] or match[3]) else 5.0
        margin = _parsed_number(match[4]) if match[4] is not None else 3.0
    except (OverflowError, ValueError):
        return _question("Укажи конечные высоту и отступ в миллиметрах.")
    if not literal.strip():
        return _question("Укажи текст внутри кавычек.")
    if not 1 <= height <= 100:
        return _question("Для печатных букв укажи высоту заглавных от 1 до 100 мм.")
    if not 0 <= margin <= 3000:
        return _question("Отступ должен быть от 0 до 3000 мм и оставлять место для текста на листе.")
    anchor = _placement_anchor(match[2])
    return _ready(Plan("print_text", {"text": literal, "height_mm": height, "spacing_mm": None,
                                      "anchor": anchor, "margin_mm": margin},
                       f"Подготовить печатный текст «{literal}»: номинальная высота заглавных {height:g} мм; "
                       f"разместить {_ANCHOR_LABELS[anchor]} листа, отступ {margin:g} мм по фактическим штрихам. "
                       "Диакритика и выносные элементы входят в проверку границ. Без масштабирования и обрезки; "
                       "если текст не помещается, изменение отменяется. Это только предпросмотр, "
                       "после отдельного подтверждения. Без подъёма пера переходы между штрихами тоже рисуются."), state)


def _printed_text_plan(command, state):
    """A literal quoted payload is text data, never reparsed as an instruction.

    The whole outside command must match the closed print grammar. The caller
    checks code/injection patterns only outside this explicitly literal payload.
    Case, ё and spacing inside the payload are preserved.
    """
    quoted = re.fullmatch(r'([^«»"]*)(?:«([^«»]+)»|"([^"]+)")([^«»"]*)', command.strip())
    if quoted is None:
        return None
    prefix, french, plain, suffix = quoted.groups()
    literal = french if french is not None else plain
    skeleton = prefix + "__printed_text__" + suffix
    skeleton = _request_text(re.sub(r"\s+", " ", skeleton.casefold().replace("ё", "е")).strip(" .!?"))
    match = re.fullmatch(
        r"(?:напечатай|напиши\s+печатными(?:\s+буквами)?|подготовь\s+печатный\s+текст)\s+"
        r"__printed_text__\s+(?:(?:высотой|размером)\s+)?(" + _VALUE + r")\s*" + _MM +
        r"(?:\s+с\s+(?:интервалом|межбуквенным\s+интервалом)\s+(" + _VALUE + r")\s*" + _MM + r")?",
        skeleton)
    if match is None:
        return None
    try:
        height = _parsed_number(match[1])
        spacing = _parsed_number(match[2]) if match[2] is not None else None
    except (OverflowError, ValueError):
        return _question("Укажи конечные высоту и интервал печатных букв в миллиметрах.")
    if not literal.strip():
        return _question("Укажи текст внутри кавычек.")
    if not 1 <= height <= 100:
        return _question("Для печатных букв укажи высоту заглавных от 1 до 100 мм.")
    if spacing is not None and not 0 <= spacing <= 100:
        return _question("Межбуквенный интервал должен быть от 0 до 100 мм.")
    return _ready(Plan("print_text", {"text": literal, "height_mm": height, "spacing_mm": spacing},
                       f"Подготовить печатный текст «{literal}»: номинальная высота заглавных {height:g} мм; "
                       "диакритика и выносные штрихи могут выходить выше или ниже. "
                       "Разместить по центру листа без изменения указанного размера. "
                       "Без подъёма пера переходы между штрихами тоже рисуются."), state)


def _unquoted_word_plan(command, state):
    """One explicit word plus height; phrases still require literal quotes."""
    match = re.fullmatch(r"(?i)\s*напиши\s+([a-zа-яё0-9]+)\s+(?:высотой|размером)\s+(" +
                         _VALUE + r")\s*" + _MM + r"[.!?]?\s*", command)
    if match is None or match[1].casefold().replace("ё", "е") == "привет":
        return None
    # Reuse the same strict print grammar without interpreting payload words.
    return _printed_text_plan(f"Напечатай «{match[1]}» высотой {match[2]} мм", state)


def parse(command: str, state: Mapping | None = None) -> Plan:
    """Parse only one whole, unambiguous, permitted preview command.

    Optional state keys are sheet_width, sheet_height, has_paths and preview_anchor.
    A preview_anchor supplied by the editor preserves an existing text corner
    when scaling; it is not guessed from conversation or supplied by a model.
    Missing
    prerequisites produce a question, not an action. A classifier/model result
    must never be passed here as trusted parameters: always parse user text.
    """
    state = state if isinstance(state, Mapping) else {}
    if not isinstance(command, str) or not command.strip():
        return _question("Что изменить в рисунке? Например: «по центру» или «поверни на 90 градусов».")
    if len(command) > 1000:
        return _question("Напиши одну короткую команду без вставленного кода.")
    raw = command.strip().lower().replace("ё", "е")
    printed = _printed_text_plan(command, state)
    if printed is None:
        printed = _placed_printed_text_plan(command, state)
    if printed is None:
        printed = _unquoted_word_plan(command, state)
    security_text = command.lower().replace("ё", "е")
    if printed is not None:
        quoted = re.fullmatch(r'([^«»"]*)(?:«([^«»]+)»|"([^"]+)")([^«»"]*)', command)
        if quoted is not None:
            group = 2 if quoted[2] is not None else 3
            start, end = quoted.span(group)
            # Every glyph inside the full-match print payload is literal data,
            # even 'G0', 'ignore' or motor verbs. Never reparse or execute it.
            # Outside newlines/code remain visible to the security gate.
            security_text = (command[:start] + "__literal_glyphs__" + command[end:]).lower().replace("ё", "е")
        else:
            placed = _placed_text_command(command)
            word_match = re.fullmatch(r"(?i)\s*напиши\s+([a-zа-яё0-9]+)\s+(?:высотой|размером)\s+(" +
                                      _VALUE + r")\s*" + _MM + r"[.!?]?\s*", command)
            if placed is not None:
                start, end = placed[1]
                security_text = (command[:start] + "__literal_glyphs__" + command[end:]).lower().replace("ё", "е")
            elif word_match is not None:
                start, end = word_match.span(1)
                security_text = (command[:start] + "__literal_glyphs__" + command[end:]).lower().replace("ё", "е")
    # No code, controller commands, shell fragments, URLs or mixed requests
    # outside a fully validated explicit literal text payload.
    unsafe = (r"[;$`\n\r]|```|https?://|\b(?:g\s*\d+|m\s*\d+)\b|"
              r"(?:powershell|cmd\.exe|subprocess|os\.|exec\s*\(|eval\s*\(|"
              r"игнорируй|ignore\b|инструкции|системный\s+промпт|g-code|gcode|g-код)")
    if re.search(unsafe, security_text):
        return Plan("refused", explanation="Код, G-code и обход правил не выполняются. Изменений нет.")
    if printed is not None:
        return printed
    text = re.sub(r"\s+", " ", raw).strip(" .!?")
    text = _request_text(text)
    try:
        for value in re.findall(_NUMBER, text):
            _parsed_number(value)
    except (OverflowError, ValueError):
        return _question("Укажи обычное конечное число в допустимых размерах листа.")
    if re.search(r"\b(?:потом|затем|после этого|а также|и еще)\b|[,;&]", text.replace(",", ".")):
        return _question("Выполняем одну команду за раз. Какое изменение сделать первым?")
    if re.match(r"^(?:не|ничего\s+не)\b", text):
        return _question("Ничего не изменено. Если нужна подготовка рисунка, сформулируй одно желаемое изменение без отрицания.")
    if text in {"статус", "состояние", "покажи статус", "что сейчас", "что загружено",
                "что сейчас загружено", "покажи что загружено"}:
        return Plan("status", explanation="Запрошено состояние программы, без изменений и движения.")
    if text in {"помощь", "справка", "что ты умеешь", "команды", "покажи команды"}:
        return Plan("help", explanation="Умею готовить предпросмотр: центр, размещение по углам, поворот, масштаб, ширина, поля, отражение, сдвиг, рамка, линейка и тест точности. Печатные слова на кириллице и латинице — 1–100 мм; фразы и строки внутри кавычек. Например: «напиши привет в левом верхнем углу листа» — печатные буквы высотой заглавных 5 мм, отступ 3 мм. Рукописный образец «привет» — 1,5 / 2 / 3 мм. Каждое изменение подтверждается отдельно; моторы не двигаются.")
    if re.match(r"^(?:почему|как|зачем|что такое|что если|можно ли|стоит ли|нужно ли|"
                r"какой|какая|какие|объясни|расскажи|поясни|покажи как|если)\b", text):
        return Plan("unsupported", explanation="Это вопрос для помощника, не команда изменения рисунка.")
    if re.search(r"\b(?:мотор\w*|каретк\w*|головк\w*|портал\w*|станок|станк\w*|"
                 r"плоттер\w*|устройств\w*|базир\w*|homing|прош\w*|"
                 r"скорост\w*|ускорени\w*|ток|драйвер\w*|удержани\w*|запусти|"
                 r"начни|рисуй|нарисуй|останови|подними|опусти|двигай|двинь)\b", text):
        return Plan("refused", explanation="Помощник не двигает моторы, не запускает рисование и не меняет настройки станка. Используй явные кнопки движения, запуска и настройки в программе.")
    placement = re.fullmatch(r"(?:размести|помести|поставь|перенеси)\s+(?:рисунок|изображение|эскиз)\s+"
                             r"(" + _PLACEMENT + r")(?:\s+с\s+отступом\s+(" + _VALUE + r")\s*" + _MM + r")?", text)
    if placement and ("углу" in placement[1] or placement[2] is not None):
        anchor = _placement_anchor(placement[1])
        margin = _parsed_number(placement[2]) if placement[2] is not None else 3.0
        if not 0 <= margin <= 3000:
            return _question("Отступ должен быть от 0 до 3000 мм и оставлять место для рисунка на листе.")
        return _ready(Plan("place_preview", {"anchor": anchor, "margin_mm": margin},
                           f"Разместить рисунок {_ANCHOR_LABELS[anchor]} листа с отступом {margin:g} мм "
                           "по фактической границе всех штрихов. Размер не меняется; без масштабирования и обрезки. "
                           "Если рисунок не помещается, изменение отменяется. Только предпросмотр, моторы не двигаются."), state)
    frame = re.fullmatch(r"(?:добавь|сделай|создай)\s+(?:прямоугольную\s+)?рамку\s+"
                         r"(?:вокруг\s+(?:рисунка|изображения|эскиза)\s+)?с\s+отступом\s+(" + _VALUE + r")\s*" + _MM, text)
    if frame:
        padding = _parsed_number(frame[1])
        if not 0 <= padding <= 3000:
            return _question("Отступ рамки должен быть от 0 до 3000 мм; сама рамка должна помещаться на листе.")
        return _ready(Plan("frame", {"padding_mm": padding},
                           f"Добавить прямоугольную рамку вокруг эскиза с отступом {padding:g} мм. "
                           "Сам эскиз не меняется. Без подъёма пера переход к рамке тоже рисуется."), state)
    ruler = re.fullmatch(r"(?:(?:создай|сделай|подготовь)\s+)?(?:линейку|линейка)\s+"
                         r"(?:длиной\s+)?(" + _VALUE + r")\s*" + _MM, text)
    if ruler:
        length = _parsed_number(ruler[1])
        if not 1 <= length <= 1000:
            return _question("Укажи длину тестовой линейки от 1 до 1000 мм.")
        return _ready(Plan("ruler", {"length_mm": length},
                           f"Подготовить одноконтурную линейку длиной {length:g} мм с делениями через 1 мм. "
                           "Штрихи проходятся туда и обратно, без подъёма пера. Центрировать на листе "
                           "без масштабирования. Это образец для измерения, не автоматическая калибровка."), state)
    test = re.fullmatch(r"(?:(?:создай|сделай|подготовь)\s+)?тест\s+точности\s+"
                        r"(?:размером\s+)?(" + _VALUE + r")\s*" + _MM, text)
    if test:
        size = _parsed_number(test[1])
        if not 1 <= size <= 200:
            return _question("Укажи сторону тестового квадрата от 1 до 200 мм.")
        return _ready(Plan("test_pattern", {"size_mm": size},
                           f"Подготовить один связный контур: квадрат {size:g} × {size:g} мм, "
                           f"диагонали, крест и окружность диаметром {size / 2:g} мм. "
                           "Некоторые линии проходятся повторно. Центрировать без масштабирования. "
                           "Это измерительный эскиз, не настройка станка."), state)
    if re.fullmatch(r"(?:(?:размести|помести|поставь|выровняй)\s+" + _ART + r")?по центру(?:\s+листа)?|"
                    r"(?:размести|помести|поставь|перенеси)\s+(?:рисунок|изображение|эскиз)\s+в центр(?:\s+листа)?|"
                    r"(?:центрируй|отцентруй)(?:\s+(?:рисунок|изображение|эскиз))?(?:\s+на листе)?", text):
        return _ready(Plan("center", explanation="Переместить рисунок в центр листа, не меняя размер. Это только предпросмотр."), state)
    match = re.fullmatch(r"поверни\s+" + _ART + r"(?:на\s+)?(" + _VALUE + r")\s*(?:градус(?:ов|а)?|°)?(?:\s+(по|против)\s+часовой(?:\s+стрелки)?)?", text)
    if match:
        degrees = _parsed_number(match[1])
        if not -360 <= degrees <= 360:
            return _question("Укажи угол от −360 до 360 градусов.")
        if match[2] and degrees < 0:
            return _question("Укажи положительный угол и одно направление поворота.")
        if match[2] == "против":
            degrees = -degrees
        return _ready(Plan("rotate", {"degrees": degrees}, f"Повернуть предпросмотр на {degrees:g}° вокруг центра рисунка; положительный угол — по часовой стрелке."), state)
    match = re.fullmatch(r"(?:масштаб(?:ируй)?\s+" + _SCALE_ART + r"(?:(?:до|на)\s+)?|(?:увеличь|уменьши)\s+" + _SCALE_ART + r"до\s+)(" + _VALUE + r")\s*(?:%|процент(?:ов|а)?)", text)
    percent = _parsed_number(match[1]) if match else None
    relative = re.fullmatch(r"(увеличь|уменьши)\s+" + _SCALE_ART + r"на\s+(" + _VALUE + r")\s*(?:%|процент(?:ов|а)?)", text)
    if relative:
        change = _parsed_number(relative[2])
        if change < 0:
            return _question("Укажи неотрицательный процент изменения.")
        percent = 100 + change if relative[1] == "увеличь" else 100 - change
    ratio = re.fullmatch(r"(увеличь|уменьши)\s+" + _SCALE_ART + r"в\s*(" + _VALUE + r")\s*раз(?:а)?", text)
    made_ratio = re.fullmatch(r"сделай\s+" + _SCALE_ART + r"в\s*(" + _VALUE + r")\s*раз(?:а)?\s+(больше|меньше)", text)
    reversed_ratio = re.fullmatch(r"сделай\s+" + _SCALE_ART + r"(больше|меньше)\s+в\s*(" + _VALUE + r")\s*раз(?:а)?", text)
    if ratio or made_ratio or reversed_ratio:
        increase = (ratio[1] == "увеличь" if ratio else
                    made_ratio[2] == "больше" if made_ratio else reversed_ratio[1] == "больше")
        value = _parsed_number(ratio[2] if ratio else made_ratio[1] if made_ratio else reversed_ratio[2])
        if value <= 0:
            return _question("Коэффициент должен быть положительным.")
        percent = value * 100 if increase else 100 / value
    if percent is not None:
        if not 0 < percent <= 10000:
            return _question("Для предпросмотра укажи масштаб больше 0 и не больше 10 000%.")
        parameters = {"percent": percent}
        anchor = state.get("preview_anchor")
        if anchor is not None:
            if not isinstance(anchor, str) or anchor not in PLACEMENT_ANCHORS:
                return _question("Положение текущего эскиза неизвестно. Сначала явно размести эскиз или выбери центр.")
            parameters["anchor"] = anchor
        pivot = ("центра" if anchor in {None, "center"} else
                 "точки привязки " + _ANCHOR_LABELS[anchor])
        return _ready(Plan("scale", parameters, f"Размер предпросмотра станет {percent:g}% от текущего, с сохранением пропорций и {pivot}."), state)
    match = re.fullmatch(r"(?:(?:сделай|задай|измени|установи)\s+)?ширин(?:а|у)\s+" + _ART + r"(?:(?:до|на)\s+)?(" + _VALUE + r")\s*" + _MM, text)
    if match:
        width = _parsed_number(match[1])
        if not 0 < width <= 3000:
            return _question("Ширина рисунка должна быть положительной и не больше 3000 мм.")
        return _ready(Plan("resize_width", {"width_mm": width}, f"Задать ширину предпросмотра {width:g} мм, сохранив пропорции и центр рисунка."), state)
    match = re.fullmatch(r"(?:впиши\s+" + _ART + r"(?:в\s+лист\s+)?с\s+(?:полем|полями|отступом)\s+|"
                        r"(?:(?:сделай|задай|установи)\s+)?(?:поле|поля|отступ)\s+)(" + _VALUE + r")\s*" +
                        _MM + r"(?:\s+(?:по периметру|со всех сторон))?", text)
    if match:
        margin = _parsed_number(match[1])
        if not 0 <= margin <= 1500:
            return _question("Поле должно быть неотрицательным и меньше половины короткой стороны листа.")
        return _ready(Plan("margin", {"margin_mm": margin}, f"Вписать предпросмотр в лист с полем {margin:g} мм со всех сторон, сохранив пропорции и расположив по центру."), state)
    if re.match(r"^(?:отрази|зеркально отрази)\b", text) and re.search(r"\b(?:оси|ось)\b", text):
        return _question("Уточни отражение: поменять левую и правую стороны («по горизонтали») или верх и низ («по вертикали»)?")
    match = re.fullmatch(r"(?:отрази|зеркально отрази)\s+" + _ART + r"(?:по\s+)?(горизонтали|вертикали|горизонтально|вертикально)", text)
    if match:
        axis = "horizontal" if match[1].startswith("горизонт") else "vertical"
        detail = "поменять левую и правую стороны" if axis == "horizontal" else "поменять верх и низ"
        return _ready(Plan("mirror", {"axis": axis}, f"Зеркально отразить предпросмотр: {detail}. Центр рисунка останется на месте."), state)
    # An implicit 'move right' is unsafe: users may mean the physical carriage.
    shift = r"(?:сдвинь|перемести|перенеси)\s+(?:рисунок|изображение|эскиз)\s+"
    direction_words = r"(вправо|влево|вверх|вниз|правее|левее|выше|ниже)"
    match = re.fullmatch(shift + direction_words + r"\s+(?:на\s+)?(" + _VALUE + r")\s*" + _MM, text)
    reverse = re.fullmatch(shift + r"(?:на\s+)?(" + _VALUE + r")\s*" + _MM + r"\s+" + direction_words, text)
    if match or reverse:
        direction, amount = (match[1], _parsed_number(match[2])) if match else (reverse[2], _parsed_number(reverse[1]))
        direction = {"правее": "вправо", "левее": "влево", "выше": "вверх", "ниже": "вниз"}.get(direction, direction)
        if not 0 <= amount <= 3000:
            return _question("Для сдвига укажи расстояние от 0 до 3000 мм и направление словами.")
        dx, dy = {"вправо": (amount, 0), "влево": (-amount, 0),
                  "вниз": (0, amount), "вверх": (0, -amount)}[direction]
        return _ready(Plan("move_preview", {"dx_mm": dx, "dy_mm": dy}, f"Сдвинуть только рисунок в предпросмотре {direction} на {amount:g} мм. Каретка не двигается."), state)
    match = re.fullmatch(r"(?:напиши|подготовь\s+текст)\s+[«\"']?([^«»\"']+?)[»\"']?(?:\s+(?:высотой|размером)\s+|\s+)(" + _VALUE + r")\s*" + _MM, text)
    if match:
        word, height = match[1].strip(), _parsed_number(match[2])
        if word != "привет" or height not in _SAMPLES:
            return _question("Пока есть только готовое рукописное «привет» высотой строчных 1,5 / 2 / 3 мм. Какой образец выбрать?")
        return _ready(Plan("text", {"text": "привет", "height_mm": height}, f"Подготовить готовый рукописный образец «привет»: высота основных строчных {height:g} мм, выносные элементы выше и ниже. Масштаб 1:1, поле 3 мм; это не универсальный генератор текста."), state)
    if re.match(r"^(?:напиши|подготовь\s+текст)\b", text):
        return _question("Пока поддерживается только готовое «привет» высотой строчных 1,5 / 2 / 3 мм. Какой размер выбрать?")
    if re.match(r"^(?:сдвинь|перемести|перенеси)\b", text):
        return _question("Если нужно изменить только предпросмотр, укажи «сдвинь рисунок вправо на 5 мм». Для каретки используй кнопки движения.")
    if " и " in text:
        return _question("Выполняем одну команду за раз. Какое изменение сделать первым?")
    return Plan("unsupported", explanation="Не удалось однозначно распознать безопасную команду. Ничего не изменено. Примеры: «по центру», «поверни на 90 градусов», «впиши в лист с полем 5 мм».")


def _copy_paths(paths, width, height, *, check_bounds=True):
    if not isinstance(paths, (list, tuple)) or not paths:
        raise ValueError("В рисунке нет линий.")
    copied, count = [], 0
    for path in paths:
        if not isinstance(path, (list, tuple)) or len(path) < 2:
            raise ValueError("Каждая линия должна содержать хотя бы две точки.")
        result = []
        for point in path:
            count += 1
            if count > MAX_POINTS:
                raise ValueError("Максимум 50 000 точек в рисунке.")
            if not isinstance(point, (list, tuple)) or len(point) != 2:
                raise ValueError("Точка должна содержать две координаты.")
            x, y = (_number(v) for v in point)
            if check_bounds and not (-1e-7 <= x <= width + 1e-7 and -1e-7 <= y <= height + 1e-7):
                raise ValueError("Рисунок выходит за границы листа; изменение отменено.")
            result.append((min(width, max(0.0, x)), min(height, max(0.0, y))) if check_bounds else (x, y))
        copied.append(result)
    return copied


def _bounds(paths):
    points = [point for path in paths for point in path]
    left, top = min(x for x, _ in points), min(y for _, y in points)
    right, bottom = max(x for x, _ in points), max(y for _, y in points)
    if max(right - left, bottom - top) <= 1e-9:
        raise ValueError("У рисунка должен быть ненулевой размер хотя бы по одной стороне.")
    return left, top, right, bottom


def _placement_parameters(anchor, margin):
    if not isinstance(anchor, str) or anchor not in PLACEMENT_ANCHORS:
        raise ValueError("Неизвестное положение рисунка на листе.")
    margin = _number(margin)
    if not 0 <= margin <= 3000:
        raise ValueError("Отступ должен быть от 0 до 3000 мм.")
    return anchor, margin


def _place_generated(paths, width, height, anchor="center", margin=0):
    """Translate the true ink bbox, including all accents, without changing size.

    Margin is a minimum distance from every edge, including centered layouts.
    No clipping, wrapping, rotation or scale-to-fit occurs. Source paths can be
    local generated geometry; the returned paths must all fit the known sheet.
    """
    anchor, margin = _placement_parameters(anchor, margin)
    source = _copy_paths(paths, width, height, check_bounds=False)
    left, top, right, bottom = _bounds(source)
    ink_width, ink_height = right - left, bottom - top
    if (width - 2 * margin < 0 or height - 2 * margin < 0 or
            ink_width > width - 2 * margin + 1e-7 or ink_height > height - 2 * margin + 1e-7):
        raise ValueError("Эскиз указанного размера и отступа не помещается на листе. Уменьши размер или отступ явно; автоматического масштабирования и обрезки нет.")
    target_left = margin if anchor.endswith("left") else width - margin - ink_width
    target_top = margin if anchor.startswith("top") else height - margin - ink_height
    if anchor == "center":
        target_left, target_top = (width - ink_width) / 2, (height - ink_height) / 2
    dx, dy = target_left - left, target_top - top
    return _copy_paths([[(x + dx, y + dy) for x, y in path] for path in source], width, height)


def _center_generated(paths, width, height):
    return _place_generated(paths, width, height)


def transform_paths(paths, plan: Plan, sheet_width, sheet_height):
    """Return a validated copy, or raise ValueError; never modify original paths.

    The caller must obtain fresh per-request user confirmation before replacing
    its preview. No hardware, controller, G-code, shell or network is involved.
    Legacy handwriting reads only its hardcoded sample; block letters and test
    shapes are generated by local deterministic modules, never supplied paths.
    """
    width, height = _sheet(sheet_width, sheet_height)
    if not isinstance(plan, Plan) or plan.kind not in ACTION_KINDS:
        raise ValueError("Это не разрешенная команда изменения предпросмотра.")
    if plan.kind == 'object_scene':
        if not isinstance(plan.parameters, dict) or set(plan.parameters) != {'scene'}:
            raise ValueError("Неверные поля сцены объектов.")
        from .scene import render_scene
        return render_scene(plan.parameters['scene'], width, height)
    if plan.kind == 'text_block':
        fields = {'text', 'width_mm', 'height_mm', 'font_height_mm', 'spacing_mm', 'line_spacing', 'align'}
        if not isinstance(plan.parameters, dict) or set(plan.parameters) != fields:
            raise ValueError("Неверные параметры текстового блока.")
        from .text_block import layout_block
        p = plan.parameters
        block = layout_block(p['text'], p['width_mm'], p['height_mm'], p['font_height_mm'],
                             p['spacing_mm'], p['line_spacing'], p['align'])
        if p['width_mm'] > width or p['height_mm'] > height:
            raise ValueError("Прямоугольник блока не помещается в лист.")
        dx, dy = (width - p['width_mm']) / 2, (height - p['height_mm']) / 2
        return _copy_paths([[(x + dx, y + dy) for x, y in line] for line in block.paths], width, height)
    if plan.kind == "free_preview":
        if not isinstance(plan.parameters, dict) or set(plan.parameters) != {"operations"}:
            raise ValueError("Неверные параметры составного предложения.")
        from .free_preview import execute_preview_program
        return execute_preview_program(paths, plan.parameters["operations"], width, height)
    expected = {"center": set(), "rotate": {"degrees"}, "scale": {"percent"},
                "resize_width": {"width_mm"}, "margin": {"margin_mm"},
                "mirror": {"axis"}, "move_preview": {"dx_mm", "dy_mm"},
                "place_preview": {"anchor", "margin_mm"},
                "text": {"text", "height_mm"},
                "print_text": {"text", "height_mm", "spacing_mm"},
                "frame": {"padding_mm"}, "ruler": {"length_mm"},
                "test_pattern": {"size_mm"}}[plan.kind]
    # Existing print plans keep their exact schema and centered default. A
    # placed print plan must carry both explicit placement fields, not one
    # implicit default supplied by a model or stale UI state.
    placed_print = (plan.kind == "print_text" and isinstance(plan.parameters, dict)
                    and set(plan.parameters) == expected | {"anchor", "margin_mm"})
    anchored_scale = (plan.kind == "scale" and isinstance(plan.parameters, dict)
                      and set(plan.parameters) == expected | {"anchor"})
    if not isinstance(plan.parameters, dict) or (set(plan.parameters) != expected and not placed_print and not anchored_scale):
        raise ValueError("Неверные или лишние параметры команды.")
    p = plan.parameters
    if plan.kind == "print_text":
        from .text_paths import print_text_paths
        height_mm = _number(p["height_mm"])
        spacing_mm = None if p["spacing_mm"] is None else _number(p["spacing_mm"])
        if not isinstance(p["text"], str) or not 1 <= height_mm <= 100:
            raise ValueError("Нужны текст и высота печатных букв от 1 до 100 мм.")
        if spacing_mm is not None and not 0 <= spacing_mm <= 100:
            raise ValueError("Межбуквенный интервал должен быть от 0 до 100 мм.")
        local = print_text_paths(p["text"], height_mm, spacing_mm)
        if placed_print:
            return _place_generated(local, width, height, p["anchor"], p["margin_mm"])
        return _center_generated(local, width, height)
    if plan.kind == "ruler":
        from .drawing_tools import ruler_paths
        return _center_generated(ruler_paths(p["length_mm"]), width, height)
    if plan.kind == "test_pattern":
        from .drawing_tools import calibration_pattern_paths
        return _center_generated(calibration_pattern_paths(p["size_mm"]), width, height)
    if plan.kind == "text":
        size = _number(p["height_mm"])
        if p["text"] != "привет" or size not in _SAMPLES:
            raise ValueError("Допускается только готовое «привет» размером 1,5 / 2 / 3 мм.")
        from geometry import Sheet, load_svg
        result = load_svg(Path(__file__).resolve().parent.parent / _SAMPLES[size], Sheet(width, height), margin=3)
        result = _copy_paths(result, width, height)
        _bounds(result)
        return result
    source = _copy_paths(paths, width, height)
    left, top, right, bottom = _bounds(source)
    if plan.kind == "place_preview":
        return _place_generated(source, width, height, p["anchor"], p["margin_mm"])
    if plan.kind == "frame":
        from .drawing_tools import rectangle_frame
        result = _copy_paths(source + [rectangle_frame((left, top, right, bottom), p["padding_mm"])], width, height)
        _bounds(result)
        return result
    cx, cy = (left + right) / 2, (top + bottom) / 2
    sx, sy, dx, dy, radians = 1.0, 1.0, 0.0, 0.0, 0.0
    if plan.kind == "center":
        dx, dy = width / 2 - cx, height / 2 - cy
    elif plan.kind == "rotate":
        degrees = _number(p["degrees"])
        if not -360 <= degrees <= 360:
            raise ValueError("Угол должен быть от −360 до 360 градусов.")
        radians = math.radians(degrees)
    elif plan.kind == "scale":
        percent = _number(p["percent"])
        if not 0 < percent <= 10000:
            raise ValueError("Масштаб должен быть больше 0 и не больше 10 000%.")
        sx = sy = percent / 100
        if anchored_scale:
            anchor = p["anchor"]
            if not isinstance(anchor, str) or anchor not in PLACEMENT_ANCHORS:
                raise ValueError("Неизвестная точка привязки масштаба.")
            if anchor != "center":
                cx = left if anchor.endswith("left") else right
                cy = top if anchor.startswith("top") else bottom
    elif plan.kind == "resize_width":
        target = _number(p["width_mm"])
        if not 0 < target <= width:
            raise ValueError("Ширина рисунка должна быть положительной и не больше ширины листа.")
        if right - left <= 1e-9:
            raise ValueError("У вертикальной линии нельзя изменить ширину с сохранением пропорций. Сначала поверни рисунок.")
        sx = sy = target / (right - left)
    elif plan.kind == "margin":
        margin = _number(p["margin_mm"])
        if margin < 0 or 2 * margin >= min(width, height):
            raise ValueError("Поле должно быть меньше половины короткой стороны листа.")
        sx = sy = min((width - 2 * margin) / (right - left) if right - left > 1e-9 else math.inf,
                      (height - 2 * margin) / (bottom - top) if bottom - top > 1e-9 else math.inf)
        dx, dy = width / 2 - cx, height / 2 - cy
    elif plan.kind == "mirror":
        if not isinstance(p["axis"], str) or p["axis"] not in {"horizontal", "vertical"}:
            raise ValueError("Неизвестная ось отражения.")
        sx, sy = (-1.0, 1.0) if p["axis"] == "horizontal" else (1.0, -1.0)
    elif plan.kind == "move_preview":
        dx, dy = _number(p["dx_mm"]), _number(p["dy_mm"])
        if max(abs(dx), abs(dy)) > 3000:
            raise ValueError("Сдвиг слишком большой для листа.")
    cosine, sine = math.cos(radians), math.sin(radians)
    def point(x, y):
        ax, ay = (x - cx) * sx, (y - cy) * sy
        return cx + ax * cosine - ay * sine + dx, cy + ax * sine + ay * cosine + dy
    result = _copy_paths([[point(x, y) for x, y in path] for path in source], width, height)
    _bounds(result)
    return result
