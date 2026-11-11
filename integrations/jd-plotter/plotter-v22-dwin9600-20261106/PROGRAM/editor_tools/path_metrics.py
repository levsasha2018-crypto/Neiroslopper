"""Preview-only path measurements. No live head position or physical timing."""
import math


def measure_paths(paths):
    drawing = connections = 0.0
    count = 0
    last = None
    for path in paths:
        if len(path) < 2:
            continue
        if not all(math.isfinite(value) for point in path for value in point):
            raise ValueError("Неконечные координаты траектории.")
        if last is not None and last != path[0]:
            connections += math.dist(last, path[0])
            count += 1
        drawing += sum(math.dist(a, b) for a, b in zip(path, path[1:]))
        last = path[-1]
    return {"contour_length_mm": drawing, "connection_length_mm": connections,
            "travel_length_mm": drawing + connections, "connection_count": count}


def time_lower_bound(length_mm, feed_mm_min):
    if (isinstance(length_mm, bool) or isinstance(feed_mm_min, bool) or
            not isinstance(length_mm, (int, float)) or not isinstance(feed_mm_min, (int, float)) or
            not math.isfinite(length_mm) or not math.isfinite(feed_mm_min) or length_mm < 0 or feed_mm_min <= 0):
        raise ValueError("Для оценки нужны конечная длина и положительная скорость.")
    seconds = length_mm / feed_mm_min * 60
    if not math.isfinite(seconds):
        raise ValueError("Оценка времени выходит за допустимый числовой диапазон.")
    return seconds
