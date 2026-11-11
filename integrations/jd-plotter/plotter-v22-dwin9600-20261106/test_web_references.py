"""Regression coverage for explicit web references and follow-up requests."""
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent / "PROGRAM"))
from assistant import cloud_backend, reference_images, web_assets, web_lookup


class ReferenceSearchTests(unittest.TestCase):
    def test_original_phrasing_and_followup_share_one_subject(self):
        prompt = ("нарисуй именно официальный логотип МФТИ, который есть у него на сайте и самый "
                  "первый, что тебе попадется, когда ты это загугришь.")
        first = reference_images.web_reference_request(prompt)
        self.assertTrue(first.official_mipt)
        self.assertEqual(first, reference_images.web_reference_request("найди его сам", prompt))

    def test_another_brand_uses_general_web_search(self):
        request = reference_images.web_reference_request("найди в интернете официальный логотип Blender")
        self.assertEqual(request.query, "официальный логотип Blender")
        self.assertTrue(request.official_requested)
        self.assertFalse(request.official_mipt)
        retry = reference_images.web_reference_request("найди его сам", "нарисуй логотип Blender")
        self.assertEqual(retry.query, "логотип Blender")

    def test_drawings_and_literal_text_do_not_trigger_search(self):
        for prompt in ("нарисуй трапецию", "нарисуй розу", "нарисуй логотип для новой студии",
                       "нарисуй вымышленный спорткар", "нарисуй Porsche 911 без образца",
                       "напиши «найди официальный логотип Blender»"):
            self.assertIsNone(reference_images.web_reference_request(prompt))
        self.assertEqual(reference_images.web_reference_request("найди его сам").query, "")

    def test_vehicle_routes_to_art_direction_and_keeps_the_requested_backdrop(self):
        prompt = "нарисуй Porsche 911 красиво на фоне деревьев и пляжа"
        request = reference_images.web_reference_request(prompt)
        self.assertTrue(request.art_direction)
        self.assertEqual(request.prompt, prompt)
        self.assertEqual(request.query, "Porsche 911 на фоне деревьев и пляжа")
        self.assertFalse(reference_images.web_reference_request("найди официальный логотип Porsche").art_direction)

    def test_generic_vehicle_is_drawn_by_codex_unless_reference_requested(self):
        self.assertIsNone(reference_images.web_reference_request('Нарисуй красивую машину'))
        self.assertTrue(reference_images.web_reference_request('Нарисуй машину по фото').art_direction)
        self.assertEqual(reference_images.self_draw_request('нет, нарисуй сам', 'Нарисуй Porsche 911'),
                         'Нарисуй Porsche 911 без образца')
        self.assertEqual(reference_images.self_draw_request('нарисуй сам', None), '')

    def test_find_side_view_then_trace_is_one_workflow(self):
        prompt = 'найди картинку с боку 2107 в интернете и просто обведи её по контурам и закинь сда'
        request = reference_images.web_reference_request(prompt)
        self.assertTrue(request.auto_trace)
        self.assertIn('ВАЗ-2107', request.query)
        self.assertIn('side view', request.query)
        self.assertNotIn('обведи', request.query)
        for followup in ('сделай что сказано', 'это не то'):
            retry = reference_images.web_reference_request(followup, prompt)
            self.assertTrue(retry.alternative)
            self.assertEqual(retry.query, request.query)
        plain = reference_images.drawing_reference_request('Найди в интернете ВАЗ-2107 сбоку и обведи')
        self.assertIn('side view', plain.query)
        self.assertIsNone(reference_images.drawing_reference_request('Найди фото ВАЗ-2107 сбоку'))

    def test_pen_status_without_drawing_or_with_a_quoted_example_is_not_a_request(self):
        for prompt in ('Перо теперь поднимается', 'Перо теперь поднимается, не нарисуй по фото 2107',
                       'Перо теперь поднимается, а что значит «нарисуй поверх фото»?'):
            with self.subTest(prompt=prompt):
                self.assertEqual(reference_images.drawing_clause(prompt), prompt)
                self.assertIsNone(reference_images.drawing_reference_request(prompt))

    def test_web_mode_scoped_to_reference_channel(self):
        self.assertIn('web_search="disabled"', cloud_backend.command("codex", ".", None))
        self.assertIn('web_search="live"', cloud_backend.command("codex", ".", None, web_reference=True))
        result = '[{"title":"Blender","page":"https://www.blender.org/about/logo/",' \
                 '"asset":"https://127.0.0.1/art.svg","note":"Страница организации"}]'
        with patch.object(web_lookup, "generate", return_value=result) as generate:
            rows = web_lookup.lookup("официальный логотип Blender")
        self.assertIsNone(rows[0]["asset"])
        self.assertEqual(generate.call_args.kwargs["channel"], "web_reference")
        self.assertEqual(generate.call_args.args[0][-1]["content"], "официальный логотип Blender")

    def test_direct_image_download_rejects_private_targets(self):
        for url in ("http://example.org/logo.svg", "https://127.0.0.1/logo.svg",
                    "https://admin@example.org/logo.svg", "https://test.local/logo.svg"):
            with self.assertRaises(ValueError):
                web_assets.public_url(url)
        for address in ("10.0.0.1", "127.0.0.1", "::ffff:192.168.0.1", "fe80::1"):
            self.assertFalse(web_assets.global_address(address))


if __name__ == "__main__":
    unittest.main()
