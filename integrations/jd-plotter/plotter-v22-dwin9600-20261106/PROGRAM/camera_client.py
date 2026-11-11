"""Bounded MJPEG and telemetry clients. No dependency on the motion controller."""
from dataclasses import dataclass
import io
import json
import queue
import threading
import time
from urllib.parse import urlsplit, urlunsplit, urlencode
from urllib.request import Request, urlopen

import cv2
import numpy as np
from PIL import Image

MAX_FRAME_BYTES = 8_000_000
MAX_PIXELS = 20_000_000


def endpoint(url, path=None):
    parts = urlsplit(url.strip())
    if parts.scheme not in ("http", "https") or not parts.hostname or parts.username or parts.password or parts.fragment:
        raise ValueError("Введите HTTP-адрес камеры без пароля и фрагмента, например http://192.168.1.50:81/stream.")
    try:
        parts.port
    except ValueError as exc:
        raise ValueError("Некорректный порт камеры.") from exc
    return urlunsplit((parts.scheme, parts.netloc, path if path is not None else parts.path or "/stream",
                       "" if path is not None else parts.query, ""))


def decode_image(raw):
    if not raw or len(raw) > MAX_FRAME_BYTES:
        raise ValueError("Кадр пустой или больше 8 МБ.")
    try:
        with Image.open(io.BytesIO(raw)) as header:
            if header.width * header.height > MAX_PIXELS or min(header.size) < 16:
                raise ValueError("Некорректные размеры кадра или более 20 Мп.")
    except (OSError, Image.DecompressionBombError) as exc:
        raise ValueError("Камера прислала не изображение.") from exc
    result = cv2.imdecode(np.frombuffer(raw, np.uint8), cv2.IMREAD_COLOR)
    if result is None:
        raise ValueError("Не удалось декодировать кадр.")
    return result


def read_image(path):
    from pathlib import Path
    with Path(path).open("rb") as source:
        return decode_image(source.read(MAX_FRAME_BYTES + 1))


class JpegParser:
    """JPEG boundary parser for sensor MJPEG (no embedded thumbnail segments)."""
    def __init__(self):
        self.buffer = bytearray()

    def feed(self, chunk):
        self.buffer.extend(chunk)
        frames = []
        while True:
            start = self.buffer.find(b"\xff\xd8")
            if start < 0:
                self.buffer = self.buffer[-1:]
                break
            if start:
                del self.buffer[:start]
            end = self.buffer.find(b"\xff\xd9", 2)
            if end < 0:
                if len(self.buffer) > MAX_FRAME_BYTES:
                    self.buffer.clear()
                    raise ValueError("Слишком большой или повреждённый MJPEG-кадр.")
                break
            if end + 2 > MAX_FRAME_BYTES:
                raise ValueError("Слишком большой MJPEG-кадр.")
            frames.append(bytes(self.buffer[:end + 2]))
            del self.buffer[:end + 2]
        return frames


@dataclass(frozen=True)
class Frame:
    image: object
    sequence: int
    received_at: float
    source: str


class CameraClient:
    def __init__(self):
        self.events = queue.Queue(maxsize=32)
        self.cancelled = threading.Event()
        self.lock = threading.Lock()
        self.latest = None
        self.telemetry = None
        self.connection_state = "Отключено"
        self.sequence = 0
        self.response = None
        self.socket = None
        self.threads = []

    def emit(self, kind, data):
        if self.cancelled.is_set():
            return
        try:
            self.events.put_nowait((kind, data))
        except queue.Full:
            try:
                self.events.get_nowait()
            except queue.Empty:
                pass
            try:
                self.events.put_nowait((kind, data))
            except queue.Full:
                pass

    def publish(self, raw, source):
        image = decode_image(raw)
        with self.lock:
            if self.cancelled.is_set():
                return
            self.sequence += 1
            self.latest = Frame(image, self.sequence, time.monotonic(), source)
            self.connection_state = "Подключено"

    def get_frame(self, max_age=2.0):
        with self.lock:
            frame = self.latest
            if frame is None or self.connection_state != "Подключено" or time.monotonic() - frame.received_at > max_age:
                raise ValueError("Свежего кадра нет. Проверьте камеру и Wi-Fi.")
            return Frame(frame.image.copy(), frame.sequence, frame.received_at, frame.source)

    def start(self, stream_url, telemetry_url=""):
        if self.threads or self.cancelled.is_set():
            raise ValueError("Создайте новое подключение после отключения.")
        stream_url = endpoint(stream_url)
        if telemetry_url:
            telemetry_url = telemetry_url.strip()
            parts = urlsplit(telemetry_url)
            if parts.scheme not in ("ws", "wss") or not parts.hostname or parts.username or parts.password or parts.fragment:
                raise ValueError("Некорректный WebSocket-адрес телеметрии.")
            try:
                parts.port
            except ValueError as exc:
                raise ValueError("Некорректный порт телеметрии.") from exc
        target = threading.Thread(target=self.stream_loop, args=(stream_url,), daemon=True)
        self.threads.append(target)
        target.start()
        if telemetry_url:
            target = threading.Thread(target=self.telemetry_loop, args=(telemetry_url,), daemon=True)
            self.threads.append(target)
            target.start()

    def stream_loop(self, url):
        backoff = 1
        while not self.cancelled.is_set():
            try:
                with self.lock:
                    if self.cancelled.is_set():
                        break
                    self.connection_state = "Подключение"
                self.emit("state", "Подключение к камере…")
                with urlopen(Request(url, headers={"Accept": "multipart/x-mixed-replace"}), timeout=3) as response:
                    with self.lock:
                        if self.cancelled.is_set():
                            break
                        self.response = response
                    content_type = response.headers.get("Content-Type", "").lower()
                    if "multipart" not in content_type and "jpeg" not in content_type:
                        raise ValueError("Адрес не отдаёт MJPEG. Проверьте порт и путь /stream.")
                    parser = JpegParser()
                    last_frame = time.monotonic()
                    while not self.cancelled.is_set():
                        chunk = response.read1(8192)
                        if not chunk:
                            raise ConnectionError("Видеопоток закрыт камерой.")
                        for raw in parser.feed(chunk):
                            self.publish(raw, url)
                            last_frame = time.monotonic()
                            backoff = 1
                        if time.monotonic() - last_frame > 5:
                            raise TimeoutError("Камера не передаёт кадры.")
            except Exception as exc:
                with self.lock:
                    if self.cancelled.is_set():
                        break
                    self.connection_state = "Нет связи"
                self.emit("state", f"Нет связи: {exc}. Повтор через {backoff} с.")
                if self.cancelled.wait(backoff):
                    break
                backoff = min(8, backoff * 2)
            finally:
                with self.lock:
                    self.response = None

    def telemetry_loop(self, url):
        import websocket
        backoff = 1
        while not self.cancelled.is_set():
            try:
                with websocket.create_connection(url, timeout=2, http_proxy_host=None) as connection:
                    with self.lock:
                        if self.cancelled.is_set():
                            break
                        self.socket = connection
                    backoff = 1
                    while not self.cancelled.is_set():
                        try:
                            raw = connection.recv()
                        except websocket.WebSocketTimeoutException:
                            continue
                        if not raw or len(raw) > 8192:
                            raise ValueError("Некорректный пакет телеметрии.")
                        data = json.loads(raw)
                        if not isinstance(data, dict):
                            raise ValueError("Телеметрия должна быть JSON-объектом.")
                        # External data is display-only, never motion coordinates.
                        with self.lock:
                            if self.cancelled.is_set():
                                break
                            self.telemetry = (time.monotonic(), data)
                        self.emit("telemetry", data)
            except Exception as exc:
                self.emit("telemetry_error", "Телеметрия: " + str(exc))
                if self.cancelled.wait(backoff):
                    break
                backoff = min(8, backoff * 2)
            finally:
                with self.lock:
                    self.socket = None

    def close(self):
        self.cancelled.set()
        with self.lock:
            self.latest = None
            self.telemetry = None
            self.connection_state = "Отключено"
            response, socket = self.response, self.socket
        # Socket shutdown is kept away from Tk; reads have finite timeouts.
        def release():
            for handle in (response, socket):
                if handle is not None:
                    try:
                        handle.close()
                    except Exception:
                        pass
        threading.Thread(target=release, daemon=True).start()


def capture(url):
    deadline = time.monotonic() + 8
    with urlopen(endpoint(url), timeout=8) as response:
        # read1 returns available data instead of waiting to fill an 8-MB buffer.
        # A slow trickle must not restart an unbounded whole-response wait.
        raw = bytearray()
        while True:
            if time.monotonic() >= deadline:
                raise TimeoutError("Камера не завершила снимок вовремя.")
            chunk = response.read1(min(65536, MAX_FRAME_BYTES + 1 - len(raw)))
            if not chunk:
                break
            raw.extend(chunk)
            if len(raw) > MAX_FRAME_BYTES:
                raise ValueError("Снимок больше 8 МБ.")
    return decode_image(raw)


def camera_control(url, **settings):
    allowed = {"framesize", "quality", "brightness", "contrast", "saturation", "awb", "awb_gain",
               "aec", "aec2", "agc", "hmirror", "vflip"}
    for name, value in settings.items():
        if name not in allowed or type(value) is not int:
            raise ValueError("Неизвестная настройка камеры.")
        control_url = endpoint(url, "/control") + "?" + urlencode({"var": name, "val": value})
        with urlopen(control_url, timeout=3) as response:
            response.read(8192)
