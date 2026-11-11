"""Select documented project paragraphs, not model-generated 'facts'."""
from __future__ import annotations

from pathlib import Path
import re

ROOT = Path(__file__).resolve().parent
CORE = ('Это справка', 'Поддерживаются две', 'Arduino Uno', 'Концевиков нет', 'Нет концевиков', 'Лист прямоугольный', 'Скорость 100%', 'Без концевиков')
DOMAINS = {
    'text': r'текст|надпис|букв|шрифт|слов|почерк|печат|строк|перенос',
    'editor': r'эскиз|редактор|объект|выбр|выбор|сохран|проект|отмен|повтор|истори|вариант|шаблон|маршрут|оформлен',
    'camera': r'камер|скан|фото|изображени|dpi|esp|ov5640|тени|лист',
    'calibration': r'точност|калибр|размер|шаг|угл|портал|каретк|скорост|двигател|мотор|удержан',
    'assistant': r'помощник|нейросет|codex|кодекс|модел|рассужден|вдумчив|качеств|памят|контекст|оценк',
    'display': r'dwin|диспле|шлейф|type.?c',
    'pen': r'соленоид|электромагнит|подъем|подъём|реле|coolen|hcne|переход|M8|M9',
}


def retrieve(prompt='', *, limit=5):
    paragraphs = [p.strip() for p in (ROOT / 'knowledge.md').read_text(encoding='utf-8').split('\n\n')
                  if p.strip() and not p.startswith('#')]
    core, rest = [], []
    topics = {name for name, pattern in DOMAINS.items() if re.search(pattern, prompt, re.I)}
    stems = {word[:6] for word in re.findall(r'\w{4,}', prompt.casefold())}
    for index, paragraph in enumerate(paragraphs):
        if paragraph.startswith(CORE):
            core.append(paragraph)
            continue
        # Shared broad words ("эскиз") must not outrank the actual requested
        # feature. Literal feature stems give the newer detailed docs priority.
        score = sum(1 for name in topics if re.search(DOMAINS[name], paragraph, re.I))
        score += 3 * sum(stem in paragraph.casefold() for stem in stems)
        if score:
            rest.append((-score, index, paragraph))
    chosen = sorted(rest)[:limit]
    return '\n\n'.join(core + [p for _, _, p in chosen])
