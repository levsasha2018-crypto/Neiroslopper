"""Desktop end-to-end tests using DEMO or fake links, with no hardware access."""
import gc
import json
from pathlib import Path
import sys
import tempfile
import threading
import time
import tkinter as tk
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parent / 'PROGRAM'))
from app import App
from assistant import cloud_backend
from dwin_bridge import TOUCH_VP, Upload
from dwin_ui import DwinWindow
from geometry import Sheet, load_svg
from machine_profiles import DLC32


class DeviceUITests(unittest.TestCase):
    def setUp(self):
        for guard in (patch('serial.tools.list_ports.comports', return_value=[]),
                      patch('serial.Serial', side_effect=AssertionError('No USB in QA')),
                      patch('network_transport.TcpWire', side_effect=AssertionError('No real TCP in QA'))):
            guard.start()
            self.addCleanup(guard.stop)
        self.root = tk.Tk()
        self.root.withdraw()
        self.app = App(self.root)
        self.addCleanup(self.close_app)
        self.errors = self.enterContext(patch('app.messagebox.showerror'))
        self.enterContext(patch('app.messagebox.askokcancel', return_value=True))

    def close_app(self):
        self.app.close()
        self.app.controller.worker.join(1)
        self.app = self.root = None
        gc.collect()

    def wait_for(self, predicate):
        deadline = time.monotonic() + 4
        while time.monotonic() < deadline:
            self.root.update()
            if predicate():
                return
            time.sleep(.01)
        self.fail('UI did not settle')

    def idle(self):
        self.wait_for(lambda: not self.app.controller.busy and self.app.controller.events.empty()
                      and not self.app.svg_loading)

    def prepare(self):
        self.app.connect()
        self.idle()
        self.app.set_zero()
        self.idle()
        self.app.step.set('20')
        self.app.jog(1, 0)
        self.idle()
        self.app.jog(0, 1)
        self.idle()
        self.app.set_corner()
        self.idle()
        self.assertEqual(self.app.sheet, Sheet(20, 20))

    def test_demo_full_job_with_bad_order_and_svg_retry(self):
        self.app.load_test()
        self.app.draw()
        self.assertFalse(self.app.paths)
        self.assertTrue(self.errors.called)
        self.errors.reset_mock()
        self.prepare()
        self.app.load_test()
        previous = self.app.paths
        with tempfile.TemporaryDirectory() as folder:
            svg = Path(folder) / 'drawing.svg'
            svg.write_text('<svg><path', encoding='utf-8')
            self.app.load_file(svg)
            self.idle()
            self.assertIs(self.app.paths, previous)
            self.assertTrue(self.errors.called)
            self.errors.reset_mock()
            svg.write_text('<svg xmlns="http://www.w3.org/2000/svg"><path d="M0,0 L10,10"/></svg>', encoding='utf-8')
            self.app.load_file(svg)
            self.idle()
        self.assertIsNot(self.app.paths, previous)
        self.assertTrue(self.app.draw())
        self.assertFalse(self.app.draw())
        self.idle()
        self.assertEqual(float(self.app.progress['value']), 100)
        self.assertFalse(self.errors.called)

    def test_large_actual_svg_is_fitted_and_refitted_to_a_new_sheet(self):
        self.prepare()
        with tempfile.TemporaryDirectory() as folder:
            svg = Path(folder) / 'large.svg'
            svg.write_text(
                '<svg xmlns="http://www.w3.org/2000/svg" width="100mm" height="80mm" '
                'viewBox="0 0 100 80" data-plotter-size="actual"><path d="M0,0 L100,80"/></svg>',
                encoding='utf-8',
            )
            self.app.load_file(svg)
            self.idle()
            for actual, expected in zip(self.app.paths[0], [(3.0, 4.4), (17.0, 15.6)]):
                for a, b in zip(actual, expected):
                    self.assertAlmostEqual(a, b)
            self.app.sheet = Sheet(4, 4)
            self.app.refit_svg()
            self.idle()
            for point in self.app.paths[0]:
                self.assertTrue(self.app.sheet.contains(point))
            self.assertAlmostEqual(self.app.paths[0][0][0], 1)
            self.assertAlmostEqual(self.app.paths[0][-1][0], 3)
        self.errors.assert_not_called()

    def test_drawing_and_travel_have_separate_speed_fields(self):
        self.prepare()
        self.app.load_test()
        self.app.feed.set('100')
        self.app.draw_feed.set('40')
        with patch.object(self.app, 'submit', return_value=True) as submit:
            self.assertTrue(self.app.draw())
            self.assertEqual(submit.call_args.kwargs['feed'], 1200)
            self.assertEqual(submit.call_args.kwargs['travel_feed'], 3000)
            self.assertTrue(self.app.go_start())
            self.assertEqual(submit.call_args.kwargs['feed'], 3000)

    def test_stop_blocks_job_until_calibrated_again(self):
        self.prepare()
        self.app.load_test()
        original = self.app.paths
        self.app.stop()
        with patch.object(self.app, 'submit') as submit:
            self.assertFalse(self.app.draw())
            submit.assert_not_called()
        self.assertIs(self.app.paths, original)
        self.assertIsNone(self.app.sheet)

    def test_draw_and_go_start_need_no_confirmation_dialog(self):
        self.prepare()
        self.app.load_test()
        with patch('app.messagebox.askokcancel') as ask:
            self.assertTrue(self.app.go_start())
            self.idle()
            self.assertTrue(self.app.draw())
            self.idle()
        ask.assert_not_called()

    def test_svg_chooser_result_is_stale_after_calibration_change(self):
        self.prepare()
        self.app.load_test()

        def choose(*_args, **_kwargs):
            self.app.stop()
            return 'old-context.svg'

        with patch('app.filedialog.askopenfilename', side_effect=choose), patch.object(self.app, 'load_file') as load:
            self.app.open_svg()
        load.assert_not_called()

    def test_cancelled_svg_read_cannot_accumulate_workers_or_replace_preview(self):
        self.prepare()
        self.app.load_test()
        original = self.app.paths
        started, release = threading.Event(), threading.Event()

        def read(*_args, **_kwargs):
            started.set()
            release.wait(2)
            return [[(2, 2), (9, 9)]]

        with patch('app.load_svg', side_effect=read) as reader:
            try:
                self.app.load_file('first.svg')
                self.assertTrue(started.wait(1))
                self.app.cancel_svg()
                for _ in range(3):
                    self.app.load_file('another.svg')
                reader.assert_called_once()
            finally:
                release.set()
                self.app.svg_worker.join(1)
        self.wait_for(self.app.svg_events.empty)
        self.assertIs(self.app.paths, original)

    def test_invalid_offline_project_does_not_adopt_a_new_sheet(self):
        from editor_project import SketchProject
        with self.assertRaises(ValueError):
            self.app.load_editor_project(SketchProject(Sheet(20, 20), [[(0, 0), (30, 30)]], 'bad'))
        self.assertIsNone(self.app.drawing_sheet())
        self.assertFalse(self.app.paths)

    def test_legacy_dwin_defers_dialog_but_dispatches_stop_immediately(self):
        self.app.open_dwin()
        panel = self.app.dwin_window
        self.assertIsInstance(panel, DwinWindow)
        moment = time.monotonic()
        draw, stop = Upload(TOUCH_VP, (13,), moment), Upload(TOUCH_VP, (14,), moment)
        with patch.object(panel.bridge, 'pending', return_value=[draw, stop]), \
             patch.object(panel, 'dispatch_event') as dispatch:
            panel.pump()
            dispatch.assert_called_once_with(stop)
            self.root.update_idletasks()
            self.assertEqual([call.args[0] for call in dispatch.call_args_list], [stop, draw])

    def test_no_reference_followup_keeps_vehicle_and_previews_only(self):
        self.app.open_assistant()
        window = self.app.assistant_window
        direct = json.dumps({'kind': 'preview', 'explanation': 'Контур машины', 'assumptions': [], 'operations': [
            {'kind': 'polyline', 'parameters': {'points': [[3, 3], [20, 20]], 'mode': 'replace'}}]})
        with patch.object(window, 'open_reference') as trace_panel, \
             patch.object(cloud_backend, 'generate', side_effect=[self.web_drawing_response(), direct]) as model:
            window.entry.insert('1.0', 'Нарисуй Porsche 911')
            window.send()
            self.wait_for(lambda: not window.busy)
            self.assertEqual(model.call_args.kwargs['channel'], 'creative_web')
            window.entry.insert('1.0', 'нет, нарисуй сам')
            window.send()
            self.wait_for(lambda: not window.busy)
            trace_panel.assert_not_called()
            self.assertEqual(model.call_args.kwargs['channel'], 'creative')
            self.assertIn('Porsche 911 без образца', model.call_args.args[0][-1]['content'])
        self.assertIsNotNone(window.pending)
        self.assertFalse(self.app.paths)

    @staticmethod
    def web_drawing_response():
        return json.dumps({'kind': 'preview', 'explanation': 'Контур сбоку',
                           'sources': [{'title': 'Исходник', 'url': 'https://example.org/model'}],
                           'paths': ['M10 10 L50 10 Q60 10 60 20 L60 30 Z']})

    def test_find_and_draw_previews_codex_geometry_in_the_bound_chat(self):
        self.app.open_assistant()
        window = self.app.assistant_window
        first = 'найди картинку с боку 2107 в интернете и просто обведи её по контурам и закинь сда'
        with tempfile.TemporaryDirectory() as folder, \
             patch.object(window, 'open_reference') as trace_panel, \
             patch.object(cloud_backend, '_generate', return_value=self.web_drawing_response()) as model:
            from assistant.saved_chat import save
            thread_id = '01234567-89ab-cdef-0123-456789abcdef'
            window.assistant.data_dir = Path(folder)
            save(window.assistant.data_dir, thread_id)
            for message in (first, 'это не то'):
                window.entry.insert('1.0', message)
                window.send()
                self.wait_for(lambda: not window.busy)
                self.assertIsNotNone(window.pending, window.transcript.get('1.0', 'end'))
                self.assertFalse(self.app.paths)
                self.assertEqual(model.call_args.kwargs['channel'], 'creative_web')
                self.assertEqual(model.call_args.kwargs['resume_thread'], thread_id)
                self.assertEqual(model.call_args.kwargs['original_prompt'], message)
            trace_panel.assert_not_called()

    def test_pen_status_and_request_for_more_photo_detail_produce_new_preview(self):
        self.app.open_assistant()
        window = self.app.assistant_window
        prior = 'нарисуй детализированный ваз 2107 с боку'
        revised = ('перо теперь поднимается, нарисуй ещё более детализированно, как бы накладывай '
                   'лист на фото 2107 и рисуя на нём главные детали машины, линии, и тл')
        first = json.dumps({'kind': 'preview', 'explanation': 'Контур машины', 'assumptions': [], 'operations': [
            {'kind': 'polyline', 'parameters': {'points': [[3, 3], [20, 20]], 'mode': 'replace'}}]})
        with tempfile.TemporaryDirectory() as folder, \
             patch.object(window, 'open_reference') as trace_panel, \
             patch.object(cloud_backend, '_generate', side_effect=[first, self.web_drawing_response()]) as model:
            from assistant.saved_chat import save
            thread_id = '01234567-89ab-cdef-0123-456789abcdef'
            window.assistant.data_dir = Path(folder)
            save(folder, thread_id)
            for prompt in (prior, revised):
                window.entry.insert('1.0', prompt)
                window.send()
                self.wait_for(lambda: not window.busy)
                self.assertIsNotNone(window.pending, window.transcript.get('1.0', 'end'))
            trace_panel.assert_not_called()
        self.assertEqual([call.kwargs['channel'] for call in model.call_args_list], ['creative', 'creative_web'])
        self.assertEqual(model.call_args.kwargs['original_prompt'], revised)
        self.assertIn('ВАЗ-2107 (Lada 2107) side view', model.call_args.args[0][0]['content'])
        self.assertEqual(window.pending[1].kind, 'object_scene')
        self.assertFalse(self.app.paths)

    def test_short_drawing_revision_uses_the_pending_draft_and_previews_contours(self):
        self.app.open_assistant()
        window = self.app.assistant_window
        first = json.dumps({'kind': 'preview', 'explanation': 'Башня', 'assumptions': [], 'operations': [
            {'kind': 'polyline', 'parameters': {'points': [[3, 3], [20, 20]], 'mode': 'replace'}}]})
        second = json.dumps({'kind': 'preview', 'explanation': 'Башня с деталями',
                             'paths': ['M3 3 L20 20', 'M6 6 L9 6']})
        question = json.dumps({'kind': 'answer', 'explanation': 'Две линии.', 'paths': []})
        with patch.object(cloud_backend, 'generate', side_effect=[first, second, question]) as model:
            for prompt in ('нарисуй бурдж халифу очень красиво и детализированно', 'ещё больше деталей дбавь'):
                window.entry.insert('1.0', prompt)
                window.send()
                self.wait_for(lambda: not window.busy)
                self.assertIsNotNone(window.pending, window.transcript.get('1.0', 'end'))
            self.assertEqual(len(window.pending[2]), 2)
            self.assertEqual(model.call_args.kwargs['output_schema']['required'], ['kind', 'explanation', 'paths'])
            rules = model.call_args.args[0][0]['content']
            self.assertIn('"draft": true', rules)
            self.assertIn('M3.000 3.000 L20.000 20.000', rules)
            self.assertFalse(self.app.paths)
            window.entry.insert('1.0', 'Что сейчас в эскизе?')
            window.send()
            self.wait_for(lambda: not window.busy)
            self.assertIn('Две линии.', window.transcript.get('1.0', 'end'))
            self.assertEqual(len(window.context_proposal[2]), 2)
            window.entry.insert('1.0', 'отобрази его в окне')
            window.send()
            self.assertEqual(model.call_count, 3)
            self.assertEqual(len(window.pending[2]), 2)
            self.assertFalse(self.app.paths)


class NativeStartupTests(unittest.TestCase):
    def test_link_is_recognized_before_panel_exists_and_lease_uses_actual_peer(self):
        wire = SimpleNamespace(port='tcp://fluidnc.local:23', peer_address='192.168.0.12')
        session = SimpleNamespace(wire=wire, machine_profile=DLC32, native_panel=True)
        app = SimpleNamespace(controller=SimpleNamespace(session=session), dwin_window=None)
        self.assertTrue(App.network_controller(app))
        self.assertFalse(App.network_lease_ready(app))
        bridge = SimpleNamespace(port='tcp://192.168.0.12:8888', peer_address=wire.peer_address,
                                 lease_ready=Mock(return_value=True))
        app.dwin_window = SimpleNamespace(native=True, closed=False, bridge=bridge)
        self.assertTrue(App.network_lease_ready(app))
        bridge.peer_address = '192.168.0.13'
        self.assertFalse(App.network_lease_ready(app))
        bridge.peer_address = wire.peer_address
        bridge.lease_ready.return_value = False
        self.assertFalse(App.network_lease_ready(app))


class SVGInputTests(unittest.TestCase):
    def test_utf16_dtd_is_rejected_before_parsing(self):
        with tempfile.TemporaryDirectory() as folder:
            svg = Path(folder) / 'utf16.svg'
            svg.write_text('<?xml version="1.0" encoding="UTF-16"?>'
                           '<!DOCTYPE svg [<!ENTITY coords "M0 0 L10 10">]>'
                           '<svg xmlns="http://www.w3.org/2000/svg"><path d="&coords;"/></svg>',
                           encoding='utf-16')
            with self.assertRaisesRegex(ValueError, 'DTD/ENTITY'):
                load_svg(svg, Sheet(20, 20))


if __name__ == '__main__':
    unittest.main()
