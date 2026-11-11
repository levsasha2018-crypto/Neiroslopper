"""Independent DGUS_3 header, CRC, page-index and image-decoding audit.

DWIN extends the JPEG SOS descriptor with zero padding for hardware alignment.
For desktop verification only, remove this extension before decoding in Pillow.
The deployable ICL is never modified by this audit.
"""
from hashlib import sha256
from io import BytesIO
import json
from pathlib import Path
import re
import struct
import sys
from typing import Iterable

from PIL import Image


FLASH_SLOT_BYTES = 256 * 1024
FLASH_SLOT_COUNT = 64  # 16 MiB model; do not use larger models' address space.
JPEG_LIMIT = 248 * 1024


def library_id(path: Path) -> int:
    match = re.fullmatch(r"(\d+)(?:[^0-9].*)?\.icl", Path(path).name, flags=re.I)
    if not match:
        raise ValueError(f"ICL filename must start with its decimal flash ID: {path}")
    result = int(match[1])
    if not 32 <= result < FLASH_SLOT_COUNT:
        raise ValueError(f"ICL ID {result} is outside the permitted picture/icon flash area 32..63")
    return result


def audit_flash_allocations(paths: Iterable[Path]) -> list[dict]:
    """Check actual 256 KiB address spans, including multi-slot libraries."""
    ranges = []
    for path in sorted(set(Path(p).resolve() for p in paths)):
        start = library_id(path)
        size = path.stat().st_size
        if not size:
            raise ValueError(f"Empty ICL: {path}")
        end = start + (size + FLASH_SLOT_BYTES - 1) // FLASH_SLOT_BYTES
        if end > FLASH_SLOT_COUNT:
            raise ValueError(f"{path.name} overflows the 16 MiB flash: slots {start}..{end - 1}")
        ranges.append({"file": path.name, "start_slot": start, "end_slot_exclusive": end, "bytes": size})
    ranges.sort(key=lambda item: item["start_slot"])
    for left, right in zip(ranges, ranges[1:]):
        if left["end_slot_exclusive"] > right["start_slot"]:
            raise ValueError(f"Flash overlap: {left['file']} overwrites {right['file']}")
    return ranges


def crc16_modbus(data: bytes) -> int:
    result = 0xFFFF
    for byte in data:
        result ^= byte
        for _ in range(8):
            result = (result >> 1) ^ (0xA001 if result & 1 else 0)
    return result


def desktop_jpeg(payload: bytes) -> bytes:
    """Undo the official packager's SOS-extension padding for independent QA."""
    pos = payload.find(b"\xff\xda")
    if pos < 0:
        raise ValueError("JPEG has no SOS descriptor")
    length = int.from_bytes(payload[pos + 2:pos + 4], "big")
    components = payload[pos + 4]
    standard_length = 6 + components * 2
    if length < standard_length:
        raise ValueError("Invalid JPEG SOS descriptor")
    padding = payload[pos + 2 + standard_length:pos + 2 + length]
    if any(padding):
        raise ValueError("Unexpected nonzero bytes in DWIN SOS extension")
    return (
        payload[:pos + 2]
        + standard_length.to_bytes(2, "big")
        + payload[pos + 4:pos + 2 + standard_length]
        + payload[pos + 2 + length:]
    )


def audit_icl(
    path: Path,
    expected_pages: int | None = None,
    *,
    expected_ids: Iterable[int] | None = None,
    expected_dimensions: tuple[int, int] | None = None,
) -> dict:
    resource_id = library_id(path)
    data = Path(path).read_bytes()
    if len(data) < 20:
        raise ValueError("Truncated ICL header")
    if data[:6] != b"DGUS_3":
        raise ValueError("Not a DGUS III ICL")
    if int.from_bytes(data[8:12], "big") != len(data) - 8:
        raise ValueError("ICL file-length header does not match file")
    stored_crc = int.from_bytes(data[6:8], "little")
    actual_crc = crc16_modbus(data[8:])
    if stored_crc != actual_crc:
        raise ValueError("ICL CRC mismatch")
    if data[12] != 4 or data[15] != 0:
        raise ValueError("Expected four-byte indexes and unencrypted ICL")
    max_id = int.from_bytes(data[13:15], "big")
    if max_id > 1023:
        raise ValueError("Image ID exceeds T5L library limit")
    index_end = 16 + 4 * (max_id + 1)
    if len(data) < index_end:
        raise ValueError("Truncated page index")
    indexes = [struct.unpack_from(">I", data, 16 + 4 * i)[0] for i in range(max_id + 1)]
    nonzero = [x for x in indexes if x]
    if not nonzero or indexes[-1] == 0:
        raise ValueError("Empty library or inconsistent maximum image ID")
    if len(nonzero) != len(set(nonzero)) or nonzero != sorted(nonzero):
        raise ValueError("Duplicate/unsorted image pointers")
    pages = []
    for page_id, offset in enumerate(indexes):
        if not offset:
            continue
        next_offset = min((x for x in nonzero if x > offset), default=len(data))
        if offset < index_end or offset + 10 >= next_offset:
            raise ValueError("Invalid image pointer")
        width, height, jpeg_header, compressed_size = struct.unpack_from(">HHHI", data, offset)
        expected = expected_dimensions or ((800, 480) if resource_id == 32 else None)
        if expected is not None and (width, height) != expected:
            raise ValueError(f"Wrong image dimensions: {width}x{height}, expected {expected}")
        if not 1 <= width <= 800 or not 1 <= height <= 800 or width % 4:
            raise ValueError(f"Invalid T5L image dimensions: {width}x{height}")
        payload = data[offset + 10:next_offset]
        if len(payload) >= JPEG_LIMIT:
            raise ValueError(f"Image {page_id} reaches conservative T5L1 JPEG size limit")
        if len(payload) != jpeg_header + compressed_size:
            raise ValueError("Image length fields do not match next pointer")
        if (offset + 10 + jpeg_header) % 4 or next_offset % 4:
            raise ValueError("DWIN JPEG entropy/frame alignment is not four-byte")
        if not payload.startswith(b"\xff\xd8") or b"\xff\xd9" not in payload:
            raise ValueError("Missing JPEG boundary marker")
        restored = desktop_jpeg(payload)
        with Image.open(BytesIO(restored)) as im:
            im.load()
            if im.size != (width, height):
                raise ValueError("Decoded image dimensions mismatch")
            subsampling = im.layer
            if im.mode != "RGB" or len(subsampling) != 3:
                raise ValueError("Expected three-component RGB JPEG")
            if any(component[1:3] != (1, 1) for component in subsampling):
                raise ValueError("Expected 4:4:4 JPEG chroma subsampling")
        pages.append({
            "page": page_id, "offset": offset, "width": width, "height": height,
            "bytes": next_offset - offset, "jpeg_header": jpeg_header,
            "decoded": True, "jpeg_components": subsampling,
        })
    if expected_pages is not None and len(pages) != expected_pages:
        raise ValueError(f"Expected {expected_pages} pages, got {len(pages)}")
    if expected_ids is not None and set(expected_ids) != {item["page"] for item in pages}:
        raise ValueError("Compiled image IDs do not match expected image IDs")
    allocation = audit_flash_allocations([path])[0]
    return {
        "valid": True, "format": "DGUS_3", "file": str(Path(path).resolve()),
        "bytes": len(data), "sha256": sha256(data).hexdigest(),
        "crc16": f"{actual_crc:04X}", "library_id": resource_id, "pages": pages,
        "flash_slots": allocation["end_slot_exclusive"] - allocation["start_slot"],
        "flash_allocation": allocation,
        "compiler": "Official DWIN DGUS V7.651 ICLGenerator.dll IclFileBuilder",
    }


if __name__ == "__main__":
    if len(sys.argv) == 3 and sys.argv[1] == "--flash":
        directory = Path(sys.argv[2])
        files = sorted(set(directory.glob("*.ICL")) | set(directory.glob("*.icl")))
        if not files:
            raise SystemExit("No ICL resources in the requested directory")
        report = {
            "valid": True,
            "flash_allocations": audit_flash_allocations(files),
            "libraries": [audit_icl(path) for path in files],
        }
        print(json.dumps(report, ensure_ascii=False, indent=2))
        raise SystemExit(0)
    if not 2 <= len(sys.argv) <= 4:
        raise SystemExit("usage: audit_icl.py ICL [EXPECTED_PAGES] [REPORT_JSON] | --flash DWIN_SET_DIRECTORY")
    report = audit_icl(Path(sys.argv[1]), int(sys.argv[2]) if len(sys.argv) > 2 else None)
    result = json.dumps(report, ensure_ascii=False, indent=2)
    if len(sys.argv) > 3:
        Path(sys.argv[3]).write_text(result, encoding="utf-8")
    print(result)
