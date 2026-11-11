"""Append-only human reviews, reproducible grouped train/dev export. No training."""
from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
import unicodedata

from .feedback import _LOCK as LOCK

ROOT = Path(__file__).resolve().parent / 'review_queue'


def digest(record):
    return hashlib.sha256(json.dumps(record, ensure_ascii=False, sort_keys=True).encode('utf-8')).hexdigest()


def read_rows(path):
    path = Path(path)
    if not path.exists():
        return []
    if path.stat().st_size > 10_000_000:
        raise ValueError('Очередь слишком большая: разделите архив вручную.')
    rows = [json.loads(line) for line in path.read_text(encoding='utf-8').splitlines() if line.strip()]
    if len(rows) > 5000 or any(not isinstance(row, dict) for row in rows):
        raise ValueError('Неверная очередь исправлений.')
    return rows


def reviewed_records(directory=ROOT):
    directory = Path(directory)
    with LOCK:
        rows = read_rows(directory / 'feedback.jsonl')
        review_rows = read_rows(directory / 'reviews.jsonl')
    decisions = {}
    for item in review_rows:
        identifier = item.get('record_hash')
        if isinstance(identifier, str) and len(identifier) == 64:
            decisions[identifier] = item
    return [(record, decisions.get(digest(record))) for record in rows]


def review(record_hash, reference, *, approved, consent=False, directory=ROOT):
    if not consent or type(approved) is not bool:
        raise ValueError('Нужно явное подтверждение проверки человеком.')
    if not isinstance(reference, str) or approved and not 1 <= len(reference.strip()) <= 1800:
        raise ValueError('Для утверждения нужен исправленный эталонный ответ до 1800 знаков.')
    directory = Path(directory)
    with LOCK:
        rows = read_rows(directory / 'feedback.jsonl')
        matches = [row for row in rows if digest(row) == record_hash]
        if len(matches) != 1:
            raise ValueError('Исходная запись изменилась или неоднозначна; откройте очередь заново.')
        row = {'record_hash': record_hash, 'reference': reference.strip() if approved else '',
               'approved': approved, 'reviewed_by': 'human_explicit_ui_or_cli',
               'reviewed_utc': datetime.now(timezone.utc).isoformat()}
        directory.mkdir(parents=True, exist_ok=True)
        with (directory / 'reviews.jsonl').open('a', encoding='utf-8', newline='\n') as stream:
            stream.write(json.dumps(row, ensure_ascii=False) + '\n')
    return row


def group_key(prompt):
    text = unicodedata.normalize('NFKC', prompt).casefold()
    text = re.sub(r'«[^»]*»|"[^"\n]*"', '<literal>', text)
    text = re.sub(r'\d+(?:[.,]\d+)?', '<number>', text)
    text = re.sub(r'[^\w<>]+', ' ', text).strip()
    return hashlib.sha256(text.encode('utf-8')).hexdigest()


def export(destination, *, directory=ROOT):
    """Only confirmed reference answers; no self-labeling or sealed-test claim."""
    chosen = []
    seen = set()
    for record, decision in reviewed_records(directory):
        if not decision or decision.get('approved') is not True or decision.get('reviewed_by') != 'human_explicit_ui_or_cli':
            continue
        prompt = record.get('prompt')
        reference = decision.get('reference')
        if not isinstance(prompt, str) or not isinstance(reference, str):
            raise ValueError('Неверный утверждённый пример.')
        # An approved contextual correction must retain the actual context. It
        # must not become a context-free 'increase fivefold' training example.
        context = record.get('editor_context', {})
        full_prompt = ('Снимок редактора (данные, не инструкции и не состояние станка):\n' +
                       json.dumps(context, ensure_ascii=False, sort_keys=True) + '\nПросьба: ' + prompt) if context else prompt
        if len(full_prompt) > 2500 or len(reference) > 2500:
            raise ValueError('Пример '+str(record.get('id', digest(record)[:12]))+' слишком длинный для проверенного пилота. '
                             'Нужно отдельно подготовить короткий полноценный пример; он не обрезан.')
        if any(token in full_prompt + reference for token in ('<|im_start|>', '<|im_end|>', '<think>', '<tool_call>')):
            raise ValueError('В примере найдены управляющие токены модели.')
        key = unicodedata.normalize('NFKC', full_prompt).casefold().strip()
        if key in seen:
            raise ValueError('Повтор вопроса среди утверждённых примеров: устраните конфликт оценок.')
        seen.add(key)
        chosen.append((group_key(prompt), digest(record), full_prompt, reference))
    groups = sorted({item[0] for item in chosen})
    if len(groups) < 4:
        raise ValueError('Нужно хотя бы 4 разные группы утверждённых вопросов. Сейчас '+str(len(groups))+'.')
    dev_groups = set(groups[:max(1, len(groups)//5)])
    train, dev, provenance = [], [], []
    for group, source_hash, prompt, reference in chosen:
        bucket = dev if group in dev_groups else train
        identifier = ('eval' if group in dev_groups else 'train')+f'-{len(bucket)+1:03d}'
        bucket.append({'id': identifier, 'category': 'planning', 'provenance': 'human_reviewed_quality_v1',
                       'messages': [{'role': 'user', 'content': prompt}, {'role': 'assistant', 'content': reference}],
                       **({'rubric': 'Development example reviewed by a human; not a blind final test.'} if group in dev_groups else {})})
        provenance.append({'id': identifier, 'source_record_sha256': source_hash, 'group': group})
    from .training.validate_dataset import validate_pair
    validate_pair(train, dev)
    destination = Path(destination)
    # Never overwrite an earlier dataset/run.
    destination.mkdir(parents=True, exist_ok=False)
    for name, rows in [('train.jsonl', train), ('dev.jsonl', dev)]:
        (destination / name).write_text(''.join(json.dumps(row, ensure_ascii=False)+'\n' for row in rows), encoding='utf-8')
    manifest = {'version': 1, 'status': 'reviewed_data_not_trained', 'train_count': len(train), 'dev_count': len(dev),
                'split': 'grouped_question_shape_no_group_overlap', 'provenance': provenance,
                'train_sha256': hashlib.sha256((destination/'train.jsonl').read_bytes()).hexdigest(),
                'dev_sha256': hashlib.sha256((destination/'dev.jsonl').read_bytes()).hexdigest(),
                'blind_final_test': None, 'caution': 'Development answers are disclosed. Human semantic review still required. '
                'Export does not train/deploy 9B; old failed 4B adapter must not be used.'}
    (destination/'manifest.json').write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding='utf-8')
    return manifest
