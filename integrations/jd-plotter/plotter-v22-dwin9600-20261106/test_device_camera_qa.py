"""Camera confirmation and recovery checks without a camera or plotter."""
import gc
from pathlib import Path
import sys
import tkinter as tk
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parent / 'PROGRAM'))

from geometry import Sheet

try:
    import numpy as np
    from camera_ui import CameraWindow
except ImportError:
    CameraWindow = None


@unittest.skipIf(CameraWindow is None, 'Optional camera dependencies are not installed')
class CameraConfirmationTests(unittest.TestCase):
    def setUp(self):
        try:
            root = tk.Tk()
        except tk.TclError as exc:
            self.skipTest(str(exc))
        root.withdraw()
        self.addCleanup(root.destroy)
        self.context = {'connected': True, 'busy': False, 'position': (10, 10),
                        'revision': ('board1', 'origin1'), 'sheet': Sheet(100, 80),
                        'simulation': True, 'pen_lift_enabled': False}
        self.submit_scan = Mock(return_value=True)
        self.window = CameraWindow(root, lambda: dict(self.context), self.submit_scan)
        self.window.root.withdraw()
        self.addCleanup(self.close_window)
        self.window.client = Mock()
        self.window.calibration = SimpleNamespace(resolution=(20, 20))
        self.window.capture_url.set('http://127.0.0.1/capture')
        self.image = np.zeros((20, 20, 3), np.uint8)
        self.window.tiles = [(self.image, (4, 5))]
        self.window.tile_pixel_count = 400
        self.window.tile_area = (30, 20)
        self.window.tile_revision = ('old',)
        planner = patch('camera_ui.scan_plan', return_value=[(5, 6)])
        planner.start()
        self.addCleanup(planner.stop)

    def close_window(self):
        self.window.close()
        self.window = None
        gc.collect()  # Release Tcl variables on the Tk thread before worker tests.

    def assert_old_scan_kept(self):
        self.assertEqual(len(self.window.tiles), 1)
        self.assertIs(self.window.tiles[0][0], self.image)
        self.assertEqual(self.window.tile_pixel_count, 400)
        self.assertEqual(self.window.tile_area, (30, 20))
        self.assertEqual(self.window.tile_revision, ('old',))

    def test_refused_scan_preserves_previous_tiles(self):
        self.submit_scan.side_effect = [False, True]
        with patch('camera_ui.messagebox.askokcancel', return_value=True):
            self.window.start_scan()
            self.assert_old_scan_kept()
            self.assertFalse(self.window.scan_active)
            self.assertIn('не запущен', self.window.status.get())
            self.window.start_scan()
        self.assertTrue(self.window.scan_active)
        self.assertEqual(self.window.tiles, [])
        self.assertEqual(self.window.tile_revision, ('board1', 'origin1'))

    def test_camera_frame_lost_while_confirming_never_submits_motion(self):
        self.window.client.get_frame.side_effect = [object(), ValueError('Свежего кадра нет')]
        with patch('camera_ui.messagebox.askokcancel', return_value=True):
            self.window.start_scan()
        self.submit_scan.assert_not_called()
        self.assert_old_scan_kept()

    def test_other_board_or_origin_same_position_requires_a_new_confirmation(self):
        def confirm(*args, **kwargs):
            self.context['revision'] = ('board2', 'origin2')
            return True

        with patch('camera_ui.messagebox.askokcancel', side_effect=confirm):
            self.window.start_scan()
        self.submit_scan.assert_not_called()
        self.assert_old_scan_kept()
        self.assertIn('Состояние пульта изменилось', self.window.status.get())

    def test_invalid_camera_address_keeps_old_connection_and_tiles(self):
        client = self.window.client
        self.window.stream_url.set('not-a-camera-url')
        with patch('camera_ui.messagebox.askokcancel', return_value=True), \
                patch('camera_client.threading.Thread') as start_worker:
            self.window.connect()
        self.assertIs(self.window.client, client)
        client.close.assert_not_called()
        start_worker.assert_not_called()
        self.assert_old_scan_kept()


if __name__ == '__main__':
    unittest.main()
