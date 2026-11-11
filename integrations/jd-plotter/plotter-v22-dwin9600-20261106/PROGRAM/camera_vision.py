"""Deterministic paper detection, planar calibration, faithful scan processing.

No generated text, inferred hidden corners, motion commands or neural restoration.
"""
from dataclasses import dataclass
import json
import math
from pathlib import Path

import cv2
import numpy as np
from PIL import Image

from editor_project import atomic_json

MAX_SCAN_PIXELS = 15_000_000


def checked_image(image):
    if not isinstance(image, np.ndarray) or image.dtype != np.uint8 or image.ndim != 3 or image.shape[2] != 3:
        raise ValueError("Нужно цветное изображение uint8 BGR.")
    if min(image.shape[:2]) < 16 or image.shape[0] * image.shape[1] > 20_000_000:
        raise ValueError("Некорректные размеры изображения.")
    return image


def ordered_quad(points):
    points = np.asarray(points, dtype=np.float32)
    if points.shape != (4, 2) or not np.isfinite(points).all():
        raise ValueError("Нужны четыре конечные точки.")
    hull = cv2.convexHull(points).reshape(-1, 2)
    if len(hull) != 4 or abs(cv2.contourArea(hull)) < 16:
        raise ValueError("Углы должны образовывать выпуклый четырёхугольник.")
    center = points.mean(axis=0)
    indices = np.argsort(np.arctan2(points[:, 1] - center[1], points[:, 0] - center[0]))
    points = points[indices]
    return np.roll(points, -int(np.argmin(points.sum(axis=1))), axis=0)


def detect_paper(image):
    image = checked_image(image)
    h, w = image.shape[:2]
    scale = min(1.0, 1200 / max(h, w))
    small = cv2.resize(image, None, fx=scale, fy=scale) if scale < 1 else image
    gray = cv2.GaussianBlur(cv2.cvtColor(small, cv2.COLOR_BGR2GRAY), (5, 5), 0)
    _, light = cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
    edges = cv2.Canny(gray, 40, 120)
    kernel = np.ones((5, 5), np.uint8)
    candidates = []
    sh, sw = gray.shape
    for mask in (cv2.morphologyEx(light, cv2.MORPH_CLOSE, kernel), cv2.dilate(edges, kernel)):
        contours, _ = cv2.findContours(mask, cv2.RETR_LIST, cv2.CHAIN_APPROX_SIMPLE)
        for contour in contours:
            area = abs(cv2.contourArea(contour))
            if not .12 * sh * sw <= area <= .97 * sh * sw:
                continue
            poly = cv2.approxPolyDP(contour, .02 * cv2.arcLength(contour, True), True)
            if len(poly) != 4 or not cv2.isContourConvex(poly):
                continue
            quad = ordered_quad(poly.reshape(4, 2))
            if (quad[:, 0].min() < 2 or quad[:, 1].min() < 2 or
                    quad[:, 0].max() > sw - 3 or quad[:, 1].max() > sh - 3):
                continue  # A clipped sheet must not become an invented rectangle.
            candidates.append((area, quad / scale))
    if not candidates:
        raise ValueError("Все четыре края не найдены. Нужен контрастный фон и целый лист в кадре/мозаике. "
                         "Можно отметить четыре угла вручную.")
    return max(candidates, key=lambda item: item[0])[1]


def transform_points(points, matrix):
    points = np.asarray(points, np.float32)
    return cv2.perspectiveTransform(points.reshape(-1, 1, 2), np.asarray(matrix, np.float64)).reshape(-1, 2)


@dataclass(frozen=True)
class PlanarCalibration:
    resolution: tuple
    pixel_to_local: object
    reference_mm: tuple
    camera_matrix: object = None
    distortion: object = None

    def check(self, image):
        checked_image(image)
        if (image.shape[1], image.shape[0]) != self.resolution:
            raise ValueError("Разрешение изменилось. Нужна отдельная калибровка этого разрешения; масштаб автоматически не угадывается.")

    def undistort(self, image):
        self.check(image)
        if self.camera_matrix is None:
            return image.copy()
        return cv2.undistort(image, self.camera_matrix, self.distortion)

    def local_points(self, points):
        return transform_points(points, self.pixel_to_local)

    def world_points(self, points, pen_position):
        if len(pen_position) != 2 or not all(math.isfinite(v) for v in pen_position):
            raise ValueError("Нет достоверной позиции каретки.")
        return self.local_points(points) + np.asarray(pen_position, np.float32)

    def save(self, path):
        atomic_json(path, {"format": "plotter-camera-calibration", "version": 1,
            "resolution": list(self.resolution), "pixel_to_local": np.asarray(self.pixel_to_local).tolist(),
            "reference_mm": list(self.reference_mm),
            "camera_matrix": self.camera_matrix.tolist() if self.camera_matrix is not None else None,
            "distortion": self.distortion.tolist() if self.distortion is not None else None})

    @classmethod
    def load(cls, path):
        path = Path(path)
        if path.stat().st_size > 20_000:
            raise ValueError("Слишком большой файл калибровки.")
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict) or set(data) != {"format", "version", "resolution", "pixel_to_local", "reference_mm", "camera_matrix", "distortion"}:
            raise ValueError("Некорректная калибровка.")
        if data["format"] != "plotter-camera-calibration" or data["version"] != 1:
            raise ValueError("Неподдерживаемая калибровка.")
        resolution, matrix = tuple(data["resolution"]), np.asarray(data["pixel_to_local"], np.float64)
        reference = tuple(data["reference_mm"])
        if (len(resolution) != 2 or any(type(v) is not int or not 16 <= v <= 10000 for v in resolution)
                or resolution[0] * resolution[1] > 20_000_000 or matrix.shape != (3, 3)
                or not np.isfinite(matrix).all() or abs(np.linalg.det(matrix)) < 1e-12
                or len(reference) != 2 or any(type(v) not in (int, float) or not math.isfinite(v) or not 1 <= v <= 3000 for v in reference)):
            raise ValueError("Некорректные размеры или матрица калибровки.")
        camera = distortion = None
        if data["camera_matrix"] is not None or data["distortion"] is not None:
            camera = np.asarray(data["camera_matrix"], np.float64)
            distortion = np.asarray(data["distortion"], np.float64)
            if camera.shape != (3, 3) or distortion.size not in (4, 5, 8, 12, 14) or not np.isfinite(camera).all() or not np.isfinite(distortion).all():
                raise ValueError("Некорректная калибровка объектива.")
        return cls(resolution, matrix, reference, camera, distortion)


def calibrate_plane(image, corners, reference_mm, center_from_pen=(0, 0), lens=None):
    """Reference edges must be parallel to machine right/down, not merely a random sheet."""
    checked_image(image)
    width, height = reference_mm
    if not all(math.isfinite(v) and 1 <= v <= 3000 for v in (width, height)) or not all(math.isfinite(v) and abs(v) <= 3000 for v in center_from_pen):
        raise ValueError("Некорректные размеры эталона или его смещение относительно пера.")
    quad = ordered_quad(corners)
    if (quad[:, 0].min() < 0 or quad[:, 1].min() < 0 or
            quad[:, 0].max() >= image.shape[1] or quad[:, 1].max() >= image.shape[0]):
        raise ValueError("Углы эталона выходят за изображение.")
    cx, cy = center_from_pen
    target = np.float32([[cx - width / 2, cy - height / 2], [cx + width / 2, cy - height / 2],
                         [cx + width / 2, cy + height / 2], [cx - width / 2, cy + height / 2]])
    matrix = cv2.getPerspectiveTransform(quad, target)
    camera, distortion = lens if lens is not None else (None, None)
    return PlanarCalibration((image.shape[1], image.shape[0]), matrix, tuple(reference_mm), camera, distortion)


def calibrate_lens(images, pattern=(9, 6), square_mm=10):
    if len(images) < 8 or not 3 <= pattern[0] <= 20 or not 3 <= pattern[1] <= 20 or not 1 <= square_mm <= 100:
        raise ValueError("Нужно минимум 8 снимков шахматной доски; pattern — число внутренних углов.")
    objects, detected, size = [], [], None
    template = np.zeros((pattern[0] * pattern[1], 3), np.float32)
    template[:, :2] = np.mgrid[0:pattern[0], 0:pattern[1]].T.reshape(-1, 2) * square_mm
    for image in images:
        checked_image(image)
        gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
        current = (gray.shape[1], gray.shape[0])
        if size is not None and current != size:
            raise ValueError("Снимки калибровки должны иметь одно разрешение.")
        size = current
        found, corners = cv2.findChessboardCornersSB(gray, pattern)
        if found:
            objects.append(template)
            detected.append(corners)
    if len(detected) < 8:
        raise ValueError("Шахматная доска найдена менее чем на 8 снимках.")
    error, camera, distortion, _, _ = cv2.calibrateCamera(objects, detected, size, None, None)
    if not math.isfinite(error) or error > 2:
        raise ValueError(f"Большая ошибка калибровки: {error:.2f} пикселя. Сделайте более резкие снимки.")
    return camera, distortion, error, len(detected), size


def paper_metrics(corners, calibration, pen_position=(0, 0)):
    world = calibration.world_points(ordered_quad(corners), pen_position)
    width = (np.linalg.norm(world[1] - world[0]) + np.linalg.norm(world[2] - world[3])) / 2
    height = (np.linalg.norm(world[3] - world[0]) + np.linalg.norm(world[2] - world[1])) / 2
    angle = math.degrees(math.atan2(world[1, 1] - world[0, 1], world[1, 0] - world[0, 0]))
    return {"width_mm": float(width), "height_mm": float(height), "rotation_deg": angle,
            "corners_mm": world.tolist(), "note": "Программные координаты, не измерение энкодером; ноль станка не изменён."}


def rectify_document(image, corners, dimensions_mm=None, dpi=300):
    checked_image(image)
    quad = ordered_quad(corners)
    if quad[:, 0].min() < 0 or quad[:, 1].min() < 0 or quad[:, 0].max() >= image.shape[1] or quad[:, 1].max() >= image.shape[0]:
        raise ValueError("Углы выходят за изображение.")
    if dimensions_mm is None:
        width = round(max(np.linalg.norm(quad[1] - quad[0]), np.linalg.norm(quad[2] - quad[3])))
        height = round(max(np.linalg.norm(quad[3] - quad[0]), np.linalg.norm(quad[2] - quad[1])))
    else:
        if not 50 <= dpi <= 600 or not all(math.isfinite(v) and 1 <= v <= 3000 for v in dimensions_mm):
            raise ValueError("Некорректный размер скана или DPI.")
        width, height = [round(v / 25.4 * dpi) for v in dimensions_mm]
    if min(width, height) < 16 or width * height > MAX_SCAN_PIXELS:
        raise ValueError("Скан слишком мал или больше 15 Мп. Уменьшите DPI.")
    target = np.float32([[0, 0], [width - 1, 0], [width - 1, height - 1], [0, height - 1]])
    return cv2.warpPerspective(image, cv2.getPerspectiveTransform(quad, target), (width, height))


def clean_document(image, mode="color", strength=.7, white_reference=None):
    image = checked_image(image)
    if mode not in {"original", "color", "gray", "bw"} or not math.isfinite(strength) or not 0 <= strength <= 1:
        raise ValueError("Неизвестный режим очистки или сила вне 0–1.")
    if mode == "original":
        return image.copy()
    lab = cv2.cvtColor(image, cv2.COLOR_BGR2LAB)
    luminance = lab[:, :, 0].astype(np.float32)
    if white_reference is not None:
        checked_image(white_reference)
        if white_reference.shape != image.shape:
            raise ValueError("Белый эталон должен иметь тот же размер и геометрию.")
        background = cv2.cvtColor(white_reference, cv2.COLOR_BGR2LAB)[:, :, 0].astype(np.float32)
    else:
        # Estimate illumination on a reduced image, preserving fine foreground strokes.
        scale = min(1., 800 / max(image.shape[:2]))
        small = cv2.resize(luminance, None, fx=scale, fy=scale)
        kernel_size = max(9, int(min(small.shape) / 12) | 1)
        background = cv2.morphologyEx(small, cv2.MORPH_CLOSE, np.ones((kernel_size, kernel_size), np.uint8))
        background = cv2.GaussianBlur(background, (0, 0), kernel_size / 3)
        background = cv2.resize(background, (image.shape[1], image.shape[0]))
    normalized = np.clip(luminance * 245 / np.maximum(background, 24), 0, 255)
    corrected = np.uint8(np.clip(luminance * (1 - strength) + normalized * strength, 0, 255))
    if mode == "color":
        lab[:, :, 0] = corrected
        return cv2.cvtColor(lab, cv2.COLOR_LAB2BGR)
    if mode == "bw":
        corrected = cv2.adaptiveThreshold(corrected, 255, cv2.ADAPTIVE_THRESH_GAUSSIAN_C, cv2.THRESH_BINARY, 35, 12)
    return cv2.cvtColor(corrected, cv2.COLOR_GRAY2BGR)


def frame_quality(image):
    gray = cv2.cvtColor(checked_image(image), cv2.COLOR_BGR2GRAY)
    return {"sharpness": float(cv2.Laplacian(gray, cv2.CV_64F).var()),
            "highlight_fraction": float(np.mean(gray >= 252)),
            "dark_fraction": float(np.mean(gray <= 5))}


def save_scan(path, image, original, dimensions_mm=None, dpi=300):
    """Explicit export, always keeps the uncleaned scan alongside the result."""
    image, original = checked_image(image), checked_image(original)
    path = Path(path)
    if path.suffix.lower() not in {".png", ".pdf"}:
        raise ValueError("Выберите PNG или PDF.")
    if not math.isfinite(dpi) or not 50 <= dpi <= 600:
        raise ValueError("DPI должен быть от 50 до 600.")
    if dimensions_mm is None and path.suffix.lower() == ".pdf":
        raise ValueError("Для PDF задайте физический размер листа. Он не угадывается по пикселям.")
    if dimensions_mm is not None and not all(math.isfinite(v) and 1 <= v <= 3000 for v in dimensions_mm):
        raise ValueError("Некорректный физический размер листа.")
    raw_path = path.with_name(path.stem + ".original.png")
    info_path = path.with_name(path.stem + ".scan.json")
    if path.resolve() == raw_path.resolve() or path.resolve() == info_path.resolve():
        raise ValueError("Имена выходных файлов совпадают.")
    original_pil = Image.fromarray(cv2.cvtColor(original, cv2.COLOR_BGR2RGB))
    result_pil = Image.fromarray(cv2.cvtColor(image, cv2.COLOR_BGR2RGB))
    # The UI checks ALL destinations before calling this function.
    original_pil.save(raw_path, format="PNG")
    if dimensions_mm is None:
        if path.suffix.lower() == ".pdf":
            raise ValueError("Для PDF задайте физический размер листа. Он не угадывается по пикселям.")
        result_pil.save(path, format="PNG")
    else:
        width, height = dimensions_mm
        if not all(math.isfinite(v) and 1 <= v <= 3000 for v in (width, height)):
            raise ValueError("Некорректный физический размер листа.")
        if path.suffix.lower() == ".pdf":
            # Independent x/y resolutions preserve the requested page size exactly.
            result_pil.save(path, format="PDF", dpi=(image.shape[1] / width * 25.4, image.shape[0] / height * 25.4))
        else:
            result_pil.save(path, format="PNG", dpi=(image.shape[1] / width * 25.4, image.shape[0] / height * 25.4))
    atomic_json(info_path, {"format": "plotter-scan", "version": 1, "pixels": [image.shape[1], image.shape[0]],
        "dimensions_mm": list(dimensions_mm) if dimensions_mm else None, "requested_dpi": dpi,
        "original": raw_path.name, "note": "Очистка не восстанавливает скрытые детали; оригинал сохранён."})
    return raw_path, info_path


def scan_plan(area_mm, calibration, overlap=.25):
    if not .1 <= overlap <= .5 or not all(math.isfinite(v) and 1 <= v <= 3000 for v in area_mm):
        raise ValueError("Некорректная область или перекрытие (10–50%).")
    w, h = calibration.resolution
    polygon = calibration.local_points([[1, 1], [w - 2, 1], [w - 2, h - 2], [1, h - 2]])
    center = polygon.mean(axis=0)
    half = (polygon.max(axis=0) - polygon.min(axis=0)) / 2
    for _ in range(30):
        rect = np.float32([center + [-half[0], -half[1]], center + [half[0], -half[1]],
                           center + [half[0], half[1]], center + [-half[0], half[1]]])
        if all(cv2.pointPolygonTest(polygon, tuple(map(float, p)), False) >= 0 for p in rect):
            break
        half *= .9
    else:
        raise ValueError("Не удалось определить полезное поле камеры.")
    half *= .98  # Reserve a border for lens and blending uncertainty.
    def centers(extent, radius):
        if radius * 2 >= extent:
            return [extent / 2]
        count = int(math.ceil((extent - radius * 2) / (radius * 2 * (1 - overlap)))) + 1
        if count > 200:
            raise ValueError("Слишком много кадров. Проверьте калибровку и область.")
        return np.linspace(radius, extent - radius, count).tolist()
    xs, ys = centers(area_mm[0], half[0]), centers(area_mm[1], half[1])
    if len(xs) * len(ys) > 200:
        raise ValueError("Слишком много кадров: максимум 200.")
    positions = []
    for index, y in enumerate(ys):
        for x in xs if index % 2 == 0 else reversed(xs):
            target = (float(x - center[0]), float(y - center[1]))
            if not (0 <= target[0] <= area_mm[0] and 0 <= target[1] <= area_mm[1]):
                raise ValueError("Из-за смещения камеры край листа недоступен в заданной области хода. "
                                 "Измените крепление/смещение, не обходите границы станка.")
            positions.append(target)
    return positions


def assemble_mosaic(tiles, calibration, area_mm, dpi=300):
    if not tiles or len(tiles) > 200 or not 50 <= dpi <= 600 or not all(math.isfinite(v) and 1 <= v <= 3000 for v in area_mm):
        raise ValueError("Некорректная мозаика, размер или DPI.")
    width, height = [round(v / 25.4 * dpi) for v in area_mm]
    if width * height > MAX_SCAN_PIXELS or min(width, height) < 16:
        raise ValueError("Размер мозаики должен быть от 16 пикселей по стороне и не больше 15 Мп.")
    total = np.zeros((height, width, 3), np.float32)
    weights = np.zeros((height, width), np.float32)
    for image, position in tiles:
        image = calibration.undistort(image)
        if len(position) != 2 or not all(math.isfinite(v) for v in position):
            raise ValueError("Некорректная позиция кадра.")
        sx, sy = (width - 1) / area_mm[0], (height - 1) / area_mm[1]
        matrix = np.array([[sx, 0, position[0] * sx], [0, sy, position[1] * sy], [0, 0, 1]]) @ calibration.pixel_to_local
        ih, iw = image.shape[:2]
        edge_x = np.minimum(np.arange(iw) + 1, iw - np.arange(iw)) / max(1, iw / 10)
        edge_y = np.minimum(np.arange(ih) + 1, ih - np.arange(ih)) / max(1, ih / 10)
        mask = np.minimum(np.minimum(edge_y[:, None], edge_x[None, :]), 1).astype(np.float32)
        warped = cv2.warpPerspective(image.astype(np.float32) * mask[:, :, None], matrix, (width, height))
        weight = cv2.warpPerspective(mask, matrix, (width, height))
        total += warped
        weights += weight
    covered = weights > .001
    coverage = float(np.mean(covered))
    result = np.uint8(np.clip(total / np.maximum(weights[:, :, None], .001), 0, 255))
    result[~covered] = (220, 0, 220)  # Missing pixels are never silently turned into white paper.
    return result, coverage


def load_tiles(path):
    """Offline input is strictly confined to files beside its manifest."""
    from camera_client import decode_image, MAX_FRAME_BYTES
    from geometry import Sheet
    path = Path(path).resolve()
    if path.stat().st_size > 100_000:
        raise ValueError("Слишком большое описание прохода.")
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict) or set(data) != {"format", "version", "area_mm", "tiles"} or data['format'] != 'plotter-scan-tiles' or data['version'] != 1:
        raise ValueError("Некорректное описание прохода.")
    area = data['area_mm']
    if not isinstance(area, list) or len(area) != 2 or any(type(v) not in (float, int) for v in area):
        raise ValueError("Некорректный размер прохода.")
    Sheet(*area)
    calibration = PlanarCalibration.load(path.parent / 'calibration.camera.json')
    records = data['tiles']
    if not isinstance(records, list) or not 1 <= len(records) <= 200:
        raise ValueError("В проходе должно быть от 1 до 200 кадров.")
    tiles, pixels = [], 0
    for item in records:
        if not isinstance(item, dict) or set(item) != {'file', 'pen_position_mm'} or not isinstance(item['file'], str):
            raise ValueError("Некорректный кадр прохода.")
        name = item['file']
        if Path(name).name != name or any(character in name for character in ('/', '\\', ':')):
            raise ValueError("Путь кадра должен быть только именем файла.")
        source = (path.parent / name).resolve()
        if source.parent != path.parent or source.stat().st_size > MAX_FRAME_BYTES:
            raise ValueError("Файл кадра вне папки или больше 8 МБ.")
        position = item['pen_position_mm']
        if not isinstance(position, list) or len(position) != 2 or any(type(v) not in (int, float) or not math.isfinite(v) for v in position):
            raise ValueError("Некорректная позиция кадра.")
        image = decode_image(source.read_bytes())
        calibration.check(image)
        pixels += image.shape[0] * image.shape[1]
        if pixels > 80_000_000:
            raise ValueError("Проход больше 80 Мп в памяти.")
        tiles.append((image, tuple(position)))
    return tiles, calibration, tuple(area)
