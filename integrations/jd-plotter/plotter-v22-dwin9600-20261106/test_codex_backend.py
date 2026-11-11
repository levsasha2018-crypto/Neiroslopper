import io
import json
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent / 'PROGRAM'))
from assistant import cloud_backend as cloud


class CodexModelTests(unittest.TestCase):
    def test_explicit_model_and_reasoning_preserve_isolation(self):
        args = cloud.command('codex', '.', None, effort='xhigh')
        self.assertEqual(args[args.index('--model') + 1], 'gpt-6.1-sol')
        self.assertEqual(args[args.index('--sandbox') + 1], 'read-only')
        self.assertIn('model_reasoning_effort="xhigh"', args)
        self.assertIn('--ignore-user-config', args)
        self.assertIn('web_search="disabled"', args)

    def test_reasoning_depth_uses_current_request_and_channel(self):
        history = [{'role': 'assistant', 'content': 'Глубоко разберём все гипотезы.'}]
        for channel, prompt, expected in (
            ('chat', 'Сколько миллиметров в сантиметре?', 'medium'),
            ('chat', 'Почему шаговый мотор пропускает шаги?', 'high'),
            ('chat', 'Разбери подробно, как различить три причины.', 'xhigh'),
            ('creative', 'Нарисуй облако.', 'high'),
            ('creative_web', 'Нарисуй машину по фотографии из сети.', 'high'),
            ('structured', 'Разбери подробно эту формулировку.', 'low'),
            ('web_reference', 'Проверь все гипотезы.', 'low'),
        ):
            with self.subTest(channel=channel, prompt=prompt):
                messages = [*history, {'role': 'user', 'content': prompt}]
                self.assertEqual(cloud._automatic_effort(channel, messages), expected)

    def test_chat_accepts_builtin_search_but_rejects_command_execution(self):
        messages = [{'role': 'system', 'content': 'Отвечай с источниками.'},
                    {'role': 'user', 'content': 'Найди справку.'}]
        for kind in ('web_search', 'command_execution'):
            output = [{'type': 'item.completed', 'item': {'type': kind}},
                      {'type': 'item.completed', 'item': {'type': 'agent_message', 'text': 'Нашёл.'}},
                      {'type': 'turn.completed'}]
            process = SimpleNamespace(stdin=io.StringIO(), stderr=io.StringIO(),
                                      stdout=io.StringIO('\n'.join(map(json.dumps, output)) + '\n'),
                                      poll=lambda: 0, wait=lambda timeout: 0)
            with self.subTest(kind=kind), patch.object(cloud, 'executable', return_value='codex'), \
                 patch.object(cloud, 'login_status'), patch.object(cloud.subprocess, 'Popen', return_value=process) as launch:
                if kind == 'web_search':
                    self.assertEqual(cloud.generate(messages, channel='chat'), 'Нашёл.')
                else:
                    with self.assertRaisesRegex(cloud.CloudError, 'инструмент'):
                        cloud.generate(messages, channel='chat')
                self.assertIn('web_search="live"', launch.call_args.args[0])
                self.assertIn('read-only', launch.call_args.args[0])

    def test_creative_web_accepts_long_work_and_requires_completed_search(self):
        messages = [{'role': 'system', 'content': 'Построй эскиз по образцу.'},
                    {'role': 'user', 'content': 'Найди фотографию и нарисуй машину.'}]
        schema = {'type': 'object', 'properties': {}, 'additionalProperties': False}
        for web_event in ('item.started', 'item.completed'):
            output = [{'type': web_event, 'item': {'type': 'web_search'}},
                      {'type': 'item.completed', 'item': {'type': 'agent_message', 'text': '{"result":{}}'}},
                      {'type': 'turn.completed'}]
            process = SimpleNamespace(stdin=io.StringIO(), stderr=io.StringIO(),
                                      stdout=io.StringIO('\n'.join(map(json.dumps, output)) + '\n'),
                                      poll=lambda: 0, wait=lambda timeout: 0)
            with self.subTest(web_event=web_event), patch.object(cloud, 'executable', return_value='codex'), \
                 patch.object(cloud, 'login_status'), patch.object(cloud.subprocess, 'Popen', return_value=process) as launch, \
                 patch.object(cloud.time, 'monotonic', side_effect=[0, 600, 601]):
                if web_event == 'item.completed':
                    self.assertEqual(json.loads(cloud.generate(messages, channel='creative_web', output_schema=schema)), {})
                else:
                    with self.assertRaisesRegex(cloud.CloudError, 'поиск'):
                        cloud.generate(messages, channel='creative_web', output_schema=schema)
                self.assertIn('web_search="live"', launch.call_args.args[0])


if __name__ == '__main__':
    unittest.main()
