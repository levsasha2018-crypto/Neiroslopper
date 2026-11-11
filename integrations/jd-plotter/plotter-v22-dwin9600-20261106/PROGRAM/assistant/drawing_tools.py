"""Small, deterministic pen paths in millimetres; no devices, files or network."""
from __future__ import annotations

import math


def _finite(value, name):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{name}: требуется число.")
    try:
        result = float(value)
    except (OverflowError, ValueError):
        raise ValueError(f"{name}: слишком большое число.") from None
    if not math.isfinite(result):
        raise ValueError(f"{name}: число должно быть конечным.")
    return result


def rectangle_frame(bounds, padding_mm):
    """One closed rectangle around an existing bbox; no clamping or scaling."""
    if not isinstance(bounds, (tuple, list)) or len(bounds) != 4:
        raise ValueError("Нужны четыре границы рисунка.")
    left, top, right, bottom = (_finite(value, "Граница") for value in bounds)
    padding = _finite(padding_mm, "Отступ рамки")
    if not 0 <= padding <= 3000 or right < left or bottom < top:
        raise ValueError("Неверные границы или отступ рамки.")
    left, right = left - padding, right + padding
    top, bottom = top - padding, bottom + padding
    if right - left <= 1e-9 or bottom - top <= 1e-9:
        raise ValueError("У рамки должны быть ненулевые ширина и высота.")
    return [(left, top), (right, top), (right, bottom), (left, bottom), (left, top)]


def ruler_paths(length_mm):
    """One path with 1 mm ticks, returning over each tick without a pen lift.

    Local bbox is (0,0,length,5). Long marks occur at 10 mm, middle at 5 mm;
    the endpoint keeps its requested physical length, including fractions.
    """
    length = _finite(length_mm, "Длина линейки")
    if not 1 <= length <= 1000:
        raise ValueError("Длина линейки должна быть от 1 до 1000 мм.")
    points = [(0.0, 5.0)]
    for tick in range(math.floor(length) + 1):
        x = float(tick)
        height = 5.0 if tick % 10 == 0 else 3.0 if tick % 5 == 0 else 2.0
        if points[-1] != (x, 5.0):
            points.append((x, 5.0))
        points.extend(((x, 5.0 - height), (x, 5.0)))
    if points[-1][0] != length:
        points.append((length, 5.0))
    return [points]


def calibration_pattern_paths(size_mm):
    """One connected square/cross/diagonal/circle test; nominal square side size.

    Retraced edges deliberately keep all components connected for plotters
    without a pen lift. The circle diameter is half the square side length.
    This is a visual/measuring test, NOT an automatic calibration operation.
    """
    size = _finite(size_mm, "Сторона тестового квадрата")
    if not 1 <= size <= 200:
        raise ValueError("Сторона тестового квадрата должна быть от 1 до 200 мм.")
    mid, radius = size / 2, size / 4
    points = [(0.0, 0.0), (size, 0.0), (size, size), (0.0, size), (0.0, 0.0),
              (size, size), (0.0, size), (size, 0.0), (mid, 0.0),
              (mid, size), (mid, mid), (0.0, mid), (size, mid), (mid, mid),
              (mid + radius, mid)]
    for step in range(1, 97):
        angle = step * 2 * math.pi / 96
        points.append((mid + radius * math.cos(angle), mid + radius * math.sin(angle)))
    points.append((mid, mid))
    return [points]
