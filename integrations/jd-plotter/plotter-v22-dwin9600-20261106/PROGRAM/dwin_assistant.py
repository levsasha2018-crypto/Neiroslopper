"""Read-only DWIN requests, with local dictation and a bounded sheet preview."""
from collections import deque
from copy import deepcopy
from dataclasses import dataclass
import queue
import re
import threading
from time import monotonic
from types import MappingProxyType
from typing import Mapping
import unicodedata

from assistant.runtime import Assistant
from assistant.voice import MAX_TEXT, VoiceService
from geometry import Sheet, validate_paths


SENDING, WAITING, RECORDING, TRANSCRIBING, DONE, ERROR = range(1, 7)
MAX_ANSWER = 112
MAX_HISTORY = 8
MAX_HISTORY_PROMPT = 600
WAITING_REFRESH_S = 30


def panel_text(text):
    """Flatten chat markup and fit the display without splitting a word."""
    text = re.sub(r'\[([^\]\n]+)\]\([^\s)]+\)', r'\1', text)
    text = re.sub(r'(?m)^\s*(?:#{1,6}|[-*>]) +', '', text).replace('**', '').replace('`', '')
    text = ' '.join(''.join(char for char in text if not unicodedata.category(char).startswith('C')
                            or char.isspace()).split())
    if len(text) <= MAX_ANSWER:
        return text
    prefix = text[:MAX_ANSWER - 1]
    if text[MAX_ANSWER - 1] != ' ' and ' ' in prefix:
        prefix = prefix.rsplit(' ', 1)[0]
    return prefix.rstrip(' ,;:') + '…'


@dataclass
class _Request:
    request_id: int
    state: Mapping
    cancel: threading.Event
    prompt: str = ''
    transcript: str = ''
    voice_error: str = ''
    waiting_at: float | None = None


class PanelAssistant:
    """One panel request at a time. Call methods and receive events on the GUI thread."""

    def __init__(self, publish, *, assistant=None, voice_factory=VoiceService):
        self._publish = publish
        self._assistant = Assistant() if assistant is None else assistant
        self._answers = queue.SimpleQueue()
        self._voice_events = queue.Queue()
        self._voice = voice_factory(self._voice_events)
        self._history = deque(maxlen=MAX_HISTORY)
        self._active = self._voice_request = None
        self._closed = False

    @property
    def busy(self):
        return self._active is not None or self._voice_request is not None

    def _begin(self, request_id, snapshot):
        if self._closed or self.busy:
            return None
        # Editor snapshots have only plain data. Do not pass geometry, paths,
        # callable handles or a desktop chat's unpublished proposals to Codex.
        state = deepcopy(dict(snapshot.state))
        state.update(chat_history=tuple(self._history), panel_reply=True)
        request = _Request(request_id, MappingProxyType(state), threading.Event())
        self._active = request
        self._publish(request_id, SENDING, '')
        return request

    def start(self, request_id, prompt, snapshot):
        request = self._begin(request_id, snapshot)
        if request is None:
            return False
        self._answer(request, prompt)
        return True

    def start_voice(self, request_id, snapshot, device=None):
        request = self._begin(request_id, snapshot)
        if request is None:
            return False
        try:
            self._voice.start(device=device)
        except (OSError, ValueError, RuntimeError) as exc:
            self._finish(request, ERROR, str(exc))
        else:
            self._voice_request = request
        return True

    def stop_voice(self, request_id):
        request = self._voice_request
        if request is None or request is not self._active or request.request_id != request_id:
            return False
        self._voice.stop()
        return True

    def _answer(self, request, prompt):
        if not isinstance(prompt, str) or not prompt.strip() or len(prompt) > MAX_TEXT:
            self._finish(request, ERROR, 'Введите короткий вопрос.')
            return
        prompt = prompt.strip()
        request.prompt = prompt
        request.waiting_at = monotonic()
        self._publish(request.request_id, WAITING, '')
        assistant, events = self._assistant, self._answers

        def work():
            try:
                reply = assistant.answer(prompt, request.state, use_llm=True,
                                         allow_creative=False, cancel=request.cancel)
                if reply.plan is not None or reply.variants:
                    stage, text = ERROR, 'Откройте помощника на ПК, чтобы подготовить изменение эскиза.'
                elif 'ответ не получен' in reply.source.casefold():
                    stage, text = ERROR, reply.text
                else:
                    stage, text = DONE, reply.text
            except Exception:
                stage, text = ERROR, 'Не удалось обработать вопрос на ПК. Повторите запрос.'
            if not request.cancel.is_set():
                events.put((request, stage, text))

        try:
            threading.Thread(target=work, name='DWIN-assistant', daemon=True).start()
        except RuntimeError:
            self._finish(request, ERROR, 'Не удалось запустить помощника на ПК.')

    def _finish(self, request, stage, text):
        if self._active is request and not request.cancel.is_set():
            self._active = None
            visible = panel_text(text)
            if stage == DONE and visible:
                prompt = request.prompt
                if len(prompt) > MAX_HISTORY_PROMPT:
                    prompt = prompt[:MAX_HISTORY_PROMPT - 1] + '…'
                self._history.append((prompt, visible))
            self._publish(request.request_id, stage, visible or 'Ответ не получен.')

    def pump(self):
        for _ in range(100):
            try:
                kind, value = self._voice_events.get_nowait()
            except queue.Empty:
                break
            request = self._voice_request
            if request is None:
                continue
            if kind == 'finished':
                self._voice_request = None
                if request is self._active:
                    if request.transcript:
                        self._answer(request, request.transcript)
                    else:
                        self._finish(request, ERROR, request.voice_error or 'Речь не обнаружена.')
            elif request is self._active:
                if kind == 'result':
                    if value.warnings:
                        request.voice_error = 'Фраза распознана неуверенно. Повторите её ближе к микрофону.'
                    else:
                        request.transcript = value.text
                elif kind == 'error':
                    request.voice_error = value
                elif kind == 'state':
                    stage = TRANSCRIBING if value.startswith('Распознаю') else RECORDING
                    self._publish(request.request_id, stage, '')
        while True:
            try:
                self._finish(*self._answers.get_nowait())
            except queue.Empty:
                break
        request = self._active
        if request is not None and request.waiting_at is not None:
            now = monotonic()
            if now - request.waiting_at >= WAITING_REFRESH_S:
                # The panel uses assistant updates to distinguish long work from a lost PC.
                request.waiting_at = now
                self._publish(request.request_id, WAITING, '')

    def cancel(self, request_id=None):
        request = self._active
        if request is None or (request_id is not None and request.request_id != request_id):
            return False
        request.cancel.set()
        self._active = None
        if self._voice_request is request:
            self._voice.cancel()
        return True

    def close(self):
        self._closed = True
        self.cancel()
        self._history.clear()
        self._voice.close()


def _pixel_paths(paths, pixel, join, start_position):
    """Keep corners and reversals while merging straight runs in pixel space."""
    current = pixel(start_position) if join and start_position is not None else None
    for path in paths:
        points = [current] if current is not None else []
        for point in path:
            end = pixel(point)
            if points and points[-1] == end:
                continue
            if len(points) >= 2:
                a, b = points[-2:]
                dx, dy = b[0] - a[0], b[1] - a[1]
                ex, ey = end[0] - b[0], end[1] - b[1]
                if dx * ey == dy * ex and dx * ex + dy * ey > 0:
                    points[-1] = end
                    continue
            points.append(end)
        if join and points:
            current = points[-1]
        yield points


def _coverage(segments, limit, width, height):
    if len(segments) <= limit:
        return tuple(segments)
    buckets = {}
    for index, (x1, y1, x2, y2) in enumerate(segments):
        cell = ((x1 + x2) * 8 // (2 * width), (y1 + y2) * 6 // (2 * height))
        length2 = (x2 - x1) ** 2 + (y2 - y1) ** 2
        buckets.setdefault(cell, []).append((-length2, index))
    # Select the longest line per area first, so one dense object cannot erase
    # the rest of the sheet. Surviving segments retain actual drawing order.
    cells = deque(deque(sorted(bucket)) for bucket in sorted(buckets.values(), key=min))
    chosen = []
    while len(chosen) < limit:
        cell = cells.popleft()
        chosen.append(cell.popleft()[1])
        if cell:
            cells.append(cell)
    return tuple(segments[index] for index in sorted(chosen))


def sheet_segments(snapshot, *, width_px=420, height_px=320, max_segments=160, start_position=None):
    """Fit the full sheet and its ink into a display-local viewport."""
    if not all(type(value) is int and value > 0 for value in (width_px, height_px, max_segments)):
        raise ValueError('Некорректный размер предпросмотра.')
    if min(width_px, height_px) < 12:
        raise ValueError('Слишком маленькая область предпросмотра.')
    try:
        sheet = Sheet(snapshot.state.get('sheet_width'), snapshot.state.get('sheet_height'))
    except ValueError:
        raise ValueError('Задайте размер листа на ПК или отметьте оба угла.') from None
    scale = min((width_px - 9) / sheet.width, (height_px - 9) / sheet.height)
    paper_width = max(1, round(sheet.width * scale))
    paper_height = max(1, round(sheet.height * scale))
    left, top = (width_px - paper_width - 1) // 2, (height_px - paper_height - 1) // 2
    bbox = left, top, left + paper_width, top + paper_height

    def pixel(point):
        return (left + min(paper_width, max(0, round(point[0] * paper_width / sheet.width))),
                top + min(paper_height, max(0, round(point[1] * paper_height / sheet.height))))

    paths = snapshot.paths
    if not paths:
        return bbox, ()
    validate_paths(paths, sheet)
    join = snapshot.state.get('pen_lift_enabled') is not True
    start = start_position if start_position is not None and sheet.contains(start_position) else None
    segments = [(*a, *b) for path in _pixel_paths(paths, pixel, join, start)
                for a, b in zip(path, path[1:])]
    return bbox, _coverage(segments, max_segments, width_px, height_px)
