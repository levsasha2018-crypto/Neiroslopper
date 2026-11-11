"""Bounded tiled edge extraction and tonal pen hatching, without AI or I/O."""
import math

import cv2
import numpy as np

from .reference_images import check_cancel

TILE = 768
HALO = 48


def checked_box(box, width, height):
    if (not isinstance(box, tuple) or len(box) != 4 or any(type(n) is not int for n in box) or
            not 0 <= box[0] < box[2] <= width or not 0 <= box[1] < box[3] <= height):
        raise ValueError('Рамка главного объекта/правки должна находиться внутри исходника.')
    return box


def local_box(box, image_shape, options):
    if box is None:
        return None
    # UI/model regions use the original image, even when a crop is active.
    height, width = image_shape[:2]
    ox, oy = (options.crop[:2] if options.crop else (0, 0))
    x0, y0, x1, y1 = box
    clipped = max(0, x0-ox), max(0, y0-oy), min(width, x1-ox), min(height, y1-oy)
    return checked_box(clipped, width, height)


def pixel_box(box, ratio, width, height):
    # Enclose the source region: rounding both sides can collapse a narrow
    # subject/repair to zero pixels after downsampling.
    x0, y0 = max(0, min(width-1, math.floor(box[0]*ratio))), max(0, min(height-1, math.floor(box[1]*ratio)))
    return (x0, y0, min(width, max(x0+1, math.ceil(box[2]*ratio))),
            min(height, max(y0+1, math.ceil(box[3]*ratio))))


def working_image(image, side):
    h, w = image.shape[:2]
    ratio = min(1., side/max(h, w))
    if ratio >= 1:
        return image.copy(), ratio
    size = round(w*ratio), round(h*ratio)
    if min(size) < 2:
        raise ValueError('Слишком вытянутый образец для обводки. Обрежьте нужную область или увеличьте рабочую сторону.')
    return cv2.resize(image, size, interpolation=cv2.INTER_AREA), ratio


def tile_edges(gray, detail, contrast, weak, cancel=None):
    """Process overlapping patches; copy cores only, so seams aren't duplicated."""
    h, w = gray.shape
    mask = np.zeros((h, w), np.uint8)
    tiles = strong_count = added_count = 0
    low = 25 + (100-detail)*.4
    sigma = .4 + (100-detail)/100
    # Compute local contrast once with a consistent grid across tile boundaries.
    if contrast:
        gray = cv2.createCLAHE(clipLimit=2, tileGridSize=(max(2, math.ceil(w/128)),
                                                      max(2, math.ceil(h/128)))).apply(gray)
    for y in range(0, h, TILE):
        for x in range(0, w, TILE):
            check_cancel(cancel)
            x0, y0, x1, y1 = max(0, x-HALO), max(0, y-HALO), min(w, x+TILE+HALO), min(h, y+TILE+HALO)
            blur = cv2.GaussianBlur(gray[y0:y1, x0:x1], (0, 0), sigma)
            strong = cv2.Canny(blur, low, low*2.5, L2gradient=True)
            edges = strong.copy()
            if weak:
                fine = cv2.Canny(blur, low*.25, low*.65, L2gradient=True)
                count, labels, stats, _ = cv2.connectedComponentsWithStats(fine, connectivity=8)
                keep = np.zeros(count, np.uint8)
                keep[1:] = (stats[1:, cv2.CC_STAT_AREA] >= 8).astype(np.uint8)*255
                edges |= keep[labels]
            cx, cy = x-x0, y-y0
            eh, ew = min(TILE, h-y), min(TILE, w-x)
            core = edges[cy:cy+eh, cx:cx+ew]
            base = strong[cy:cy+eh, cx:cx+ew]
            mask[y:y+eh, x:x+ew] = core
            strong_count += int(np.count_nonzero(base))
            added_count += int(np.count_nonzero(core & ~base))
            tiles += 1
    return mask, {'tile_count': tiles, 'strong_edge_pixels': strong_count, 'weak_edge_pixels': added_count}


def extract(image, options, cancel=None):
    check_cancel(cancel)
    h, w = image.shape[:2]
    small, ratio = working_image(image, options.working_side)
    gray = cv2.cvtColor(small, cv2.COLOR_RGB2GRAY)
    mask, metrics = tile_edges(gray, options.detail, options.local_contrast, options.weak_edges, cancel)
    subject = local_box(options.subject_box, image.shape, options)
    if subject is not None:
        box = pixel_box(subject, ratio, gray.shape[1], gray.shape[0])
        coarse, _ = tile_edges(gray, options.background_detail, False, False, cancel)
        x0, y0, x1, y1 = box
        coarse[y0:y1, x0:x1] = mask[y0:y1, x0:x1]
        mask = coarse
    for source_box, action in options.repairs:
        box = local_box(source_box, image.shape, options)
        x0, y0, x1, y1 = pixel_box(box, ratio, gray.shape[1], gray.shape[0])
        if action in ('recover_detail', 'simplify'):
            # The halo is also included for regional corrections; don't create
            # artificial edges by presenting a cropped rectangle to Canny.
            ax, ay, bx, by = max(0, x0-HALO), max(0, y0-HALO), min(gray.shape[1], x1+HALO), min(gray.shape[0], y1+HALO)
            patch, _ = tile_edges(gray[ay:by, ax:bx], 95 if action == 'recover_detail' else 15,
                                  action == 'recover_detail', action == 'recover_detail', cancel)
            mask[y0:y1, x0:x1] = patch[y0-ay:y1-ay, x0-ax:x1-ax]
    if options.remove_background:
        check_cancel(cancel)
        sh, sw = gray.shape
        inset = max(2, round(min(sw, sh)*.025))
        foreground = np.zeros(gray.shape, np.uint8)
        rect = pixel_box(subject, ratio, sw, sh) if subject else (inset, inset, sw-inset, sh-inset)
        x0, y0, x1, y1 = rect
        if (x0, y0, x1, y1) == (0, 0, sw, sh) or x0 >= x1 or y0 >= y1:
            raise ValueError('Для отделения фона оставьте участок фона вне рамки объекта или выключите удаление фона.')
        try:
            cv2.grabCut(small, foreground, (x0, y0, x1-x0, y1-y0), np.zeros((1, 65), np.float64),
                        np.zeros((1, 65), np.float64), 3, cv2.GC_INIT_WITH_RECT)
        except cv2.error as exc:
            check_cancel(cancel)
            raise ValueError('Не удалось отделить фон. Измените рамку объекта или выключите удаление фона.') from exc
        check_cancel(cancel)
        keep = np.isin(foreground, (cv2.GC_FGD, cv2.GC_PR_FGD)).astype(np.uint8)*255
        if not .005 < np.mean(keep > 0) < .95:
            raise ValueError('Фон не удалось отделить. Выключите удаление фона или измените рамку объекта.')
        mask &= cv2.dilate(keep, np.ones((3, 3), np.uint8))
        mask |= cv2.Canny(keep, 50, 150)
    metrics.update(working_width=gray.shape[1], working_height=gray.shape[0],
                   region_count=len(options.repairs), source_width=w, source_height=h)
    return mask, gray, small, ratio, metrics


def line_regions(gray, options, ratio, image_shape, cancel=None):
    """Keep centreline semantics for line art, including optional local repairs."""
    check_cancel(cancel)
    working = cv2.createCLAHE(clipLimit=2, tileGridSize=(8, 8)).apply(gray) if options.local_contrast else gray
    _, mask = cv2.threshold(working, 0, 255, cv2.THRESH_BINARY_INV + cv2.THRESH_OTSU)
    def remove_specks(region, minimum):
        count, labels, stats, _ = cv2.connectedComponentsWithStats(region, connectivity=8)
        keep = np.zeros(count, np.uint8)
        keep[1:] = (stats[1:, cv2.CC_STAT_AREA] >= minimum).astype(np.uint8)*255
        return keep[labels]
    subject = local_box(options.subject_box, image_shape, options)
    if subject:
        x0, y0, x1, y1 = pixel_box(subject, ratio, gray.shape[1], gray.shape[0])
        foreground = mask[y0:y1, x0:x1].copy()
        mask = remove_specks(mask, max(3, round((100-options.background_detail)/3)))
        mask[y0:y1, x0:x1] = foreground
    recovered = None
    for box, action in options.repairs:
        check_cancel(cancel)
        x0, y0, x1, y1 = pixel_box(local_box(box, image_shape, options), ratio, gray.shape[1], gray.shape[0])
        if action == 'recover_detail':
            if recovered is None:
                recovered = cv2.adaptiveThreshold(gray, 255, cv2.ADAPTIVE_THRESH_GAUSSIAN_C,
                                                  cv2.THRESH_BINARY_INV, 31, 5)
            mask[y0:y1, x0:x1] |= recovered[y0:y1, x0:x1]
        elif action == 'simplify':
            mask[y0:y1, x0:x1] = remove_specks(mask[y0:y1, x0:x1], 20)
    return mask


def hatching(gray, scale_mm, options, ratio, image_shape, cancel=None, *, max_paths=20_000):
    """Tonal horizontal strokes, with an additional diagonal pass for dark areas.

    Row spacing is defined in paper millimetres, not source pixels. Isolated
    runs are filtered and complexity is rejected rather than silently truncated.
    """
    check_cancel(cancel)
    if not options.hatching and not any(action == 'hatch' for _, action in options.repairs):
        return []
    h, w = gray.shape
    subject = local_box(options.subject_box, image_shape, options)
    subject = pixel_box(subject, ratio, w, h) if subject else None
    spacing = options.hatch_spacing_mm/scale_mm
    # A tiny drawing on a large working raster can make physical spacing
    # thousands of pixels. Bound the tonal filter kernel as well as path count.
    smooth = cv2.GaussianBlur(gray, (0, 0), max(.6, min(12., spacing/3)))
    darkness = (255-smooth.astype(np.float32))/255
    strength = np.full(gray.shape, options.hatch_strength, np.float32)
    if subject:
        strength *= .55
        x0, y0, x1, y1 = subject
        strength[y0:y1, x0:x1] = options.hatch_strength
    allowed = np.ones(gray.shape, bool) if options.hatching else np.zeros(gray.shape, bool)
    for box, action in options.repairs:
        x0, y0, x1, y1 = pixel_box(local_box(box, image_shape, options), ratio, w, h)
        if action == 'hatch': allowed[y0:y1, x0:x1] = True
        if action == 'simplify': strength[y0:y1, x0:x1] *= .5
    paths = []
    def runs(xs, ys, threshold):
        ix = np.clip(np.rint(xs).astype(int), 0, w-1)
        iy = np.clip(np.rint(ys).astype(int), 0, h-1)
        selected = (darkness[iy, ix]*strength[iy, ix] >= threshold) & allowed[iy, ix]
        # A stroke segment must survive the physical minimum-detail setting.
        transitions = np.diff(np.pad(selected.astype(np.int8), (1, 1)))
        for start, end in zip(np.flatnonzero(transitions == 1), np.flatnonzero(transitions == -1)):
            physical_length = math.hypot(xs[end-1]-xs[start], ys[end-1]-ys[start])*scale_mm
            if end-start >= 2 and physical_length >= options.min_detail_mm:
                paths.append([(min(w-1., max(0., float(xs[start]))), min(h-1., max(0., float(ys[start])))),
                              (min(w-1., max(0., float(xs[end-1]))), min(h-1., max(0., float(ys[end-1]))))])
                if len(paths) > max_paths:
                    from .trace_budget import ComplexityLimit
                    raise ComplexityLimit('hatches', len(paths), max_paths)
    for row, y in enumerate(np.arange(0, h-1+1e-9, spacing)):
        check_cancel(cancel)
        # Alternate densities: a light area gets every fourth line, a dark
        # area all lines. This conveys tone instead of a filled contour.
        threshold = (.12, .35, .23, .50)[row % 4]
        runs(np.arange(w), np.full(w, y), threshold)
    for intercept in np.arange(-(h-1), w-1+1e-9, spacing*2):
        check_cancel(cancel)
        ys = np.arange(max(0., -intercept), min(h-1, w-1-intercept)+1e-9)
        if len(ys): runs(ys+intercept, ys, .55)
    return paths
