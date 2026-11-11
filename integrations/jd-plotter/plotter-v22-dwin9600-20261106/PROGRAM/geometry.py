"""Paper-space geometry: origin at top left; right/down are positive."""
from dataclasses import dataclass
import math
from numbers import Real
from pathlib import Path as FilePath
import xml.etree.ElementTree as ET


@dataclass(frozen=True)
class Axes:
    swap: bool = False
    right_sign: int = 1
    down_sign: int = 1

    def machine(self, point):
        u, v = point
        a, b = u * self.right_sign, v * self.down_sign
        return (b, a) if self.swap else (a, b)

    def paper(self, point):
        x, y = point
        a, b = (y, x) if self.swap else (x, y)
        return a * self.right_sign, b * self.down_sign


@dataclass(frozen=True)
class Sheet:
    width: float
    height: float

    def __post_init__(self):
        if not all(isinstance(v, Real) and not isinstance(v, bool)
                   and 1 <= v <= 3000 and math.isfinite(v)
                   for v in (self.width, self.height)):
            raise ValueError("Вторая точка должна быть правее и ниже первой; размер каждой стороны — от 1 до 3000 мм.")

    def contains(self, point):
        x, y = point
        # Reject types and range first: isfinite() converts integers to float,
        # which can overflow on malformed, arbitrarily large coordinates.
        return (all(isinstance(v, Real) and not isinstance(v, bool) for v in (x, y))
                and -0.001 <= x <= float(self.width) + 0.001
                and -0.001 <= y <= float(self.height) + 0.001
                and math.isfinite(x) and math.isfinite(y))


def validate_paths(paths, sheet):
    if not isinstance(paths, (list, tuple)) or not paths:
        raise ValueError("В рисунке нет линий.")
    count = 0
    has_line = False
    for path in paths:
        if not isinstance(path, (list, tuple)) or not path:
            raise ValueError("Рисунок содержит пустой или некорректный контур.")
        has_line = has_line or len(path) >= 2
        for point in path:
            count += 1
            if count > 50000:
                raise ValueError("Слишком сложный рисунок: максимум 50 000 точек.")
            if (not isinstance(point, (list, tuple)) or len(point) != 2 or
                    any(isinstance(value, bool) or not isinstance(value, Real) for value in point)):
                raise ValueError("Каждая точка рисунка должна содержать два числовых значения X и Y.")
            if not sheet.contains(point):
                raise ValueError("Рисунок выходит за заданный прямоугольник.")
    if not has_line:
        raise ValueError("В рисунке нет линий.")


def route(paths, start):
    """Includes ALL moves; connectors are drawn too when there is no pen lift."""
    current = start
    for path in paths:
        for index, point in enumerate(path):
            if math.dist(current, point) > 0.0005:
                yield current, point, index == 0
            current = point


def test_square(sheet):
    size = min(10.0, sheet.width - 2, sheet.height - 2)
    if size < 1:
        raise ValueError("Для теста нужен прямоугольник не меньше 3 × 3 мм.")
    return [[(1, 1), (1 + size, 1), (1 + size, 1 + size), (1, 1 + size), (1, 1)]]


class ImportedPaths(list):
    def __init__(self, paths, actual_size=False):
        super().__init__(paths)
        self.actual_size = actual_size


def load_svg(filename, sheet, margin=3.0):
    """Outline-only SVG import. Reject unsupported rendering instead of guessing."""
    with FilePath(filename).open('rb') as stream:
        raw = stream.read(3_000_001)
    return load_svg_bytes(raw, sheet, margin)


def load_svg_bytes(raw, sheet, margin=3.0, *, paper_coordinates=False):
    """Render authored vector strokes. In paper space one unit equals one millimetre."""
    from svgelements import SVG, Shape, Path, Move, Line, Close
    if not math.isfinite(margin) or margin < 0 or 2 * margin >= min(sheet.width, sheet.height):
        raise ValueError("Поле должно быть неотрицательным и меньше половины короткой стороны.")
    if len(raw) > 3_000_000:
        raise ValueError("SVG слишком большой: максимум 3 МБ.")
    markers = raw.replace(b'\x00', b'').upper()  # Also recognize UTF-16/32 markup.
    if b"<!DOCTYPE" in markers or b"<!ENTITY" in markers:
        raise ValueError("SVG с DTD/ENTITY не поддерживается.")
    root = ET.fromstring(raw)
    actual_size = root.get("data-plotter-size") == "actual"
    forbidden = {"text", "image", "use", "clipPath", "mask", "filter", "style", "script",
                 "foreignObject", "pattern", "marker", "symbol", "switch", "textPath"}
    for element in root.iter():
        tag = element.tag.rsplit("}", 1)[-1]
        if tag in forbidden:
            raise ValueError(f"SVG содержит {tag}. Преобразуйте текст/объекты в обычные контуры, уберите маски и эффекты.")
        if any(k in element.attrib for k in ("clip-path", "mask", "filter", "marker-start", "marker-mid", "marker-end")):
            raise ValueError("Маски, обрезка и маркеры SVG не поддерживаются.")
        if any("url(" in value.lower() for value in element.attrib.values()):
            raise ValueError("Ссылочные эффекты SVG не поддерживаются; экспортируйте обычные контуры.")
    import io
    svg = SVG.parse(io.BytesIO(raw), reify=True, ppi=96)
    shapes = []
    boxes = []
    for element in svg.elements():
        if not isinstance(element, Shape):
            continue
        values = element.values
        if values.get("visibility") in ("hidden", "collapse") or values.get("display") == "none":
            continue
        path = Path(element)
        path.reify()
        box = path.bbox()
        if box and len(path):
            if not all(math.isfinite(v) for v in box):
                raise ValueError("Некорректные координаты SVG.")
            shapes.append(path)
            boxes.append(box)
    if not boxes:
        raise ValueError("В SVG нет поддерживаемых контуров.")
    left, top = min(b[0] for b in boxes), min(b[1] for b in boxes)
    width = max(b[2] for b in boxes) - left
    height = max(b[3] for b in boxes) - top
    if max(width, height) <= 0:
        raise ValueError("У рисунка нулевой размер.")
    if paper_coordinates:
        if not sheet.contains((left, top)) or not sheet.contains((left + width, top + height)):
            raise ValueError("Рисунок выходит за заданный прямоугольник.")
        scale = 1.0
        ox, oy = left, top
    elif actual_size:
        # SVG geometry is resolved into CSS pixels; convert those to mm.
        # Calibration samples must never grow or shrink to fill the paper.
        scale = 25.4 / 96
        if width * scale > sheet.width - 2 * margin + 0.001 or height * scale > sheet.height - 2 * margin + 0.001:
            raise ValueError("Рисунок 1:1 не помещается на лист с заданным полем. Выберите больший лист или уменьшите поле.")
        ox = oy = margin
    else:
        scale = min((sheet.width - 2 * margin) / width if width else math.inf,
                    (sheet.height - 2 * margin) / height if height else math.inf)
        ox, oy = (sheet.width - width * scale) / 2, (sheet.height - height * scale) / 2
    curve_tolerance = 0.02 if actual_size else 0.10
    max_curve_segment = 0.5 if actual_size else 3

    def convert(point):
        return (float(point.x) - left) * scale + ox, (float(point.y) - top) * scale + oy

    result, current = [], []
    total = 0

    def append(point):
        nonlocal total
        total += 1
        if total > 50000:
            raise ValueError("Слишком сложный SVG: упростите контуры (максимум 50 000 точек).")
        current.append(point)

    def curve(segment, a, b, depth=0):
        pa, pb = convert(segment.point(a)), convert(segment.point(b))
        error = 0.0
        for fraction in (0.25, 0.5, 0.75):
            actual = convert(segment.point(a + (b - a) * fraction))
            linear = (pa[0] + (pb[0] - pa[0]) * fraction, pa[1] + (pb[1] - pa[1]) * fraction)
            error = max(error, math.dist(actual, linear))
        if error <= curve_tolerance and math.dist(pa, pb) <= max_curve_segment:
            append(pb)
        elif depth >= 16:
            raise ValueError("Не удалось безопасно упростить кривую SVG.")
        else:
            mid = (a + b) / 2
            curve(segment, a, mid, depth + 1)
            curve(segment, mid, b, depth + 1)

    for shape in shapes:
        for segment in shape:
            if isinstance(segment, Move):
                if len(current) >= 2:
                    result.append(current)
                current = []
                append(convert(segment.end))
            else:
                if not current and segment.start is not None:
                    append(convert(segment.start))
                if isinstance(segment, (Line, Close)):
                    append(convert(segment.end))
                else:
                    curve(segment, 0, 1)
        if len(current) >= 2:
            result.append(current)
        current = []
    validate_paths(result, sheet)
    return ImportedPaths(result, actual_size or paper_coordinates)
