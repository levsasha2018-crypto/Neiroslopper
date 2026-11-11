import sys
import unittest
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent / "PROGRAM"))
from controller import board_runtime_info, dgus_marker


class RuntimeDiagnosticsTests(unittest.TestCase):
    def test_startup_runtime_and_reset_reason_are_recorded(self):
        info = ["[VER:4.1 FluidNC v4.1.1-JD-DGUS1.2 (esp32-wifi) :]",
                "[JD-DGUS:1,PORT=8888,BAUD=9600,UPTIME_MS=65536,RESET_REASON=9]]"]
        self.assertEqual(board_runtime_info(info), {"firmware_uptime_ms": 65536, "firmware_reset_reason": 9})

    def test_installed_older_firmware_remains_compatible(self):
        self.assertEqual(board_runtime_info(["[JD-DGUS:1,PORT=8888,BAUD=9600]]"]), {})

    def test_native_firmware_marker_can_be_wrapped_in_msg(self):
        marker = "[JD-DGUS:2,PORT=8888,BAUD=9600,UI=1,UPTIME_MS=42833,RESET_REASON=3]"
        wrapped = "[MSG:" + marker + "]"
        self.assertEqual(dgus_marker([wrapped]), marker)
        self.assertEqual(board_runtime_info([wrapped]), {"firmware_uptime_ms": 42833, "firmware_reset_reason": 3})
        self.assertEqual(dgus_marker([wrapped, marker]), "")

    def test_ambiguous_or_out_of_range_diagnostics_are_ignored(self):
        for marker in ("[JD-DGUS:1,UPTIME_MS=1,RESET_REASON=3,RESET_REASON=9]",
                       "[JD-DGUS:1,UPTIME_MS=4294967296,RESET_REASON=3]",
                       "[JD-DGUS:1,UPTIME_MS=1,RESET_REASON=256]",
                       "[MSG:USER:UPTIME_MS=5,RESET_REASON=3]"):
            with self.subTest(marker=marker):
                self.assertEqual(board_runtime_info([marker]), {})


if __name__ == "__main__":
    unittest.main()
