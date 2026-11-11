"""Explicit bounded preview adaptation. Never changes machine limits or geometry safety."""
import math

import cv2
import numpy as np

from .reference_images import check_cancel


class ComplexityLimit(ValueError):
    def __init__(self, kind, count, limit):
        self.kind, self.count, self.limit = kind, count, limit
        names = {'pixels': 'пикселей контура', 'raw_paths': 'исходных штрихов',
                 'hatches': 'тональных штрихов', 'points': 'точек', 'paths': 'штрихов'}
        super().__init__(f'Сложность: {count:,} {names[kind]}, предел {limit:,}. '
                         'Включите «Автоподгонка сложности» или уменьшите детали фона.')


def fit_paths(paths, options, cancel=None, *, subject_mm=None, hatch_start=None):
    """Keep all strokes when possible; first simplify coordinates, then rank
    excess tiny strokes by length, foreground and contour-vs-tone priority.
    Every change is returned as metrics/warnings, never disguised as full fidelity.
    """
    from .image_trace import Options
    check_cancel(cancel)
    if not isinstance(options, Options):
        raise ValueError('Некорректные настройки очистки контуров.')
    options.check()
    if not isinstance(paths, (tuple, list)):
        raise ValueError('Контуры должны быть списком или кортежем линий.')
    if subject_mm is not None:
        if (not isinstance(subject_mm, (tuple, list)) or len(subject_mm) != 4 or
                any(type(v) not in (int, float) or abs(v) > 3000 or not math.isfinite(v) for v in subject_mm) or
                not subject_mm[0] < subject_mm[2] or not subject_mm[1] < subject_mm[3]):
            raise ValueError('Некорректная область главного объекта в миллиметрах.')
    if hatch_start is not None and (type(hatch_start) is not int or not 0 <= hatch_start <= len(paths)):
        raise ValueError('Некорректное начало тональных штрихов.')
    minimum = options.min_detail_mm
    records = []
    filtered = 0
    for index, path in enumerate(paths):
        check_cancel(cancel)
        try:
            original = np.asarray(path, np.float64)
        except (TypeError, ValueError, OverflowError) as exc:
            raise ValueError('Некорректные координаты контура.') from exc
        if original.ndim != 2 or original.shape[1] != 2 or len(original) < 2 or not np.isfinite(original).all():
            raise ValueError('Контур должен содержать хотя бы две конечные пары координат.')
        if any(isinstance(value, (bool, np.bool_)) or not isinstance(value, (int, float, np.integer, np.floating))
               for point in path for value in point):
            raise ValueError('Координаты контура должны быть числами, не текстом или флагами.')
        if np.max(np.abs(original)) > np.finfo(np.float32).max:
            raise ValueError('Координаты контура слишком велики для обводки.')
        source = [(float(x), float(y)) for x, y in original]
        length = sum(math.dist(a, b) for a, b in zip(source, source[1:]))
        if length <= 0 or length < minimum:
            filtered += 1
            continue
        points = original.astype(np.float32).reshape(-1, 1, 2)
        closed = source[0] == source[-1]
        foreground = False
        if subject_mm:
            x0, y0, x1, y1 = subject_mm
            sampled = points.reshape(-1, 2)[::max(1, len(points)//12)]
            foreground = float(np.mean((sampled[:, 0] >= x0) & (sampled[:, 0] <= x1) &
                                       (sampled[:, 1] >= y0) & (sampled[:, 1] <= y1))) >= .5
        tone = hatch_start is not None and index >= hatch_start
        priority = length * (3 if foreground else 1) * (.25 if tone else 1) * (1.15 if closed else 1)
        records.append((index, points, source, closed, priority, foreground))
    if not records:
        raise ValueError('После очистки линий не осталось. Уменьшите минимальный размер деталей или смените режим.')
    tolerance = options.smoothing_mm
    def simplify(epsilon):
        output = []
        preserved = 0
        for index, points, source, closed, priority, foreground in records:
            check_cancel(cancel)
            retained_original = False
            # The subject receives a smaller deviation; no infinite smoothing.
            local = min(epsilon, max(options.smoothing_mm, .08)) if foreground else epsilon
            if not local:
                line = list(source)
            else:
                reduced = cv2.approxPolyDP(points, local, closed).reshape(-1, 2)
                # OpenCV needs float32, but its retained vertices are original
                # vertices. Restore their full precision instead of moving ink.
                vertices = {}
                for point, original_point in zip(points.reshape(-1, 2), source):
                    vertices.setdefault(tuple(point), original_point)
                line = [vertices[tuple(point)] for point in reduced]
                polygon = closed and len(set(source)) >= 3
                if len(set(line)) < (3 if polygon else 2):
                    # Smoothing must not silently erase a valid small feature
                    # or turn a closed contour into a straight segment.
                    line = list(source)
                    preserved += 1
                    retained_original = True
                elif closed and line[0] != line[-1]:
                    line.append(line[0])
                elif not closed:
                    line[0], line[-1] = source[0], source[-1]
            output.append((index, line, priority, retained_original))
        return output, preserved
    candidate, preserved = simplify(tolerance)
    initial_points = sum(len(row[1]) for row in candidate)
    initial_paths = len(candidate)
    if not candidate:
        raise ValueError('После сглаживания линий не осталось. Уменьшите сглаживание.')
    if not options.auto_fit:
        if initial_paths > options.stroke_limit: raise ComplexityLimit('paths', initial_paths, options.stroke_limit)
        if initial_points > options.point_limit: raise ComplexityLimit('points', initial_points, options.point_limit)
    else:
        # Slightly stronger smoothing can fit the points without discarding
        # whole strokes. Stop at 0.2 mm (or the user's already larger tolerance).
        for value in sorted(set((max(tolerance, .06), max(tolerance, .1), max(tolerance, .2)))):
            if sum(len(row[1]) for row in candidate) <= options.point_limit: break
            tolerance = value
            candidate, preserved = simplify(tolerance)
        if len(candidate) > options.stroke_limit or sum(len(row[1]) for row in candidate) > options.point_limit:
            if any(len(row[1]) > options.point_limit for row in candidate):
                raise ComplexityLimit('points', max(len(row[1]) for row in candidate), options.point_limit)
            kept, points_used = [], 0
            for row in sorted(candidate, key=lambda r: (-r[2], r[0])):
                check_cancel(cancel)
                if len(kept) >= options.stroke_limit: break
                if points_used+len(row[1]) <= options.point_limit:
                    kept.append(row); points_used += len(row[1])
            candidate = sorted(kept, key=lambda r: r[0])
    if not candidate:
        raise ValueError('Слишком маленький бюджет предпросмотра для этих контуров. Увеличьте лимит точек.')
    output = [row[1] for row in candidate]
    preserved = sum(row[3] for row in candidate)
    stats = {'source_path_count': len(paths), 'requested_minimum_filtered': filtered,
             'before_budget_paths': initial_paths, 'before_budget_points': initial_points,
             'budget_removed_paths': initial_paths-len(output), 'effective_smoothing_mm': tolerance,
             'smoothing_preserved_paths': preserved,
             'stroke_limit': options.stroke_limit, 'point_limit': options.point_limit,
             'auto_fit_applied': tolerance > options.smoothing_mm or len(output) < initial_paths}
    warnings = []
    if preserved:
        warnings.append(f'Для {preserved} мелких контуров сохранена исходная форма: заданное сглаживание схлопывало их.')
    if tolerance > options.smoothing_mm:
        protected = f'; в главном объекте не выше {max(options.smoothing_mm, .08):g} мм' if subject_mm else ''
        warnings.append(f'Автоподгонка: сглаживание увеличено до {tolerance:g} мм{protected}. Мелкие детали могли измениться.')
    if stats['budget_removed_paths']:
        warnings.append(f'Автоподгонка: исключено {stats["budget_removed_paths"]} мелких/менее приоритетных '
                        f'штрихов из {initial_paths}; длинные контуры и рамка главного объекта имеют приоритет. '
                        'Это упрощённый вариант — сравните с исходником.')
    check_cancel(cancel)
    return output, stats, warnings
