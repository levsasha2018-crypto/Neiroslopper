"""Fixed local ASR worker; bounded memory-WAV input, JSON output, no microphone.

No model downloads at inference time. A separate process releases model RAM on
completion and permits cancelling inference without blocking or destroying Tk.
"""
from __future__ import annotations

import argparse
import io
import json
import math
import os
import sys
import time
import wave

from .voice import MAX_AUDIO_BYTES, MAX_SECONDS, MAX_TEXT, model_path, prepare_wav


def summarize_segments(segments, *, elapsed_s, extra_warnings=()):
    parts, warnings, total_speech = [], list(extra_warnings), 0.0
    for index, segment in enumerate(segments):
        if index >= 100:
            raise ValueError("Слишком длинный результат. Произнесите более короткую фразу.")
        text = segment.text
        if not isinstance(text, str) or not text.strip():
            continue
        metrics = (segment.avg_logprob, segment.no_speech_prob, segment.compression_ratio,
                   segment.start, segment.end)
        if any(isinstance(value, bool) or not isinstance(value, (int, float)) or
               not math.isfinite(value) for value in metrics):
            raise ValueError("Не получена проверяемая расшифровка.")
        if not (segment.avg_logprob <= 0 and 0 <= segment.no_speech_prob <= 1 and
                segment.compression_ratio >= 0 and
                0 <= segment.start <= segment.end <= MAX_SECONDS + 0.1):
            raise ValueError("Показатели расшифровки вне допустимого диапазона.")
        if segment.no_speech_prob > 0.8 or segment.avg_logprob < -1.5 or segment.compression_ratio > 3.0:
            raise ValueError("Речь распознана ненадёжно. Повторите фразу ближе к микрофону.")
        if segment.avg_logprob < -0.7 or segment.no_speech_prob > 0.35 or segment.compression_ratio > 2.4:
            warnings.append("Есть неуверенно распознанные слова: внимательно проверьте текст.")
        total_speech += max(0, segment.end - segment.start)
        parts.append(text)
    text = "".join(parts).strip()
    if total_speech < 0.2 or not text:
        raise ValueError("Речь не обнаружена. Проверьте выбранный микрофон.")
    if len(text) > MAX_TEXT:
        raise ValueError("Слишком длинный текст: запишите более короткую фразу.")
    return {"text": text, "warnings": list(dict.fromkeys(warnings)), "elapsed_s": elapsed_s}


def transcribe_wav(raw: bytes, profile: str):
    # Defence in depth: caller cannot use this worker as a file/URL decoder.
    if not isinstance(raw, bytes) or len(raw) > MAX_AUDIO_BYTES:
        raise ValueError("Слишком большая запись.")
    try:
        with wave.open(io.BytesIO(raw), "rb") as wav:
            if wav.getnchannels() != 1 or wav.getsampwidth() != 2 or wav.getcomptype() != "NONE":
                raise ValueError("Ожидается моно PCM16 WAV.")
            rate = wav.getframerate()
            frames = wav.getnframes()
            if frames > rate * (MAX_SECONDS + 0.1):
                raise ValueError("Запись длиннее 30 секунд.")
            pcm = wav.readframes(frames)
            if len(pcm) != frames * 2:
                raise ValueError("Запись оборвана.")
    except (wave.Error, EOFError) as exc:
        raise ValueError("Некорректная запись WAV.") from exc
    checked_wav, warnings = prepare_wav(pcm, rate)
    path = model_path(profile)
    os.environ["HF_HUB_OFFLINE"] = "1"
    os.environ["HF_HUB_DISABLE_TELEMETRY"] = "1"
    from faster_whisper import WhisperModel
    started = time.perf_counter()
    model = WhisperModel(str(path), device="cpu", compute_type="int8", cpu_threads=4,
                         num_workers=1, local_files_only=True)
    segments, info = model.transcribe(io.BytesIO(checked_wav), language="ru", task="transcribe",
        beam_size=5, temperature=0.0, condition_on_previous_text=False, vad_filter=True,
        vad_parameters={"min_silence_duration_ms": 500, "speech_pad_ms": 200},
        no_speech_threshold=0.6, log_prob_threshold=-1.0, compression_ratio_threshold=2.4,
        # No initial_prompt/hotword/example that might hallucinate a command.
        without_timestamps=True)
    segments = list(segments)
    if info.duration_after_vad < 0.2:
        raise ValueError("Речь не обнаружена. Запишите фразу в более тихом месте.")
    return summarize_segments(segments, elapsed_s=time.perf_counter() - started, extra_warnings=warnings)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--profile", choices=("balanced", "quality"), required=True)
    args = parser.parse_args()
    try:
        raw = sys.stdin.buffer.read(MAX_AUDIO_BYTES + 1)
        result = transcribe_wav(raw, args.profile)
        sys.stdout.buffer.write(json.dumps(result, ensure_ascii=False, allow_nan=False).encode("utf-8"))
        return 0
    except ValueError as exc:
        message = str(exc)[:500]
    except Exception:
        message = "Не удалось запустить локальную модель речи. Проверьте установку голосового ввода."
    sys.stdout.buffer.write(json.dumps({"error": message}, ensure_ascii=False).encode("utf-8"))
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
