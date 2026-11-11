"""Build the 800×480 DWIN assistant interface without touching a device."""
from __future__ import annotations

from collections import Counter
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import struct
import sys
import zipfile

from PIL import Image, ImageDraw, ImageFont

ROOT = Path(__file__).resolve().parent
OUTPUTS = ROOT.parent
NATIVE = OUTPUTS / "dwin-native-20261107"
PREVIOUS = OUTPUTS / "dwin-black-20261103"
sys.path.insert(0, str(NATIVE))
native_spec = importlib.util.spec_from_file_location("native_layout", NATIVE / "build_release.py")
native = importlib.util.module_from_spec(native_spec)
native_spec.loader.exec_module(native)
ui = native.ui

from audit_icl import audit_icl
from dgus_binary import (
    BasicGraphics, INDEXED_DISPLAY_MAGIC, PLOTTER_VP_DEFAULTS, VariableIcon, build_vp_init,
    parse_display_file, parse_touch_file, validate_flash_allocations, write_vp_init,
)


# Logical IDs increment by one in the inherited layout. None of the
# existing work/move/paper/confirmation page IDs change on the display.
WORK, MOVE, PAPER, CONFIRM = native.WORK, native.MOVE, native.PAPER, native.CONFIRM
RU, WAIT, PREVIEW, EN = 2, 4, 5, 7
FEATURES = {RU: "Помощник", WAIT: "Ответ помощника", PREVIEW: "Предпросмотр листа", EN: "Помощник · EN"}
COMMANDS = {
    "assistant": 30, "send": 21, "preview": 23, "cancel": 24,
    "leave_preview": 25, "voice": 26, "english": 27, "russian": 28, "more_text": 29,
}
JOG_KEYS = {"↑": 4, "↓": 3, "←": 2, "→": 1}
KEYBOARD_VP, KEYBOARD_PREFIX = 0x1000, 0x4000
STAGE_VP, PREVIEW_STAGE_VP = 0x1300, 0x1301
CONNECTION_VP = 0x1302
QUESTION_VP, ANSWER_VP, GRAPH_VP = 0x1400, 0x1480, 0x2000
FONT_ID, FONT_WIDTH, FONT_HEIGHT = 60, 24, 24
GLYPH_BASE = 64
TEXT_COLUMNS, TEXT_CELLS = 28, 56
GRAPH_RECT = (190, 90, 609, 367)
QUESTION_RECT = (43, 104, 756, 179)
ANSWER_RECT = (48, 174, 751, 306)
STAGES = ["Введите вопрос", "Отправка на ПК…", "Ожидание ответа…", "Запись с микрофона ПК…",
          "Распознаю голос…", "Ответ получен", "Не удалось выполнить запрос"]
PREVIEW_STAGES = ["На ПК ещё нет рисунка", "Загружаю лист с ПК…",
                  "Текущий рисунок · упрощённый вид", "Нет предпросмотра · проверьте связь с ПК"]
CONNECTION_STAGES = ["Запуск · ожидание MKS", "Подключение к S24 Ultra…",
                     "Подключение к пульту на ПК…", "Связь с ПК подтверждена"]
KEY_ROWS = {RU: ("ЁЙЦУКЕНГШЩЗХ", "ФЫВАПРОЛДЖЭЪ", "ЯЧСМИТЬБЮ"),
            EN: ("QWERTYUIOP", "ASDFGHJKL", "ZXCVBNM")}
COMMON_KEYS = "1234567890?."
FONT_LICENSE = "LICENSE-DEJAVU.txt"
INITIAL_VP = {**PLOTTER_VP_DEFAULTS, 0x1001: 0, STAGE_VP: 0, PREVIEW_STAGE_VP: 0,
              CONNECTION_VP: 0, GRAPH_VP: 0xFF00,
              **{vp + i: 0 for vp in (QUESTION_VP, ANSWER_VP) for i in range(TEXT_CELLS)}}


class Panel(native.Panel):
    indexed_display = True

    def button(self, rect, text, *, key=None, **options):
        if self.page == MOVE and text in JOG_KEYS:
            key = JOG_KEYS[text]
        super().button(rect, text, key=key, **options)

    def home(self):
        self.common(WORK)
        self.startup_connection = self.status("startup-connection", CONNECTION_VP, 24, 355, 580, 16,
            CONNECTION_STAGES, [ui.MUTED, ui.MUTED, ui.TEXT, ui.TEXT], size=14)
        self.panel((24, 88, 343, 354))
        self.label("Текущий рисунок", 42, 105, 22, bold=True)
        self.status("job", 0x110F, 40, 141, 284, 31, ["Нет рисунка", "Рисунок готов"],
                    [ui.MUTED, ui.TEXT], 22)
        self.status("busy", 0x110A, 40, 176, 284, 28, ["Ожидание", "Выполняется…"],
                    [ui.MUTED, ui.TEXT], 19)
        self.label("Выполнено", 42, 211, 18, ui.MUTED)
        self.number(0x110B, 198, 210, width=14, decimals=0, digits=3, word=True, unit="%")
        self.draw.line((42, 252, 324, 252), fill=ui.LINE)
        self.label("Позиция, мм", 42, 262, 18, ui.MUTED)
        self.label("X", 42, 304, 20, ui.MUTED, True)
        self.number(0x1102, 67, 302, width=13)
        self.label("Y", 187, 304, 20, ui.MUTED, True)
        self.number(0x1104, 210, 302, width=13)
        self.panel((359, 88, 776, 354))
        self.label("Действия", 377, 102, 22, bold=True)
        self.button((377, 135, 555, 178), "Открыть SVG", key=16, color=native.BUTTON, size=20)
        self.button((570, 135, 758, 178), "Тестовый квадрат", key=15, color=native.BUTTON, size=17)
        self.button((377, 190, 555, 233), "К началу", key=12, color=native.BUTTON, size=20)
        self.button((570, 190, 758, 233), "Рисовать", key=13, color=ui.TEAL, size=22, accent=True)
        self.button((377, 245, 555, 288), "Помощник", key=COMMANDS["assistant"], color=native.BUTTON, size=20)
        self.button((570, 245, 758, 288), "Предпросмотр", key=COMMANDS["preview"], color=native.BUTTON, size=20)
        self.button((377, 300, 758, 343), "Проверить ПК", key=22, color=native.BUTTON, size=20)

    def paper(self):
        super().paper()
        self.button((42, 346, 324, 373), "Снять блокировку", key=31, size=17)

    def feature(self, page):
        self.page = page
        image = Image.new("RGB", (ui.WIDTH, ui.HEIGHT), ui.BG)
        self.pages[page] = image
        self.draw = ImageDraw.Draw(image)
        self.label(FEATURES[page], 24, 15, 31, bold=True)
        if page in (RU, EN):
            self.label("UI 6", 570, 29, 15, ui.MUTED)
        self.reused({**self.startup_connection, "y": 56})
        self.draw.line((24, 72, 776, 72), fill=ui.LINE)
        self.draw.line((24, 414, 776, 414), fill=ui.LINE)
        self.button((652, 12, 776, 63), "СТОП", key=14, color="#381b1e", size=22)
        self.draw.rounded_rectangle((652, 12, 776, 63), radius=10, outline="#b54446", width=2)

    def field(self, vp, rect):
        for i in range(TEXT_CELLS):
            x = rect[0] + (i % TEXT_COLUMNS) * FONT_WIDTH
            y = rect[1] + (i // TEXT_COLUMNS) * FONT_HEIGHT
            self.displays.append(VariableIcon(self.page, vp + i, x, y,
                                              value_min=0, value_max=255, icon_min=GLYPH_BASE, icon_max=GLYPH_BASE + 255,
                                              library_id=FONT_ID, icon_width=FONT_WIDTH, icon_height=FONT_HEIGHT))

    def compose(self, page):
        self.feature(page)
        self.status("compose-stage", STAGE_VP, 24, 76, 752, 23, STAGES, size=17)
        self.panel((24, 101, 776, 185))
        self.field(QUESTION_VP, QUESTION_RECT)
        for row, letters in enumerate((*KEY_ROWS[page], COMMON_KEYS)):
            gap = 6
            cell = min(67, (752 - (len(letters) - 1) * gap) // len(letters))
            x0 = (ui.WIDTH - (cell + gap) * len(letters) + gap) // 2
            for col, character in enumerate(letters):
                x, y = x0 + col * (cell + gap), 192 + row * 52
                self.button((x, y, x + cell, y + 45), character, key=KEYBOARD_PREFIX | ord(character),
                            vp=KEYBOARD_VP, color=native.BUTTON, size=22)
        for rect, label, key, vp in (
            ((24, 425, 112, 472), "Меню", COMMANDS["cancel"], 0x1000),
            ((120, 425, 176, 472), "EN" if page == RU else "RU",
             COMMANDS["english" if page == RU else "russian"], 0x1000),
            ((184, 425, 241, 472), "←", KEYBOARD_PREFIX | 8, KEYBOARD_VP),
            ((249, 425, 354, 472), "Очистить", KEYBOARD_PREFIX | 0x1B, KEYBOARD_VP),
            ((362, 425, 462, 472), "Пробел", KEYBOARD_PREFIX | 32, KEYBOARD_VP),
            ((470, 425, 591, 472), "Голос", COMMANDS["voice"], 0x1000),
            ((599, 425, 776, 472), "Отправить", COMMANDS["send"], 0x1000),
        ):
            self.button(rect, label, key=key, vp=vp, target=WORK if key == COMMANDS["cancel"] else None,
                        color=ui.TEAL if key == COMMANDS["send"] else native.BUTTON, size=19,
                        accent=key == COMMANDS["send"])

    def waiting(self):
        self.feature(WAIT)
        self.status("assistant-stage", STAGE_VP, 24, 92, 752, 38, STAGES,
                    [ui.MUTED, ui.TEXT, ui.TEXT, ui.TEXT, ui.TEXT, ui.TEXT, "#ed9b9c"], size=27)
        self.panel((24, 154, 776, 329))
        self.field(ANSWER_VP, ANSWER_RECT)
        self.label("Голос записывает микрофон подключённого ПК.", 24, 349, 17, ui.MUTED)
        self.label("Если ответ пуст, проверьте связь с ПК.", 24, 376, 17, ui.MUTED)
        for rect, label, name in (
            ((24, 425, 144, 472), "Меню", "cancel"),
            ((152, 425, 342, 472), "Голос / стоп", "voice"),
            ((350, 425, 559, 472), "Новый вопрос", "assistant"),
            ((567, 425, 776, 472), "Ещё текст", "more_text"),
        ):
            self.button(rect, label, key=COMMANDS[name], target=WORK if name == "cancel" else None,
                        color=native.BUTTON, size=19)

    def preview(self):
        self.feature(PREVIEW)
        self.panel((183, 82, 616, 374))
        self.displays.append(BasicGraphics(self.page, GRAPH_VP, GRAPH_RECT))
        self.status("sheet-stage", PREVIEW_STAGE_VP, 24, 377, 752, 29, PREVIEW_STAGES, size=18)
        self.button((24, 425, 265, 472), "Меню", key=COMMANDS["leave_preview"], target=WORK,
                    color=native.BUTTON, size=20)
        self.button((535, 425, 776, 472), "Обновить лист", key=COMMANDS["preview"], color=native.BUTTON, size=20)


def build_font(directory):
    # Use the same native icon renderer as the proven status labels.
    font_path = Path(os.environ.get("DWIN_DEJAVU_FONT", ROOT / "DejaVuSansMono.ttf"))
    if not font_path.is_file():
        raise FileNotFoundError("DejaVu Sans Mono is required to build the Cyrillic DGUS font")
    face = ImageFont.truetype(str(font_path), 21)
    glyphs = ROOT / "status-icons"
    assert len(list(glyphs.glob("*.png"))) <= GLYPH_BASE
    for code in range(256):
        character = bytes([code]).decode("cp1251", errors="replace")
        glyph = Image.new("RGB", (FONT_WIDTH, FONT_HEIGHT), native.PANEL)
        if code != 0x98 and character.isprintable():
            ImageDraw.Draw(glyph).text((FONT_WIDTH // 2, 18), character, font=face, fill="#eeeeee", anchor="ms")
        glyph.save(glyphs / f"{GLYPH_BASE + code:03d}.png")


def verify(manifest, directory):
    pages = len(manifest["pages"])
    touches = parse_touch_file((directory / "13_Touch.BIN").read_bytes())
    display_data = (directory / "14_Display.BIN").read_bytes()
    assert display_data.startswith(INDEXED_DISPLAY_MAGIC)
    displays = parse_display_file(display_data, page_count=pages)
    assert pages == 9
    assert len(touches) == len(manifest["hotspots"])
    native.verify_package.audit_native_boot(manifest, touches, displays)
    native.verify_package.audit_dynamic_layout(manifest, displays)
    saved = parse_touch_file((NATIVE / "DWIN_SET/13_Touch.BIN").read_bytes())
    def signature(control):
        return control["page"], control.get("vp"), control.get("key"), control["target_page"]
    assert Counter(signature(c) for c in saved
                   if c["page"] != CONFIRM + 1 and c["target_page"] != CONFIRM + 1) <= Counter(map(signature, touches))
    assert not any(c["target_page"] == CONFIRM + 1 for c in touches)
    assert any(c["page"] == WORK + 1 and c.get("key") == 13 and c["target_page"] == 0xFFFF for c in touches)
    assert {h["text"]: h["key"] for h in manifest["hotspots"]
            if h["page"] == MOVE + 1 and h["text"] in JOG_KEYS} == JOG_KEYS
    for page, rows in KEY_ROWS.items():
        keys = [c["key"] for c in touches if c["page"] == page + 1 and c.get("vp") == KEYBOARD_VP
                and c.get("key", 0) & 0xf000 == KEYBOARD_PREFIX]
        assert Counter(keys) == Counter(KEYBOARD_PREFIX | key for key in
                                       (8, 0x1B, 32, *map(ord, "".join((*rows, COMMON_KEYS)))))
    menus = {RU + 1: COMMANDS["cancel"], WAIT + 1: COMMANDS["cancel"],
             PREVIEW + 1: COMMANDS["leave_preview"], EN + 1: COMMANDS["cancel"]}
    local_menus = {c["page"]: c["key"] for c in touches if c.get("vp") == 0x1000
                   and c.get("key") in COMMANDS.values() and c["target_page"] == WORK + 1}
    assert local_menus == menus
    assert all(c["target_page"] == (WORK + 1 if menus.get(c["page"]) == c["key"] else 0xFFFF)
               for c in touches if c.get("vp") == 0x1000 and c.get("key") in COMMANDS.values())
    assert {c["page"] for c in touches if c.get("key") == 14 and c.get("vp") == 0x1000} == set(range(1, pages)) - {CONFIRM + 1}
    expected_touch = sorted(manifest["hotspots"], key=lambda c: c["page"])
    for index, (actual, expected) in enumerate(zip(touches, expected_touch)):
        assert actual["page"] == expected["page"] and actual["rect"] == expected["rect"]
        assert actual.get("vp") == expected["vp"] and actual.get("key") == expected["key"]
        assert actual["target_page"] == (0xFFFF if expected["target"] is None else expected["target"])
        x0, y0, x1, y1 = actual["rect"]
        assert 0 <= x0 < x1 < 800 and 0 <= y0 < y1 < 480
        assert not any(x0 <= other["rect"][2] and x1 >= other["rect"][0]
                       and y0 <= other["rect"][3] and y1 >= other["rect"][1]
                       for other in touches[:index] if other["page"] == actual["page"])
    status_icons = [icon for icon in manifest["icons"] if icon["vp"] == CONNECTION_VP]
    assert {icon["page"] for icon in status_icons} == {WORK + 1, RU + 1, WAIT + 1, PREVIEW + 1, EN + 1}
    assert all(icon["labels"] == CONNECTION_STAGES for icon in status_icons)
    assert {(d["page"], d["type"]) for d in displays if d["vp"] == CONNECTION_VP} == {
        (page, 0x5A00) for page in {WORK + 1, RU + 1, WAIT + 1, PREVIEW + 1, EN + 1}}
    glyphs = [c for c in displays if c["type"] == 0x5A00 and any(base <= c["vp"] < base + TEXT_CELLS for base in (QUESTION_VP, ANSWER_VP))]
    assert {(c["page"], c["vp"]) for c in glyphs} == {
        (page, vp + i) for page, vp in ((RU + 1, QUESTION_VP), (EN + 1, QUESTION_VP), (WAIT + 1, ANSWER_VP))
        for i in range(TEXT_CELLS)}
    for c in glyphs:
        raw = bytes.fromhex(c["record_hex"])
        assert struct.unpack_from(">4H", raw, 0x0C) == (0, 255, GLYPH_BASE, GLYPH_BASE + 255)
        assert raw[0x15] == 1
        vp = ANSWER_VP if c["page"] == WAIT + 1 else QUESTION_VP
        rect = ANSWER_RECT if vp == ANSWER_VP else QUESTION_RECT
        index = c["vp"] - vp
        x, y = struct.unpack_from(">2H", raw, 0x08)
        assert (x, y) == (rect[0] + index % TEXT_COLUMNS * FONT_WIDTH,
                          rect[1] + index // TEXT_COLUMNS * FONT_HEIGHT)
        assert x + FONT_WIDTH <= rect[2] and y + FONT_HEIGHT <= rect[3]
    graphics = [c for c in displays if c["type"] == 0x5A21]
    assert len(graphics) == 1 and (graphics[0]["page"], graphics[0]["vp"], graphics[0]["rect"]) == (
        PREVIEW + 1, GRAPH_VP, list(GRAPH_RECT))
    assert (directory / "22_VP_Init.BIN").read_bytes() == build_vp_init(INITIAL_VP)
    backgrounds = audit_icl(directory / "32_Backgrounds.ICL", expected_pages=pages, expected_ids=range(pages))
    assert all((p["width"], p["height"]) == (800, 480) for p in backgrounds["pages"])
    library = audit_icl(directory / "60_Status.ICL", expected_pages=manifest["frames"] + 256,
                        expected_ids=(*range(manifest["frames"]), *range(GLYPH_BASE, GLYPH_BASE + 256)))
    assert all((p["width"], p["height"]) == (FONT_WIDTH, FONT_HEIGHT) for p in library["pages"][-256:])
    sizes = {p.name: p.stat().st_size for p in directory.iterdir()}
    assert set(sizes) == {"13_Touch.BIN", "14_Display.BIN", "22_VP_Init.BIN",
                          "32_Backgrounds.ICL", "60_Status.ICL"}
    allocations = validate_flash_allocations(sizes)
    return dict(valid=True, hardware_tested=False, touch_controls=len(touches),
                display_controls=len(displays), locally_reachable_menus=sorted(menus),
                startup_status_vp=CONNECTION_VP, bytes=sum(sizes.values()), allocations=allocations)


def build():
    ui.ROOT = ROOT
    panel = Panel()
    for method in (panel.home, panel.move, panel.paper):
        method()
    panel.pages[CONFIRM] = Image.new("RGB", (ui.WIDTH, ui.HEIGHT), ui.BG)
    panel.compose(RU)
    panel.compose(EN)
    panel.waiting()
    panel.preview()
    with Image.open(PREVIOUS / "backgrounds/000.png") as image:
        panel.pages[8] = image.convert("RGB")
    panel.page = 8
    panel.draw = ImageDraw.Draw(panel.pages[8])
    panel.label("Подключение к S24 Ultra", 400, 381, 23, ui.TEXT, True, "mt")
    panel.label("Затем подключение к пульту на ПК", 400, 422, 17, ui.MUTED, anchor="mt")
    panel.arrange_boot_pages()
    titles = {**native.TITLES, **FEATURES}
    panel.page_titles = ["Заставка", *(titles[page] for page in range(8))]
    panel.save()

    directory = ROOT / "DWIN_SET"
    write_vp_init(directory, INITIAL_VP)
    build_font(directory)
    path = ROOT / "ui-manifest.json"
    manifest = json.loads(path.read_text(encoding="utf-8"))
    manifest.pop("help_page")
    manifest.update(navigation_pages=[WORK + 1, MOVE + 1, PAPER + 1, RU + 1, PREVIEW + 1],
                    pc_check={"vp": 0x1000, "key": 22, "pending": 11, "answered": 12, "timeout": 13},
                    startup_connection={"vp": CONNECTION_VP, "states": CONNECTION_STAGES},
                    assistant={"pages": {"compose": RU + 1, "english": EN + 1, "wait": WAIT + 1,
                                         "preview": PREVIEW + 1},
                               "keys": COMMANDS, "keyboard_vp": KEYBOARD_VP, "keyboard_prefix": KEYBOARD_PREFIX,
                               "stage_vp": STAGE_VP,
                               "question": {"vp": QUESTION_VP, "rect": QUESTION_RECT, "limit": 120},
                               "answer": {"vp": ANSWER_VP, "rect": ANSWER_RECT, "limit": 112},
                               "preview": {"vp": GRAPH_VP, "rect": GRAPH_RECT, "stage_vp": PREVIEW_STAGE_VP}})
    path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    template = (ROOT / "preview-template.html").read_text(encoding="utf-8")
    (ROOT / "preview.html").write_text(template.replace("__MANIFEST__", json.dumps(manifest, ensure_ascii=False)),
                                       encoding="utf-8")
    for folder, name in (("backgrounds", "32_Backgrounds.ICL"), ("status-icons", "60_Status.ICL")):
        native.compile_icl(ROOT / folder, directory / name)
    report = verify(manifest, directory)
    (ROOT / "package-verification.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    hashes = {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in sorted(directory.iterdir())}
    (ROOT / "SHA256.json").write_text(json.dumps(hashes, indent=2), encoding="utf-8")
    with zipfile.ZipFile(ROOT / "DWIN_ASSISTANT_800x480_20261108.zip", "w", zipfile.ZIP_DEFLATED) as archive:
        for file in sorted(directory.iterdir()):
            archive.write(file, "DWIN_SET/" + file.name)
        for name in ("README-RU.md", "SHA256.json", "package-verification.json", FONT_LICENSE):
            archive.write(ROOT / name, name)
    print(json.dumps({key: value for key, value in report.items() if key != "allocations"}))


if __name__ == "__main__":
    build()
