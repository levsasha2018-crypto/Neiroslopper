"""Bounded, declarative drawing programs for preview geometry only.

This module does not parse prose, evaluate code, or access devices. The legacy
text operation delegates only the planner's fixed handwritten sample files;
no operation accepts a file path. Its caller is responsible for fresh user
confirmation before a preview commit.
All coordinates and dimensions are paper millimetres, with y positive downward.
"""
from __future__ import annotations

import math

from .planner import Plan, transform_paths


MAX_OPERATIONS = 8
MAX_POINTS = 50_000
MAX_POLYLINE_POINTS = 1_000
ELLIPSE_SEGMENTS = 128
_EPSILON = 1e-7
_PARAMETERS = {
    "center": frozenset(),
    "rotate": frozenset({"degrees"}),
    "scale": frozenset({"percent"}),
    "resize_width": frozenset({"width_mm"}),
    "margin": frozenset({"margin_mm"}),
    "mirror": frozenset({"axis"}),
    "move_preview": frozenset({"dx_mm", "dy_mm"}),
    "place_preview": frozenset({"anchor", "margin_mm"}),
    "print_text": frozenset({"text", "height_mm", "spacing_mm"}),
    "append_text": frozenset({"text", "height_mm", "spacing_mm"}),
    "text": frozenset({"text", "height_mm"}),
    "frame": frozenset({"padding_mm"}),
    "ruler": frozenset({"length_mm"}),
    "test_pattern": frozenset({"size_mm"}),
    "polyline": frozenset({"points", "mode"}),
    "ellipse": frozenset({"cx", "cy", "rx", "ry", "mode"}),
}
_ANCHORS = frozenset({"center", "top_left", "top_right", "bottom_left", "bottom_right"})


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


def _sheet(width, height):
    width, height = _number(width), _number(height)
    if not (1 <= width <= 3000 and 1 <= height <= 3000):
        raise ValueError("Размеры листа должны быть от 1 до 3000 мм.")
    return width, height


def _copy_paths(paths, width, height, *, allow_empty=False):
    if not isinstance(paths, (list, tuple)) or (not paths and not allow_empty):
        raise ValueError("Нужен список линий; в результате должна быть хотя бы одна линия.")
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
            x, y = (_number(value) for value in point)
            if not (-_EPSILON <= x <= width + _EPSILON and
                    -_EPSILON <= y <= height + _EPSILON):
                raise ValueError("Рисунок выходит за границы листа; изменение отменено.")
            # Roundoff at exactly touching edges is not a reason to clip a
            # drawing; only a tiny floating-point tolerance is normalized.
            result.append((min(width, max(0.0, x)), min(height, max(0.0, y))))
        copied.append(result)
    if copied:
        left = min(x for path in copied for x, _ in path)
        right = max(x for path in copied for x, _ in path)
        top = min(y for path in copied for _, y in path)
        bottom = max(y for path in copied for _, y in path)
        if max(right - left, bottom - top) <= 1e-9:
            raise ValueError("У рисунка должен быть ненулевой размер хотя бы по одной стороне.")
    return copied


def _validate_operation(operation):
    if not isinstance(operation, dict) or set(operation) != {"kind", "parameters"}:
        raise ValueError("У операции должны быть только поля kind и parameters.")
    kind, parameters = operation["kind"], operation["parameters"]
    if not isinstance(kind, str) or kind not in _PARAMETERS:
        raise ValueError("Неизвестная или запрещённая операция эскиза.")
    if not isinstance(parameters, dict):
        raise ValueError("Параметры операции должны быть объектом.")
    keys = set(parameters)
    expected = _PARAMETERS[kind]
    allowed = keys == expected
    if kind == "scale":
        allowed = allowed or keys == expected | {"anchor"}
    elif kind in {"print_text", "append_text"}:
        allowed = allowed or keys == expected | {"anchor", "margin_mm"}
    if not allowed:
        raise ValueError("Неверные, отсутствующие или лишние параметры операции.")
    copied = {}
    for name, value in parameters.items():
        if name == "points":
            if not isinstance(value, (list, tuple)) or not 2 <= len(value) <= MAX_POLYLINE_POINTS:
                raise ValueError("Ломаная должна содержать от 2 до 1000 точек.")
            points = []
            for point in value:
                if not isinstance(point, (list, tuple)) or len(point) != 2:
                    raise ValueError("Точка должна содержать две координаты.")
                points.append(tuple(_number(coordinate) for coordinate in point))
            copied[name] = points
            continue
        if name == "text":
            if not isinstance(value, str) or not 1 <= len(value) <= 800:
                raise ValueError("Нужен текст длиной от 1 до 800 символов.")
        elif name == "mode":
            if not isinstance(value, str) or value not in {"replace", "append"}:
                raise ValueError("Режим фигуры должен быть replace или append.")
        elif name == "anchor":
            if not isinstance(value, str) or value not in _ANCHORS:
                raise ValueError("Неизвестное положение рисунка на листе.")
        elif name == "axis":
            if not isinstance(value, str) or value not in {"horizontal", "vertical"}:
                raise ValueError("Неизвестная ось отражения.")
        elif name == "spacing_mm" and value is None:
            copied[name] = None
            continue
        else:
            _number(value)
        copied[name] = value
    return kind, copied


def _ellipse(parameters, width, height):
    cx, cy, rx, ry = (_number(parameters[name]) for name in ("cx", "cy", "rx", "ry"))
    if not (0 < rx <= 3000 and 0 < ry <= 3000):
        raise ValueError("Оба радиуса эллипса должны быть больше 0 и не больше 3000 мм.")
    # Check the continuous ellipse too, not merely its sampled path.
    if (cx - rx < -_EPSILON or cx + rx > width + _EPSILON or
            cy - ry < -_EPSILON or cy + ry > height + _EPSILON):
        raise ValueError("Эллипс выходит за границы листа; изменение отменено.")
    points = [(cx + rx * math.cos(step * 2 * math.pi / ELLIPSE_SEGMENTS),
               cy + ry * math.sin(step * 2 * math.pi / ELLIPSE_SEGMENTS))
              for step in range(ELLIPSE_SEGMENTS)]
    points.append(points[0])
    return _copy_paths([points], width, height)


def execute_preview_program(paths, operations, sheet_width, sheet_height):
    """Return a fresh validated preview for a program of one to eight steps.

    Exact operation schemas prohibit executable or recursive instructions.
    Each step, including one later replaced, must fit the known sheet. A
    failure raises ValueError without mutating input geometry or the program.
    Polyline and ellipse can replace the preview or append a separate stroke.
    Without a pen lift, transitions between separate strokes will be drawn.
    """
    width, height = _sheet(sheet_width, sheet_height)
    if not isinstance(operations, list) or not 1 <= len(operations) <= MAX_OPERATIONS:
        raise ValueError("Нужен список от 1 до 8 операций эскиза.")
    source = _copy_paths(paths, width, height, allow_empty=True)
    # Validate the entire program first, before generating any local geometry.
    checked = [_validate_operation(operation) for operation in operations]
    for kind, parameters in checked:
        if kind == "polyline":
            points = parameters["points"]
            if not isinstance(points, (list, tuple)) or not 2 <= len(points) <= MAX_POLYLINE_POINTS:
                raise ValueError("Ломаная должна содержать от 2 до 1000 точек.")
            new_paths = _copy_paths([points], width, height)
            result = source + new_paths if parameters["mode"] == "append" else new_paths
        elif kind == "ellipse":
            new_paths = _ellipse(parameters, width, height)
            result = source + new_paths if parameters["mode"] == "append" else new_paths
        elif kind == "append_text":
            new_paths = transform_paths([], Plan("print_text", parameters), width, height)
            result = source + new_paths
        else:
            result = transform_paths(source, Plan(kind, parameters), width, height)
        # Do not permit an out-of-bounds step that a later step would "repair".
        source = _copy_paths(result, width, height)
    return source
