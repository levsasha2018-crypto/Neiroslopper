"""Small, audited DGUS II native control-file compiler (T5L, not legacy DGUS).

The structures below follow DWIN's T5L_DGUSII Application Development Guide:
chapter 6 (13.BIN), 6.6 (FE05 return key), chapter 7 (14.BIN), 7.12
(5A10 numeric display), and 7.13 (5A11 text display). A local reference is
references/DGUSII-guide.pdf, printed pages 86, 97-98, 120, 152, 157-159.
Source mirror (the PDF itself is DWIN's publication):
https://forum.arduino.cc/uploads/short-url/qiPsJbKEyGJmoCQVmVBenVXqOZz.pdf

13.BIN is a raw stream of 16/32/48-byte touch instructions. Display
instructions occupy 32 bytes. The compiler supports fixed-stride 14.BIN
and indexed 14DGUS_2, as emitted by the DGUS editor.
All multi-byte quantities are BIG ENDIAN. VP addresses count 16-bit words.

This module never creates a kernel, TCON, OS firmware, or hardware CFG.
FE05 requests automatic upload, but the existing screen CFG must also have
0x05.bit4 enabled. A host can poll the key VP if it is not enabled. A page
switch works inside the screen without any host or serial connection.

Text defaults to the preinstalled #0 ASCII font. An installed 8-bit bitmap
font can also be selected by its FLASH slot.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import struct
from typing import Iterable, Mapping, Sequence


FLASH_SLOT_BYTES = 256 * 1024
# Keep the touch stream small for early/unknown T5L kernel revisions. Later
# manuals allow the full 256 KiB slot; this UI has no need for that capacity.
TOUCH_COMPAT_BYTES = 32 * 1024
DISPLAY_RECORD_BYTES = 32
INDEXED_DISPLAY_MAGIC = b"\x14DGUS_2\x10"
INDEXED_DISPLAY_DATA_OFFSET = 0x4000
INDEXED_DISPLAY_TABLE_OFFSET = 0x10
DEFAULT_PAGE_STRIDE = 2048
NO_PAGE = 0xFFFF
VP_INIT_BYTES = 128 * 1024

# Native startup values, expressed as individual unsigned 16-bit VP words.
# Long coordinate values use two consecutive words, both initially zero.
PLOTTER_VP_DEFAULTS = {
    0x1000: 0,
    **{vp: 0 for vp in range(0x1100, 0x1113)},
    0x1101: 1,  # PC bridge protocol version
    0x1200: 1,  # Step enum 1 => 1 mm; not the physical size in hundredths
    0x1201: 50,  # Requested speed percentage
}


def _u8(value: int, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= 0xFF:
        raise ValueError(f"{name} must be an integer in 0..255")
    return value


def _u16(value: int, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= 0xFFFF:
        raise ValueError(f"{name} must be an integer in 0..65535")
    return value


def _vp(value: int) -> int:
    _u16(value, "VP")
    if not 0x1000 <= value < 0xFF00:
        raise ValueError("Application VP must be 0x1000..0xFEFF; system VPs are not accepted")
    return value


def _page(value: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value < 128:
        raise ValueError("Page must be in 0..127 (16 MB product, 64 controls/page)")
    return value


@dataclass(frozen=True)
class Rect:
    x0: int
    y0: int
    x1: int
    y1: int

    def validate(self, width: int, height: int) -> None:
        for name, val in zip(("x0", "y0", "x1", "y1"), self.as_tuple()):
            _u16(val, name)
        if not (0 <= self.x0 < self.x1 < width and 0 <= self.y0 < self.y1 < height):
            raise ValueError(f"Rectangle {self.as_tuple()} is outside {width}x{height} or empty")

    def as_tuple(self) -> tuple[int, int, int, int]:
        return self.x0, self.y0, self.x1, self.y1

    def intersects(self, other: "Rect") -> bool:
        return self.x0 <= other.x1 and self.x1 >= other.x0 and self.y0 <= other.y1 and self.y1 >= other.y0


def _rect(rect: Rect | Sequence[int]) -> Rect:
    return rect if isinstance(rect, Rect) else Rect(*rect)


@dataclass(frozen=True)
class TouchButton:
    """Native FE05 Return Key Code; one event per touch, no held-jog loop.

    target_page changes locally at the same touch. No target means remain on
    the current page. hold_tenths=0 means normal immediate button behavior;
    use e.g. 10 for a one-second deliberate hold, not as a repeated trigger.
    """
    page: int
    rect: Rect | Sequence[int]
    key: int
    vp: int = 0x1000
    target_page: int | None = None
    hold_tenths: int = 0
    pressed_picture: int | None = None

    def encode(self, width: int = 480, height: int = 800) -> bytes:
        _page(self.page)
        r = _rect(self.rect)
        r.validate(width, height)
        _u16(self.key, "key")
        _vp(self.vp)
        _u8(self.hold_tenths, "hold_tenths")
        target = NO_PAGE if self.target_page is None else _page(self.target_page)
        effect = NO_PAGE if self.pressed_picture is None else _page(self.pressed_picture)
        record = bytearray(32)
        struct.pack_into(">7H", record, 0, self.page, *r.as_tuple(), target, effect)
        struct.pack_into(">H", record, 0x0E, 0xFE05)
        record[0x10] = 0xFE
        struct.pack_into(">H", record, 0x11, self.vp)
        record[0x13] = 0  # whole 16-bit VP, not a byte/bit assignment
        struct.pack_into(">H", record, 0x14, self.key)
        record[0x16] = self.hold_tenths
        # The guide has a reserve-length typo at 0x17: "10". Chapter 6
        # defines a fixed 32-byte FE05 record, so 0x17..0x1F are 9 zero bytes.
        return bytes(record)


@dataclass(frozen=True)
class PageJump:
    """Native 16-byte basic page-switch key, with no variable event."""
    page: int
    rect: Rect | Sequence[int]
    target_page: int
    pressed_picture: int | None = None

    def encode(self, width: int = 480, height: int = 800) -> bytes:
        _page(self.page)
        target = _page(self.target_page)
        r = _rect(self.rect)
        r.validate(width, height)
        effect = NO_PAGE if self.pressed_picture is None else _page(self.pressed_picture)
        # FF** is not a variable-function key. Pic_Next remains active.
        return struct.pack(">8H", self.page, *r.as_tuple(), target, effect, 0xFFFF)


def _display_header(control_type: int, vp: int, description_words: int = 13) -> bytearray:
    _vp(vp)
    record = bytearray(32)
    struct.pack_into(">4H", record, 0, 0x5A00 | control_type, 0xFFFF, description_words, vp)
    return record


@dataclass(frozen=True)
class NumericDisplay:
    """Native 5A10 fixed-point numeric display with #0 resident ASCII font.

    data_type: 0=signed16, 1=signed32, 2=highbyte, 3=lowbyte,
    4=signed64, 5=unsigned16, 6=unsigned32. decimals is display-only:
    host must send e.g. 1234 for 12.34 at decimals=2.
    """
    page: int
    vp: int
    x: int
    y: int
    font_width: int = 16
    integer_digits: int = 5
    decimals: int = 0
    data_type: int = 0
    color: int = 0xFFFF
    alignment: int = 0
    unit: str = ""
    font_id: int = 0

    def encode(self, width: int = 480, height: int = 800) -> bytes:
        _page(self.page)
        if not 0 <= self.x < width or not 0 <= self.y < height:
            raise ValueError("Numeric display position outside screen")
        if not 4 <= self.font_width <= 64:
            raise ValueError("Numeric font width must be 4..64")
        if not 1 <= self.integer_digits <= 20 or not 0 <= self.decimals <= 19 or self.integer_digits + self.decimals > 20:
            raise ValueError("Numeric integer_digits + decimals must be in 1..20")
        if self.data_type not in range(7):
            raise ValueError("Unsupported numeric data_type")
        if self.alignment not in (0, 1, 2):
            raise ValueError("alignment must be 0=left, 1=right, or 2=center")
        unit = self.unit.encode("ascii")
        if len(unit) > 11:
            raise ValueError("Numeric unit must fit 11 ASCII bytes")
        _u16(self.color, "color")
        _u8(self.font_id, "font_id")
        record = _display_header(0x10, self.vp)
        struct.pack_into(">3H", record, 0x08, self.x, self.y, self.color)
        record[0x0E:0x15] = bytes((self.font_id, self.font_width, self.alignment, self.integer_digits, self.decimals, self.data_type, len(unit)))
        record[0x15:0x15 + len(unit)] = unit
        return bytes(record)


@dataclass(frozen=True)
class TextDisplay:
    """Native 5A11 text field with an 8-bit bitmap font."""
    page: int
    vp: int
    rect: Rect | Sequence[int]
    max_bytes: int = 40
    font_width: int = 12
    font_height: int = 24
    color: int = 0xFFFF
    font_id: int = 0
    char_spacing: int = 0
    line_spacing: int = 0

    def encode(self, width: int = 480, height: int = 800) -> bytes:
        _page(self.page)
        r = _rect(self.rect)
        r.validate(width, height)
        if not 1 <= self.max_bytes <= 2048:
            raise ValueError("max_bytes must be 1..2048")
        if not 4 <= self.font_width <= 64 or not 8 <= self.font_height <= 128 or self.font_height % 2:
            raise ValueError("Font requires width 4..64 and even height 8..128")
        if self.font_id == 0 and self.font_height != self.font_width * 2:
            raise ValueError("Resident ASCII #0 font uses height = 2 * width")
        if r.y0 + self.font_height - 1 > r.y1:
            raise ValueError("Text field is shorter than one font line")
        for name, val in (("color", self.color), ("font_id", self.font_id), ("char_spacing", self.char_spacing), ("line_spacing", self.line_spacing)):
            (_u16 if name == "color" else _u8)(val, name)
        if self.vp + (self.max_bytes + 1) // 2 > 0xFF00:
            raise ValueError("Text buffer exceeds application VP range")
        record = _display_header(0x11, self.vp)
        struct.pack_into(">3H", record, 0x08, r.x0, r.y0, self.color)
        struct.pack_into(">4H", record, 0x0E, *r.as_tuple())
        struct.pack_into(">H", record, 0x16, self.max_bytes)
        record[0x18] = 0  # Font0_ID: resident ASCII font (mixed encodings only)
        record[0x19] = self.font_id  # font library for 8-bit encoding
        record[0x1A] = self.font_width
        record[0x1B] = self.font_height
        record[0x1C] = 0x80  # 8-bit encoding, fixed character advance
        record[0x1D] = self.char_spacing
        record[0x1E] = self.line_spacing
        return bytes(record)


@dataclass(frozen=True)
class BasicGraphics:
    """Native 5A21 drawing area, DWIN guide sections 7.1 and 7.22."""
    page: int
    vp: int
    rect: Rect | Sequence[int]

    def encode(self, width: int = 480, height: int = 800) -> bytes:
        _page(self.page)
        r = _rect(self.rect)
        r.validate(width, height)
        record = _display_header(0x21, self.vp, description_words=8)
        struct.pack_into(">4H", record, 0x08, *r.as_tuple())
        return bytes(record)


@dataclass(frozen=True)
class VariableIcon:
    """Native 5A00: contiguous VP enum -> contiguous JPEG icon-library IDs.

    Library ID is the decimal FLASH slot in the filename, e.g. 33_Icons.ICL
    corresponds to library_id=33 (0x21), not hexadecimal 0x33. Values outside
    value_min..value_max draw no icon. Opaque mode is suitable for complete
    status-label strips, so no previous-state lettering remains visible.
    Width/height are compile-time bounds checks; JPEG itself holds dimensions.
    """
    page: int
    vp: int
    x: int
    y: int
    value_min: int = 0
    value_max: int = 2
    icon_min: int = 0
    icon_max: int = 2
    library_id: int = 33
    icon_width: int = 320
    icon_height: int = 40
    opaque: bool = True

    def encode(self, width: int = 480, height: int = 800) -> bytes:
        _page(self.page)
        for name, value in (("value_min", self.value_min), ("value_max", self.value_max), ("icon_min", self.icon_min), ("icon_max", self.icon_max)):
            _u16(value, name)
        if self.value_max < self.value_min or self.icon_max < self.icon_min:
            raise ValueError("Variable-icon value/icon ranges must be increasing")
        if self.value_max - self.value_min != self.icon_max - self.icon_min:
            raise ValueError("Variable-icon enum must map one-to-one to contiguous icons")
        if not 16 <= self.library_id <= 63:
            raise ValueError("Icon library must be a valid image FLASH slot 16..63")
        if not isinstance(self.icon_width, int) or not isinstance(self.icon_height, int) or self.icon_width < 1 or self.icon_height < 1:
            raise ValueError("Icon dimensions must be positive integers")
        if not (0 <= self.x and self.x + self.icon_width <= width and 0 <= self.y and self.y + self.icon_height <= height):
            raise ValueError("Variable icon extends outside screen")
        record = _display_header(0, self.vp, 10)
        struct.pack_into(">6H", record, 0x08, self.x, self.y, self.value_min, self.value_max, self.icon_min, self.icon_max)
        record[0x14] = self.library_id
        record[0x15] = 1 if self.opaque else 0
        record[0x16] = 0  # standard overlay, no blended-gamma operation
        record[0x17] = 0xFF
        record[0x18] = 0xFF
        record[0x19] = 1  # lowest documented transparent filter; unused when opaque
        return bytes(record)


TouchControl = TouchButton | PageJump
DisplayControl = NumericDisplay | TextDisplay | BasicGraphics | VariableIcon


def build_touch_file(controls: Iterable[TouchControl], *, width: int = 480, height: int = 800, full_slot: bool = True) -> bytes:
    """13.BIN. Erased FF tail provides invalid-page/end-of-stream records."""
    items = sorted(controls, key=lambda c: c.page)
    for index, control in enumerate(items):
        rect = _rect(control.rect)
        for previous in items[:index]:
            if previous.page == control.page and rect.intersects(_rect(previous.rect)):
                raise ValueError(f"Overlapping touch areas on page {control.page}")
    payload = b"".join(control.encode(width, height) for control in items)
    if len(payload) + 16 > TOUCH_COMPAT_BYTES:
        raise ValueError("Touch configuration exceeds the conservative 32 KiB compatibility limit")
    size = TOUCH_COMPAT_BYTES if full_slot else len(payload) + 16
    return payload.ljust(size, b"\xff")


def build_display_file(controls: Iterable[DisplayControl], *, page_count: int, width: int = 480, height: int = 800, page_stride: int = DEFAULT_PAGE_STRIDE, full_slot: bool = True, indexed: bool = False) -> bytes:
    """Compile display instructions as an indexed or fixed-stride 14.BIN."""
    if page_stride not in (2048, 4096, 8192):
        raise ValueError("page_stride must be 2048, 4096, or 8192")
    if not isinstance(page_count, int) or not 1 <= page_count <= FLASH_SLOT_BYTES // page_stride:
        raise ValueError("Invalid page_count for 256 KiB display slot")
    pages = [bytearray() for _ in range(page_count)]
    limit = min(page_stride // DISPLAY_RECORD_BYTES, 255)
    for control in controls:
        if not 0 <= control.page < page_count:
            raise ValueError(f"Display control page {control.page} has no background")
        page = pages[control.page]
        if len(page) // DISPLAY_RECORD_BYTES >= limit:
            raise ValueError(f"Too many controls on page {control.page}")
        record = control.encode(width, height)
        if len(record) != DISPLAY_RECORD_BYTES:
            raise ValueError("Internal error: display instruction is not 32 bytes")
        page.extend(record)

    if indexed:
        # The editor puts the page table before the packed records at 0x4000.
        # Each table entry is a count byte and a three-byte absolute offset.
        payload = bytearray(INDEXED_DISPLAY_DATA_OFFSET)
        payload[:len(INDEXED_DISPLAY_MAGIC)] = INDEXED_DISPLAY_MAGIC
        struct.pack_into(">H", payload, 8, page_count)
        offset = INDEXED_DISPLAY_DATA_OFFSET
        table_entries = (INDEXED_DISPLAY_DATA_OFFSET - INDEXED_DISPLAY_TABLE_OFFSET) // 4
        for index in range(table_entries):
            page = pages[index] if index < page_count else b""
            count = len(page) // DISPLAY_RECORD_BYTES
            struct.pack_into(">I", payload, INDEXED_DISPLAY_TABLE_OFFSET + index * 4,
                             count << 24 | offset)
            payload.extend(page)
            offset += len(page)
        payload.extend(b"\xff" * DISPLAY_RECORD_BYTES)
        if len(payload) > FLASH_SLOT_BYTES:
            raise ValueError("Indexed display file exceeds its flash slot")
        return bytes(payload)

    payload = b"".join(page.ljust(page_stride, b"\xff") for page in pages)
    return payload.ljust(FLASH_SLOT_BYTES, b"\xff") if full_slot else payload


def parse_touch_file(data: bytes) -> list[dict]:
    """Independent structural decoder used to audit generated 13.BIN."""
    decoded = []
    offset = 0
    while offset + 16 <= len(data):
        header = struct.unpack_from(">8H", data, offset)
        page, x0, y0, x1, y1, target, effect, code = header
        if page == 0xFFFF:
            if any(byte != 0xFF for byte in data[offset:]):
                raise ValueError("Non-erased data after 13.BIN terminator")
            return decoded
        record = dict(offset=offset, page=page, rect=[x0, y0, x1, y1], target_page=target, pressed_picture=effect, code=code)
        if code == 0xFE05:
            if offset + 32 > len(data) or data[offset + 0x10] != 0xFE or data[offset + 0x13] != 0:
                raise ValueError("Malformed FE05 record")
            record.update(vp=struct.unpack_from(">H", data, offset + 0x11)[0], key=struct.unpack_from(">H", data, offset + 0x14)[0], hold_tenths=data[offset + 0x16])
            if any(data[offset + 0x17:offset + 32]):
                raise ValueError("FE05 reserved bytes must be zero")
            offset += 32
        elif code == 0xFFFF:
            offset += 16
        else:
            raise ValueError(f"Unexpected touch code 0x{code:04X}")
        decoded.append(record)
    raise ValueError("13.BIN has no erased terminator")


def parse_display_file(data: bytes, *, page_count: int, page_stride: int = DEFAULT_PAGE_STRIDE) -> list[dict]:
    decoded = []
    indexed = data.startswith(INDEXED_DISPLAY_MAGIC)
    if indexed:
        if (len(data) < INDEXED_DISPLAY_DATA_OFFSET + DISPLAY_RECORD_BYTES or
                struct.unpack_from(">H", data, 8)[0] != page_count or
                not 1 <= page_count <= (INDEXED_DISPLAY_DATA_OFFSET - INDEXED_DISPLAY_TABLE_OFFSET) // 4):
            raise ValueError("Invalid indexed 14.BIN header")
    elif len(data) < page_count * page_stride:
        raise ValueError("14.BIN is truncated")
    expected_offset = INDEXED_DISPLAY_DATA_OFFSET
    for page in range(page_count):
        seen_empty = False
        start, count = page * page_stride, page_stride // DISPLAY_RECORD_BYTES
        if indexed:
            entry = struct.unpack_from(">I", data, INDEXED_DISPLAY_TABLE_OFFSET + page * 4)[0]
            start, count = entry & 0xFFFFFF, entry >> 24
            if start != expected_offset or start + count * DISPLAY_RECORD_BYTES > len(data):
                raise ValueError("Invalid indexed 14.BIN page offset")
            expected_offset += count * DISPLAY_RECORD_BYTES
        for index in range(count):
            offset = start + index * DISPLAY_RECORD_BYTES
            record = data[offset:offset + DISPLAY_RECORD_BYTES]
            if record == b"\xff" * DISPLAY_RECORD_BYTES and not indexed:
                seen_empty = True
                continue
            if seen_empty:
                raise ValueError("Non-contiguous display controls in page")
            code, sp, length, vp = struct.unpack_from(">4H", record)
            expected_length = {0x5A00: 10, 0x5A04: 5, 0x5A10: 13, 0x5A11: 13, 0x5A21: 8}.get(code)
            if expected_length is None or sp != 0xFFFF or length != expected_length:
                raise ValueError("Unknown/invalid display record header")
            entry = dict(page=page, index=index, offset=offset, type=code, sp=sp, description_words=length, vp=vp, record_hex=record.hex())
            if code == 0x5A04:
                # Image Animation has no application VP. The guide requires
                # a fixed zero word at 0x06, not a write to system VP 0.
                first_page, last_page = struct.unpack_from(">2H", record, 0x08)
                frame_ticks, library_id = record[0x0C:0x0E]
                return_page = struct.unpack_from(">H", record, 0x0E)[0]
                if vp != 0 or first_page >= last_page or frame_ticks == 0 or any(record[0x10:]):
                    raise ValueError("Malformed native Image Animation record")
                if library_id == 0 and (last_page >= page_count or return_page != 0):
                    raise ValueError("Image Animation references unavailable background or NAND return mode")
                entry.update(first_page=first_page, last_page=last_page,
                             frame_ticks=frame_ticks, frame_ms=frame_ticks * 8,
                             library_id=library_id, expanded_return_page=return_page)
            else:
                _vp(vp)
                if code == 0x5A21:
                    entry["rect"] = list(struct.unpack_from(">4H", record, 0x08))
                    if any(record[0x10:]):
                        raise ValueError("Unexpected Basic Graphics dash parameters")
            decoded.append(entry)
    return decoded


def build_vp_init(values: Mapping[int, int] | None = None) -> bytes:
    """Native 22.BIN: a 128 KiB zero-initialized VP word image, big endian.

    DWIN's guide section 3.3.2.4 (page 25) explicitly maps VP 0x2000 to
    byte offset 0x4000: there is no file header or 0x1000 address bias.
    Unspecified words are zero. The system-address prefix is left zero and
    callers cannot supply system VP writes through this helper.

    This file takes effect ONLY if the screen's existing CFG[0x05].bit5 is
    1 and its initialization file ID is 22 (factory default). No hardware
    CFG is generated or changed here. A PC host must initialize the words
    on connect too, so correct operation never depends on an unknown CFG.
    """
    words = PLOTTER_VP_DEFAULTS if values is None else values
    image = bytearray(VP_INIT_BYTES)
    for vp, value in words.items():
        _vp(vp)
        _u16(value, f"VP 0x{vp:04X} initial value")
        struct.pack_into(">H", image, vp * 2, value)
    return bytes(image)


def write_vp_init(directory: str | Path, values: Mapping[int, int] | None = None) -> dict:
    """Write 22_VP_Init.BIN and a separate audit; never alter the CFG."""
    import hashlib
    import json

    directory = Path(directory)
    words = dict(PLOTTER_VP_DEFAULTS if values is None else values)
    data = build_vp_init(words)
    directory.mkdir(parents=True, exist_ok=True)
    name = "22_VP_Init.BIN"
    (directory / name).write_bytes(data)
    report = {
        "format": "T5L DGUS II raw VP initialization words",
        "source": "DWIN T5L DGUS II guide, section 3.3.2.4, page 25; CFG table pages 26 and 30",
        "bytes": len(data),
        "sha256": hashlib.sha256(data).hexdigest(),
        "byte_offset": "VP * 2; all words big endian, no file header",
        "power_on_requirement": "Existing CFG[0x05].bit5=1 and Init_File_ID=22 (factory default 0x16)",
        "cfg_changed": False,
        "host_initialization_also_required": True,
        "values": {f"0x{vp:04X}": value for vp, value in sorted(words.items())},
        "file": name,
    }
    (directory.parent / "vp-init-audit.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    return report


def validate_flash_allocations(files: dict[str, int]) -> list[dict]:
    """Validate font/image FLASH ranges from actual *byte* sizes.

    DGUS font/image storage is shared. A 400 KiB 32_Background.ICL occupies
    both slots 32 and 33, so a 33_Icons.ICL would corrupt it. A font-style BIN
    and an ICL file with the same slot also conflict. The resident #0 ASCII
    font is preserved by rejecting outputs starting below slot 12.
    """
    import re

    result = []
    used: dict[int, str] = {}
    for name, size in files.items():
        match = re.match(r"^(\d+).*\.(?:bin|icl|hzk|dzk|gtf)$", name, re.I)
        if not match:
            raise ValueError(f"Not a numbered DGUS font/image file: {name}")
        first = int(match.group(1))
        if not isinstance(size, int) or size <= 0:
            raise ValueError(f"Invalid byte size: {name}")
        count = (size + FLASH_SLOT_BYTES - 1) // FLASH_SLOT_BYTES
        last = first + count - 1
        if first < 12 or last > 63:
            raise ValueError(f"{name} overlaps resident ASCII or exceeds 16 MiB FLASH")
        for slot in range(first, last + 1):
            if slot in used:
                raise ValueError(f"FLASH slot {slot} overlap: {used[slot]} and {name}")
            used[slot] = name
        result.append(dict(name=name, bytes=size, first_slot=first, last_slot=last, offset=first * FLASH_SLOT_BYTES))
    return result


def write_control_files(directory: str | Path, touches: Iterable[TouchControl], displays: Iterable[DisplayControl], *, page_count: int, width: int = 480, height: int = 800, page_stride: int = DEFAULT_PAGE_STRIDE, indexed_display: bool = False) -> dict:
    """Write compiled UI files to a build directory; caller handles deployment."""
    import hashlib
    import json

    directory = Path(directory)
    touch_data = build_touch_file(touches, width=width, height=height)
    display_data = build_display_file(displays, page_count=page_count, width=width, height=height,
                                      page_stride=page_stride, indexed=indexed_display)
    touch_audit = parse_touch_file(touch_data)
    display_audit = parse_display_file(display_data, page_count=page_count, page_stride=page_stride)
    if any(t["page"] >= page_count or (t["target_page"] != 0xFFFF and t["target_page"] >= page_count) for t in touch_audit):
        raise ValueError("Touch page or target page has no background")
    directory.mkdir(parents=True, exist_ok=True)
    files = {"13_Touch.BIN": touch_data, "14_Display.BIN": display_data}
    for name, data in files.items():
        (directory / name).write_bytes(data)
    report = {
        "format": "T5L DGUS II native 13/14 control files",
        "width": width, "height": height, "page_count": page_count,
        "display_layout": "14DGUS_2" if indexed_display else "fixed stride",
        "page_stride": None if indexed_display else page_stride,
        "touches": touch_audit, "displays": display_audit,
        "upload_requirement": "Existing CFG byte 0x05 bit4=1; otherwise poll VP 0x1000",
        "files": {name: {"bytes": len(data), "sha256": hashlib.sha256(data).hexdigest()} for name, data in files.items()},
    }
    (directory.parent / "control-file-audit.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    return report
