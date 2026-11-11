"""Bounded nearest-endpoint ordering. Preserves every original stroke exactly."""
import math
import time

from .scene import checked_scene, render_scene
from .reference_images import check_cancel


class RouteOptimizationTimeout(ValueError):
    pass


def connection_length(paths):
    return sum(math.dist(a[-1], b[0]) for a, b in zip(paths, paths[1:]))


def _check_work(cancel, deadline):
    check_cancel(cancel)
    if deadline is not None and time.perf_counter() > deadline:
        raise RouteOptimizationTimeout('Оптимизация слишком сложна для быстрого предпросмотра. Исходный маршрут сохранён; упростите SVG.')


def _tree(records, depth=0, *, cancel=None, deadline=None):
    _check_work(cancel, deadline)
    if not records:
        return None
    axis = depth % 2
    records.sort(key=lambda r: (r[0][axis], r[1], r[2]))
    middle = len(records)//2
    return (records[middle], axis,
            _tree(records[:middle], depth+1, cancel=cancel, deadline=deadline),
            _tree(records[middle+1:], depth+1, cancel=cancel, deadline=deadline))


def _nearest(node, point, remaining, best=None, *, cancel=None, deadline=None):
    _check_work(cancel, deadline)
    if node is None:
        return best
    record, axis, left, right = node
    distance = (record[0][0]-point[0])**2 + (record[0][1]-point[1])**2
    if record[1] in remaining:
        candidate = (distance, record[1], record[2], record)
        if best is None or candidate[:3] < best[:3]:
            best = candidate
    delta = point[axis] - record[0][axis]
    near, far = (left, right) if delta < 0 else (right, left)
    best = _nearest(near, point, remaining, best, cancel=cancel, deadline=deadline)
    if best is None or delta*delta <= best[0]:
        best = _nearest(far, point, remaining, best, cancel=cancel, deadline=deadline)
    return best


def optimize_scene(scene, w, h, cancel=None):
    check_cancel(cancel)
    deadline = time.perf_counter()+8
    checked, groups = checked_scene(scene, w, h)
    paths, keys = [], []
    for obj, group in zip(checked['objects'], groups):
        _check_work(cancel, deadline)
        for index, path in enumerate(group):
            paths.append(path)
            keys.append((obj['id'], index))
    baseline = render_scene(checked, w, h)
    _check_work(cancel, deadline)
    if connection_length(baseline)<=1e-8:
        return checked,0.,0.
    # Keep the original first contour AND its direction: the unknown initial
    # head-to-drawing move cannot be advertised as a measured improvement.
    first_key = (checked['route'][0]['object'], checked['route'][0]['path']) if checked['route'] else keys[0]
    first = keys.index(first_key)
    first_reverse = checked['route'][0]['reverse'] if checked['route'] else False
    remaining = set(range(len(paths))) - {first}
    entries = [{'object': keys[first][0], 'path': keys[first][1], 'reverse': first_reverse}]
    chosen_paths = [list(reversed(paths[first])) if first_reverse else paths[first]]
    records = [(path[side], index, side == -1) for index, path in enumerate(paths) for side in (0, -1) if index in remaining]
    tree, since_rebuild = _tree(records, cancel=cancel, deadline=deadline), 0
    while remaining:
        _check_work(cancel, deadline)
        if since_rebuild >= max(128, len(remaining)//4):
            tree = _tree([r for r in records if r[1] in remaining], cancel=cancel, deadline=deadline)
            since_rebuild = 0
        best = _nearest(tree, chosen_paths[-1][-1], remaining, cancel=cancel, deadline=deadline)
        if best is None:
            raise ValueError("Не удалось подобрать следующий контур.")
        _, index, reverse, _ = best
        remaining.remove(index)
        entries.append({'object': keys[index][0], 'path': keys[index][1], 'reverse': reverse})
        chosen_paths.append(list(reversed(paths[index])) if reverse else paths[index])
        since_rebuild += 1
    before, after = connection_length(baseline), connection_length(chosen_paths)
    _check_work(cancel, deadline)
    if after <= before + 1e-8:
        checked['route'] = entries
    else:
        after = before  # Nearest-neighbour is heuristic; never replace with a worse route.
    return checked, before, after
