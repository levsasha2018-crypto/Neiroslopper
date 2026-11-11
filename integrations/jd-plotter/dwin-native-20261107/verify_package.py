"""Independent package-level checks, without COM, GUI or SD access."""
import hashlib
import json
from pathlib import Path
import struct
import sys
from collections import Counter

from PIL import Image

ROOT = Path(__file__).resolve().parent
PREVIOUS = ROOT.parent / "dwin-black-20261103"
SIMPLE = ROOT.parent / "dwin-simple-20261107"
HOST = ROOT.parent / "plotter-v22-dwin9600-20261106" / "PROGRAM"
sys.path.extend((str(PREVIOUS), str(HOST)))

from audit_icl import audit_icl
from dgus_binary import parse_touch_file, parse_display_file, validate_flash_allocations, build_vp_init
from dwin_bridge import COMMANDS, STEPS


WORK, MOVE, PAPER, CONFIRM = 1, 2, 4, 7
RETAINED_KEYS = {
    WORK: {12, 14, 15, 16, 22},
    MOVE: {1, 2, 3, 4, 14},
    PAPER: {10, 11, 14},
    CONFIRM: {13, 14},
}


def overlaps(a, b):
    """Half-open pixel rectangles; touching edges do not obscure each other."""
    return a[0] < b[2] and a[2] > b[0] and a[1] < b[3] and a[3] > b[1]


def numeric_bounds(record):
    """Conservative native ASCII footprint, including sign, point and unit.

    DGUS font-size is the horizontal pixel advance, unlike the proportional
    Segoe UI font formerly used for sample previews. The built-in #0 font is
    twice that width in height. Signed types reserve a minus even when the
    illustrative preview value happens to be positive.
    """
    raw = bytes.fromhex(record["record_hex"])
    assert record["type"] == 0x5A10
    x, y = struct.unpack_from(">2H", raw, 8)
    font_id, width, alignment, integer, decimals, data_type, unit_len = raw[14:21]
    assert font_id == 0, "Native numeric footprint requires resident ASCII font #0"
    assert alignment == 0, "Explicit numeric geometry is required for non-left alignment"
    assert 4 <= width <= 64 and 1 <= integer and integer + decimals <= 20
    assert data_type in range(7) and unit_len <= 11
    raw[21:21 + unit_len].decode("ascii")
    chars = integer + decimals + int(bool(decimals)) + int(data_type in (0, 1, 4)) + unit_len
    return (x, y, x + chars * width, y + width * 2)


def audit_dynamic_layout(manifest, display):
    """Check actual native rectangles, not only the illustrative PNGs."""
    width, height = manifest["width"], manifest["height"]
    areas = []
    for icon in manifest["icons"]:
        areas.append((icon["page"], (icon["x"], icon["y"],
                    icon["x"] + icon["width"], icon["y"] + icon["height"]),
                    f"icon {icon['name']}"))
    for record in display:
        if record["type"] == 0x5A10:
            areas.append((record["page"], numeric_bounds(record), f"number VP {record['vp']:04X}"))
    for index, (page, bounds, name) in enumerate(areas):
        assert 0 <= bounds[0] < bounds[2] <= width and 0 <= bounds[1] < bounds[3] <= height, (page, name, bounds)
        for text in manifest["static_text"]:
            assert page != text["page"] or not overlaps(bounds, text["bounds"]), (
                f"Native {name} on page {page} obscures label {text['text']!r}")
        for other_page, other_bounds, other_name in areas[:index]:
            assert page != other_page or not overlaps(bounds, other_bounds), (
                f"Native {name} overlaps {other_name} on page {page}")
    return len(areas)


def audit_native_boot(manifest, touch, display):
    """Validate the decoded binary startup flow, independently of its builder."""
    animations = [record for record in display if record["type"] == 0x5A04]
    if "splash_page" not in manifest:
        assert not animations, "Native animation is missing its startup manifest"
        return None
    splash, home = manifest["splash_page"], manifest["home_page"]
    assert (splash, home) == (0, 1), "Native boot must be splash 0 -> home 1"
    assert len(animations) == 1, "Exactly one startup animation is required"
    animation = animations[0]
    assert (animation["page"], animation["vp"], animation["first_page"], animation["last_page"],
            animation["frame_ticks"], animation["frame_ms"], animation["library_id"],
            animation["expanded_return_page"]) == (splash, 0, splash, home, 250, 2000, 32, home)
    assert [record for record in display if record["page"] == splash] == animations, "Splash has an old status overlay"
    skip = [control for control in touch if control["page"] == splash]
    assert len(skip) == 1 and skip[0].get("code") == 0xFFFF and skip[0].get("target_page") == home
    assert skip[0].get("rect") == [0, 0, 799, 479] and "vp" not in skip[0]
    assert not any(control["target_page"] == splash for control in touch), "Navigation leads back to boot logo"
    return {"splash_page": splash, "home_page": home, "nominal_duration_ms": 2000,
            "host_required": False, "explicit_library_id": 32,
            "local_tap_skip": True, "physical_timing_verified": False}


def verify():
    directory = ROOT / "DWIN_SET"
    expected = {"13_Touch.BIN", "14_Display.BIN", "22_VP_Init.BIN", "32_Backgrounds.ICL", "60_Status.ICL"}
    assert {p.name for p in directory.iterdir()} == expected, "Unexpected deployable files"
    manifest = json.loads((ROOT / "ui-manifest.json").read_text(encoding="utf-8"))
    assert (manifest["width"], manifest["height"]) == (800, 480)
    touch = parse_touch_file((directory / "13_Touch.BIN").read_bytes())
    previous_touch = parse_touch_file((SIMPLE / "DWIN_SET/13_Touch.BIN").read_bytes())
    def outside_work(controls):
        # Adding one control shifts later records in the file, not on the panel.
        return [{k: v for k, v in c.items() if k != "offset"} for c in controls if c["page"] != WORK]
    assert outside_work(touch) == outside_work(previous_touch)
    def button_signature(control):
        return control.get("vp"), control.get("key"), control["target_page"]
    assert Counter(button_signature(c) for c in touch if c["page"] == WORK and c.get("key") != 22) == (
        Counter(button_signature(c) for c in previous_touch if c["page"] == WORK))
    assert manifest["pc_check"] == {"vp": 0x1000, "key": 22, "pending": 11, "answered": 12, "timeout": 13}
    page_count = len(manifest["pages"])
    assert 1 <= page_count <= 128
    display = parse_display_file((directory / "14_Display.BIN").read_bytes(), page_count=page_count)
    assert len(touch) == len(manifest["hotspots"]), "Native touch file differs from manifest"
    assert len([c for c in display if c["type"] == 0x5A00]) == len(manifest["icons"]), "Native icon count differs from manifest"
    operational_pages = set(manifest["operational_pages"])
    navigation_pages = set(manifest["navigation_pages"])
    reserved_pages = set(manifest["reserved_pages"])
    assert page_count == 9 and reserved_pages == {3, 5, 6, 8}
    assert operational_pages == set(RETAINED_KEYS) and navigation_pages == {WORK, MOVE, PAPER}
    assert not any(c["page"] in reserved_pages for c in display), "Unexpected display on a reserved page"
    assert not any(c["target_page"] in reserved_pages for c in touch), "A reserved page is reachable"
    expected_targets = {0: {WORK: 1}, WORK: {WORK: 1, MOVE: 1, PAPER: 1, CONFIRM: 1},
                        MOVE: {WORK: 1, MOVE: 1, PAPER: 1},
                        PAPER: {WORK: 1, MOVE: 2, PAPER: 1},
                        CONFIRM: {WORK: 2, MOVE: 1, PAPER: 1}}
    for page in range(page_count):
        controls = [c for c in touch if c["page"] == page]
        assert Counter(c["target_page"] for c in controls if "vp" not in c) == expected_targets.get(page, {})
        assert Counter(c["key"] for c in controls if c.get("vp") == 0x1000) == dict.fromkeys(RETAINED_KEYS.get(page, ()), 1)
        steps = [c["key"] for c in controls if c.get("vp") == 0x1200]
        speeds = [c["key"] for c in controls if c.get("vp") == 0x1201]
        assert sorted(steps) == (list(range(len(STEPS))) if page == MOVE else [])
        assert sorted(speeds) == ([10, 25, 50, 75, 100] if page == MOVE else [])
        for i, c in enumerate(controls):
            x0, y0, x1, y1 = c["rect"]
            assert 0 <= x0 < x1 < 800 and 0 <= y0 < y1 < 480
            assert c["target_page"] == 0xFFFF or c["target_page"] in range(page_count)
            for other in controls[:i]:
                a, b, d, e = other["rect"]
                assert not (x0 <= d and x1 >= a and y0 <= e and y1 >= b), "Touch overlap"
            if c.get("vp") == 0x1000:
                assert c["key"] in COMMANDS or (page == WORK and c["key"] == 22)
                assert c["target_page"] == (WORK if c["key"] == 13 else 0xFFFF)
            elif c.get("vp") == 0x1200:
                assert c["key"] in range(len(STEPS))
                assert c["target_page"] == 0xFFFF
            elif c.get("vp") == 0x1201:
                assert 1 <= c["key"] <= 100
                assert c["target_page"] == 0xFFFF
            else:
                assert c["code"] == 0xFFFF
    for ctrl, expected_ctrl in zip(touch, sorted(manifest["hotspots"], key=lambda x: x["page"])):
        assert ctrl["rect"] == expected_ctrl["rect"]
        assert ctrl.get("key") == expected_ctrl["key"]
        assert ctrl.get("vp") == expected_ctrl["vp"]
        assert ctrl["target_page"] == (0xFFFF if expected_ctrl["target"] is None else expected_ctrl["target"])
    for text in manifest["static_text"]:
        x0, y0, x1, y1 = text["bounds"]
        assert x0 >= 0 and y0 >= 0 and x1 <= 800 and y1 <= 480
    backgrounds = audit_icl(directory / "32_Backgrounds.ICL", expected_pages=page_count, expected_ids=range(page_count))
    assert all((p["width"], p["height"]) == (800, 480) for p in backgrounds["pages"])
    statuses = audit_icl(directory / "60_Status.ICL", expected_pages=manifest["frames"], expected_ids=range(manifest["frames"]))
    frame_dims = {p["page"]: (p["width"], p["height"]) for p in statuses["pages"]}
    for info in manifest["icons"]:
        assert info["last"] - info["first"] + 1 == len(info["labels"])
        for i in range(info["first"], info["last"] + 1):
            assert frame_dims[i] == (info["width"], info["height"])
        assert 0 <= info["x"] <= 800 - info["width"]
        assert 0 <= info["y"] <= 480 - info["height"]
        if info["vp"] == 0x1110:
            assert info["labels"][10:14] == ["Ожидаю команду от ПК", "Проверяю связь с ПК",
                                             "ПК ответил", "ПК не ответил"]
        elif info["vp"] == 0x1111:
            assert info["labels"] == ["Нет связи с MKS", "MKS не готова", "MKS готова"]
    assert {info["page"] for info in manifest["icons"] if info["vp"] == 0x1110} == operational_pages
    actual_icons = []
    for d in display:
        raw = bytes.fromhex(d["record_hex"])
        if d["type"] == 0x5A00:
            x, y, minimum, maximum, first, last = struct.unpack_from(">6H", raw, 8)
            assert raw[20] == 60 and raw[21] == 1
            assert maximum - minimum == last - first
            actual_icons.append((d["page"], d["vp"], x, y, minimum, maximum, first, last))
        elif d["vp"] in (0x1102, 0x1104, 0x1106, 0x1108):
            assert raw[18] == 2 and raw[19] == 1, "Coordinates must be signed32 fixed-point"
    expected_icons = [(info["page"], info["vp"], info["x"], info["y"], 0, len(info["labels"]) - 1,
                       info["first"], info["last"]) for info in manifest["icons"]]
    assert Counter(actual_icons) == Counter(expected_icons), "Native icon fields differ from manifest"
    assert {0x1102, 0x1104, 0x1106, 0x1108, 0x110B, 0x1201} <= {
        d["vp"] for d in display if d["type"] == 0x5A10}, "Missing numeric telemetry"
    dynamic_areas = audit_dynamic_layout(manifest, display)
    native_boot = audit_native_boot(manifest, touch, display)
    startup_records = [c for c in display if c["type"] == 0x5A04]
    official = json.loads((PREVIOUS / "official-animation-audit.json").read_text(encoding="utf-8-sig"))
    assert startup_records[0]["record_hex"] == official["record_hex"]
    with Image.open(ROOT / "backgrounds/000.png") as actual, Image.open(PREVIOUS / "backgrounds/000.png") as saved:
        assert actual.convert("RGB").tobytes() == saved.convert("RGB").tobytes(), "Boot artwork changed"
    assert (directory / "22_VP_Init.BIN").read_bytes() == build_vp_init()
    for p in (ROOT / "preview").glob("*.png"):
        with Image.open(p) as im:
            assert im.size == (800, 480)
    sizes = {p.name: p.stat().st_size for p in directory.iterdir()}
    allocation = validate_flash_allocations(sizes)
    hashes = {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in sorted(directory.iterdir())}
    report = dict(valid=True, hardware_tested=False, width=800, height=480, pages=page_count,
                  touch_controls=len(touch), display_controls=len(display), bitmap_frames=manifest["frames"],
                  native_dynamic_areas=dynamic_areas, waiting_result=manifest["waiting_result"],
                  pc_check=manifest["pc_check"],
                  native_boot=native_boot, active_pages=sorted(operational_pages),
                  navigation_pages=sorted(navigation_pages), reserved_pages=sorted(reserved_pages),
                  retained_touch_commands={page: sorted(keys) for page, keys in RETAINED_KEYS.items()},
                  bytes=sum(sizes.values()), allocations=allocation, sha256=hashes,
                  icl_audit=[backgrounds, statuses],
                  requires="DGUS II, 64 controls/page, backgrounds at32, CRC disabled; current hardware CFG unchanged")
    (ROOT / "package-verification.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({k: report[k] for k in ("valid", "hardware_tested", "pages", "touch_controls", "display_controls", "bitmap_frames", "bytes")}))


if __name__ == "__main__":
    verify()
