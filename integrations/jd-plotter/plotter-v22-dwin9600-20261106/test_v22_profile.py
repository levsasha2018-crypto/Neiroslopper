import copy
import sys
import unittest
from pathlib import Path
import yaml

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / "PROGRAM"))
from machine_profiles import DLC32, fluidnc_configuration, fluidnc_pen_profile, pen_matches, settings_ready
from pen_lift import PenLiftSettings

class V22Tests(unittest.TestCase):
    def setUp(self):
        self.real_dump = (ROOT / "firmware/jd_v22_dwin9600.yaml").read_text(encoding="utf-8")
        self.config = yaml.safe_load(self.real_dump)

    def test_actual_physical_controller_dump_and_no_limit_mode(self):
        settings = fluidnc_configuration(self.real_dump.splitlines())
        self.assertTrue(settings_ready(settings, DLC32))
        self.assertEqual(settings[21], 0)
        self.assertEqual(settings[100], 80)
        self.assertEqual(settings["_axis_layout"], "x_carriage_y_portal")
        self.assertEqual(settings["_dwin_uart_baud"], 9600)
        pen = PenLiftSettings(True, True, True, 250, 250, True, True)
        self.assertTrue(pen_matches(fluidnc_pen_profile(settings), pen, DLC32))
        self.assertFalse(pen_matches(fluidnc_pen_profile(), pen, DLC32))

    def test_wrong_motor_relay_uart_or_startup_macro_rejected(self):
        for mutate in (
            lambda c: c["axes"]["X"]["motor0"]["stepstick"].update(step_pin="i2so.1"),
            lambda c: c["coolant"].update(flood_pin="gpio.4"),
            lambda c: c["uart1"].update(baud=115200),
            lambda c: c["axes"]["Y"]["motor1"]["stepstick"].update(direction_pin="I2SO.4"),
            lambda c: c["macros"].update(startup_line0="G0 X100"),
            lambda c: c["axes"]["X"]["motor0"].update(limit_neg_pin="gpio.35"),
        ):
            config = copy.deepcopy(self.config)
            mutate(config)
            with self.assertRaises(ValueError):
                fluidnc_configuration(yaml.safe_dump(config).splitlines())

    def test_partial_middle_sections_are_not_treated_as_safe_defaults(self):
        # A lost middle block can still leave a syntactically valid YAML with
        # report_inches=False at its end. It must not certify unknown startup.
        for section in ("start", "macros", "axes", "coolant", "uart1"):
            partial = copy.deepcopy(self.config)
            partial.pop(section)
            with self.subTest(section=section), self.assertRaisesRegex(ValueError, "ответ неполный"):
                fluidnc_configuration(yaml.safe_dump(partial).splitlines())
        partial = copy.deepcopy(self.config)
        partial["macros"].pop("after_reset")
        with self.assertRaisesRegex(ValueError, "автоматические макросы"):
            fluidnc_configuration(yaml.safe_dump(partial).splitlines())

    def test_case_conflicting_axis_names_are_rejected(self):
        config = copy.deepcopy(self.config)
        config["axes"]["x"] = copy.deepcopy(config["axes"]["X"])
        with self.assertRaisesRegex(ValueError, "регистр"):
            fluidnc_configuration(yaml.safe_dump(config).splitlines())

if __name__ == "__main__":
    unittest.main()
