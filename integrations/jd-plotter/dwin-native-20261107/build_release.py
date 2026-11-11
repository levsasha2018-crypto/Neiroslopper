"""Build the MKS-operated DWIN panel using the existing DWIN toolchain."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import sys
import zipfile

from PIL import Image, ImageDraw

ROOT = Path(__file__).resolve().parent
PREVIOUS = ROOT.parent / "dwin-black-20261103"
HOST = ROOT.parent / "plotter-v22-dwin9600-20261106" / "PROGRAM"
sys.path.extend((str(PREVIOUS), str(HOST)))

import build_ui as ui
import verify_package
from compile_icl import compile_icl
from dwin_bridge import STEPS


# Keep the old logical and native IDs. The inherited boot mapping maps these
# four logical IDs to native pages 1, 2, 4 and 7, with the splash at native 0.
WORK, MOVE, PAPER, CONFIRM = 0, 1, 3, 6
TABS = ((WORK, "Работа"), (MOVE, "Движение"), (PAPER, "Лист"))
RESERVED = (2, 4, 5, 7)
TITLES = dict((*TABS, (CONFIRM, "Начать рисование")))
PANEL = "#0c0c0c"
BUTTON = "#181818"


class Panel(ui.Builder):
    def panel(self, rect):
        self.draw.rounded_rectangle(rect, radius=12, fill=PANEL, outline=ui.LINE)

    def common(self, page):
        self.page = page
        image = Image.new("RGB", (ui.WIDTH, ui.HEIGHT), ui.BG)
        self.pages[page] = image
        self.draw = ImageDraw.Draw(image)
        self.label(TITLES[page], 24, 15, 31, bold=True)
        self.draw.line((24, 72, 776, 72), fill=ui.LINE)

        if page == WORK:
            self.connection = self.status("link", 0x1111, 407, 15, 236, 24,
                ["Нет связи с MKS", "MKS не готова", "MKS готова"], size=17)
            self.authorization = self.status("armed", 0x1112, 407, 40, 236, 24,
                ["Управление закрыто", "Управление разрешено"], size=17)
            self.result = self.status("result", 0x1110, 24, 375, 752, 31,
                ["", "Управление заблокировано", "Нет связи с MKS",
                 "Станок занят", "Проверьте углы листа и рисунок",
                 "Команда не поддерживается", "Команда не выполнена",
                 "Проверьте станок и настройки", "Запрос отменён",
                 "Продолжите на ПК", "Ожидаю команду от ПК",
                 "Проверяю связь с ПК", "ПК ответил", "ПК не ответил"], size=19)
        else:
            for info in (self.connection, self.authorization, self.result):
                self.reused(info)

        stop = (652, 12, 776, 63)
        self.button(stop, "СТОП", key=14, color="#381b1e", size=22)
        self.draw.rounded_rectangle(stop, radius=10, outline="#b54446", width=2)
        self.draw.line((24, 414, 776, 414), fill=ui.LINE)
        active = WORK if page == CONFIRM else page
        for index, (target, title) in enumerate(TABS):
            x = 24 + index * 256
            rect = (x, 425, x + 240, 472)
            self.button(rect, title, target=target, color=BUTTON if target == active else ui.BG, size=21)
            if target == active:
                self.draw.rounded_rectangle((x + 87, 467, x + 153, 469), radius=1, fill=ui.TEAL)

    def home(self):
        self.common(WORK)
        self.panel((24, 88, 343, 354))
        self.label("Текущий рисунок", 42, 105, 22, bold=True)
        self.status("job", 0x110F, 40, 141, 284, 31,
                    ["Нет рисунка", "Рисунок готов"], [ui.MUTED, ui.TEXT], 22)
        self.status("busy", 0x110A, 40, 176, 284, 28,
                    ["Ожидание", "Выполняется…"], [ui.MUTED, ui.TEXT], 19)
        self.label("Выполнено", 42, 211, 18, ui.MUTED)
        self.number(0x110B, 198, 210, width=14, decimals=0, digits=3, word=True, unit="%")
        self.draw.line((42, 252, 324, 252), fill=ui.LINE)
        self.label("Позиция, мм", 42, 262, 18, ui.MUTED)
        self.label("X", 42, 304, 20, ui.MUTED, True)
        self.number(0x1102, 67, 302, width=13)
        self.label("Y", 187, 304, 20, ui.MUTED, True)
        self.number(0x1104, 210, 302, width=13)

        self.panel((359, 88, 776, 354))
        self.label("Действия", 377, 105, 22, bold=True)
        self.button((377, 145, 555, 201), "Открыть SVG", key=16, color=BUTTON, size=21)
        self.button((570, 145, 758, 201), "Тестовый\nквадрат", key=15, color=BUTTON, size=20)
        self.button((377, 213, 555, 269), "К началу", key=12, color=BUTTON, size=22)
        self.button((570, 213, 758, 269), "Рисовать", target=CONFIRM, color=ui.TEAL, size=24, accent=True)
        self.button((377, 281, 758, 337), "Проверить ПК", key=22, color=BUTTON, size=22)

    def move(self):
        self.common(MOVE)
        self.panel((24, 88, 294, 354))
        self.panel((306, 88, 776, 354))
        self.label("Перемещение", 42, 106, 22, bold=True)
        for rect, text, key in (
            ((114, 149, 200, 209), "↑", 3),
            ((42, 215, 112, 275), "←", 1),
            ((202, 215, 272, 275), "→", 2),
            ((114, 281, 200, 341), "↓", 4),
        ):
            self.button(rect, text, key=key, color=BUTTON, size=30)
        self.draw.ellipse((153, 241, 161, 249), fill=ui.MUTED)

        self.label("Шаг, мм", 324, 107, 22, bold=True)
        step_labels = [f"{value:g}".replace(".", ",") + " мм" for value in STEPS]
        self.status("step", 0x1200, 560, 106, 192, 29, step_labels, [ui.TEXT] * len(STEPS), 21)
        for i, value in enumerate(STEPS):
            x = 324 + 73 * i
            self.button((x, 145, x + 66, 200), f"{value:g}".replace(".", ","), key=i, vp=0x1200, color=BUTTON, size=21)
        self.draw.line((324, 221, 758, 221), fill=ui.LINE)
        self.label("Скорость", 324, 240, 22, bold=True)
        self.number(0x1201, 560, 239, decimals=0, digits=3, width=14, word=True, unit="%")
        for i, value in enumerate((10, 25, 50, 75, 100)):
            x = 324 + 88 * i
            self.button((x, 282, x + 80, 337), f"{value}%", key=value, vp=0x1201, color=BUTTON, size=21)

    def paper(self):
        self.common(PAPER)
        self.panel((24, 88, 343, 354))
        self.panel((359, 88, 776, 354))
        self.label("Границы листа", 42, 105, 22, bold=True)
        self.status("calibration", 0x110C, 40, 141, 284, 29,
                    ["Углы не заданы", "Первый угол сохранён", "Лист готов"], size=19)
        self.draw.rounded_rectangle((54, 190, 315, 260), radius=5, outline="#626262", width=2)
        for x, y, text in ((55, 192, "1"), (313, 258, "2")):
            self.draw.ellipse((x - 12, y - 12, x + 12, y + 12), fill=BUTTON, outline=ui.MUTED)
            self.label(text, x, y, 14, bold=True, anchor="mm")
        self.label("Ширина, мм", 42, 284, 17, ui.MUTED)
        self.number(0x1106, 42, 311, width=12)
        self.label("Высота, мм", 187, 284, 17, ui.MUTED)
        self.number(0x1108, 187, 311, width=12)

        self.label("Подведите ручку и сохраните угол", 377, 105, 21, bold=True)
        self.button((377, 145, 758, 204), "1. Верхний левый", key=10, color=BUTTON, size=23)
        self.button((377, 212, 758, 271), "2. Нижний правый", key=11, color=BUTTON, size=23)
        self.button((377, 279, 758, 337), "Подвести стрелками", target=MOVE, size=21)

    def confirm(self):
        self.common(CONFIRM)
        self.panel((116, 102, 684, 350))
        self.label("Начать текущий рисунок?", 400, 131, 30, bold=True, anchor="mt")
        self.label("Убедитесь, что лист закреплён и ручка установлена.", 400, 188, 20, ui.MUTED, anchor="mt")
        self.label("MKS дождётся подтверждения на ПК.", 400, 220, 20, ui.MUTED, anchor="mt")
        self.button((140, 272, 389, 329), "Отмена", target=WORK, color=BUTTON, size=23)
        self.button((410, 272, 660, 329), "Да, начать", key=13, target=WORK, color=ui.TEAL, size=23, accent=True)


def build():
    ui.ROOT = ROOT
    panel = Panel()
    for method in (panel.home, panel.move, panel.paper, panel.confirm):
        method()
    for page in RESERVED:
        panel.pages[page] = Image.new("RGB", (ui.WIDTH, ui.HEIGHT), ui.BG)
    # Reuse the already built black logo artwork, pixel for pixel.
    with Image.open(PREVIOUS / "backgrounds/000.png") as image:
        panel.pages[8] = image.convert("RGB")
    panel.arrange_boot_pages()
    panel.page_titles = ["Заставка", *(TITLES.get(page, "Резерв") for page in range(8))]
    panel.save()

    manifest_path = ROOT / "ui-manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest.pop("help_page")
    manifest.update(operational_pages=[WORK + 1, MOVE + 1, PAPER + 1, CONFIRM + 1],
                    navigation_pages=[page + 1 for page, _ in TABS],
                    reserved_pages=[page + 1 for page in RESERVED],
                    theme="black", hardware_tested=False, build_date="2026-11-07",
                    controller="MKS DLC32 V2.2", waiting_result=10,
                    pc_check={"vp": 0x1000, "key": 22, "pending": 11, "answered": 12, "timeout": 13})
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    preview = (ROOT / "preview-template.html").read_text(encoding="utf-8")
    (ROOT / "preview.html").write_text(preview.replace("__MANIFEST__", json.dumps(manifest, ensure_ascii=False)), encoding="utf-8")
    overview = Image.new("RGB", (ui.WIDTH * 2, ui.HEIGHT * 2), ui.BG)
    for index, page in enumerate(manifest["operational_pages"]):
        with Image.open(ROOT / "preview" / f"{page:02d}.png") as screenshot:
            overview.paste(screenshot, (index % 2 * ui.WIDTH, index // 2 * ui.HEIGHT))
    overview.save(ROOT / "interface-overview.png")

    for folder, name in (("backgrounds", "32_Backgrounds.ICL"), ("status-icons", "60_Status.ICL")):
        compile_icl(ROOT / folder, ROOT / "DWIN_SET" / name)
    verify_package.verify()
    hashes = {path.name: hashlib.sha256(path.read_bytes()).hexdigest()
              for path in sorted((ROOT / "DWIN_SET").iterdir())}
    (ROOT / "SHA256.json").write_text(json.dumps(hashes, indent=2), encoding="utf-8")
    with zipfile.ZipFile(ROOT / "DWIN_NATIVE_800x480_20261107.zip", "w", zipfile.ZIP_DEFLATED) as archive:
        for path in sorted((ROOT / "DWIN_SET").iterdir()):
            archive.write(path, "DWIN_SET/" + path.name)
        for name in ("README-RU.md", "SHA256.json", "package-verification.json"):
            archive.write(ROOT / name, name)


if __name__ == "__main__":
    build()
