import copy
import sys
import threading
import unittest
from unittest.mock import Mock, call, patch
from pathlib import Path
import yaml

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / "PROGRAM"))
from controller import GrblSession, axis_feed_limit
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

    def test_speed_limits_are_read_back_without_motion_or_flash_write(self):
        callback = Mock()
        session = GrblSession(Mock(), threading.Event(), callback, DLC32)
        session.settings = fluidnc_configuration(self.real_dump.splitlines())

        def updated():
            session.settings.update({110: 1500, 111: 1500, 120: 40, 121: 40})

        with patch.object(session, "ready") as ready, patch.object(session, "command") as command, \
             patch.object(session, "refresh_settings", side_effect=updated):
            session.set_motion_limits(1500, 40)
        ready.assert_called_once()
        self.assertEqual(command.call_args_list, [
            call("$/axes/x/max_rate_mm_per_min=1500.000"),
            call("$/axes/y/max_rate_mm_per_min=1500.000"),
            call("$/axes/x/acceleration_mm_per_sec2=40.000"),
            call("$/axes/y/acceleration_mm_per_sec2=40.000"),
        ])
        self.assertEqual(min(session.settings[110], session.settings[111]), 1500)

    def test_speed_above_i2s_pulse_capacity_is_rejected_before_writes(self):
        session = GrblSession(Mock(), threading.Event(), Mock(), DLC32)
        session.settings = fluidnc_configuration(self.real_dump.splitlines())
        self.assertEqual(session.settings["_xy_pulse_feed_max"], 93750)
        with patch.object(session, "ready"), patch.object(session, "command") as command:
            with self.assertRaisesRegex(ValueError, "частоте STEP"):
                session.set_motion_limits(100000, 40, persist=True)
        command.assert_not_called()

    def test_speed_uses_the_direction_instead_of_the_slower_axis(self):
        rates = {110: 1800, 111: 900}
        self.assertEqual(axis_feed_limit(rates, (15, 0)), 1800)
        self.assertEqual(axis_feed_limit(rates, (0, -15)), 900)
        self.assertAlmostEqual(axis_feed_limit(rates, (20, -10)), (20**2 + 10**2)**.5 * 90)

    def test_saving_speed_checks_inactive_file_before_switching(self):
        session = GrblSession(Mock(port="tcp://localhost:23"), threading.Event(), Mock(), DLC32)
        lines = self.real_dump.splitlines()
        session.check_network_interlock = Mock()
        session._network_config_dump = Mock(side_effect=[lines, lines])
        session.command = Mock(side_effect=[["$Config/Filename=jd-motion-a.yaml"], [], [],
                                            ["$Config/Filename=jd-motion-b.yaml"]])
        session.save_motion_config()
        self.assertEqual(session.command.call_args_list, [
            call("$Config/Filename"), call("$Config/Dump=jd-motion-b.yaml", timeout=12),
            call("$Config/Filename=jd-motion-b.yaml"), call("$Config/Filename"),
        ])
        self.assertEqual(session._network_config_dump.call_args_list, [call(None), call("jd-motion-b.yaml")])

    def test_saving_speed_keeps_active_file_on_readback_mismatch(self):
        session = GrblSession(Mock(port="tcp://localhost:23"), threading.Event(), Mock(), DLC32)
        session.check_network_interlock = Mock()
        changed = copy.deepcopy(self.config)
        changed["axes"]["X"]["max_rate_mm_per_min"] = 900
        session._network_config_dump = Mock(side_effect=[self.real_dump.splitlines(),
                                                       yaml.safe_dump(changed).splitlines()])
        session.command = Mock(side_effect=[["$Config/Filename=config.yaml"], []])
        with self.assertRaisesRegex(ValueError, "не совпал"):
            session.save_motion_config()
        self.assertEqual(session.command.call_args_list,
                         [call("$Config/Filename"), call("$Config/Dump=jd-motion-a.yaml", timeout=12)])

    def test_case_conflicting_axis_names_are_rejected(self):
        config = copy.deepcopy(self.config)
        config["axes"]["x"] = copy.deepcopy(config["axes"]["X"])
        with self.assertRaisesRegex(ValueError, "регистр"):
            fluidnc_configuration(yaml.safe_dump(config).splitlines())

if __name__ == "__main__":
    unittest.main()
