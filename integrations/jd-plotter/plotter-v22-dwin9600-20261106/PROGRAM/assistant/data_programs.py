"""Pure allowlisted data programs, never controller or sketch actions.

The model may propose a tool name and arguments, but this dispatcher grants no
execution authority. Its caller must also verify that extracted arguments came
from the user's request. All numeric answers reuse the maintained calculator.
"""
from __future__ import annotations

from copy import deepcopy
from decimal import Decimal
import math
from typing import Mapping

from .calculations import calculate
from .project_answers import lookup
from .verified import editor_report


_UNITS = {
    "mm": ("length", "мм"), "cm": ("length", "см"),
    "m": ("length", "м"), "km": ("length", "км"),
    "mg": ("mass", "мг"), "g": ("mass", "г"),
    "kg": ("mass", "кг"), "t": ("mass", "т"),
    "s": ("time", "с"), "min": ("time", "мин"),
    "h": ("time", "ч"), "day": ("time", "сут"),
    "mm/min": ("feed", "мм/мин"), "mm/s": ("feed", "мм/с"),
}
_OPERATORS = {"add": "+", "subtract": "−", "multiply": "×", "divide": "÷"}

# Topic identifiers are not arbitrary questions. Each maps to one maintained
# canonical lookup, with its topic checked again to catch reference drift.
_REFERENCES = {
    "stop": "Как остановить плоттер при потере USB?",
    "power_disconnect": "Можно менять моторный разъём, если моторное питание выключено, а USB подключён?",
    "computer_host": "Нужен ли Raspberry Pi для плоттера?",
    "display": "Почему дисплей DWIN не включается?",
    "driver_current": "Как выбирать Vref и ток драйвера A4988?",
    "motor_wiring": "Как подключать провода двигателя?",
    "driver_orientation": "Какой стороной вставлять драйвер A4988?",
    "stall": "Мотор гудит, но не двигается. Что проверить?",
    "idle_noise": "Моторы шипят в простое, без движения. Почему?",
    "holding": "Зачем удержание моторов в простое?",
    "belt": "Как работает ремень GT2 и шкив?",
    "steps": "Что означает коэффициент шагов/мм?",
    "speed": "Почему моторы медленно двигаются?",
    "jog_step": "Что задаёт ход за нажатие стрелки?",
    "paper_rotation": "Можно ли использовать лист под углом?",
    "area_reset": "Что делать с углами после сброса Arduino?",
    "manual_area": "Как задать область двумя углами?",
    "camera_features": "Есть ли машинное зрение и камера?",
    "camera_hardware": "Как подключить камеру OV5640 к ESP32-S3?",
    "editor_projects": "Как сохранить проект эскиза?",
    "no_lift": "Почему переходы контуров рисуются без подъёма ручки?",
    "handwriting": "Какие печатные буквы и рукописный текст поддерживаются?",
    "svg_export": "Как сохранить SVG и загрузить обратно на прежнее место?",
    "svg_margin": "Когда задают поле при открытии SVG?",
    "svg": "Какие форматы SVG поддерживаются?",
    "accuracy": "Что может вызывать неточность движения моторов?",
    "hardware": "Из чего состоит наша сборка, какие оси подключены?",
    "feedback": "Обучается ли модель на оценке ответа помощника?",
    "assistant_features": "Какие функции умеет помощник?",
    "image_tracing": "Как работает рисунок по образцу и обводка изображения?",
    "assistant_roadmap": "Какие следующие функции помощника можно добавить?",
}


def _numeric(*, positive: bool = False, maximum: int = 1_000_000,
             integer: bool = False) -> dict:
    schema = {"type": "integer" if integer else "number",
              "minimum": 1 if positive and integer else 0.000001 if positive else 0,
              "maximum": maximum}
    if not integer:
        schema["multipleOf"] = 0.000001
    return schema


def _enum(values) -> dict:
    return {"type": "string", "enum": list(values)}


def _schema(properties: dict, description: str) -> dict:
    return {"type": "object", "properties": properties, "required": list(properties),
            "additionalProperties": False, "description": description}


_SCHEMAS = {
    "arithmetic": _schema({"a": _numeric(), "b": _numeric(),
                           "operator": _enum(_OPERATORS)}, "Одна арифметическая операция; делитель не равен нулю."),
    "percentage": _schema({"value": _numeric(), "percent": _numeric()}, "Процент от явно введённого значения; не установка скорости."),
    "convert_units": _schema({"value": _numeric(), "source_unit": _enum(_UNITS),
                              "target_unit": _enum(_UNITS)}, "Перевод совместимых единиц; для подачи значение положительное."),
    "paper_area": _schema({"width_mm": _numeric(positive=True), "height_mm": _numeric(positive=True),
                           "margin_mm": _numeric()}, "Доступная область прямоугольного листа; одинаковое поле с каждой стороны."),
    "pulse_distance": _schema({"pulses": _numeric(integer=True), "steps_per_mm": _numeric(positive=True)}, "Условный ход по заданным импульсам и коэффициенту; не измерение и не движение."),
    "pulse_frequency": _schema({"speed_mm_min": _numeric(positive=True), "steps_per_mm": _numeric(positive=True)}, "Частота импульсов, если скорость относится к одной оси; не максимальная скорость."),
    "profile_speed": _schema({"profile_mm_min": _numeric(positive=True), "percent": _numeric(maximum=100)}, "0–100% от явно заданного профиля; настройки не меняются."),
    "ideal_time": _schema({"length_mm": _numeric(), "feed_mm_min": _numeric(positive=True)}, "Идеальная нижняя граница времени без разгона и пауз."),
    "fit_drawing": _schema({"drawing_width_mm": _numeric(positive=True), "drawing_height_mm": _numeric(positive=True),
                            "sheet_width_mm": _numeric(positive=True), "sheet_height_mm": _numeric(positive=True),
                            "margin_mm": _numeric()}, "Расчёт вписывания без поворота и обрезки; эскиз не изменяется."),
    "scale_correction": _schema({"target_mm": _numeric(positive=True), "measured_mm": _numeric(positive=True),
                                 "current_steps_per_mm": _numeric(positive=True)}, "Условный кандидат коэффициента по одному измерению; калибровка не применяется."),
    "editor_report": _schema({}, "Проверка только текущих данных редактора, не физических измерений."),
    "project_reference": _schema({"topic": _enum(_REFERENCES)}, "Одна проверенная тема справки; неизвестные маркировки и контакты не угадываются."),
}

# Public JSON schemas are copies: editing a displayed schema cannot change the
# dispatcher's validation or introduce another callable tool.
DATA_SCHEMAS = deepcopy(_SCHEMAS)


def _validate(tool: str, args: dict) -> dict:
    if type(tool) is not str or tool not in _SCHEMAS:
        raise ValueError("Неподдерживаемая программа данных.")
    if type(args) is not dict or set(args) != set(_SCHEMAS[tool]["required"]):
        raise ValueError("Нужны все и только объявленные аргументы программы.")
    checked = {}
    for key, schema in _SCHEMAS[tool]["properties"].items():
        item = args[key]
        if schema["type"] == "string":
            if type(item) is not str or item not in schema["enum"]:
                raise ValueError(f"Неподдерживаемое значение {key}.")
        else:
            allowed_types = (int,) if schema["type"] == "integer" else (int, float)
            if type(item) not in allowed_types or not schema["minimum"] <= item <= schema["maximum"]:
                raise ValueError(f"Число {key} вне допустимого диапазона или имеет неверный тип.")
            if not math.isfinite(item):
                raise ValueError(f"Число {key} должно быть конечным.")
            if schema["type"] != "integer" and Decimal(str(item)).normalize().as_tuple().exponent < -6:
                raise ValueError(f"Для {key} поддерживаются не более шести знаков после запятой; округлять ввод нельзя.")
        checked[key] = item
    return checked


def _n(item: int | float) -> str:
    """Canonical decimal, without rounding or exponential notation."""
    if item == 0:
        return "0"  # Negative floating-point zero is the same explicit zero.
    return format(Decimal(str(item)), "f")


def _answer(request: str) -> str:
    result = calculate(request)
    if result is None or "выходит за диапазон калькулятора" in result:
        raise ValueError("Программа не поддерживает эти исходные данные.")
    return result


def dispatch(tool: str, args: dict, state: Mapping) -> str:
    """Return a checked read-only answer, or ValueError; never execute actions."""
    values = _validate(tool, args)
    if not isinstance(state, Mapping):
        raise ValueError("Данные редактора должны быть отображением полей.")
    if tool == "editor_report":
        return editor_report(state)
    if tool == "project_reference":
        topic = values["topic"]
        result = lookup(_REFERENCES[topic], state)
        if result is None or result.topic != topic:
            raise ValueError("Для выбранной темы нет однозначной проверенной справки.")
        return result.text
    if tool == "arithmetic":
        if values["operator"] == "divide" and values["b"] == 0:
            raise ValueError("Делить на ноль нельзя.")
        return _answer(f"{_n(values['a'])} {_OPERATORS[values['operator']]} {_n(values['b'])}")
    if tool == "percentage":
        return _answer(f"{_n(values['percent'])}% от {_n(values['value'])}")
    if tool == "convert_units":
        source, target = _UNITS[values["source_unit"]], _UNITS[values["target_unit"]]
        if source[0] != target[0]:
            raise ValueError("Нельзя переводить единицы разных величин.")
        if source[0] == "feed" and values["value"] <= 0:
            raise ValueError("Для перевода подачи требуется положительное значение.")
        return _answer(f"Переведи {_n(values['value'])} {source[1]} в {target[1]}")
    if tool in ("paper_area", "fit_drawing"):
        width_key, height_key = (("width_mm", "height_mm") if tool == "paper_area"
                                else ("sheet_width_mm", "sheet_height_mm"))
        if min(values[width_key], values[height_key]) <= 2 * values["margin_mm"]:
            raise ValueError("Поля должны оставлять положительную область листа.")
        paper = f"Лист {_n(values[width_key])}×{_n(values[height_key])} мм, поля {_n(values['margin_mm'])} мм"
        if tool == "paper_area":
            return _answer(paper)
        return _answer(f"Рисунок {_n(values['drawing_width_mm'])}×{_n(values['drawing_height_mm'])} мм, "
                       f"{paper}. Как вписать?")
    if tool == "pulse_distance":
        return _answer(f"Сколько мм для {_n(values['pulses'])} импульсов при {_n(values['steps_per_mm'])} шагов/мм?")
    if tool == "pulse_frequency":
        return _answer(f"Подача {_n(values['speed_mm_min'])} мм/мин, коэффициент "
                       f"{_n(values['steps_per_mm'])} шагов/мм. Какая частота импульсов?")
    if tool == "profile_speed":
        return _answer(f"{_n(values['percent'])}% от профиля {_n(values['profile_mm_min'])} мм/мин")
    if tool == "ideal_time":
        return _answer(f"Путь {_n(values['length_mm'])} мм, подача {_n(values['feed_mm_min'])} мм/мин. Какое время?")
    if tool == "scale_correction":
        return _answer(f"Задано {_n(values['target_mm'])} мм, измерено {_n(values['measured_mm'])} мм, текущий "
                       f"коэффициент {_n(values['current_steps_per_mm'])} шагов/мм. Какой новый коэффициент?")
    # Validation has a closed allowlist; an implementation omission must fail.
    raise ValueError("Выбранная программа данных не реализована.")
