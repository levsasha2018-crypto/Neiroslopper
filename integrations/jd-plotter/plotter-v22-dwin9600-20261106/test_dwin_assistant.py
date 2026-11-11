from pathlib import Path
import sys
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent / 'PROGRAM'))
from assistant.runtime import Reply
from assistant.voice import Transcript
from dwin_assistant import (DONE, ERROR, MAX_HISTORY, RECORDING, SENDING, TRANSCRIBING, WAITING,
                            PanelAssistant, panel_text, sheet_segments)


def snapshot(paths=(), **state):
    return SimpleNamespace(paths=paths, state={'sheet_width': 210, 'sheet_height': 297, **state})


class FakeVoice:
    def __init__(self, events):
        self.events = events
        self.cancelled = False

    def start(self, device):
        self.events.put(('state', 'Запись'))

    def stop(self):
        self.events.put(('state', 'Распознаю локально'))
        self.events.put(('result', Transcript('Сколько миллиметров в метре?')))
        self.events.put(('finished', None))

    def cancel(self):
        self.cancelled = True

    def close(self):
        self.cancel()


class PanelAssistantTests(unittest.TestCase):
    def make_panel(self, answer):
        updates = []
        panel = PanelAssistant(lambda *args: updates.append(args), assistant=SimpleNamespace(answer=answer),
                               voice_factory=FakeVoice)
        self.addCleanup(panel.close)
        return panel, updates

    def pump_to_result(self, panel):
        deadline = time.monotonic() + 2
        while panel.busy and time.monotonic() < deadline:
            panel.pump()
            time.sleep(.001)
        self.assertFalse(panel.busy)

    def test_text_request_cannot_apply_plan(self):
        calls = []

        def answer(prompt, state, **kwargs):
            calls.append((prompt, state, kwargs))
            return Reply('готово', plan=object())

        panel, updates = self.make_panel(answer)
        self.assertTrue(panel.start(1, 'перемести надпись', snapshot()))
        self.assertFalse(panel.start(2, 'вопрос', snapshot()))
        self.pump_to_result(panel)
        self.assertEqual([stage for _, stage, _ in updates], [SENDING, WAITING, ERROR])
        self.assertIn('на ПК', updates[-1][2])
        self.assertNotIn('editor_paths', calls[0][1])
        self.assertFalse(calls[0][2]['allow_creative'])

    def test_voice_passes_only_transcript_after_stop(self):
        prompts = []

        def answer(prompt, _state, **_kwargs):
            prompts.append(prompt)
            return Reply('1000 мм.')

        panel, updates = self.make_panel(answer)
        self.assertTrue(panel.start_voice(7, snapshot()))
        panel.pump()
        self.assertFalse(panel.stop_voice(9))
        self.assertEqual(prompts, [])
        self.assertTrue(panel.stop_voice(7))
        self.pump_to_result(panel)
        self.assertEqual(prompts, ['Сколько миллиметров в метре?'])
        self.assertEqual([stage for _, stage, _ in updates], [SENDING, RECORDING, TRANSCRIBING, WAITING, DONE])
        self.assertEqual(updates[-1], (7, DONE, '1000 мм.'))

    def test_followups_use_only_successful_turns_from_this_panel(self):
        calls = []

        def answer(prompt, state, **_kwargs):
            calls.append((prompt, state['chat_history'], state['panel_reply']))
            if prompt == 'ошибка':
                return Reply('Нет связи', source='Codex · ответ не получен')
            return Reply('Ответ на ' + prompt)

        panel, updates = self.make_panel(answer)
        desktop = snapshot(chat_history=(('секретный вопрос ПК', 'ответ ПК'),))
        for number, prompt in enumerate(('как задать углы?', 'ошибка', 'а потом?'), 1):
            self.assertTrue(panel.start(number, prompt, desktop))
            self.pump_to_result(panel)
        first = ('как задать углы?', 'Ответ на как задать углы?')
        self.assertEqual(calls, [('как задать углы?', (), True), ('ошибка', (first,), True),
                                 ('а потом?', (first,), True)])
        self.assertEqual([stage for _, stage, _ in updates if stage in (DONE, ERROR)], [DONE, ERROR, DONE])

    def test_history_is_bounded_and_voice_uses_its_start_snapshot(self):
        seen = []

        def answer(prompt, state, **_kwargs):
            seen.append((prompt, state['chat_history'], state['sheet_width']))
            return Reply(prompt)

        panel, _ = self.make_panel(answer)
        for index in range(MAX_HISTORY + 2):
            panel.start(index, f'вопрос {index}', snapshot())
            self.pump_to_result(panel)
        original = snapshot()
        self.assertTrue(panel.start_voice(20, original))
        original.state['sheet_width'] = 100
        self.assertTrue(panel.stop_voice(20))
        self.pump_to_result(panel)
        _prompt, history, width = seen[-1]
        self.assertEqual(history, tuple((f'вопрос {index}', f'вопрос {index}')
                                        for index in range(2, MAX_HISTORY + 2)))
        self.assertEqual(width, 210)

    def test_cancel_discards_voice_result_already_queued(self):
        panel, updates = self.make_panel(lambda *_args, **_kwargs: self.fail('cancelled audio sent'))
        panel.start_voice(7, snapshot())
        panel.stop_voice(7)
        panel.cancel()
        self.pump_to_result(panel)
        self.assertEqual(updates, [(7, SENDING, '')])

    def test_cancel_discards_a_late_reply_with_same_request_id(self):
        release, completed = threading.Event(), threading.Event()

        def answer(prompt, _state, **_kwargs):
            if prompt == 'старый':
                release.wait(2)
                completed.set()
            return Reply(prompt)

        panel, updates = self.make_panel(answer)
        panel.start(1, 'старый', snapshot())
        panel.cancel()
        panel.start(1, 'новый', snapshot())
        release.set()
        self.pump_to_result(panel)
        self.assertTrue(completed.wait(2))
        panel.pump()
        self.assertEqual([text for _, stage, text in updates if stage == DONE], ['новый'])

    def test_cancel_for_older_request_does_not_interrupt_current_reply(self):
        release = threading.Event()
        self.addCleanup(release.set)

        def answer(*_args, **_kwargs):
            release.wait(2)
            return Reply('Новый ответ')

        panel, updates = self.make_panel(answer)
        self.assertTrue(panel.start(11, 'текущий вопрос', snapshot()))
        self.assertFalse(panel.cancel(10))
        self.assertTrue(panel.busy)
        release.set()
        self.pump_to_result(panel)
        self.assertEqual(updates[-1], (11, DONE, 'Новый ответ'))

    def test_active_request_keeps_panel_waiting_beyond_its_watchdog(self):
        release = threading.Event()
        self.addCleanup(release.set)

        def answer(*_args, **_kwargs):
            release.wait(2)
            return Reply('Готово')

        panel, updates = self.make_panel(answer)
        with patch('dwin_assistant.monotonic', return_value=0) as clock:
            self.assertTrue(panel.start(3, 'подробный вопрос', snapshot()))
            for elapsed in (10, 30, 60, 90, 120, 150):
                clock.return_value = elapsed
                panel.pump()
            self.assertTrue(panel.busy)
            self.assertEqual(updates, [(3, SENDING, '')] + [(3, WAITING, '')] * 6)
            release.set()
            self.pump_to_result(panel)
            clock.return_value = 300
            panel.pump()
        self.assertEqual(updates[-1], (3, DONE, 'Готово'))

    def test_short_answer_preserves_unicode_and_words(self):
        raw = '# Тест\n**лист** [помощь](https://example.org/help) ' + ('подробности ' * 15)
        text = panel_text(raw)
        self.assertLessEqual(len(text), 112)
        self.assertTrue(text.startswith('Тест лист помощь '))
        self.assertTrue(text.endswith('подробности…'))


class SheetPreviewTests(unittest.TestCase):
    def test_keeps_paper_orientation_and_joins_only_when_ink_would_show(self):
        paths = (((15, 10), (40, 10)), ((60, 20), (60, 30)))
        box, raised = sheet_segments(snapshot(paths, pen_lift_enabled=True), start_position=(0, 0))
        _, lowered = sheet_segments(snapshot(paths, pen_lift_enabled=False), start_position=(0, 0))
        left, top, right, bottom = box
        self.assertLess(right-left, bottom-top)
        self.assertEqual(len(raised), 2)
        self.assertEqual(len(lowered), 4)
        self.assertEqual(lowered[0][:2], (left, top))
        self.assertEqual(lowered[2], (*raised[0][2:], *raised[1][:2]))

    def test_large_image_is_bounded_without_inventing_connections(self):
        paths = tuple(((x, 20), (x, 200)) for x in range(200))
        state = snapshot(paths, pen_lift_enabled=True)
        _box, lines = sheet_segments(state, max_segments=24)
        self.assertEqual(len(lines), 24)
        self.assertTrue(all(x1 == x2 and y1 < y2 for x1, y1, x2, y2 in lines))

    def test_empty_and_unset_sheet(self):
        self.assertEqual(sheet_segments(snapshot())[1], ())
        with self.assertRaisesRegex(ValueError, 'размер листа'):
            sheet_segments(snapshot(sheet_width=None))


if __name__ == '__main__':
    unittest.main()
