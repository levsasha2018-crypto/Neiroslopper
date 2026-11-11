"""Build LCD backgrounds and status icons with official DGUS V7.651 ICLGenerator.

JPEG normalization is local Pillow; ICL preprocessing/index/CRC comes from the
unmodified DWIN DGUSCommon.Tools.IclFileBuilder. No device access is performed.
"""
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile

from PIL import Image

from audit_icl import audit_icl, audit_flash_allocations, library_id


def compile_icl(input_dir: Path, output: Path) -> None:
    source = Path(input_dir).resolve()
    output = Path(output).resolve()
    resource_id = library_id(output)
    if resource_id not in {32, 36, 60}:
        raise ValueError("Expected glyph library 36, background library 32, or status library 60")
    files = sorted(source.glob("*"))
    files = [p for p in files if p.is_file() and p.suffix.lower() in {".png", ".jpg", ".jpeg", ".bmp"}]
    if not files:
        raise ValueError("No page images")
    output.parent.mkdir(parents=True, exist_ok=True)
    ids = set()
    ps = shutil.which("pwsh")
    if not ps:
        raise RuntimeError("PowerShell 7 is required to load the official modern ICL compiler")
    # Only task-owned temporary JPEGs are cleaned up; input images are never edited.
    with tempfile.TemporaryDirectory(prefix="icl-jpeg-", dir=output.parent) as temporary:
        stage = Path(temporary)
        for file in files:
            match = re.match(r"^(\d+)", file.name)
            if not match:
                raise ValueError(f"Image filename must start with its ID: {file.name}")
            image_id = int(match[1])
            if image_id > 999 or image_id in ids:
                raise ValueError(f"Invalid or duplicate image ID {image_id}")
            ids.add(image_id)
            with Image.open(file) as im:
                width, height = im.size
                if resource_id == 32 and im.size != (800, 480):
                    raise ValueError(f"Expected landscape 800x480 background, got {im.size}: {file.name}")
                if not 1 <= width <= 800 or not 1 <= height <= 800 or width % 4:
                    raise ValueError(f"Invalid T5L image dimensions {im.size}; width must be a multiple of four")
                rgb = im.convert("RGB")
                jpg = stage / f"{image_id:03d}.jpg"
                rgb.save(jpg, quality=95, subsampling=0, optimize=False, progressive=False)
                if jpg.stat().st_size >= 248 * 1024:
                    raise ValueError(f"Image {image_id} reaches conservative T5L1 248 KiB JPEG limit")
        subprocess.run([
            ps, "-NoProfile", "-File", str(Path(__file__).with_name("compile_icl_modern.ps1")),
            "-InputDir", str(stage), "-OutputFile", str(output), "-LibraryId", str(resource_id),
        ], check=True)
    audit_icl(output, expected_ids=ids)
    # Check sibling ICLs too: an oversized background must not overwrite icons.
    audit_flash_allocations(list(output.parent.glob("*.ICL")) + list(output.parent.glob("*.icl")))


if __name__ == "__main__":
    if len(sys.argv) != 3:
        raise SystemExit("usage: compile_icl.py NUMBERED_IMAGE_DIRECTORY OUTPUT_32_OR_60.ICL")
    compile_icl(Path(sys.argv[1]), Path(sys.argv[2]))
