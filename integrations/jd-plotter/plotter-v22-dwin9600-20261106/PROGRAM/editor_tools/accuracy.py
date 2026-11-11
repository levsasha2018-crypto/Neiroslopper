"""Measurement wizard math and preview samples. Cannot modify GRBL settings."""
import math

from .drawing_tools import calibration_pattern_paths
from .planner import Plan
from .scene import new_object, scene_plan


def correction(target_x, target_y, measured_x, measured_y, current_x, current_y, swap=False):
    if type(swap) is not bool:
        raise ValueError('Перестановка осей должна быть true/false.')
    values = target_x, target_y, measured_x, measured_y, current_x, current_y
    if any(type(v) not in (int, float) or not math.isfinite(v) or v <= 0 for v in values):
        raise ValueError("Все измерения и текущие шаги/мм должны быть положительными конечными числами.")
    if any(v > 3000 for v in values[:4]) or any(v > 10000 for v in values[4:]):
        raise ValueError("Измерения до 3000 мм, коэффициент до 10000 шагов/мм.")
    # UI measurements are right/down; settings are physical GRBL X/Y.
    rx, ry = target_x/measured_x, target_y/measured_y
    ratios = (ry, rx) if swap else (rx, ry)
    proposed = current_x*ratios[0], current_y*ratios[1]
    if not all(0 < v <= 10000 for v in proposed):
        raise ValueError("Результат коррекции выходит за 0–10000 шагов/мм.")
    return {'grbl_x_steps_per_mm': proposed[0], 'grbl_y_steps_per_mm': proposed[1],
            'right_error_mm': measured_x-target_x, 'down_error_mm': measured_y-target_y,
            'swap': bool(swap), 'settings_were_changed': False}


def repeatability(measurements):
    if not isinstance(measurements, (list, tuple)) or not 3 <= len(measurements) <= 30:
        raise ValueError("Введите 3–30 измеренных отклонений возврата X,Y в мм.")
    if any(not isinstance(p, (list, tuple)) or len(p) != 2 or any(type(v) not in (int, float) or not math.isfinite(v) or abs(v)>100 for v in p) for p in measurements):
        raise ValueError("Отклонения возврата должны быть конечными, до ±100 мм.")
    xs, ys = zip(*measurements)
    return {'max_return_error_mm': max(math.hypot(x,y) for x,y in measurements),
            'range_right_mm': max(xs)-min(xs), 'range_down_mm': max(ys)-min(ys),
            'mean_right_mm': sum(xs)/len(xs), 'mean_down_mm': sum(ys)/len(ys),
            'sample_count': len(xs), 'sensor_measurement': False}


def accuracy_sample(size, w, h, repeats=5):
    if type(size) not in (int, float) or not math.isfinite(size) or not 5 <= size <= 200:
        raise ValueError("Сторона измерительного образца — 5–200 мм.")
    if type(repeats) is not int or not 1 <= repeats <= 10:
        raise ValueError("Повторов — 1–10.")
    if size+6 > min(w,h):
        raise ValueError("Для образца нужны размер стороны и поля хотя бы 3 мм.")
    x,y = (w-size)/2, (h-size)/2
    pattern = [[(a+x,b+y) for a,b in line] for line in calibration_pattern_paths(size)]
    # Repeated forward/back strokes start AND end at the same known point.
    start = (x, y)
    returns = [start]
    for _ in range(repeats):
        returns.extend([(x+size,y), start, (x,y+size), start])
    scene = {'version':1, 'objects':[], 'route':None}
    new_object(scene, {'kind':'paths', 'parameters':{'paths':pattern}}, f'Тест {size:g} мм')
    new_object(scene, {'kind':'paths', 'parameters':{'paths':[returns]}}, f'Возврат · {repeats} циклов')
    return scene_plan(scene, f"Измерительный образец {size:g} мм и {repeats} возвратов. Это эскиз, "
                      "не измерение и не изменение шагов/мм. Первый прогон без ручки; реальный запуск отдельно в пульте.")
