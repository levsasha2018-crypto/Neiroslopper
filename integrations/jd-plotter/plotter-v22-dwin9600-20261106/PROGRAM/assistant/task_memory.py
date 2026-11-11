"""Typed, revision-scoped task journal. Never interpreted as motion authority."""
from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
import re

OUTCOMES = {'proposed', 'applied', 'rejected', 'needs_data', 'answered', 'cancelled'}


def clean(value):
    if not isinstance(value, dict) or set(value) - {'version', 'request', 'selected_object', 'awaiting_field', 'outcome', 'validation_error', 'events', 'not_motion_state'}:
        return {}
    if type(value.get('version')) is not int or value.get('version') != 1 or value.get('outcome') not in OUTCOMES:
        return {}
    result = {'version': 1, 'not_motion_state': True, 'outcome': value['outcome']}
    for key, limit in [('request', 500), ('selected_object', 100), ('awaiting_field', 80), ('validation_error', 300)]:
        item = value.get(key, '')
        if not isinstance(item, str):
            return {}
        result[key] = item[:limit]
    events = value.get('events', [])
    if not isinstance(events, list):
        return {}
    result['events'] = []
    for event in events[-3:]:
        if not isinstance(event, dict) or set(event) != {'outcome', 'request'} or event['outcome'] not in OUTCOMES or not isinstance(event['request'], str):
            return {}
        result['events'].append({'outcome': event['outcome'], 'request': event['request'][:160]})
    return result


@dataclass
class TaskMemory:
    scope: tuple | None = None
    data: dict = field(default_factory=dict)
    events: deque = field(default_factory=lambda: deque(maxlen=3))

    def reset(self, scope):
        self.scope = scope
        self.data.clear()
        self.events.clear()

    def note(self, scope, outcome, request='', *, selected='', awaiting='', error=''):
        if outcome not in OUTCOMES:
            raise ValueError('Неизвестный исход задачи.')
        if scope != self.scope:
            self.reset(scope)
        request = request if isinstance(request, str) else ''
        # An explanatory question must not overwrite the active editing goal.
        if outcome == 'answered' and self.data:
            self.events.append({'outcome': outcome, 'request': request[:160]})
            self.data['events'] = list(self.events)
            return
        request = request or self.data.get('request', '')
        self.events.append({'outcome': outcome, 'request': request[:160]})
        self.data = clean({'version': 1, 'outcome': outcome, 'request': request,
                           'selected_object': selected or '', 'awaiting_field': awaiting or '',
                           'validation_error': error or '', 'events': list(self.events)})

    def snapshot(self, scope, *, selected=None):
        if scope != self.scope:
            self.reset(scope)
        result = clean(self.data)
        if result and selected is not None:
            result['selected_object'] = str(selected)[:100]
        return result

    def applied(self, before_scope, after_scope):
        if self.scope != before_scope:
            self.reset(after_scope)
            return
        data = dict(self.data)
        self.scope = after_scope
        self.note(after_scope, 'applied', data.get('request', ''), selected=data.get('selected_object', ''))


def describe(prompt, memory):
    """Read-only questions about actual task outcomes need no generative model."""
    if not re.fullmatch(r'\s*(?:что (?:я (?:просил|просила)|мы (?:редактируем|делаем))|какая (?:сейчас )?задача|что (?:уже )?применено|почему (?:последняя )?правка отклонена)[?!. ]*', prompt, re.I):
        return None
    data = clean(memory)
    if not data:
        return 'Для текущей версии эскиза ещё нет запомненной задачи. Это не означает состояние станка.'
    labels = {'proposed': 'Подготовлено предложение, ещё не применено', 'applied': 'Применено только к эскизу',
              'rejected': 'Проверка отклонила предложение; изменений нет', 'needs_data': 'Ожидается уточнение',
              'answered': 'Дан ответ без нового предложения', 'cancelled': 'Запрос отменён'}
    parts = [labels[data['outcome']] + '.', 'Последняя просьба: «' + data['request'] + '».']
    if data['selected_object']:
        parts.append('Выбран объект ' + data['selected_object'] + '.')
    if data['awaiting_field']:
        parts.append('Ожидаемое поле: ' + data['awaiting_field'] + '.')
    if data['validation_error']:
        parts.append('Причина проверки: ' + data['validation_error'])
    parts.append('Это память редактора, не измерение и не подтверждение движения.')
    return '\n'.join(parts)


def editor_question(prompt, state):
    """Natural read-only caption/last-edit queries from verified editor metadata."""
    text = re.sub(r'\s+', ' ', prompt.casefold()).strip()
    if not re.match(r'(?:что|какая|какие|какой|почему)\b', text):
        return None
    caption_question = bool(re.search(r'надпис|текст|букв', text) and re.search(r'сейчас|текущ|предложен|эскиз|редактир',text))
    outcome_question = bool(re.search(r'что (?:произошло|случилось)|почему.*(?:отклон|не примен|не помещ)',text) and
                            re.search(r'последн|увелич|изменен|изменён|правк|попытк',text))
    if not caption_question and not outcome_question:
        return None
    from .chat_context import chat_snapshot
    snapshot = chat_snapshot(state)
    memory = clean(state.get('task_memory'))
    parts = []
    if caption_question:
        context = snapshot.get('current_text', {})
        captions = context.get('captions', [])
        selected = context.get('selected_object_id')
        if selected:
            captions = [item for item in captions if item.get('object_id') == selected]
        if not captions:
            parts.append('Для текущих контуров нет достоверных параметров надписи. Текст по линиям не угадываю.')
        for item in captions:
            p = item['parameters']
            parts.append(('Начало надписи' if p.get('text_truncated') else 'Надпись')+': «'+p['text']+'».'+
                         (f" Исходная высота заглавных в генераторе {p['height_mm']:g} мм." if 'height_mm' in p else ''))
        if context.get('source') == 'preview_draft':
            parts.append('Это ожидающее предложение, не подтверждённое применение.')
        if context.get('transforms') or context.get('object_transforms_not_reported'):
            parts.append('После преобразований фактический размер контуров может отличаться от исходного параметра букв.')
        if context.get('captions_truncated'):
            parts.append('Показаны только надписи краткого снимка, не вся композиция.')
    if outcome_question or memory and memory['outcome'] == 'rejected':
        if memory:
            labels = {'rejected':'Последняя правка отклонена; ничего не применено', 'proposed':'Последняя правка только предложена',
                      'applied':'Последняя правка применена только к эскизу', 'needs_data':'Для последней правки нужно уточнение',
                      'answered':'На последнюю просьбу дан текстовый ответ', 'cancelled':'Последний запрос отменён'}
            parts.append(labels[memory['outcome']]+'. Просьба: «'+memory['request']+'».')
            if memory['validation_error']:
                parts.append('Причина проверки: '+memory['validation_error'])
        else:
            parts.append('Для этой версии эскиза нет подтверждённого результата последней правки.')
    parts.append('Это сведения редактора, не измерение положения головки и не подтверждение движения.')
    return '\n'.join(parts)
