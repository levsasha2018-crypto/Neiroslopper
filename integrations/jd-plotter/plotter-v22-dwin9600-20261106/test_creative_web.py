"""Codex-authored vector curves are checked directly as paper geometry."""
import json
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent / 'PROGRAM'))
from assistant import cloud_backend, creative_planner
from assistant.planner import transform_paths
from assistant.reference_images import WebReference


class CreativeWebTests(unittest.TestCase):
    prompt = 'Найди изображение ВАЗ-2107 сбоку и обведи его.'
    reference = WebReference(prompt, 'ВАЗ-2107 сбоку', auto_trace=True)
    state = {'sheet_width': 210, 'sheet_height': 297, 'editor_paths': []}
    sources = [{'title': 'Боковой вид', 'url': 'https://example.org/side'}]

    def test_curves_render_in_place_without_an_eight_stroke_cap(self):
        paths = [f'M10 {y} Q20 {y + 5} 30 {y}' for y in range(10, 40, 3)]
        output = {'kind': 'preview', 'explanation': 'Вид сбоку', 'sources': self.sources, 'paths': paths}
        with patch.object(cloud_backend, 'generate', return_value=json.dumps(output)) as generate:
            reply = creative_planner.route(self.prompt, self.state, reference=self.reference)
        self.assertIn('https://example.org/side', reply.text)
        self.assertEqual(generate.call_args.kwargs['channel'], 'creative_web')
        checked = transform_paths([], reply.plan, 210, 297)
        self.assertEqual(len(checked), len(paths))
        self.assertEqual(checked[0][0], (10., 10.))
        self.assertGreater(len(checked[0]), 2)

    def test_rejects_markup_and_curves_outside_sheet(self):
        for path in ('M10 10 L30 30"/><image href="file:///test"/>',
                     'M10 10 Q-100 10 20 10'):
            with self.subTest(path=path):
                output = {'kind': 'preview', 'explanation': 'Вид сбоку', 'sources': self.sources, 'paths': [path]}
                with patch.object(cloud_backend, 'generate', return_value=json.dumps(output)):
                    reply = creative_planner.route(self.prompt, self.state, reference=self.reference)
                self.assertIsNone(reply.plan)
                self.assertTrue(reply.validation_error)


if __name__ == '__main__':
    unittest.main()
