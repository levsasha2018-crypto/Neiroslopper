"""Closed object scene, faithful geometry and per-object affine editing. No hardware."""
from copy import deepcopy
import math
import re

from geometry import Sheet, validate_paths
from .planner import Plan, transform_paths

IDENTITY = [1., 0., 0., 1., 0., 0.]
GEOMETRY_KINDS = {'center', 'rotate', 'scale', 'resize_width', 'margin', 'mirror', 'move_preview', 'place_preview'}


def bounds(paths):
    points = [p for line in paths for p in line]
    if not points:
        raise ValueError("Нет линий объекта.")
    return min(x for x, _ in points), min(y for _, y in points), max(x for x, _ in points), max(y for _, y in points)


def compose(a, b):
    """a after b; [xx,yx,xy,yy,tx,ty]."""
    aa, ab, ac, ad, ax, ay = a
    ba, bb, bc, bd, bx, by = b
    return [aa*ba + ac*bb, ab*ba + ad*bb, aa*bc + ac*bd, ab*bc + ad*bd,
            aa*bx + ac*by + ax, ab*bx + ad*by + ay]


def apply_matrix(paths, m):
    a, b, c, d, tx, ty = m
    return [[(a*x + c*y + tx, b*x + d*y + ty) for x, y in line] for line in paths]


def _raw_paths(value, w, h):
    if not isinstance(value, (list, tuple)) or not value or len(value) > 25000:
        raise ValueError("Некорректные контуры объекта.")
    copied, count = [], 0
    for path in value:
        if not isinstance(path, (list, tuple)) or not 2 <= len(path) <= 50000:
            raise ValueError("В контуре должно быть не меньше двух точек.")
        line = []
        for point in path:
            count += 1
            if count > 50000 or not isinstance(point, (list, tuple)) or len(point) != 2 or any(type(v) not in (int, float) or not math.isfinite(v) for v in point):
                raise ValueError("Некорректные точки объекта.")
            line.append(tuple(point))
        copied.append(line)
    validate_paths(copied, Sheet(w, h))
    return copied


def source_paths(source, w, h):
    if not isinstance(source, dict) or set(source) != {'kind', 'parameters'} or not isinstance(source['parameters'], dict):
        raise ValueError("Некорректный источник объекта.")
    kind, p = source['kind'], source['parameters']
    if not isinstance(kind, str):
        raise ValueError("Тип источника объекта должен быть строкой.")
    if kind == 'paths':
        if set(p) != {'paths'}:
            raise ValueError("Лишние поля контурного объекта.")
        return _raw_paths(p['paths'], w, h)
    from .planner import ACTION_KINDS
    if kind == 'object_scene' or kind not in ACTION_KINDS:
        raise ValueError("Неизвестный/вложенный источник объекта.")
    if kind == 'free_preview':
        from .free_preview import _validate_operation
        steps = p.get('operations')
        if not isinstance(steps, list) or not 1 <= len(steps) <= 8:
            raise ValueError("Неверная программа объекта.")
        for item in steps:
            _validate_operation(item)
    return transform_paths([], Plan(kind, p), w, h)


def checked_scene(scene, w, h):
    if any(type(v) not in (int,float) or not math.isfinite(v) for v in (w,h)):
        raise ValueError('Размеры листа должны быть конечными числами, не флагами.')
    Sheet(w, h)
    if not isinstance(scene, dict) or set(scene) != {'version', 'objects', 'route'} or type(scene['version']) is not int or scene['version'] != 1:
        raise ValueError("Неизвестный формат сцены.")
    if not isinstance(scene['objects'], list) or not 1 <= len(scene['objects']) <= 100:
        raise ValueError("В сцене должно быть 1–100 объектов.")
    identifiers, groups = set(), []
    for item in scene['objects']:
        if not isinstance(item, dict) or set(item) != {'id', 'name', 'source', 'matrix'}:
            raise ValueError("Неверные поля объекта.")
        identifier, name, m = item['id'], item['name'], item['matrix']
        if not isinstance(identifier, str) or not re.fullmatch(r'o[1-9][0-9]{0,5}', identifier) or identifier in identifiers:
            raise ValueError("Неуникальный/некорректный идентификатор объекта.")
        identifiers.add(identifier)
        if not isinstance(name, str) or not 1 <= len(name) <= 160:
            raise ValueError("Неверное имя объекта.")
        if not isinstance(m, (tuple, list)) or len(m) != 6 or any(type(v) not in (int, float) or not math.isfinite(v) or abs(v) > 1e6 for v in m):
            raise ValueError("Неверное преобразование объекта.")
        if abs(m[0]*m[3] - m[1]*m[2]) < 1e-12:
            raise ValueError("Объект нельзя схлопнуть в точку/линию.")
        paths = apply_matrix(source_paths(item['source'], w, h), m)
        validate_paths(paths, Sheet(w, h))
        groups.append(paths)
    all_paths = [path for group in groups for path in group]
    validate_paths(all_paths, Sheet(w, h))
    route = scene['route']
    if route is not None:
        expected = {(obj['id'], index) for obj, paths in zip(scene['objects'], groups) for index in range(len(paths))}
        if not isinstance(route, list) or len(route) != len(expected):
            raise ValueError("Маршрут должен содержать каждый контур ровно один раз.")
        actual = set()
        for entry in route:
            if not isinstance(entry, dict) or set(entry) != {'object', 'path', 'reverse'} or not isinstance(entry['object'], str) or type(entry['path']) is not int or type(entry['reverse']) is not bool:
                raise ValueError("Неверная запись маршрута.")
            key = entry['object'], entry['path']
            if key in actual or key not in expected:
                raise ValueError("Маршрут повторяет или теряет контур.")
            actual.add(key)
    return deepcopy(scene), groups


def render_scene(scene, w, h):
    scene, groups = checked_scene(scene, w, h)
    if scene['route'] is None:
        return [line for group in groups for line in group]
    by_id = {obj['id']: group for obj, group in zip(scene['objects'], groups)}
    return [list(reversed(by_id[e['object']][e['path']])) if e['reverse'] else by_id[e['object']][e['path']]
            for e in scene['route']]


def scene_plan(scene, explanation='Изменить объекты эскиза; станок не двигается.'):
    return Plan('object_scene', {'scene': deepcopy(scene)}, explanation)


def new_object(scene, source, name, matrix=None):
    identifier = 'o' + str(max((int(obj['id'][1:]) for obj in scene['objects']), default=0) + 1)
    scene['objects'].append({'id': identifier, 'name': str(name)[:160] or 'Объект',
                             'source': deepcopy(source), 'matrix': list(matrix or IDENTITY)})
    scene['route'] = None
    return identifier


def from_plan(plan, paths, w, h):
    """Split known generators faithfully; unknown SVG stays one honest contour object."""
    if isinstance(plan, Plan) and plan.kind == 'object_scene':
        scene = plan.parameters.get('scene')
        rendered = render_scene(scene,w,h)
        if len(rendered)==len(paths) and all(len(a)==len(b) and all(math.dist(x,y)<1e-6 for x,y in zip(a,b)) for a,b in zip(rendered,paths)):
            return deepcopy(scene)
        plan = None  # Never infer object ownership from stale geometry metadata.
    scene = {'version': 1, 'objects': [], 'route': None}
    if isinstance(plan, Plan):
        steps = plan.parameters.get('operations') if plan.kind == 'free_preview' else [{'kind': plan.kind, 'parameters': plan.parameters}]
        try:
            for item in steps:
                kind, p = item['kind'], deepcopy(item['parameters'])
                if kind in {'print_text', 'text', 'ruler', 'test_pattern', 'text_block', 'append_text', 'polyline', 'ellipse'}:
                    replacing = kind not in {'append_text', 'polyline', 'ellipse'} or p.get('mode') == 'replace'
                    if replacing:
                        scene['objects'] = []
                    if kind == 'append_text':
                        kind = 'print_text'
                    source = {'kind': kind, 'parameters': p}
                    if kind in {'polyline', 'ellipse'}:
                        p['mode'] = 'replace'
                        source = {'kind': 'free_preview', 'parameters': {'operations': [{'kind': kind, 'parameters': p}]}}
                    new_object(scene, source, p.get('text', kind))
                elif kind in GEOMETRY_KINDS:
                    scene = transform_scene(scene, Plan(kind, p), w, h)
                elif kind == 'frame':
                    before = render_scene(scene, w, h)
                    result = transform_paths(before, Plan(kind, p), w, h)
                    new_object(scene, {'kind': 'paths', 'parameters': {'paths': result[len(before):]}}, 'Рамка')
                else:
                    raise ValueError("Неизвестный генератор.")
            rendered = render_scene(scene, w, h)
            if len(rendered) == len(paths) and all(len(a) == len(b) and all(math.dist(x, y) < 1e-6 for x, y in zip(a, b)) for a, b in zip(rendered, paths)):
                return scene
        except (ValueError, TypeError, KeyError, OverflowError):
            pass
    scene = {'version': 1, 'objects': [], 'route': None}
    new_object(scene, {'kind': 'paths', 'parameters': {'paths': deepcopy(paths)}}, 'Импортированные контуры')
    checked_scene(scene, w, h)
    return scene


def affine_for(plan, paths, w, h):
    # The existing executor validates exact fields, limits and final bounds first.
    transform_paths(paths, plan, w, h)
    left, top, right, bottom = bounds(paths)
    cx, cy = (left + right)/2, (top + bottom)/2
    p, kind = plan.parameters, plan.kind
    sx = sy = 1.
    dx = dy = angle = 0.
    if kind == 'move_preview':
        dx, dy = p['dx_mm'], p['dy_mm']
    elif kind == 'center':
        dx, dy = w/2-cx, h/2-cy
    elif kind == 'rotate':
        angle = math.radians(p['degrees'])
    elif kind == 'scale':
        sx = sy = p['percent']/100
        anchor = p.get('anchor', 'center')
        if anchor != 'center':
            cx = left if anchor.endswith('left') else right
            cy = top if anchor.startswith('top') else bottom
    elif kind == 'mirror':
        sx, sy = (-1, 1) if p['axis'] == 'horizontal' else (1, -1)
    elif kind == 'resize_width':
        sx = sy = p['width_mm']/(right-left)
    elif kind == 'margin':
        margin = p['margin_mm']
        sx = sy = min((w-2*margin)/(right-left) if right>left else math.inf,
                      (h-2*margin)/(bottom-top) if bottom>top else math.inf)
        dx, dy = w/2-cx, h/2-cy
    elif kind == 'place_preview':
        anchor, margin = p['anchor'], p['margin_mm']
        tx = margin if anchor.endswith('left') else w-margin-(right-left) if anchor.endswith('right') else (w-(right-left))/2
        ty = margin if anchor.startswith('top') else h-margin-(bottom-top) if anchor.startswith('bottom') else (h-(bottom-top))/2
        dx, dy = tx-left, ty-top
    else:
        raise ValueError("Не геометрическое изменение объекта.")
    co, si = math.cos(angle), math.sin(angle)
    a, b, c, d = sx*co, sx*si, -sy*si, sy*co
    return [a, b, c, d, cx+dx-a*cx-c*cy, cy+dy-b*cx-d*cy]


def transform_scene(scene, plan, w, h, selected=None):
    result, groups = checked_scene(scene, w, h)
    targets = [i for i, obj in enumerate(result['objects']) if selected is None or obj['id'] == selected]
    if not targets:
        raise ValueError("Выбранного объекта больше нет.")
    paths = [path for i in targets for path in groups[i]]
    matrix = affine_for(plan, paths, w, h)
    for i in targets:
        result['objects'][i]['matrix'] = compose(matrix, result['objects'][i]['matrix'])
    result['route'] = None
    checked_scene(result, w, h)
    return result


def hit_test(scene, w, h, point, tolerance):
    _, groups = checked_scene(scene, w, h)
    best, chosen = tolerance, None
    for obj, group in reversed(list(zip(scene['objects'], groups))):
        for line in group:
            for a, b in zip(line, line[1:]):
                ax, ay, bx, by, px, py = *a, *b, *point
                length = (bx-ax)**2 + (by-ay)**2
                t = min(1, max(0, ((px-ax)*(bx-ax)+(py-ay)*(by-ay))/length)) if length else 0
                distance = math.hypot(px-ax-t*(bx-ax), py-ay-t*(by-ay))
                if distance < best:
                    best, chosen = distance, obj['id']
    return chosen
