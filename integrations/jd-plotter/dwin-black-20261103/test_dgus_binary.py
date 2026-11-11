import struct
import tempfile
import unittest
from pathlib import Path

from dgus_binary import (
    TouchButton, PageJump, NumericDisplay, TextDisplay, VariableIcon, Rect,
    build_touch_file, build_display_file, parse_touch_file,
    parse_display_file, write_control_files, validate_flash_allocations,
    build_vp_init, write_vp_init, PLOTTER_VP_DEFAULTS, VP_INIT_BYTES,
    FLASH_SLOT_BYTES, TOUCH_COMPAT_BYTES,
)


class DGUSNativeFormatTests(unittest.TestCase):
    def test_return_key_exact_manual_offsets_and_endianness(self):
        r = TouchButton(2, (10, 20, 80, 90), key=0x1234, vp=0x1000, target_page=3, hold_tenths=10).encode()
        self.assertEqual(len(r), 32)
        self.assertEqual(r[:16].hex(), "0002000a00140050005a0003fffffe05")
        self.assertEqual(r[16:].hex(), "fe10000012340a000000000000000000")

    def test_native_basic_page_jump_is_16_bytes(self):
        r = PageJump(0, (0, 0, 40, 40), 1).encode()
        self.assertEqual(r.hex(), "000000000000002800280001ffffffff")
        self.assertEqual(len(r), 16)

    def test_touch_stream_mixed_lengths_and_erased_tail(self):
        data = build_touch_file([TouchButton(1, (20, 20, 80, 80), 7), PageJump(0, (0, 0, 40, 40), 1)])
        audit = parse_touch_file(data)
        self.assertEqual(len(data), TOUCH_COMPAT_BYTES)
        self.assertEqual([x["offset"] for x in audit], [0, 16])
        self.assertEqual([x["page"] for x in audit], [0, 1])
        self.assertEqual(audit[1]["key"], 7)
        self.assertEqual(data[48:], b"\xff" * (TOUCH_COMPAT_BYTES - 48))

    def test_reject_touch_overlap_and_offscreen(self):
        with self.assertRaisesRegex(ValueError, "Overlapping"):
            build_touch_file([PageJump(0, (0, 0, 40, 40), 1), TouchButton(0, (40, 40, 80, 80), 7)])
        with self.assertRaises(ValueError):
            TouchButton(0, (0, 0, 480, 799), 1).encode()

    def test_numeric_exact_32_byte_layout(self):
        r = NumericDisplay(1, 0x1100, 30, 40, integer_digits=4, decimals=2, data_type=1, color=0x1234, unit="mm").encode()
        self.assertEqual(len(r), 32)
        self.assertEqual(struct.unpack_from(">4H", r), (0x5A10, 0xFFFF, 13, 0x1100))
        self.assertEqual(struct.unpack_from(">3H", r, 8), (30, 40, 0x1234))
        self.assertEqual(r[14:21], bytes([0, 16, 0, 4, 2, 1, 2]))
        self.assertEqual(r[21:23], b"mm")
        self.assertEqual(r[23:], bytes(9))

    def test_text_exact_32_byte_layout(self):
        r = TextDisplay(0, 0x1120, Rect(20, 100, 450, 150), max_bytes=40).encode()
        self.assertEqual(len(r), 32)
        self.assertEqual(struct.unpack_from(">4H", r), (0x5A11, 0xFFFF, 13, 0x1120))
        self.assertEqual(struct.unpack_from(">4H", r, 14), (20, 100, 450, 150))
        self.assertEqual(struct.unpack_from(">H", r, 22)[0], 40)
        self.assertEqual(r[24:32], bytes([0, 0, 12, 24, 0x80, 0, 0, 0]))

    def test_pages_have_native_fixed_stride_no_custom_header(self):
        data = build_display_file([NumericDisplay(2, 0x1100, 10, 20), TextDisplay(0, 0x1120, (10, 10, 400, 60))], page_count=3)
        self.assertEqual(len(data), FLASH_SLOT_BYTES)
        self.assertEqual(data[:2], b"\x5a\x11")
        self.assertEqual(data[2048:4096], b"\xff" * 2048)
        self.assertEqual(data[4096:4098], b"\x5a\x10")
        audit = parse_display_file(data, page_count=3)
        self.assertEqual([x["page"] for x in audit], [0, 2])

    def test_indexed_display_keeps_empty_pages_in_the_directory(self):
        controls = [
            NumericDisplay(2, 0x1200, 10, 20),
            NumericDisplay(0, 0x1100, 10, 20),
        ]
        data = build_display_file(controls, page_count=3, indexed=True)
        self.assertEqual(data[:16].hex(), "14444755535f32100003000000000000")
        self.assertEqual(data[16:32].hex(), "01004000000040200100402000004040")
        self.assertEqual(data[0x4000:], controls[1].encode() + controls[0].encode() + b"\xff" * 32)
        decoded = parse_display_file(data, page_count=3)
        self.assertEqual([(r["page"], r["vp"]) for r in decoded], [(0, 0x1100), (2, 0x1200)])

    def test_variable_icon_exact_32_byte_layout_and_decimal_library(self):
        r = VariableIcon(0, 0x1111, 40, 80, value_min=0, value_max=2, icon_min=6, icon_max=8, library_id=33).encode()
        self.assertEqual(len(r), 32)
        self.assertEqual(struct.unpack_from(">4H", r), (0x5A00, 0xFFFF, 10, 0x1111))
        self.assertEqual(struct.unpack_from(">6H", r, 8), (40, 80, 0, 2, 6, 8))
        self.assertEqual(r[20:26], bytes([0x21, 1, 0, 255, 255, 1]))
        self.assertEqual(r[26:], bytes(6))
        data = build_display_file([VariableIcon(0, 0x1111, 40, 80)], page_count=1)
        self.assertEqual(parse_display_file(data, page_count=1)[0]["description_words"], 10)

    def test_variable_icon_range_and_screen_validation(self):
        for icon in (VariableIcon(0, 0x1111, 200, 80), VariableIcon(0, 0x1111, 40, 80, icon_max=5), VariableIcon(0, 0x1111, 40, 80, library_id=64)):
            with self.assertRaises(ValueError):
                icon.encode()

    def test_flash_map_prevents_shared_icl_font_slot_overlap(self):
        with self.assertRaisesRegex(ValueError, "overlap"):
            validate_flash_allocations({"32_Background.ICL": 300000, "33_Icons.ICL": 20000})
        audit = validate_flash_allocations({"13_Touch.BIN": 32768, "14_Display.BIN": 262144, "32_Background.ICL": 200000, "33_Icons.ICL": 20000})
        self.assertEqual([(x["first_slot"], x["last_slot"]) for x in audit], [(13, 13), (14, 14), (32, 32), (33, 33)])
        with self.assertRaisesRegex(ValueError, "resident ASCII"):
            validate_flash_allocations({"0_Font.BIN": 100})

    def test_capacity_64_controls_per_page_and_page_bounds(self):
        with self.assertRaisesRegex(ValueError, "Too many"):
            build_display_file([NumericDisplay(0, 0x1100, 10, 20)] * 65, page_count=1)
        with self.assertRaisesRegex(ValueError, "background"):
            build_display_file([NumericDisplay(1, 0x1100, 10, 20)], page_count=1)

    def test_no_system_vp_or_unavailable_font_claims(self):
        for control in (TouchButton(0, (10, 10, 80, 80), 1, vp=0x0084), NumericDisplay(0, 0xFF00, 10, 20)):
            with self.assertRaises(ValueError):
                control.encode()
        with self.assertRaises(UnicodeEncodeError):
            NumericDisplay(0, 0x1100, 10, 20, unit="мм").encode()
        with self.assertRaises(ValueError):
            TextDisplay(0, 0x1120, (10, 10, 400, 80), font_height=25).encode()

    def test_corrupt_tail_or_header_detected(self):
        touch = bytearray(build_touch_file([TouchButton(0, (10, 10, 50, 50), 1)], full_slot=False))
        touch[-1] = 0
        with self.assertRaises(ValueError):
            parse_touch_file(touch)
        display = bytearray(build_display_file([NumericDisplay(0, 0x1100, 20, 20)], page_count=1, full_slot=False))
        display[0] = 0
        with self.assertRaises(ValueError):
            parse_display_file(display, page_count=1)

    def test_writer_only_outputs_ui_controls_not_hardware_firmware(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "DWIN_SET"
            report = write_control_files(path, [PageJump(0, (10, 10, 50, 50), 0)], [NumericDisplay(0, 0x1100, 20, 20)], page_count=1)
            self.assertEqual(sorted(x.name for x in path.iterdir()), ["13_Touch.BIN", "14_Display.BIN"])
            self.assertEqual(len(report["touches"]), 1)
            self.assertTrue((path.parent / "control-file-audit.json").is_file())

    def test_vp_initialization_exact_raw_offsets_and_startup_values(self):
        data = build_vp_init()
        self.assertEqual(len(data), VP_INIT_BYTES)
        self.assertEqual(data[:0x2000], bytes(0x2000))
        for vp, value in PLOTTER_VP_DEFAULTS.items():
            self.assertEqual(struct.unpack_from(">H", data, vp * 2)[0], value)
        self.assertEqual(data[0x2202:0x2204], b"\x00\x01")  # VP1101, protocol
        self.assertEqual(data[0x2400:0x2404], b"\x00\x01\x00\x32")
        self.assertEqual(sum(data), 52)

    def test_vp_initialization_manual_example_no_address_bias(self):
        data = build_vp_init({0x2000: 0x1234, 0x2010: 100, 0xFEFF: 0xABCD})
        self.assertEqual(data[0x4000:0x4002], b"\x12\x34")
        self.assertEqual(data[0x4020:0x4022], b"\x00\x64")
        self.assertEqual(data[0x1FDFE:0x1FE00], b"\xab\xcd")
        self.assertEqual(data[0x2000:0x2002], b"\x00\x00")

    def test_vp_initialization_refuses_system_vps_and_non_word_values(self):
        for words in ({0x0084: 1}, {0x1100: -1}, {0x1100: 65536}, {0x1100: True}, {0xFF00: 1}):
            with self.assertRaises(ValueError):
                build_vp_init(words)

    def test_vp_initialization_writer_preserves_cfg_and_documents_bit5(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "DWIN_SET"
            path.mkdir()
            cfg = path / "T5LCFG.CFG"
            cfg.write_bytes(b"UNCHANGED")
            report = write_vp_init(path)
            self.assertEqual(cfg.read_bytes(), b"UNCHANGED")
            self.assertEqual((path / "22_VP_Init.BIN").read_bytes(), build_vp_init())
            self.assertIn("bit5=1", report["power_on_requirement"])
            self.assertFalse(report["cfg_changed"])

    def test_landscape_signed32_numeric_coordinates_and_native_stride(self):
        controls = [NumericDisplay(1, 0x1102, 600, 370, font_width=12,
                                   integer_digits=4, decimals=2, data_type=1)]
        data = build_display_file(controls, page_count=2, width=800, height=480)
        record = data[2048:2080]
        self.assertEqual(struct.unpack_from(">2H", record, 8), (600, 370))
        self.assertEqual(record[18:20], b"\x02\x01")
        self.assertEqual(parse_display_file(data, page_count=2)[0]["vp"], 0x1102)
        words = struct.pack(">i", -12345)
        self.assertEqual(words, b"\xff\xff\xcf\xc7")  # -123.45 mm
        self.assertEqual(struct.unpack(">2H", words), (0xFFFF, 0xCFC7))


if __name__ == "__main__":
    unittest.main(verbosity=2)
