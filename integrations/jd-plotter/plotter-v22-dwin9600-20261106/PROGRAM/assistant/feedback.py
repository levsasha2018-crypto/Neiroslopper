"""Explicitly opted-in local review queue, NOT automatic model training."""
from datetime import datetime, timezone
import json
from pathlib import Path
import threading
import uuid

ROOT = Path(__file__).resolve().parent
_LOCK = threading.Lock()


def save_feedback(prompt: str, answer: str, source: str, model_version: str,
                  rating: str, correction: str = "", *, consent: bool = False,
                  path: Path | None = None, context=None):
    if not consent:
        raise ValueError("Нужно согласие на локальное сохранение оценки.")
    if rating not in {"good", "bad"}:
        raise ValueError("Неизвестная оценка.")
    if not isinstance(prompt, str) or not 1 <= len(prompt.strip()) <= 1800:
        raise ValueError("Неизвестный или слишком длинный запрос.")
    if not isinstance(answer, str) or len(answer) > 20000:
        raise ValueError("Слишком длинный ответ.")
    if not isinstance(correction, str) or len(correction) > 1800:
        raise ValueError("Сократите исправление до 1800 знаков.")
    from .chat_context import chat_snapshot
    record = {"id": uuid.uuid4().hex, "captured_utc": datetime.now(timezone.utc).isoformat(),
              "review_status": "unreviewed", "prompt": prompt, "answer": answer,
              "source": str(source)[:200], "intent_model_version": str(model_version)[:100],
              "rating": rating, "human_correction": correction,
              "eligible_for_training": False, 'editor_context': chat_snapshot(context or {})}
    target = Path(path) if path is not None else ROOT / "review_queue" / "feedback.jsonl"
    with _LOCK:
        target.parent.mkdir(parents=True, exist_ok=True)
        with target.open("a", encoding="utf-8", newline="\n") as stream:
            stream.write(json.dumps(record, ensure_ascii=False) + "\n")
    return record
