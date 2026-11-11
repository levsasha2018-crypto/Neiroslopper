"""Local sketch-only autosaves and declarative templates. Never restores machine state."""
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import queue
import threading

from editor_project import SketchProject, save_project, load_project, atomic_json, MAX_BYTES
from geometry import Sheet
from .scene import checked_scene, from_plan, scene_plan, render_scene


class Autosaves:
    MODES = {'Пробный лист':'sandbox', 'Эскиз плоттера':'editor'}

    def __init__(self, directory):
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True)
        self.pending = {}
        self.lock = threading.Lock()
        self.wake = threading.Event()
        self.stopping = False
        self.errors = queue.Queue(maxsize=10)
        self.worker = threading.Thread(target=self.run, daemon=True)
        self.worker.start()

    def path(self, mode, backup=False):
        if mode not in self.MODES:
            raise ValueError('Неизвестный макет автосохранения.')
        return self.directory / (self.MODES[mode] + ('.previous' if backup else '') + '.autosave.plotter.json')

    def schedule(self, mode, project):
        self.path(mode)
        with self.lock:
            if self.stopping:
                return
            self.pending[mode] = deepcopy(project)
        self.wake.set()

    def run(self):
        while True:
            self.wake.wait()
            with self.lock:
                if not self.pending:
                    self.wake.clear()
                    if self.stopping:
                        return
                    continue
                mode = next(iter(self.pending))
                project = self.pending.pop(mode)
            try:
                target = self.path(mode)
                if target.exists():
                    try:
                        old = load_project(target)
                        save_project(self.path(mode, True), old.sheet, old.paths, old.name, old.task, old.anchor)
                    except (ValueError, OSError, TypeError):
                        # Never overwrite a valid previous recovery with a corrupt latest file.
                        pass
                save_project(target, project.sheet, project.paths, project.name[:500], project.task, project.anchor)
            except (ValueError, OSError, TypeError) as exc:
                try:
                    self.errors.put_nowait(str(exc))
                except queue.Full:
                    pass

    def available(self):
        return [mode for mode in self.MODES if self.path(mode).exists() or self.path(mode, True).exists()]

    def restore(self, mode):
        errors = []
        for backup in (False, True):
            try:
                return load_project(self.path(mode, backup))
            except (ValueError, OSError, TypeError) as exc:
                errors.append(str(exc))
        raise ValueError('Нет исправной автокопии: ' + '; '.join(errors))

    def close(self):
        with self.lock:
            self.stopping = True
        self.wake.set()
        self.worker.join(timeout=2)


class Templates:
    def __init__(self, directory):
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True)

    def path(self, name):
        if not isinstance(name, str) or not 1 <= len(name.strip()) <= 100:
            raise ValueError('Имя шаблона: 1–100 знаков.')
        key = hashlib.sha256(name.strip().casefold().encode('utf-8')).hexdigest()[:24]
        return self.directory / ('template-' + key + '.json')

    def validate(self, data):
        required = {'format','version','name','sheet_mm','scene','recipient_object','recipient_format'}
        if not isinstance(data, dict) or set(data) != required or data['format'] != 'plotter-template' or type(data['version']) is not int or data['version'] != 1:
            raise ValueError('Неверный шаблон.')
        self.path(data['name'])
        dimensions = data['sheet_mm']
        if not isinstance(dimensions, list) or len(dimensions) != 2 or any(type(v) not in (float,int) for v in dimensions):
            raise ValueError('Неверный размер шаблона.')
        Sheet(*dimensions)
        checked_scene(data['scene'], *dimensions)
        identifier, format_text = data['recipient_object'], data['recipient_format']
        if identifier is not None and (not isinstance(identifier,str) or identifier not in {o['id'] for o in data['scene']['objects']}):
            raise ValueError('Неизвестный объект имени.')
        if not isinstance(format_text,str) or len(format_text) > 300 or format_text.count('{name}') != 1:
            raise ValueError('Формат имени должен содержать один буквальный {name}.')
        return deepcopy(data)

    def save(self, name, sheet, scene, recipient_object=None, recipient_format='{name}'):
        data = self.validate({'format':'plotter-template','version':1,'name':name.strip(),
            'sheet_mm':[sheet.width,sheet.height], 'scene':scene, 'recipient_object':recipient_object,
            'recipient_format':recipient_format})
        if not self.path(name).exists() and len(list(self.directory.glob('template-*.json'))) >= 100:
            raise ValueError('Лимит библиотеки: 100 шаблонов.')
        atomic_json(self.path(name), data)

    def load(self, name):
        path = self.path(name)
        if path.stat().st_size > MAX_BYTES:
            raise ValueError('Шаблон больше 5 МБ.')
        try:
            return self.validate(json.loads(path.read_text(encoding='utf-8')))
        except (UnicodeError, json.JSONDecodeError, RecursionError) as exc:
            raise ValueError('Повреждённый шаблон.') from exc

    def names(self):
        names = []
        for path in list(self.directory.glob('template-*.json'))[:100]:
            try:
                if path.stat().st_size <= MAX_BYTES:
                    data = self.validate(json.loads(path.read_text(encoding='utf-8')))
                    if self.path(data['name']).name == path.name:
                        names.append(data['name'])
            except (ValueError, OSError, TypeError, KeyError, RecursionError):
                continue
        return sorted(names, key=str.casefold)
