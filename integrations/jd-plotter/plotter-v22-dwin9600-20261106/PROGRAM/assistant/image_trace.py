"""Bounded local raster/outline-to-pen conversion; never a controller command."""
from dataclasses import dataclass, replace
import io
import math
from pathlib import Path
import tempfile
import xml.etree.ElementTree as ET

import cv2
import numpy as np
from PIL import Image, ImageOps, ImageDraw

from geometry import Sheet, load_svg, validate_paths
from .path_metrics import measure_paths
from .reference_images import MAX_DOWNLOAD, check_cancel
from .scene import scene_plan, render_scene

MAX_PIXELS = 20_000_000
MAX_TRACE_POINTS = 48_000


@dataclass(frozen=True)
class Asset:
    name: str
    image: object  # RGB ndarray, composited on white, orientation corrected.
    vector_paths: tuple = ()


@dataclass(frozen=True)
class Options:
    kind: str = 'auto'
    detail: int = 70
    smoothing_mm: float = .12
    min_detail_mm: float = .6
    width_mm: float = 160
    margin_mm: float = 3
    remove_background: bool = False
    optimize: bool = True
    crop: tuple | None = None
    anchor: str = 'center'
    high_detail: bool = False
    working_side: int = 3072
    local_contrast: bool = False
    weak_edges: bool = False
    subject_box: tuple | None = None
    background_detail: int = 30
    hatching: bool = False
    hatch_spacing_mm: float = .8
    hatch_strength: float = .65
    repairs: tuple = ()
    auto_fit: bool = True
    stroke_limit: int = 12_000
    point_limit: int = 45_000

    def check(self):
        if self.kind not in ('auto', 'line', 'photo', 'vector'):
            raise ValueError('Неизвестный режим обводки.')
        if type(self.detail) is not int or not 0 <= self.detail <= 100:
            raise ValueError('Детализация должна быть от 0 до 100%.')
        for value, lo, hi in ((self.smoothing_mm, 0, 2), (self.min_detail_mm, 0, 10),
                               (self.width_mm, 1, 3000), (self.margin_mm, 0, 500)):
            if type(value) not in (int, float) or not lo <= value <= hi or not math.isfinite(value):
                raise ValueError('Проверьте ширину, поля, сглаживание и минимальный размер деталей.')
        if type(self.remove_background) is not bool or type(self.optimize) is not bool:
            raise ValueError('Некорректные переключатели обводки.')
        if self.anchor not in ('center', 'top_left', 'top_right', 'bottom_left', 'bottom_right'):
            raise ValueError('Неизвестное размещение.')
        if any(type(v) is not bool for v in (self.high_detail, self.local_contrast, self.weak_edges, self.hatching)):
            raise ValueError('Некорректные переключатели детальной обработки.')
        if type(self.working_side) is not int or not 512 <= self.working_side <= 4096:
            raise ValueError('Рабочая сторона должна быть 512–4096 пикселей.')
        if type(self.background_detail) is not int or not 0 <= self.background_detail <= 100:
            raise ValueError('Детализация фона должна быть 0–100%.')
        for v, lo, hi in ((self.hatch_spacing_mm, .2, 5), (self.hatch_strength, 0, 1)):
            if type(v) not in (int, float) or not lo <= v <= hi or not math.isfinite(v):
                raise ValueError('Шаг штриховки: 0,2–5 мм; насыщенность: 0–1.')
        if not isinstance(self.repairs, tuple) or len(self.repairs) > 4:
            raise ValueError('За один проход разрешены не более четырёх локальных правок.')
        for item in self.repairs:
            if (not isinstance(item, tuple) or len(item) != 2 or
                    item[1] not in ('recover_detail', 'simplify', 'hatch')):
                raise ValueError('Неизвестная локальная правка обводки.')
        if type(self.auto_fit) is not bool:
            raise ValueError('Некорректный режим автоподгонки сложности.')
        if type(self.stroke_limit) is not int or not 100 <= self.stroke_limit <= 20_000:
            raise ValueError('Лимит штрихов должен быть 100–20 000.')
        if type(self.point_limit) is not int or not 1000 <= self.point_limit <= MAX_TRACE_POINTS:
            raise ValueError('Лимит точек должен быть 1000–48 000.')
        if self.crop is not None and (not isinstance(self.crop, tuple) or len(self.crop) != 4 or
                any(type(v) is not int for v in self.crop)):
            raise ValueError('Некорректная рамка обрезки.')


@dataclass(frozen=True)
class Result:
    plan: object
    paths: tuple
    image: object
    metrics: dict
    warnings: tuple
    kind: str
    sheet: tuple
    options: Options = Options()
    registration: dict | None = None


def decode_rgb(raw):
    if not isinstance(raw, bytes) or not 0 < len(raw) <= MAX_DOWNLOAD:
        raise ValueError('Картинка пустая или больше 8 МБ.')
    try:
        with Image.open(io.BytesIO(raw)) as header:
            if header.width * header.height > MAX_PIXELS or min(header.size) < 8:
                raise ValueError('Изображение должно быть не меньше 8 пикселей и не больше 20 Мп.')
            if header.format not in ('PNG', 'JPEG', 'WEBP', 'GIF', 'BMP', 'TIFF'):
                raise ValueError('Поддерживаются PNG, JPEG, WebP, GIF, BMP и TIFF.')
            oriented = ImageOps.exif_transpose(header)
            rgba = oriented.convert('RGBA')
            white = Image.new('RGBA', rgba.size, 'white')
            white.alpha_composite(rgba)
            return np.asarray(white.convert('RGB')).copy()
    except (OSError, Image.DecompressionBombError) as exc:
        raise ValueError('Не удалось прочитать изображение.') from exc


def vector_preview(paths, size=900):
    from .scene import bounds
    left, top, right, bottom = bounds(paths)
    span = max(right-left, bottom-top)
    scale = (size-30)/max(span, 1e-6)
    width, height = max(32, int((right-left)*scale)+30), max(32, int((bottom-top)*scale)+30)
    image = Image.new('RGB', (width, height), 'white')
    draw = ImageDraw.Draw(image)
    for path in paths:
        draw.line([(15+(x-left)*scale, 15+(y-top)*scale) for x, y in path], fill='#163b60', width=2)
    return np.asarray(image).copy()


def asset_from_bytes(raw, name='Образец', *, mime=''):
    if not isinstance(raw, bytes) or not 0 < len(raw) <= MAX_DOWNLOAD:
        raise ValueError('Образец пустой или больше 8 МБ.')
    name = str(name)[:160] or 'Образец'
    is_svg = mime == 'image/svg+xml' or name.lower().endswith('.svg') or b'<svg' in raw[:4096].lower()
    if is_svg:
        if len(raw) > 3_000_000:
            raise ValueError('SVG больше 3 МБ.')
        # Reuse the existing fail-closed importer: scripts, text, external
        # images, DTD, stylesheets, use, clipping and effects are never rendered.
        with tempfile.TemporaryDirectory(prefix='plotter-reference-') as directory:
            filename = Path(directory) / 'reference.svg'
            filename.write_bytes(raw)
            try:
                paths = load_svg(filename, Sheet(1000, 1000), 3)
            except ET.ParseError as exc:
                raise ValueError('Не удалось прочитать SVG: повреждена структура изображения.') from exc
            except RecursionError as exc:
                raise ValueError('SVG содержит слишком много вложенных групп. Упростите структуру изображения.') from exc
        frozen = tuple(tuple(point for point in path) for path in paths)
        return Asset(str(name)[:160], vector_preview(frozen), frozen)
    return Asset(str(name)[:160], decode_rgb(raw))


def read_asset(path):
    source = Path(path)
    with source.open('rb') as stream:
        raw = stream.read(MAX_DOWNLOAD + 1)
    return asset_from_bytes(raw, source.name)


def thin(mask, cancel=None):
    """Zhang-Suen centreline with bounded iterations; no extra contrib package."""
    image = np.pad((mask > 0).astype(np.uint8), 1)
    for _ in range(256):
        check_cancel(cancel)
        changed = False
        for phase in (0, 1):
            p = image[1:-1, 1:-1]
            ns = (image[:-2, 1:-1], image[:-2, 2:], image[1:-1, 2:], image[2:, 2:],
                  image[2:, 1:-1], image[2:, :-2], image[1:-1, :-2], image[:-2, :-2])
            count = sum(ns)
            transitions = sum(((a == 0) & (b > 0)).astype(np.uint8)
                              for a, b in zip(ns, ns[1:] + ns[:1]))
            if phase == 0:
                free = (ns[0]*ns[2]*ns[4] == 0) & (ns[2]*ns[4]*ns[6] == 0)
            else:
                free = (ns[0]*ns[2]*ns[6] == 0) & (ns[0]*ns[4]*ns[6] == 0)
            remove = (p > 0) & (count >= 2) & (count <= 6) & (transitions == 1) & free
            if remove.any():
                p[remove] = 0
                changed = True
        if not changed:
            return image[1:-1, 1:-1] * 255
    raise ValueError('Слишком толстые заливки для штрихов. Выберите режим «Фото» или линейный образец.')


def skeleton_paths(mask, cancel=None):
    from .trace_graph import trails
    return trails(mask, cancel)


def _fit(paths, sheet, options, *, frame=None, registration=None):
    from .scene import bounds
    left, top, right, bottom = (0, 0, frame[0]-1, frame[1]-1) if frame else bounds(paths)
    width, height = right-left, bottom-top
    available_w = min(options.width_mm, sheet.width-2*options.margin_mm)
    available_h = sheet.height-2*options.margin_mm
    if min(available_w, available_h) <= 0 or max(width, height) < 1e-9:
        raise ValueError('Рисунок или доступная область слишком малы.')
    scale = min(available_w/width if width else math.inf, available_h/height if height else math.inf)
    anchor = options.anchor
    ox = (sheet.width-width*scale)/2
    oy = (sheet.height-height*scale)/2
    if anchor.endswith('left'): ox = options.margin_mm
    if anchor.endswith('right'): ox = sheet.width-options.margin_mm-width*scale
    if anchor.startswith('top'): oy = options.margin_mm
    if anchor.startswith('bottom'): oy = sheet.height-options.margin_mm-height*scale
    if registration is not None:
        registration.update(scale_mm_per_pixel=scale, x_mm=ox-left*scale, y_mm=oy-top*scale)
    result = [[((x-left)*scale+ox, (y-top)*scale+oy) for x, y in path] for path in paths]
    return result


def _simplify(paths, options, cancel):
    from .trace_budget import fit_paths
    return fit_paths(paths, options, cancel)[0]


def _trace_once(asset, sheet, options=Options(), *, cancel=None):
    options.check()
    check_cancel(cancel)
    image = asset.image
    warnings = []
    from .trace_detail import checked_box, working_image
    if options.subject_box is not None:
        checked_box(options.subject_box, image.shape[1], image.shape[0])
    for box, _ in options.repairs:
        checked_box(box, image.shape[1], image.shape[0])
    registration = {}
    detail_metrics = {}
    frame = None
    hatch_start = None
    if options.crop is not None:
        if asset.vector_paths and options.kind in ('auto', 'vector'):
            raise ValueError('Обрезка рамкой доступна для растровой обводки; выберите «Линии» или «Фото».')
        box = options.crop
        if not isinstance(box, tuple) or len(box) != 4 or any(type(v) is not int for v in box):
            raise ValueError('Некорректная рамка обрезки.')
        x0, y0, x1, y1 = box
        if not (0 <= x0 < x1 <= image.shape[1] and 0 <= y0 < y1 <= image.shape[0]) or min(x1-x0, y1-y0) < 8:
            raise ValueError('Выделите рамку не меньше 8 × 8 пикселей внутри изображения.')
        image = image[y0:y1, x0:x1].copy()
    kind = options.kind
    if asset.vector_paths and kind in ('auto', 'vector'):
        if options.hatching or options.repairs or options.subject_box:
            raise ValueError('Рамки и штриховка работают с растром: выберите «Фотография» или «Линейный рисунок».')
        paths = [list(path) for path in asset.vector_paths]
        kind = 'vector'
    else:
        if kind == 'vector':
            raise ValueError('Этот образец не содержит векторных контуров SVG.')
        advanced = options.high_detail or options.local_contrast or options.weak_edges or options.hatching or options.subject_box or options.repairs
        side = options.working_side if options.high_detail else 480 + options.detail*8
        small, ratio = working_image(image, side)
        gray = cv2.cvtColor(small, cv2.COLOR_RGB2GRAY)
        if kind == 'auto':
            colored = np.mean(np.ptp(small.astype(np.int16), axis=2) > 20) > .02
            midtones = np.mean((gray > 40) & (gray < 230)) > .04
            kind = 'line' if np.mean(gray > 245) > .75 and np.mean(gray < 180) < .18 and not colored and not midtones else 'photo'
        if kind == 'line':
            if advanced:
                from .trace_detail import line_regions
                mask = line_regions(gray, options, ratio, image.shape, cancel)
            else:
                _, mask = cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY_INV + cv2.THRESH_OTSU)
        elif advanced:
            from .trace_detail import extract
            mask, gray, small, ratio, detail_metrics = extract(image, replace(options, working_side=side), cancel)
            warnings.append('Детальный режим: слабые границы могут добавить шум. Рамка объекта сохраняет фон с отдельной детализацией.')
        else:
            sigma = .4 + (100-options.detail)/55
            blur = cv2.GaussianBlur(gray, (0, 0), sigma)
            low = 25+(100-options.detail)*.65
            mask = cv2.Canny(blur, low, low*2.5)
            if options.remove_background:
                check_cancel(cancel)
                h, w = gray.shape
                inset = max(2, round(min(w, h)*.025))
                foreground = np.zeros((h, w), np.uint8)
                try:
                    cv2.grabCut(small, foreground, (inset, inset, w-2*inset, h-2*inset),
                                np.zeros((1, 65), np.float64), np.zeros((1, 65), np.float64), 3, cv2.GC_INIT_WITH_RECT)
                    check_cancel(cancel)
                    keep = np.isin(foreground, (cv2.GC_FGD, cv2.GC_PR_FGD)).astype(np.uint8)*255
                    if .005 < np.mean(keep > 0) < .95:
                        mask &= cv2.dilate(keep, np.ones((3, 3), np.uint8))
                        mask |= cv2.Canny(keep, 50, 150)
                    else:
                        warnings.append('Фон не удалось уверенно отделить; оставлены исходные границы.')
                except cv2.error:
                    warnings.append('Отделение фона не получилось; обрежьте фон рамкой.')
        if kind == 'photo':
            warnings.append('Фото: отражения и тени могут стать линиями. Сравните с оригиналом; фон отделяется приблизительно.')
        check_cancel(cancel)
        # Delete single-pixel specks without erasing legitimate narrow lines.
        count, labels, stats, _ = cv2.connectedComponentsWithStats(mask, connectivity=8)
        keep = np.zeros(count, np.uint8)
        keep[1:] = (stats[1:, cv2.CC_STAT_AREA] >= 3).astype(np.uint8)*255
        mask = thin(keep[labels], cancel)
        detail_metrics['skeleton_pixels'] = int(np.count_nonzero(mask))
        paths = skeleton_paths(mask, cancel)
        if advanced:
            frame = (gray.shape[1], gray.shape[0])
            _fit([], sheet, options, frame=frame, registration=registration)
            hatch_start = len(paths)
            hatch_paths, options, tone_warnings = _tone_paths(gray, registration['scale_mm_per_pixel'], options,
                                                             ratio, image.shape, cancel)
            warnings.extend(tone_warnings)
            paths.extend(hatch_paths)
            detail_metrics['hatch_path_count'] = len(hatch_paths)
            if hatch_paths:
                warnings.append('Штриховка передаёт тон, но увеличивает время и соединения без подъёма ручки.')
        if not paths:
            raise ValueError('Не найдены линии. Попробуйте другой режим или более контрастный образец.')
        registration.update(working_width=gray.shape[1], working_height=gray.shape[0],
                            source_width=image.shape[1], source_height=image.shape[0],
                            crop=options.crop, full_frame=bool(frame))
    paths = _fit(paths, sheet, options, frame=frame, registration=registration)
    subject_mm = None
    if options.subject_box and kind != 'vector':
        from .trace_detail import local_box
        x0, y0, x1, y1 = local_box(options.subject_box, image.shape, options)
        s = registration['scale_mm_per_pixel']*ratio
        subject_mm = x0*s+registration['x_mm'], y0*s+registration['y_mm'], x1*s+registration['x_mm'], y1*s+registration['y_mm']
    from .trace_budget import fit_paths
    paths, budget_metrics, budget_warnings = fit_paths(paths, options, cancel, subject_mm=subject_mm, hatch_start=hatch_start)
    detail_metrics.update(budget_metrics)
    warnings.extend(budget_warnings)
    validate_paths(paths, sheet)
    scene = {'version': 1, 'objects': [{'id': 'o1', 'name': asset.name[:160],
             'source': {'kind': 'paths', 'parameters': {'paths': paths}},
             'matrix': [1, 0, 0, 1, 0, 0]}], 'route': None}
    if options.optimize:
        from .route_opt import optimize_scene, RouteOptimizationTimeout
        try:
            scene, _, _ = optimize_scene(scene, sheet.width, sheet.height, cancel)
            paths = render_scene(scene, sheet.width, sheet.height)
        except RouteOptimizationTimeout:
            warnings.append('Маршрут не успел оптимизироваться: предпросмотр готов с исходным порядком штрихов. Ни один штрих из-за этого не удалён.')
    metrics = measure_paths(paths)
    metrics.update(path_count=len(paths), point_count=sum(map(len, paths)))
    metrics.update(detail_metrics)
    warnings.append('Без подъёма ручки все соединения тоже рисуются. Первый подвод проверяется в основном пульте.')
    explanation = (f'Рисунок по образцу «{asset.name}»: {len(paths)} штрихов, {metrics["point_count"]} точек. '
                   f'Соединения {metrics["connection_length_mm"]:.1f} мм. Пропорции сохранены. '
                   'Это предложение замены выбранного эскиза, не движение станка.')
    check_cancel(cancel)
    return Result(scene_plan(scene, explanation), tuple(tuple(p for p in path) for path in paths),
                  image.copy(), metrics, tuple(warnings), kind, (sheet.width, sheet.height), options, registration)


def _tone_paths(gray, scale, options, ratio, image_shape, cancel):
    from .trace_detail import hatching
    from .trace_budget import ComplexityLimit
    initial = options.hatch_spacing_mm
    cap = min(20_000, max(200, options.stroke_limit//3)) if options.auto_fit else 20_000
    for _ in range(8):
        check_cancel(cancel)
        try:
            paths = hatching(gray, scale, options, ratio, image_shape, cancel, max_paths=cap)
            notes = ([f'Автоподгонка: шаг штриховки увеличен с {initial:g} до {options.hatch_spacing_mm:g} мм, '
                      f'чтобы тон не переполнил бюджет {cap} штрихов.'] if options.hatch_spacing_mm != initial else [])
            return paths, options, notes
        except ComplexityLimit:
            if not options.auto_fit or options.hatch_spacing_mm >= 5: raise
            options = replace(options, hatch_spacing_mm=min(5, options.hatch_spacing_mm*1.6))
    raise ComplexityLimit('hatches', cap+1, cap)


def trace(asset, sheet, options=Options(), *, cancel=None):
    """Adaptive local preview, bounded to four resource retries; no external requests."""
    from .trace_budget import ComplexityLimit
    check_cancel(cancel)
    if not isinstance(options, Options):
        raise ValueError('Некорректные настройки обводки.')
    options.check()
    if not isinstance(asset, Asset) or not isinstance(asset.name, str) or not asset.name:
        raise ValueError('Некорректный образец изображения.')
    image = asset.image
    if (not isinstance(image, np.ndarray) or image.dtype != np.uint8 or image.ndim != 3 or
            image.shape[2] != 3 or min(image.shape[:2]) < 8 or
            image.shape[0]*image.shape[1] > MAX_PIXELS):
        raise ValueError('Образец должен быть RGB-изображением от 8 пикселей до 20 Мп.')
    if not isinstance(sheet, Sheet):
        raise ValueError('Некорректный размер листа.')
    if not isinstance(asset.vector_paths, (tuple, list)):
        raise ValueError('Некорректные векторные контуры образца.')
    if asset.vector_paths and options.kind in ('auto', 'vector'):
        vector_points = 0
        for path in asset.vector_paths:
            check_cancel(cancel)
            try:
                points = np.asarray(path, np.float64)
            except (TypeError, ValueError, OverflowError) as exc:
                raise ValueError('Некорректные координаты векторного образца.') from exc
            if (points.ndim != 2 or points.shape[1] != 2 or len(points) < 2 or not np.isfinite(points).all() or
                    any(isinstance(v, (bool, np.bool_)) or not isinstance(v, (int, float, np.integer, np.floating))
                        for point in path for v in point)):
                raise ValueError('Векторный контур должен содержать конечные числовые пары координат.')
            vector_points += len(points)
            if vector_points > 50_000:
                raise ComplexityLimit('points', vector_points, 50_000)
    effective = options
    notes = []
    for attempt in range(4):
        check_cancel(cancel)
        try:
            result = _trace_once(asset, sheet, effective, cancel=cancel)
            metrics = {**result.metrics, 'processing_retries': attempt}
            metrics['auto_fit_applied'] = metrics.get('auto_fit_applied', False) or bool(notes) or effective.hatch_spacing_mm != result.options.hatch_spacing_mm
            return replace(result, metrics=metrics, warnings=tuple(notes)+result.warnings)
        except ComplexityLimit as exc:
            if (not options.auto_fit or (asset.vector_paths and options.kind in ('auto', 'vector')) or
                    exc.kind == 'hatches' or attempt == 3): raise
            current_side = effective.working_side if effective.high_detail else 480+effective.detail*8
            new_side = max(512, round(current_side*.75))
            if new_side >= current_side: raise
            effective = replace(effective, high_detail=True, working_side=new_side)
            notes.append(f'Автоподгонка: рабочая сторона уменьшена до {new_side} px после превышения внутреннего ресурса. '
                         'Композиция сохранена, мелкие детали могли измениться.')
