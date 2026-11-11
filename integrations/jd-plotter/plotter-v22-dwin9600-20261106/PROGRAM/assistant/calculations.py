"""Bounded numeric calculations from explicit inputs, never eval or movement."""
import math
import re

NUMBER = r"\d{1,7}(?:[.,]\d{1,6})?"
END = r"[.!?]?"
# Clauses are matched as a whole. These separators never introduce a number;
# allowing a free-form prefix could silently discard a negative sign or code.
QUESTION_SEPARATOR = r"\s*(?:[,.!?]|[—–])?\s*"
AREA_QUESTION = (r"(?:какая\s+(?:будет\s+)?(?:доступная|рабочая)\s+область|"
                 r"(?:посчитай|рассчитай|вычисли)\s+(?:(?:доступную|рабочую)\s+)?область|"
                 r"сколько\s+(?:останется|остается)(?:\s+для\s+рисунка)?)")
DISTANCE_QUESTION = (r"(?:сколько(?:\s+(?:это|будет|получится))?(?:\s+мм)?|"
                     r"какое\s+(?:расчетное\s+)?расстояние(?:\s+получится)?|"
                     r"какой\s+(?:расчетный\s+)?(?:путь|ход)(?:\s+получится)?|"
                     r"(?:посчитай|рассчитай|вычисли)(?:\s+расстояние)?)")
TIME_QUESTION = (r"(?:сколько(?:\s+(?:минимум|минимально))?\s+(?:времени|секунд)(?:\s+(?:нужно|потребуется|займет))?|"
                 r"(?:какое|каково|каким\s+будет)\s+(?:(?:идеальное|минимальное|расчетное)\s+)?время|"
                 r"(?:какая|какова)\s+нижняя\s+граница\s+времени|"
                 r"(?:посчитай|рассчитай|вычисли)\s+(?:(?:идеальное|минимальное|расчетное)\s+)?время)")
FEED_UNIT = r"мм\s*/\s*(мин|с|сек)"
COEFFICIENT_SLOT = r"(" + NUMBER + r")\s*шаг(?:ов|а|ах)?\s*(?:/|на)\s*мм"
SCALAR_UNITS = (
    ("length", "мм", 0.001, r"мм|mm|миллиметр(?:а|ов|ы|ах)?"),
    ("length", "см", 0.01, r"см|cm|сантиметр(?:а|ов|ы|ах)?"),
    ("length", "м", 1, r"м|m|метр(?:а|ов|ы|ах)?"),
    ("length", "км", 1000, r"км|km|километр(?:а|ов|ы|ах)?"),
    ("mass", "мг", 0.001, r"мг|mg|миллиграмм(?:а|ов|ы|ах)?"),
    ("mass", "г", 1, r"г|g|грамм(?:а|ов|ы|ах)?"),
    ("mass", "кг", 1000, r"кг|kg|килограмм(?:а|ов|ы|ах)?"),
    ("mass", "т", 1000000, r"т|тонн(?:а|ы|у|ах)?"),
    ("time", "с", 1, r"с|s|сек|секунд(?:а|ы|у|ах)?"),
    ("time", "мин", 60, r"мин|min|минут(?:а|ы|у|ах)?"),
    ("time", "ч", 3600, r"ч|h|час(?:а|ов|ы|ах)?"),
    ("time", "сут", 86400, r"сут|сутки|суток"),
)
SCALAR_UNIT = "(?:" + "|".join(entry[3] for entry in SCALAR_UNITS) + ")"


def is_calculation_request(prompt: str) -> bool:
    """Recognize calculation intent, not input validity or permission to act.

    This deliberately recognizes malformed numbers/units and missing inputs.
    The caller can request explicit data instead of allowing a language model
    to guess. Only ``calculate`` decides whether an entire request is valid.
    """
    text = prompt.casefold().replace("ё", "е").strip()
    has_number = bool(re.search(r"\d", text))
    if has_number and re.search(
            r"\b(?:корень|корня|степен\w*|логарифм\w*|факториал\w*|"
            r"умнож\w*|раздел\w*|calculate|compute|sqrt|root|power|logarithm|factorial)\b", text):
        return True  # Intent only: unsupported whole expressions are not evaluated.
    if has_number and re.search(r"\b(?:переведи\w*|пересчита\w*|convert)\b|сколько\s+(?:это|будет)", text):
        return True
    if re.fullmatch(r"(" + NUMBER + r")\s*(" + SCALAR_UNIT + r")\s+(?:в|in|to)\s+(" +
                    SCALAR_UNIT + r")" + END, text):
        return True
    # A stand-alone factual question is not a request to calculate a motor
    # profile. Keep this exemption exact, so an appended operation still goes
    # through the arithmetic gate rather than bypassing it.
    if re.fullmatch(r"(?:какая|какова)\s+скорость\s+света(?:\s+в\s+вакууме)?[.!?]?", text):
        return False
    calculation_words = re.search(
        r"\b(?:сколько|посчита(?:й|йте)|рассчита(?:й|йте)|вычисли(?:те)?|переведи(?:те)?|"
        r"пересчита(?:й|йте)|каков[ао]?|какой|какая|какое|какие)\b", text)
    numeric_topic = re.search(
        r"\b(?:импульс\w*|коэффициент\w*|частот\w*|скорост\w*|подач\w*|"
        r"масштаб\w*|пол[еях]|област\w*|времен\w*|секунд\w*|минут\w*|процент\w*|ошибк\w*|"
        r"расстояни\w*|путь|ход|пропорци\w*|корень|корня|степен\w*|логарифм\w*|факториал\w*)\b|\bмм\b|%", text)
    if calculation_words and numeric_topic:
        return True
    if (re.search(r"\b(?:посчита(?:й|йте)|рассчита(?:й|йте)|вычисли(?:те)?)\b", text) and
            re.search(r"\d", text)):
        return True
    math_operator = re.search(r"[+−×÷*/%^]|\d\s*-|\b(?:плюс|минус|умножить|умножь|умножи(?:те)?|"
                              r"разделить|раздели(?:те)?)\b", text)
    if calculation_words and math_operator:
        return True
    if re.fullmatch(r"[\d.,\s+−×÷*/%^?!=()enifa-]+", text) and math_operator:
        return True
    if re.fullmatch(r"[\d.,\s+−enifa-]+(?:плюс|минус|(?:умножить|умножь|умножи(?:те)?)\s+на|"
                    r"(?:разделить|раздели(?:те)?)\s+на)"
                    r"[\d.,\s+−?!enifa-]+", text):
        return True
    # Unsupported arithmetic still needs a checked clarification, not a model
    # approximation. The token detector only marks intent; it never extracts
    # an operand or calculates a subexpression from a larger request.
    numeric_start = r"(?:[-−+]?\s*(?:\d|nan\b|inf\b)|минус\s+\d)"
    if re.search(r"\b(?:квадратный\s+)?корень\s+из\s+" + numeric_start, text):
        return True
    if re.search(numeric_start + r"[\d.,e+−\s-]*\s+в\s+степени\s+" + numeric_start, text):
        return True
    if re.search(r"\b(?:calculate|compute|evaluate)\b", text) and re.search(r"\d", text):
        return True
    if (re.search(r"\b(?:what\s+is|what's|how\s+much(?:\s+is)?)\b", text) and
            (math_operator or re.search(r"\b(?:square\s+root|sqrt|power|percent|percentage|logarithm)\b", text)) and
            re.search(r"\d", text)):
        return True
    if re.search(r"\bкак\s+(?:вписать|скорректировать\s+коэффициент|рассчитать|посчитать)\b", text):
        return True
    # Declarative whole forms are also accepted by calculate. Rejecting bad
    # variants through this gate must not require finding a positive number.
    return bool(
        re.search(r"(?:%|процент\w*)\s*от\b", text) or
        (re.search(r"\bимпульс\w*\b", text) and re.search(r"шаг\w*\s*(?:/|на)\s*мм", text)) or
        (re.search(r"\b(?:лист|листок|бумага)\b", text) and re.search(r"\bпол[еях]\b", text)) or
        (re.search(r"\b(?:рисунок|чертеж)\b", text) and re.search(r"\bлист\b", text) and
         re.search(r"\bпол[еях]\b", text)) or
        (re.search(r"\b(?:задано|ожидалось|цель|целевой)\b", text) and
         re.search(r"\bизмер\w*\b", text) and re.search(r"\bкоэффициент\w*\b", text)))


def value(text: str) -> float:
    number = float(text.replace(",", "."))
    if not math.isfinite(number) or not 0 <= number <= 1_000_000:
        raise ValueError("Number outside calculation range")
    return number


def calculate(prompt: str) -> str | None:
    text = prompt.casefold().replace("ё", "е").strip()
    if len(text) > 1800:
        return None
    # Only one explicit operation. Do not silently calculate part of a request.
    if re.search(r"[;$`]|\b(?:затем|потом|выполни|запусти|отправь)\b", text):
        return None
    # Complete frequency clauses also mention impulses and a coefficient.
    # Recognize them before the older missing-distance-input clarification.
    try:
        practical = _scalar_conversion(text) or _practical_calculation(text)
    except ValueError:
        return "Числа слишком велики для этого расчёта. Изменений станка нет."
    if practical is not None:
        return practical
    dimensions = r"(" + NUMBER + r")\s*(?:мм\s*)?(?:на|×|x|х)\s*(" + NUMBER + r")\s*"
    margin = (r"(?:по\s*)?(" + NUMBER + r")\s*мм(?:\s+(?:с\s+каждой\s+стороны|"
              r"на\s+каждой\s+стороне|со\s+всех\s+сторон|по\s+всем\s+сторонам))?")
    area = re.fullmatch(r"(?:прямоугольный\s+)?(?:лист|листок|бумага)\s+" + dimensions +
                        r"(?:мм)?\s*,?\s*(?:пол[ея]|поля\s+одинаковые)\s*" + margin + END +
                        r"(?:" + QUESTION_SEPARATOR + AREA_QUESTION + END + r")?", text)
    area_question = re.fullmatch(r"(?:какая|какова)\s+(?:полезная|доступная|рабочая)\s+область\s+"
                                 r"(?:бумаги|листа)\s+" + dimensions + r"мм\s+с\s+(?:полями|полем)\s+" +
                                 margin + END, text)
    area = area or area_question
    if area:
        try:
            width, height, margin = map(value, area.groups())
        except ValueError:
            return "Числа слишком велики для этого расчёта. Изменений станка нет."
        if min(width, height) <= 2 * margin:
            return "Такие поля не оставляют положительной области. Проверьте размеры листа и поле."
        return (f"По введённым числам доступная область: {width:g} − 2 × {margin:g} = "
                f"{width - 2 * margin:g} мм на {height:g} − 2 × {margin:g} = "
                f"{height - 2 * margin:g} мм. Это расчёт, не измерение листа, "
                "не предел хода станка и не смена калибровки.")
    # Whole-request grammar: searching individual slots could turn -100 or 1e3
    # into unrelated positive numbers or silently take one of several operations.
    prefix = (r"(?:сколько(?:\s+мм)?(?:\s+(?:будет|пройдет(?:\s+(?:каретка|головка|ось))?))?|"
              r"какое\s+расстояние(?:\s+пройдет(?:\s+(?:каретка|головка|ось))?)?|"
              r"какой\s+(?:расчетный\s+)?(?:путь|ход)\s+дают|"
              r"какое\s+(?:расчетное\s+)?расстояние\s+дают|"
              r"посчитай(?:\s+расстояние)?|рассчитай(?:\s+расстояние)?)\s*(?:за|для|при)?\s*")
    pulses_slot = r"(" + NUMBER + r")\s*импульс(?:ов|а)?"
    ratio_slot = r"(" + NUMBER + r")\s*шаг(?:ов|а|ах)?\s*(?:/|на)\s*мм"
    forward = re.fullmatch(prefix + pulses_slot + r"\s+при\s+" + ratio_slot + r"[.!?]?", text)
    reverse = re.fullmatch(r"при\s+" + ratio_slot + r"\s+" + prefix + pulses_slot + r"[.!?]?", text)
    declaration = re.fullmatch(r"(?:(?:подано|задано|дано|есть)\s+)?" + pulses_slot +
                               r"\s*(?:при|,\s*(?:коэффициент(?:\s+калибровки)?\s*)?)\s*" +
                               ratio_slot + END + r"(?:" + QUESTION_SEPARATOR + DISTANCE_QUESTION + END + r")?", text)
    declaration_reverse = re.fullmatch(r"(?:при\s+|коэффициент(?:\s+калибровки)?\s*)?" + ratio_slot +
                                       r"\s*,?\s*(?:для\s+|(?:подано|задано|дано|есть)\s+)" + pulses_slot +
                                       END + r"(?:" + QUESTION_SEPARATOR + DISTANCE_QUESTION + END + r")?", text)
    if forward or reverse or declaration or declaration_reverse:
        groups = ((forward or declaration).groups() if forward or declaration
                  else tuple(reversed((reverse or declaration_reverse).groups())))
        try:
            count, coefficient = map(value, groups)
        except ValueError:
            return "Числа слишком велики для этого расчёта. Изменений станка нет."
        if coefficient == 0:
            return "Шагов/мм должно быть больше нуля. Изменений нет."
        if not count.is_integer():
            return "Количество импульсов должно быть целым неотрицательным числом. Изменений нет."
        return (f"При введённом коэффициенте {coefficient:g} шагов/мм: "
                f"{count:g} / {coefficient:g} = {count / coefficient:g} мм. "
                "Это расчёт по вашим числам; он не подтверждает перемычки или реальный ход. "
                "Команды движения не отправлялись.")
    if ("импульс" in text and re.search(r"шаг.*мм", text) and
            re.search(r"сколько|какое расстояние|какой\s+(?:расчетный\s+)?(?:путь|ход)|посчитай|рассчитай", text)):
        return ("Укажите один коэффициент шагов/мм и одно целое неотрицательное количество "
                "импульсов, без диапазонов или второй операции. Например: "
                "«Сколько мм для 100 импульсов при 40 шагов/мм?»")
    speed_question = (r"(?:сколько(?:\s+(?:это|будет|получится))?|"
                      r"какая\s+(?:расчетная\s+)?скорость(?:\s+получится)?|"
                      r"(?:посчитай|рассчитай|вычисли)(?:\s+скорость)?)")
    speed = re.fullmatch(r"(?:(?:посчитай|рассчитай|вычисли)(?:\s+скорость)?\s+|"
                         r"сколько\s+(?:будет\s+)?|(?:нужно|надо)\s+)?(" + NUMBER +
                         r")\s*%\s*от\s*(?:(?:профиля(?:\s+скорости)?|скорости(?:\s+профиля)?)\s+)?(" +
                         NUMBER + r")\s*мм\s*/\s*мин" + END +
                         r"(?:" + QUESTION_SEPARATOR + speed_question + END + r")?", text)
    if speed:
        try:
            percent, maximum = map(value, speed.groups())
        except ValueError:
            return "Числа слишком велики для этого расчёта. Изменений станка нет."
        if percent > 100 or maximum <= 0:
            return "Для профиля укажите 0–100% и положительную скорость. Настройки не изменены."
        return (f"{percent:g}% от {maximum:g} мм/мин = {percent * maximum / 100:g} мм/мин. "
                "Это арифметика по введённым данным, не измерение максимума моторов "
                "и не установка скорости.")
    path_slot = (r"(?:(?:путь|пути)(?:\s+с\s+соединениями)?|длина\s+(?:пути|траектории)|"
                 r"траектори[яи])\s*:?\s*(" + NUMBER + r")\s*мм")
    feed_slot = r"(?:подача\s*:?|(?:при\s+)?(?:подаче|скорости))\s*(" + NUMBER + r")\s*мм\s*/\s*мин"
    time_forward = re.fullmatch(path_slot + r"\s*,?\s*" + feed_slot + END +
                               QUESTION_SEPARATOR + TIME_QUESTION + END, text)
    time_prefix = re.fullmatch(TIME_QUESTION + r"\s+(?:для\s+)?" + path_slot +
                              r"\s*,?\s*" + feed_slot + END, text)
    if time_forward or time_prefix:
        try:
            length, feed = map(value, (time_forward or time_prefix).groups())
        except ValueError:
            return "Числа слишком велики для этого расчёта. Изменений станка нет."
        if feed <= 0:
            return "Для расчёта времени нужна положительная подача в мм/мин. Настройки не изменены."
        seconds = length / feed * 60
        return (f"Идеальная нижняя граница времени по вашим числам: {length:g} / {feed:g} × 60 = "
                f"{seconds:g} с. Это расчёт при постоянной заданной подаче, не измерение "
                "и не гарантия времени рисования. Разгон, торможение, повороты, паузы USB "
                "и задержка старта могут увеличить фактическое время. Команды станку не отправлялись.")
    return None


def _scalar_conversion(text: str) -> str | None:
    """One explicit quantity and two dimension-compatible units; no inference."""
    prefix = r"(?:(?:переведи|пересчитай|convert)\s+|сколько(?:\s+будет)?\s+)?"
    matched = re.fullmatch(prefix + r"(" + NUMBER + r")\s*(" + SCALAR_UNIT +
                           r")\s+(?:в|in|to)\s+(" + SCALAR_UNIT + r")" + END, text)
    if matched is None:
        return None
    entered, source, target = matched.groups()
    source_entry = next(entry for entry in SCALAR_UNITS if re.fullmatch(entry[3], source))
    target_entry = next(entry for entry in SCALAR_UNITS if re.fullmatch(entry[3], target))
    if source_entry[0] != target_entry[0]:
        return "Нельзя перевести между разными величинами: длиной, массой и временем. Настройки не изменены."
    quantity = value(entered)
    converted = quantity * source_entry[2] / target_entry[2]
    return (f"{quantity:g} {source_entry[1]} = {converted:g} {target_entry[1]}. "
            "Это только перевод единиц по введённому числу, не измерение и не изменение настроек.")


def _practical_calculation(text: str) -> str | None:
    """Additional whole-request arithmetic, with no controller dependency."""
    arithmetic_prefix = r"(?:(?:сколько(?:\s+будет)?|посчитай|рассчитай|вычисли)\s+)?"
    percentage = re.fullmatch(arithmetic_prefix + r"(" + NUMBER + r")\s*(?:%|процент(?:ов|а)?)\s*от\s*(" + NUMBER + r")" + END, text)
    if percentage:
        percent, base = map(value, percentage.groups())
        return f"{percent:g}% от {base:g} = {percent * base / 100:g}. Это арифметика; настройки станка не изменены."
    binary = re.fullmatch(arithmetic_prefix + r"(" + NUMBER + r")\s*([+−×÷*/-]|плюс|минус|"
                         r"умножить\s+на|разделить\s+на)\s*(" + NUMBER + r")" + END, text)
    if binary:
        left_text, operator, right_text = binary.groups()
        left, right = value(left_text), value(right_text)
        operator = {"плюс": "+", "минус": "−", "умножить на": "×", "разделить на": "÷"}.get(
            " ".join(operator.split()), operator)
        if operator in ("/", "÷") and right == 0:
            return "Делить на ноль нельзя. Настройки станка не изменены."
        if operator == "+":
            result = left + right
        elif operator in ("-", "−"):
            result = left - right
        elif operator in ("*", "×"):
            result = left * right
        else:
            result = left / right
        return f"{left:g} {operator} {right:g} = {result:g}. Это арифметика; настройки станка не изменены."
    conversion = re.fullmatch(
        r"(?:переведи|пересчитай)\s+(?:скорость\s+|подачу\s+)?(" + NUMBER + r")\s*" +
        FEED_UNIT + r"\s+в\s*" + FEED_UNIT + END, text)
    conversion_declaration = re.fullmatch(
        r"(?:(?:скорость|подача)\s*)?(" + NUMBER + r")\s*" + FEED_UNIT +
        QUESTION_SEPARATOR + r"(?:сколько(?:\s+(?:это|будет|получится))?|переведи)(?:\s+в)?\s*" +
        FEED_UNIT + END, text)
    conversion_question = re.fullmatch(
        r"сколько\s*" + FEED_UNIT + r"\s+(?:в|при)\s*(" + NUMBER + r")\s*" +
        FEED_UNIT + END, text)
    if conversion or conversion_declaration or conversion_question:
        if conversion_question:
            target_unit, entered, source_unit = conversion_question.groups()
        else:
            entered, source_unit, target_unit = (conversion or conversion_declaration).groups()
        speed = value(entered)
        if speed <= 0:
            return "Для перевода скорости укажите положительное число и обе единицы. Настройки не изменены."
        source_minute, target_minute = source_unit == "мин", target_unit == "мин"
        factor = (1 / 60 if source_minute else 60) if source_minute != target_minute else 1
        source_label = "мм/мин" if source_minute else "мм/с"
        target_label = "мм/мин" if target_minute else "мм/с"
        return (f"{speed:g} {source_label} = {speed * factor:g} {target_label}. "
                "Это только перевод единиц, не допустимая скорость моторов и не изменение настроек.")

    frequency_question = (r"(?:какая\s+(?:расчетная\s+)?частота(?:\s+импульсов)?(?:\s+(?:получится|в\s+гц))?|"
                          r"сколько\s+импульсов\s+(?:в\s+секунду|/\s*с)|"
                          r"(?:посчитай|рассчитай)\s+(?:частоту|частоту\s+импульсов))")
    feed_slot = r"(?:подача|скорость)\s*:?\s*(" + NUMBER + r")\s*" + FEED_UNIT
    frequency = re.fullmatch(
        feed_slot + r"\s*,?\s*(?:коэффициент(?:\s+калибровки)?\s*:?\s*)?" + COEFFICIENT_SLOT +
        END + QUESTION_SEPARATOR + frequency_question + END, text)
    frequency_prefix = re.fullmatch(
        frequency_question + r"\s+при\s+(?:подаче|скорости)\s*(" + NUMBER + r")\s*" + FEED_UNIT +
        r"\s+(?:и|при)\s*" + COEFFICIENT_SLOT + END, text)
    if frequency or frequency_prefix:
        entered, unit, ratio = (frequency or frequency_prefix).groups()
        speed, coefficient = value(entered), value(ratio)
        if speed <= 0 or coefficient <= 0:
            return "Нужны положительная скорость с единицами и положительный коэффициент шагов/мм. Настройки не изменены."
        speed_seconds = speed / 60 if unit == "мин" else speed
        frequency_hz = speed_seconds * coefficient
        return (f"Если введённая скорость относится к этой оси: {speed_seconds:g} мм/с × "
                f"{coefficient:g} шагов/мм = {frequency_hz:g} импульсов/с (Гц). "
                "Это расчёт требуемой частоты, не максимальная частота Arduino, драйвера или мотора "
                "и не гарантия движения без пропусков шагов. Подача вдоль диагональной траектории "
                "не равна скорости каждой оси. Команды станку не отправлялись.")

    profile = re.fullmatch(
        r"(?:скорость\s+)?профил[ья]\s*:?\s*(" + NUMBER + r")\s*мм\s*/\s*мин\s*,?\s*"
        r"(?:нужно|надо|доля)\s*(" + NUMBER + r")\s*%" + END + QUESTION_SEPARATOR +
        r"(?:сколько(?:\s+(?:это|будет|получится))?|какая\s+(?:расчетная\s+)?скорость(?:\s+получится)?)" +
        END, text)
    if profile:
        profile_speed, percent = map(value, profile.groups())
        if percent > 100 or profile_speed <= 0:
            return "Для профиля укажите 0–100% и положительную скорость. Настройки не изменены."
        return (f"{percent:g}% от {profile_speed:g} мм/мин = {profile_speed * percent / 100:g} мм/мин. "
                "Это арифметика по введённым данным, не измерение максимума моторов и не установка скорости.")

    target_slot = (r"(?:задано|ожидалось|цель|целевой\s+(?:ход|размер|путь)|должно\s+быть)\s*:?\s*"
                   r"(" + NUMBER + r")\s*мм")
    measured_slot = (r"(?:измерено|фактически\s+измерено|измеренный\s+(?:ход|размер|путь)|"
                     r"измеренное\s+расстояние)\s*:?\s*(" + NUMBER + r")\s*мм")
    current_slot = r"текущий\s+коэффициент(?:\s+калибровки)?\s*:?\s*" + COEFFICIENT_SLOT
    correction_question = (r"(?:какой\s+(?:(?:новый|расчетный|исправленный)\s+)?коэффициент(?:\s+(?:нужен|получится))?|"
                           r"как\s+(?:арифметически\s+)?скорректировать\s+коэффициент|"
                           r"(?:посчитай|рассчитай)\s+(?:коррекцию(?:\s+коэффициента)?|"
                           r"ошибку(?:\s+масштаба)?\s+и\s+(?:новый\s+)?коэффициент))")
    correction = re.fullmatch(
        target_slot + r"\s*,?\s*" + measured_slot + r"\s*,?\s*" + current_slot + END +
        QUESTION_SEPARATOR + correction_question + END, text)
    correction_prefix = re.fullmatch(
        correction_question + r"\s*:?\s*" + target_slot + r"\s*,?\s*" + measured_slot +
        r"\s*,?\s*" + current_slot + END, text)
    if correction or correction_prefix:
        target, measured, coefficient = map(value, (correction or correction_prefix).groups())
        if min(target, measured, coefficient) <= 0:
            return "Нужны положительные заданное расстояние, измеренное расстояние и текущий коэффициент. Настройки не изменены."
        proposed = coefficient * target / measured
        if not math.isfinite(proposed) or proposed > 1_000_000:
            return "Расчётный коэффициент выходит за диапазон калькулятора. Проверьте ввод и единицы; настройки не изменены."
        difference = measured - target
        relative_error = difference / target * 100
        return (f"Отклонение измеренного от заданного: {measured:g} − {target:g} = {difference:g} мм "
                f"({relative_error:g}%). Арифметический кандидат: {coefficient:g} × {target:g} / "
                f"{measured:g} = {proposed:g} шагов/мм. Это не установленная калибровка: "
                "одного измерения недостаточно, чтобы отличить масштаб от люфта, пропусков шагов "
                "или ошибки измерения. Перед изменением коэффициента нужны повторные измерения "
                "разных расстояний и направлений. Настройки не изменены, команды станку не отправлялись.")

    size_slot = r"(" + NUMBER + r")\s*(?:мм\s*)?(?:на|×|x|х)\s*(" + NUMBER + r")\s*мм"
    drawing_slot = r"(?:рисунок|чертеж)\s*:?\s*" + size_slot
    paper_slot = r"лист\s*:?\s*" + size_slot
    margin_slot = (r"(?:поля|поле)\s*(?:по\s*)?(" + NUMBER + r")\s*мм"
                   r"(?:\s+(?:с\s+каждой\s+стороны|со\s+всех\s+сторон))?")
    fit_question = (r"(?:как\s+вписать(?:\s+с\s+сохранением\s+пропорций)?|"
                    r"(?:посчитай|рассчитай)\s+масштаб(?:\s+вписывания)?(?:\s+с\s+сохранением\s+пропорций)?|"
                    r"какой\s+масштаб\s+вписывания(?:\s+с\s+сохранением\s+пропорций)?)")
    fit = re.fullmatch(
        drawing_slot + r"\s*,?\s*" + paper_slot + r"\s*,?\s*" + margin_slot + END +
        QUESTION_SEPARATOR + fit_question + END, text)
    fit_prefix = re.fullmatch(
        fit_question + r"\s*:?\s*" + drawing_slot + r"\s*,?\s*" + paper_slot +
        r"\s*,?\s*" + margin_slot + END, text)
    if fit or fit_prefix:
        drawing_width, drawing_height, sheet_width, sheet_height, margin = map(value, (fit or fit_prefix).groups())
        if min(drawing_width, drawing_height, sheet_width, sheet_height) <= 0:
            return "Размеры рисунка и листа должны быть положительными. Рисунок и настройки не изменены."
        available_width, available_height = sheet_width - 2 * margin, sheet_height - 2 * margin
        if min(available_width, available_height) <= 0:
            return "Такие поля не оставляют положительной области. Рисунок и настройки не изменены."
        scale = min(available_width / drawing_width, available_height / drawing_height)
        width, height = drawing_width * scale, drawing_height * scale
        left, top = (sheet_width - width) / 2, (sheet_height - height) / 2
        return (f"Доступная область: {available_width:g} × {available_height:g} мм. "
                f"Масштаб с сохранением пропорций: min({available_width:g} / {drawing_width:g}, "
                f"{available_height:g} / {drawing_height:g}) = {scale:g} ({scale * 100:g}%). "
                f"Итоговый рисунок: {width:g} × {height:g} мм, без обрезки и без поворота. "
                f"При центрировании отступ слева {left:g} мм, сверху {top:g} мм. "
                "Это расчёт по введённым размерам, не измерение листа, не проверка пределов "
                "станка и не применение масштаба. Рисунок, настройки и положение головки не изменены.")
    return None
