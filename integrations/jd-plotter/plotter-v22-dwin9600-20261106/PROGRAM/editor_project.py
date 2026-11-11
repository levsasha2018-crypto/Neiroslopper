"""Versioned sketch documents; no code, serial settings or physical calibration."""
from dataclasses import dataclass
import json
import math
import os
from pathlib import Path
import tempfile

from geometry import Sheet, validate_paths

MAX_BYTES = 5_000_000


@dataclass(frozen=True)
class SketchProject:
    sheet: Sheet
    paths: list
    name: str
    task: object = None
    anchor: object = None


def atomic_json(path, data):
    path = Path(path)
    raw = json.dumps(data, ensure_ascii=False, allow_nan=False, indent=2).encode("utf-8")
    if len(raw) > MAX_BYTES:
        raise ValueError("Файл слишком большой: максимум 5 МБ.")
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(dir=path.parent, prefix=path.name + ".", suffix=".tmp", delete=False) as f:
            temporary = Path(f.name)
            f.write(raw)
            f.flush()
            os.fsync(f.fileno())
        os.replace(temporary, path)
    finally:
        if temporary is not None and temporary.exists():
            temporary.unlink()


def checked_task(data, paths, sheet):
    """Only retain typography if its closed program reproduces the saved geometry."""
    if data is None:
        return None
    from editor_tools.planner import Plan, ACTION_KINDS, transform_paths
    if not isinstance(data, dict) or set(data) != {"kind", "parameters"}:
        raise ValueError("Некорректное описание текстового эскиза.")
    if not isinstance(data["kind"], str) or data["kind"] not in ACTION_KINDS or not isinstance(data["parameters"], dict):
        raise ValueError("Неизвестная операция эскиза.")
    plan = Plan(data["kind"], data["parameters"], "Восстановленный текстовый эскиз")
    try:
        rebuilt = transform_paths([], plan, sheet.width, sheet.height)
    except (ValueError, TypeError, KeyError, OverflowError):
        return None
    if len(rebuilt) != len(paths):
        return None
    for actual, expected in zip(rebuilt, paths):
        if len(actual) != len(expected) or any(math.dist(a, b) > 1e-6 for a, b in zip(actual, expected)):
            return None
    return plan


def decode_project(data):
    if not isinstance(data, dict) or set(data) != {"format", "version", "sheet_mm", "paths", "name", "task", "anchor"}:
        raise ValueError("Это не файл проекта плоттера или в нём есть неизвестные поля.")
    if data["format"] != "plotter-sketch" or type(data["version"]) is not int or data["version"] != 1:
        raise ValueError("Неподдерживаемая версия проекта.")
    dimensions = data["sheet_mm"]
    if not isinstance(dimensions, list) or len(dimensions) != 2 or any(type(v) not in (int, float) for v in dimensions):
        raise ValueError("Некорректный размер пробного листа.")
    sheet = Sheet(*dimensions)
    if not isinstance(data["name"], str) or len(data["name"]) > 500:
        raise ValueError("Некорректное имя эскиза.")
    raw = data["paths"]
    if not isinstance(raw, list) or not raw or len(raw) > 25_000:
        raise ValueError("Нет контуров или их слишком много.")
    paths, count = [], 0
    for path in raw:
        if not isinstance(path, list) or not path:
            raise ValueError("Некорректный контур.")
        points = []
        for point in path:
            count += 1
            if count > 50_000 or not isinstance(point, list) or len(point) != 2 or any(type(v) not in (int, float) for v in point):
                raise ValueError("Некорректные точки или более 50 000 точек.")
            points.append(tuple(float(v) for v in point))
        paths.append(points)
    validate_paths(paths, sheet)
    anchor = data["anchor"]
    if anchor is not None and (not isinstance(anchor, str) or anchor not in {"center", "top_left", "top_right", "bottom_left", "bottom_right"}):
        raise ValueError("Некорректная привязка эскиза.")
    task = checked_task(data["task"], paths, sheet)
    return SketchProject(sheet, paths, data["name"], task, anchor if task else None)


def save_project(path, sheet, paths, name, task=None, anchor=None):
    data = {"format": "plotter-sketch", "version": 1, "sheet_mm": [sheet.width, sheet.height],
            "paths": [[list(p) for p in contour] for contour in paths], "name": name,
            "task": {"kind": task.kind, "parameters": task.parameters} if task is not None else None,
            "anchor": anchor}
    checked = decode_project(data)
    if checked.task is None:
        data["task"] = data["anchor"] = None
    atomic_json(path, data)


def load_project(path):
    path = Path(path)
    if path.stat().st_size > MAX_BYTES:
        raise ValueError("Проект слишком большой: максимум 5 МБ.")
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (UnicodeError, json.JSONDecodeError, RecursionError) as exc:
        raise ValueError("Не удалось прочитать проект.") from exc
    return decode_project(data)
