"""Editor snapshots and a separate offline sandbox. No physical calibration here."""
from __future__ import annotations

from dataclasses import dataclass
import math
from pathlib import Path

from .planner import Plan, transform_paths
from .path_metrics import measure_paths


@dataclass(frozen=True)
class Snapshot:
    state: dict
    paths: tuple
    revision: tuple


def snapshot(state, paths, revision):
    copied = tuple(tuple((float(x), float(y)) for x, y in path) for path in paths)
    data = dict(state)
    data["has_paths"] = bool(copied)
    data["path_count"] = len(copied)
    data["point_count"] = sum(len(path) for path in copied)
    points = [point for path in copied for point in path]
    if points and all(math.isfinite(v) for point in points for v in point):
        data["drawing_width"] = max(x for x, _ in points) - min(x for x, _ in points)
        data["drawing_height"] = max(y for _, y in points) - min(y for _, y in points)
        data.update(measure_paths(copied))
    return Snapshot(data, copied, revision)


class Sandbox:
    """Local example paper that cannot be used as the plotter's zero or sheet."""
    def __init__(self):
        from geometry import Sheet, load_svg
        self.sheet = Sheet(210, 297)
        self.paths = load_svg(Path(__file__).resolve().parent.parent / "rosette_simple.svg", self.sheet, 10)
        self.name = "Пробный цветок"
        self.version = 0
        self.previous = None
        from editor_history import EditHistory
        self.history = EditHistory()

    def snapshot(self):
        return snapshot({"sandbox": True, "connected": False, "simulation": False,
                         "sheet_width": self.sheet.width, "sheet_height": self.sheet.height,
                         "file_name": self.name, "busy": False, "feed_mm_min": 3000},
                        self.paths, ("sandbox", self.version))

    def commit(self, before: Snapshot, plan: Plan):
        if before.revision != self.snapshot().revision:
            raise ValueError("Пробный эскиз изменился. Отправьте команду заново.")
        result = transform_paths(before.paths, plan, self.sheet.width, self.sheet.height)
        self.previous = (self.paths, self.name)
        self.history.push((self.paths, self.name, self.sheet))
        self.paths = result
        self.name = "Пробный эскиз · " + plan.explanation
        self.version += 1
        return self.snapshot()

    def undo(self):
        self.paths, self.name, self.sheet = self.history.undo((self.paths, self.name, self.sheet))
        self.previous = None
        self.version += 1
        return self.snapshot()

    def redo(self):
        self.paths, self.name, self.sheet = self.history.redo((self.paths, self.name, self.sheet))
        self.version += 1
        return self.snapshot()

    def load_project(self, project):
        self.history.push((self.paths, self.name, self.sheet))
        self.paths, self.name, self.sheet = project.paths, project.name, project.sheet
        self.version += 1
        return self.snapshot()
