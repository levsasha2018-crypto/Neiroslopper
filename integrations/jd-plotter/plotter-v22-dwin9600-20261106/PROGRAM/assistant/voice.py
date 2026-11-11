"""Explicit local dictation. No assistant dispatch, serial, Tk or cloud calls.

Microphone audio lives in memory, then goes to a cancellable local process.
An event with text is only a draft: neither this module nor its worker sends it.
"""
from __future__ import annotations

from array import array
from dataclasses import dataclass
import importlib.util
import io
import json
import math
import os
from pathlib import Path
import queue
import subprocess
import sys
import threading
import time
import wave

ROOT = Path(__file__).resolve().parent
MODEL_ROOT = Path(os.environ.get('PLOTTER_VOICE_MODEL_ROOT') or ROOT / 'voice_models').expanduser()
PROFILES = {"balanced": "small", "quality": "medium"}
MODEL_SPECS = {
    "small": {"repo": "Systran/faster-whisper-small", "revision": "536b0662742c02347bc0e980a01041f333bce120",
              "sha256": "3e305921506d8872816023e4c273e75d2419fb89b24da97b4fe7bce14170d671", "size": 483546902},
    "medium": {"repo": "Systran/faster-whisper-medium", "revision": "08e178d48790749d25932bbc082711ddcfdfbc4f",
               "sha256": "9b45e1009dcc4ab601eff815b61d80e60ce3fd8c74c1a14f4a282258286b51ae", "size": 1527906378},
}
MAX_SECONDS = 30
MAX_AUDIO_BYTES = 12_000_000
MAX_RESPONSE_BYTES = 65_536
MAX_TEXT = 1800
WORKER_TIMEOUT = 120
_MICROPHONE_LOCK = threading.Lock()


@dataclass(frozen=True)
class InputDevice:
    index: int
    name: str
    is_default: bool = False


@dataclass(frozen=True)
class Transcript:
    text: str
    warnings: tuple[str, ...] = ()
    elapsed_s: float = 0.0


def model_path(profile: str) -> Path:
    if profile not in PROFILES:
        raise ValueError("Неизвестный режим распознавания.")
    path = MODEL_ROOT / PROFILES[profile]
    required = ("model.bin", "config.json", "tokenizer.json")
    if not all((path / name).is_file() for name in required):
        raise ValueError("Модель голоса не установлена. Запустите INSTALL_VOICE.cmd.")
    try:
        ready = json.loads((path / "installed.json").read_text(encoding="utf-8"))
        spec = MODEL_SPECS[PROFILES[profile]]
        if ready != spec or (path / "model.bin").stat().st_size != spec["size"]:
            raise ValueError("Модель голоса не проверена. Запустите INSTALL_VOICE.cmd.")
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError("Модель голоса не проверена. Запустите INSTALL_VOICE.cmd.") from exc
    return path


def pcm_stats(data: bytes) -> dict:
    if not isinstance(data, bytes) or not data or len(data) % 2:
        raise ValueError("Нет целой записи PCM16.")
    samples = array("h")
    samples.frombytes(data)
    if sys.byteorder != "little":
        samples.byteswap()
    count = len(samples)
    return {"rms": math.sqrt(sum(x * x for x in samples) / count) / 32768,
            "peak": max(abs(x) for x in samples) / 32768,
            "clipping_ratio": sum(abs(x) >= 32760 for x in samples) / count}


def prepare_wav(pcm: bytes, sample_rate: int) -> tuple[bytes, tuple[str, ...]]:
    if isinstance(sample_rate, bool) or not isinstance(sample_rate, int) or not 8000 <= sample_rate <= 96000:
        raise ValueError("Неподдерживаемая частота микрофона.")
    if not isinstance(pcm, bytes) or len(pcm) > MAX_AUDIO_BYTES:
        raise ValueError("Слишком большая запись.")
    duration = len(pcm) / (sample_rate * 2)
    if not 0.25 <= duration <= MAX_SECONDS + 0.1:
        raise ValueError("Запись должна длиться от 0,25 до 30 секунд.")
    stats = pcm_stats(pcm)
    if stats["rms"] < 0.0008:
        raise ValueError("Не слышно речи. Проверьте микрофон и повторите запись.")
    warnings = []
    if stats["rms"] < 0.008:
        warnings.append("Тихий сигнал: слова и числа могли распознаться неверно.")
    if stats["clipping_ratio"] > 0.01:
        warnings.append("Микрофон перегружен: уменьшите его усиление или отодвиньтесь.")
    output = io.BytesIO()
    with wave.open(output, "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(sample_rate)
        wav.writeframes(pcm)
    return output.getvalue(), tuple(warnings)


def _strict_pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Повтор поля ответа распознавания.")
        result[key] = value
    return result


def parse_worker_result(raw: bytes) -> Transcript:
    if not isinstance(raw, bytes) or len(raw) > MAX_RESPONSE_BYTES:
        raise ValueError("Слишком большой ответ распознавания.")
    result = json.loads(raw.decode("utf-8"), object_pairs_hook=_strict_pairs,
                        parse_constant=lambda value: (_ for _ in ()).throw(ValueError(value)))
    if not isinstance(result, dict) or set(result) != {"text", "warnings", "elapsed_s"}:
        raise ValueError("Неверная форма ответа распознавания.")
    text, warnings, elapsed = result["text"], result["warnings"], result["elapsed_s"]
    if not isinstance(text, str) or not text.strip() or len(text) > MAX_TEXT:
        raise ValueError("Нет короткого распознанного текста. Повторите фразу.")
    if any(ord(char) < 32 and char not in "\n\t" for char in text):
        raise ValueError("Недопустимые символы в распознанном тексте.")
    if not isinstance(warnings, list) or len(warnings) > 12 or any(
            not isinstance(item, str) or not item or len(item) > 500 for item in warnings):
        raise ValueError("Неверные предупреждения распознавания.")
    if isinstance(elapsed, bool) or not isinstance(elapsed, (int, float)) or not math.isfinite(elapsed) or not 0 <= elapsed <= WORKER_TIMEOUT:
        raise ValueError("Неверное время распознавания.")
    # Do not rewrite words, numerals, units, punctuation, case or Cyrillic ё.
    return Transcript(text, tuple(warnings), float(elapsed))


class VoiceService:
    """One job at a time. Exactly one finished event for each accepted start.

    Events: (state, str), (level, float), (result, Transcript), (error, str),
    (finished, None). Cancellation suppresses new results; UI also discards any
    result already queued before cancellation and waits for finished before a new job.
    """

    def __init__(self, events: queue.Queue):
        self.events = events
        self._lock = threading.Lock()
        self._busy = False
        self._recording = False
        self._closed = False
        self._stop = threading.Event()
        self._cancel = threading.Event()
        self._process = None
        self._thread = None

    @property
    def busy(self):
        with self._lock:
            return self._busy

    @property
    def recording(self):
        with self._lock:
            return self._recording

    @staticmethod
    def devices() -> list[InputDevice]:
        try:
            import sounddevice as sd
            default = int(sd.default.device[0])
            result = []
            hosts = sd.query_hostapis()
            for index, item in enumerate(sd.query_devices()):
                if item["max_input_channels"] > 0:
                    host = hosts[item["hostapi"]]["name"]
                    result.append(InputDevice(index, f"{item['name']} · {host}", index == default))
            return result
        except ImportError as exc:
            raise ValueError("Голосовой ввод не установлен. Запустите INSTALL_VOICE.cmd.") from exc
        except Exception as exc:
            raise ValueError("Не удалось получить микрофоны. Проверьте устройства и разрешение Windows.") from exc

    def start(self, device=None, profile="balanced"):
        if device is not None and (isinstance(device, bool) or not isinstance(device, int) or device < 0):
            raise ValueError("Выберите доступный микрофон.")
        path = model_path(profile)
        if importlib.util.find_spec("sounddevice") is None or importlib.util.find_spec("faster_whisper") is None:
            raise ValueError("Голосовой ввод не установлен. Запустите INSTALL_VOICE.cmd.")
        with self._lock:
            if self._closed:
                raise ValueError("Окно голосового ввода закрыто.")
            if self._busy:
                raise ValueError("Предыдущая запись ещё завершается.")
            if not _MICROPHONE_LOCK.acquire(blocking=False):
                raise ValueError("Голосовой ввод уже работает в другом окне.")
            self._busy = True
            self._stop = threading.Event()
            self._cancel = threading.Event()
            self._thread = threading.Thread(target=self._run, args=(device, profile, path), daemon=True)
            try:
                self._thread.start()
            except Exception:
                self._busy = False
                _MICROPHONE_LOCK.release()
                raise

    def stop(self):
        self._stop.set()

    def cancel(self):
        self._cancel.set()
        self._stop.set()
        # Worker loop notices cancellation within 100 ms. Never wait in Tk.

    def close(self):
        with self._lock:
            self._closed = True
        self.cancel()

    def _publish(self, kind, value):
        with self._lock:
            if self._closed or self._cancel.is_set() and kind not in {"finished", "state"}:
                return
        self.events.put((kind, value))

    def _run(self, device, profile, path):
        try:
            if self._cancel.is_set():
                return
            pcm, sample_rate, warnings = self._capture(device)
            if self._cancel.is_set():
                return
            wav, signal_warnings = prepare_wav(pcm, sample_rate)
            self._publish("state", "Распознаю локально…")
            transcript = self._transcribe(wav, profile)
            if transcript is not None:
                self._publish("result", Transcript(transcript.text,
                    tuple(dict.fromkeys((*warnings, *signal_warnings, *transcript.warnings))), transcript.elapsed_s))
        except Exception as exc:
            if not self._cancel.is_set():
                self._publish("error", str(exc)[:500])
        finally:
            with self._lock:
                self._recording = False
                self._busy = False
                self._process = None
            _MICROPHONE_LOCK.release()
            # finished must be delivered even when closed/cancelled.
            self.events.put(("finished", None))

    def _capture(self, device):
        import sounddevice as sd
        try:
            if self._cancel.is_set():
                return b"", 16000, ()
            selected = sd.query_devices(device, "input")
            rate = int(round(selected["default_samplerate"]))
            if not 8000 <= rate <= 96000:
                raise ValueError("Неподдерживаемая частота микрофона; выберите другой вход.")
            block = max(1, rate // 50)
            limit = rate * MAX_SECONDS
            chunks, count = [], 0
            with sd.RawInputStream(device=device, samplerate=rate, channels=1, dtype="int16",
                                   blocksize=block, latency="high") as stream:
                with self._lock:
                    self._recording = True
                self._publish("state", "Запись · нажмите «Стоп», когда закончите (до 30 с)")
                while not self._stop.is_set() and count < limit:
                    raw, overflow = stream.read(min(block, limit - count))
                    if overflow:
                        raise ValueError("Пропущен фрагмент звука. Повторите запись; числа могли потеряться.")
                    chunk = bytes(raw)
                    if len(chunk) != min(block, limit - count) * 2:
                        raise ValueError("Микрофон вернул неполный фрагмент. Повторите запись.")
                    chunks.append(chunk)
                    count += len(chunk) // 2
                    if len(chunks) % 5 == 0:
                        self._publish("level", min(1.0, pcm_stats(chunk)["rms"] * 8))
            with self._lock:
                self._recording = False
            warnings = ("Запись закончена по лимиту 30 с: проверьте, не обрезан ли конец.",) if count >= limit else ()
            return b"".join(chunks), rate, warnings
        except ValueError:
            raise
        except Exception as exc:
            raise ValueError("Не удалось записать звук. Проверьте микрофон и доступ к нему: "
                             "Параметры Windows → Конфиденциальность → Микрофон.") from exc

    def _transcribe(self, wav, profile):
        if self._cancel.is_set():
            return None
        environment = dict(os.environ, HF_HUB_OFFLINE="1", TRANSFORMERS_OFFLINE="1",
                           HF_HUB_DISABLE_TELEMETRY="1", HF_HUB_DISABLE_PROGRESS_BARS="1",
                           PYTHONIOENCODING="utf-8", OMP_NUM_THREADS="4")
        python = Path(sys.executable)
        # Hidden parent uses pythonw; pipe-based worker requires python.exe.
        if python.name.lower() == "pythonw.exe":
            python = python.with_name("python.exe")
        command = [str(python), "-X", "utf8", "-m", "assistant.voice_worker", "--profile", profile]
        creationflags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
        process = subprocess.Popen(command, cwd=ROOT.parent, env=environment, stdin=subprocess.PIPE,
                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE, creationflags=creationflags)
        with self._lock:
            self._process = process
        # communicate drains both pipes in separate stdlib threads; timeout retries
        # do not resend audio. No temp WAV or transcript log is created.
        started = time.monotonic()
        first = True
        try:
            while True:
                if self._cancel.is_set():
                    process.terminate()
                    process.communicate(timeout=5)
                    return None
                if time.monotonic() - started > WORKER_TIMEOUT:
                    process.terminate()
                    process.communicate(timeout=5)
                    raise ValueError("Распознавание заняло слишком долго. Выберите «Баланс» и повторите короче.")
                try:
                    stdout, stderr = process.communicate(input=wav if first else None, timeout=0.1)
                    break
                except subprocess.TimeoutExpired:
                    first = False
            if self._cancel.is_set():
                return None
            if process.returncode:
                # Only public messages from our own fixed worker protocol.
                try:
                    error = json.loads(stdout.decode("utf-8"))
                    message = error.get("error") if set(error) == {"error"} else None
                except (ValueError, AttributeError, UnicodeError):
                    message = None
                raise ValueError(message[:500] if isinstance(message, str) else
                                 "Локальное распознавание не запустилось. Проверьте INSTALL_VOICE.cmd.")
            return parse_worker_result(stdout)
        finally:
            if process.poll() is None:
                process.kill()
                process.communicate(timeout=5)
