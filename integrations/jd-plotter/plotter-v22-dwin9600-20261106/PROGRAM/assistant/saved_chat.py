"""Local selection of the Codex chat that receives conversational questions."""
import re
from pathlib import Path


FILE_NAME = 'codex-chat.txt'
_THREAD_ID = r'[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}'


def thread_id(value):
    match = re.fullmatch(r'(?:codex://threads/)?(' + _THREAD_ID + r')/?', value.strip(), re.I)
    if not match:
        raise ValueError('Вставьте ID локального чата или ссылку codex://threads/… . '
                         'Ссылка «Поделиться» chatgpt.com/s/… открывает опубликованную копию.')
    return match[1].lower()


def load(data_dir):
    if data_dir is None:
        return None
    path = Path(data_dir) / FILE_NAME
    try:
        value = path.read_text(encoding='utf-8').strip()
    except FileNotFoundError:
        return None
    return thread_id(value) if value else None


def save(data_dir, value):
    selected = thread_id(value) if value.strip() else ''
    directory = Path(data_dir)
    directory.mkdir(parents=True, exist_ok=True)
    target = directory / FILE_NAME
    pending = target.with_suffix('.tmp')
    pending.write_text(selected + '\n', encoding='utf-8')
    pending.replace(target)
