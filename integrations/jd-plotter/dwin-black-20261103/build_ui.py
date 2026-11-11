"""Reproducible, native 800x480 DGUS II plotter panel assets and controls.

No device access. Russian text is drawn into JPEG resources, not dependent on
an installed Cyrillic font. The manifest is shared with independent tests.
"""
from __future__ import annotations

import json
from pathlib import Path
import math
from dataclasses import replace

from PIL import Image, ImageDraw, ImageFont

from dgus_binary import NumericDisplay, PageJump, TouchButton, VariableIcon, write_control_files, write_vp_init
from boot_splash import ImageAnimation, audit_boot_pages


ROOT = Path(__file__).resolve().parent
WIDTH, HEIGHT = 800, 480
BG = "#000000"
CARD = "#000000"
LINE = "#303030"
TEXT = "#E8E8E8"
MUTED = "#A0A0A0"
TEAL = "#D0D0D0"
BLUE = "#C0C0C0"
AMBER = "#C0C0C0"
RED = "#E0E0E0"
FONT = Path("C:/Windows/Fonts/segoeui.ttf")
BOLD = Path("C:/Windows/Fonts/segoeuib.ttf")
TITLES = ("Плоттер", "Перемещение", "Ручка", "Лист", "Рисование", "Настройки", "Начать рисунок", "Помощь", "Заставка")
SUBTITLES = (
    "",
    "Одно нажатие — одно перемещение на выбранный шаг",
    "",
    "Подведите ручку к двум противоположным углам",
    "",
    "",
    "",
    "",
)
NAV = ("Главная", "Двигать", "Ручка", "Лист", "Рисунок", "Настройки")


def font(size=22, bold=False):
    return ImageFont.truetype(str(BOLD if bold else FONT), size)


class Builder:
    indexed_display = False

    def __init__(self):
        self.touches = []
        self.displays = []
        self.hotspots = []
        self.texts = []
        self.icons = []
        self.numbers = []
        self.frames = []
        self.pages = {}
        self.draw = None
        self.page = 0

    def label(self, text, x, y, size=22, color=TEXT, bold=False, anchor=None):
        f = font(size, bold)
        box = self.draw.textbbox((x, y), text, font=f, anchor=anchor)
        if min(box[:2]) < 0 or box[2] > WIDTH or box[3] > HEIGHT:
            raise ValueError(f"Text outside screen: {text!r}: {box}")
        self.draw.text((x, y), text, font=f, fill=color, anchor=anchor)
        self.texts.append(dict(page=self.page, text=text, bounds=box, size=size))

    def centered(self, text, rect, size=23, color=TEXT, bold=True):
        x0, y0, x1, y1 = rect
        lines = text.split("\n")
        f = font(size, bold)
        height = len(lines) * (size + 6) - 6
        y = (y0 + y1 - height) / 2
        for line in lines:
            if self.draw.textlength(line, font=f) > x1 - x0 - 18:
                raise ValueError(f"Button label does not fit: {line!r}")
            self.label(line, (x0 + x1) / 2, y, size, color, bold, "mt")
            y += size + 6

    def button(self, rect, text, *, target=None, key=None, vp=0x1000, color=CARD, size=23, accent=False):
        self.draw.rounded_rectangle(rect, radius=10, fill=color, outline=TEAL if accent else LINE, width=1)
        self.centered(text, rect, size, BG if accent else TEXT)
        if target is not None and key is None:
            c = PageJump(self.page, rect, target)
        elif key is not None:
            c = TouchButton(self.page, rect, key, vp=vp, target_page=target)
        else:
            raise ValueError("Button needs a page or a command")
        self.touches.append(c)
        self.hotspots.append(dict(page=self.page, rect=rect, text=text, target=target, key=key, vp=vp if key is not None else None))

    def status(self, name, vp, x, y, width, height, labels, colors=None, size=20):
        """An opaque bitmap enum; group frame dimensions are intentionally equal."""
        first = len(self.frames)
        colors = colors or [MUTED] * len(labels)
        for text, color in zip(labels, colors):
            im = Image.new("RGB", (width, height), BG)
            d = ImageDraw.Draw(im)
            f = font(size, True)
            if d.textlength(text, font=f) > width - 4:
                raise ValueError(f"Status label {name!r} too long: {text!r}")
            d.text((2, height / 2), text, font=f, fill=color, anchor="lm")
            self.frames.append(im)
        info = dict(name=name, vp=vp, x=x, y=y, width=width, height=height,
                    first=first, last=len(self.frames) - 1, labels=labels)
        self.icons.append(dict(page=self.page, **info))
        self._icon(info)
        return info

    def _icon(self, info):
        self.displays.append(VariableIcon(self.page, info["vp"], info["x"], info["y"],
                                         value_min=0, value_max=len(info["labels"]) - 1,
                                         icon_min=info["first"], icon_max=info["last"],
                                         library_id=60, icon_width=info["width"], icon_height=info["height"]))

    def reused(self, info):
        self.icons.append(dict(page=self.page, **info))
        self._icon(info)

    def number(self, vp, x, y, *, decimals=2, digits=4, width=14, word=False, unit=""):
        self.displays.append(NumericDisplay(self.page, vp, x, y, font_width=width,
                                             integer_digits=digits, decimals=decimals,
                                             data_type=5 if word else 1, unit=unit))
        cells = digits + decimals + bool(decimals) + (not word) + len(unit)
        rect = [x, y, x + cells * width, y + width * 2]
        if rect[2] > WIDTH or rect[3] > HEIGHT:
            raise ValueError(f"Native numeric field exceeds screen: {rect}")
        self.numbers.append(dict(page=self.page, vp=vp, rect=rect, cells=cells, font_width=width))

    def common(self, page):
        self.page = page
        im = Image.new("RGB", (WIDTH, HEIGHT), BG)
        self.pages[page] = im
        self.draw = ImageDraw.Draw(im)
        self.label(TITLES[page], 24, 14, 29, bold=True)
        if SUBTITLES[page]:
            self.label(SUBTITLES[page], 24, 73, 18, MUTED)
        self.button((650, 15, 779, 63), "СТОП", key=14, color="#101010", size=22)
        self.button((584, 15, 634, 59), "?", target=7, size=26)
        if page == 0:
            self.link = self.status("link", 0x1111, 320, 21, 252, 32,
                ["Нет связи", "ПК подключён", "Станок подключён"], [MUTED, MUTED, TEAL], 18)
            self.result = self.status("result", 0x1110, 24, 374, 752, 30,
                ["", "Разрешите управление экраном на ПК", "Подключите станок на ПК",
                 "Дождитесь завершения операции", "Задайте лист и выберите рисунок",
                 "Проверьте версию программы", "Ошибка • подробности на ПК",
                 "Проверьте настройки станка и ручки на ПК", "Запоздалая команда отменена",
                 "Подтвердите действие на ПК"],
                [MUTED, AMBER, AMBER, BLUE, AMBER, RED, RED, AMBER, AMBER, BLUE], 19)
        else:
            self.reused(self.link)
            self.reused(self.result)
        self.draw.line((12, 410, 788, 410), fill=LINE, width=1)
        section = 4 if page == 6 else page
        for index, title in enumerate(NAV):
            rect = (12 + index * 130, 420, 128 + index * 130, 472)
            self.centered(title, rect, 18, TEXT if section == index else MUTED)
            self.touches.append(PageJump(page, rect, index))
            self.hotspots.append(dict(page=page, rect=rect, text=title, target=index, key=None, vp=None))
            if section == index:
                self.draw.rounded_rectangle((rect[0] + 28, 469, rect[2] - 28, 471), radius=1, fill=TEAL)
        return im

    def home(self):
        self.common(0)
        for i, (title, target) in enumerate((("Двигать", 1), ("Лист", 3), ("Рисовать", 4))):
            x = 24 + i * 256
            self.button((x, 115, x + 240, 267), title, target=target, size=29)
            self.label(f"0{i+1}", x + 18, 131, 18, MUTED)
        self.label("Позиция · мм", 24, 294, 18, MUTED)
        self.label("X", 28, 331, 21, MUTED, True)
        self.number(0x1102, 62, 331, width=16)
        self.label("Y", 275, 331, 21, MUTED, True)
        self.number(0x1104, 307, 331, width=16)
        self.status("home_arm", 0x1112, 524, 327, 252, 32,
                    ["Управление закрыто", "Управление разрешено"], [MUTED, TEAL], 17)

    def move(self):
        self.common(1)
        self.status("step", 0x1200, 24, 103, 228, 30,
                    [f"Шаг: {x} мм" for x in ("0,1", "1", "5", "10", "50", "100")], [TEAL] * 6, 22)
        for i, text in enumerate(("0,1 мм", "1 мм", "5 мм", "10 мм", "50 мм", "100 мм")):
            x, y = 24 + i % 2 * 118, 146 + i // 2 * 68
            self.button((x, y, x + 106, y + 55), text, key=i, vp=0x1200, size=20)
        for rect, text, key in (
            ((367, 144, 464, 202), "Вверх", 3),
            ((267, 211, 364, 269), "Влево", 1),
            ((467, 211, 564, 269), "Вправо", 2),
            ((367, 278, 464, 336), "Вниз", 4),
        ):
            self.button(rect, text, key=key, size=19)
        self.draw.ellipse((409, 234, 421, 246), fill=LINE)
        self.label("Скорость", 594, 105, 20, MUTED)
        self.number(0x1201, 704, 106, decimals=0, digits=3, width=11, word=True, unit="%")
        for i, n in enumerate((10, 25, 50, 75, 100)):
            x, y = 597 + (i % 2) * 94, 146 + (i // 2) * 68
            end_x = 775 if i == 4 else x + 84
            self.button((x, y, end_x, y + 55), f"{n}%", key=n, vp=0x1201, size=20)

    def pen(self):
        self.common(2)
        self.button((24, 121, 384, 228), "Поднять", key=19, size=30)
        self.button((416, 121, 776, 228), "Опустить", key=20, size=30)
        self.status("pen", 0x110D, 24, 245, 752, 36,
                    ["Положение неизвестно", "Команда: поднята", "Команда: опущена"],
                    [MUTED, TEAL, AMBER], 24)
        self.label("Автоматический подъём при перемещении включается в программе.", 24, 302, 20, MUTED)
        self.label("На экране — команда, не показание датчика.", 24, 337, 17, MUTED)

    def paper(self):
        self.common(3)
        self.status("calibration", 0x110C, 24, 103, 752, 32,
                    ["Углы не заданы", "Первый угол сохранён", "Лист готов"],
                    [AMBER, BLUE, TEAL], 22)
        self.button((24, 148, 384, 235), "1. Верхний левый", key=10, size=25)
        self.button((416, 148, 776, 235), "2. Нижний правый", key=11, size=25)
        self.button((24, 250, 320, 306), "Подвести стрелками", target=1, size=21)
        self.label("Ширина", 351, 254, 17, MUTED)
        self.number(0x1106, 350, 280, width=12)
        self.label("Высота", 586, 254, 17, MUTED)
        self.number(0x1108, 585, 280, width=12)
        self.label("Размеры в мм. После потери позиции задайте углы заново.", 24, 337, 18, MUTED)

    def drawing(self):
        self.common(4)
        self.status("job", 0x110F, 24, 105, 480, 32,
                    ["Нет рисунка", "Рисунок готов"], [MUTED, TEAL], 24)
        self.status("busy", 0x110A, 550, 106, 228, 30,
                    ["Ожидание", "Рисование…"], [MUTED, BLUE], 21)
        self.label("Выполнено", 24, 151, 19, MUTED)
        self.number(0x110B, 155, 149, width=16, decimals=0, digits=3, word=True, unit="%")
        self.button((24, 200, 384, 265), "Открыть SVG", key=16, size=24)
        self.button((416, 200, 776, 265), "Тестовый квадрат", key=15, size=24)
        self.button((24, 283, 384, 350), "К началу рисунка", key=12, size=24)
        self.button((416, 283, 776, 350), "Начать", target=6, color="#080808", size=26)

    def settings(self):
        self.common(5)
        self.status("hold", 0x110E, 24, 106, 440, 32,
                    ["Удержание: выкл.", "Удержание: вкл."], [MUTED, TEAL], 22)
        self.status("armed", 0x1112, 480, 108, 296, 28,
                    ["Управление закрыто", "Управление разрешено"], [MUTED, TEAL], 18)
        self.button((24, 151, 384, 218), "Переключить удержание", key=21, size=23)
        self.label("Отключение удержания может", 416, 158, 20, MUTED)
        self.label("сбросить привязку листа.", 416, 188, 20, MUTED)
        self.button((24, 237, 384, 306), "Помощник", key=17, size=25)
        self.button((416, 237, 776, 306), "Камера", key=18, size=24)
        self.label("Помощник и камера открываются на компьютере.", 24, 337, 19, MUTED)

    def confirm(self):
        self.common(6)
        self.label("Начать текущий рисунок?", 400, 117, 31, TEXT, True, "mt")
        self.label("Убедитесь, что лист закреплён, а ручка установлена.", 400, 172, 22, MUTED, anchor="mt")
        self.label("Если на ПК появится подтверждение — ответьте в его окне.", 400, 211, 21, MUTED, anchor="mt")
        self.button((24, 269, 384, 345), "Назад", target=4, size=27)
        self.button((416, 269, 776, 345), "Да, начать", key=13, target=4, color="#080808", size=27)

    def help(self):
        self.common(7)
        for i, (title, detail) in enumerate((
            ("Подключите экран", "На ПК: «Экран DWIN» → его порт → разрешить управление."),
            ("Задайте лист", "Подведите ручку к верхнему левому и нижнему правому углам."),
            ("Выберите рисунок", "SVG, помощник, фотография и предпросмотр — в программе на ПК."),
            ("Запустите рисование", "Кнопка «СТОП» останавливает задание через работающий ПК."),
        )):
            y = 107 + i * 64
            self.label(f"{i + 1}", 26, y, 28, TEAL, True)
            self.label(title, 70, y, 22, TEXT, True)
            self.label(detail, 70, y + 30, 17, MUTED)

    def splash(self):
        # Original artwork with its white background replaced by black.
        # Keep the whole composition; never crop to force the LCD aspect ratio.
        self.page = 8
        original = Image.open(ROOT / "assets" / "jerusalem-dynamics-black.png").convert("RGB")
        scale = min(WIDTH / original.width, HEIGHT / original.height)
        resized = original.resize((round(original.width * scale), round(original.height * scale)), Image.Resampling.LANCZOS)
        canvas = Image.new("RGB", (WIDTH, HEIGHT), "black")
        canvas.paste(resized, ((WIDTH-resized.width)//2, (HEIGHT-resized.height)//2))
        self.pages[8] = canvas

    def arrange_boot_pages(self):
        """Reserve native page 0 for a PC-independent startup animation.

        Semantic builder indices stay stable; only final native page IDs move.
        The final page of an animation must be larger than its starting page.
        """
        mapping = {8: 0, **{p: p + 1 for p in range(8)}}
        self.pages = dict(sorted((mapping[p], im) for p, im in self.pages.items()))
        self.touches = [replace(c, page=mapping[c.page],
                        target_page=None if c.target_page is None else mapping[c.target_page])
                        for c in self.touches]
        self.displays = [replace(c, page=mapping[c.page]) for c in self.displays]
        self.displays.append(ImageAnimation())
        # Local page switch only. No key VP, serial packet, or machine action.
        # Also provides access if an old installed kernel ignores the animation.
        self.touches.append(PageJump(0, (0, 0, 799, 479), 1))
        for item in self.hotspots:
            item["page"] = mapping[item["page"]]
            if item["target"] is not None:
                item["target"] = mapping[item["target"]]
        for collection in (self.icons, self.numbers, self.texts):
            for item in collection:
                item["page"] = mapping[item["page"]]
        self.hotspots.append(dict(page=0, rect=(0, 0, 799, 479),
                                  text="Открыть меню", target=1, key=None, vp=None))
        self.page_titles = [TITLES[8], *TITLES[:8]]
        self.boot_report = audit_boot_pages(self.displays, self.touches, page_count=len(self.pages))

    def save(self):
        page_dir = ROOT / "backgrounds"
        icon_dir = ROOT / "status-icons"
        preview_dir = ROOT / "preview"
        for directory in (page_dir, icon_dir, preview_dir):
            directory.mkdir(exist_ok=True)
            # Clear only our numbered generated PNGs, never originals or user
            # assets, so old frame IDs cannot silently survive a rebuild.
            for old in directory.glob("*.png"):
                if old.stem.isdigit():
                    old.unlink()
        for i, im in self.pages.items():
            im.save(page_dir / f"{i:03d}.png")
        for i, im in enumerate(self.frames):
            im.save(icon_dir / f"{i:03d}.png")
        write_control_files(ROOT / "DWIN_SET", self.touches, self.displays,
                            page_count=len(self.pages), width=WIDTH, height=HEIGHT,
                            indexed_display=self.indexed_display)
        write_vp_init(ROOT / "DWIN_SET")
        # Independent of the runtime hardware: sample values only, clearly demo.
        samples = {0x1111: 2, 0x1110: 0, 0x1200: 1, 0x110D: 1, 0x110C: 2,
                   0x110F: 1, 0x110A: 0, 0x110E: 1, 0x1112: 1}
        numeric = {0x1102: "12.50", 0x1104: "48.00", 0x1106: "210.00",
                   0x1108: "297.00", 0x1201: "50%", 0x110B: "36%"}
        previews = []
        for i, im in self.pages.items():
            sample = im.copy()
            for icon in (x for x in self.icons if x["page"] == i):
                value = samples.get(icon["vp"], 0)
                sample.paste(self.frames[icon["first"] + value], (icon["x"], icon["y"]))
            d = ImageDraw.Draw(sample)
            for c in (x for x in self.displays if isinstance(x, NumericDisplay) and x.page == i):
                # Match the native resident font's fixed cell width. A
                # proportional preview previously hid possible overlaps.
                f = ImageFont.truetype("C:/Windows/Fonts/consola.ttf", c.font_width * 2)
                for j, char in enumerate(numeric.get(c.vp, "0")):
                    d.text((c.x + (j + 0.5) * c.font_width, c.y), char, font=f, fill=TEXT, anchor="ma")
            sample.save(preview_dir / f"{i:02d}.png")
            previews.append(sample)
        sheet = Image.new("RGB", (1600, 276 * math.ceil(len(previews)/4)), BG)
        d = ImageDraw.Draw(sheet)
        for i, im in enumerate(previews):
            x, y = i % 4 * 400, i // 4 * 276
            sheet.paste(im.resize((400, 240), Image.Resampling.LANCZOS), (x, y + 30))
            d.text((x + 12, y + 4), f"{i}: {self.page_titles[i]}", font=font(20, True), fill=TEXT)
        # Prefer individual full-size previews; contact sheet is a quick overview.
        sheet.save(ROOT / "interface-overview.png")
        manifest = dict(width=WIDTH, height=HEIGHT, pages=self.page_titles,
                        protocol_version=1, library_status=60,
                        hotspots=self.hotspots, icons=self.icons,
                        static_text=self.texts, numeric_fields=self.numbers, frames=len(self.frames),
                        operational_pages=list(range(1, 9)), navigation_pages=list(range(1, 7)), help_page=8,
                        splash_page=0, home_page=1, splash_duration_ms=2000,
                        boot_animation=self.boot_report,
                        theme="black-monochrome", splash_tap_to_skip=True,
                        note="Preview numbers are illustrative, not measured hardware state.")
        (ROOT / "ui-manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
        html = (ROOT / "preview-template.html").read_text(encoding="utf-8")
        html = html.replace("__MANIFEST__", json.dumps(manifest, ensure_ascii=False))
        (ROOT / "preview.html").write_text(html, encoding="utf-8")
        print(json.dumps(dict(pages=len(self.pages), touch_controls=len(self.touches), display_controls=len(self.displays), status_frames=len(self.frames))))


if __name__ == "__main__":
    b = Builder()
    for method in (b.home, b.move, b.pen, b.paper, b.drawing, b.settings, b.confirm, b.help, b.splash):
        method()
    b.arrange_boot_pages()
    b.save()
