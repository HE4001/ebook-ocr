import copy
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import httpx
from PIL import Image

from backend.latex_export import GENERATOR_VERSION, generate_source_fidelity_latex
from backend.pipeline import BookProcessor
from backend.storage import Storage
from backend.tests.layout_response_fixtures import original_printed_page, page_response


def response_envelope(protocol, value):
    text = json.dumps(value, ensure_ascii=False)
    if protocol == 'gemini':
        return {'candidates': [{'finishReason': 'STOP', 'content': {
            'role': 'model', 'parts': [{'text': text}],
        }}], 'usageMetadata': {'promptTokenCount': 2, 'candidatesTokenCount': 3, 'totalTokenCount': 5}}
    return {'status': 'completed', 'output_text': text,
            'usage': {'input_tokens': 2, 'output_tokens': 3, 'total_tokens': 5}}


class LayoutPipelineTests(unittest.IsolatedAsyncioTestCase):
    async def process_response(self, storage, protocol, value):
        requests = []
        real_client = httpx.AsyncClient

        def handler(request):
            requests.append(json.loads(request.content))
            return httpx.Response(200, json=response_envelope(protocol, value))

        settings = {**storage.get_settings(), 'api_protocol': protocol,
                    'base_url': 'https://model.invalid', 'extraction_model': 'fixed-model'}
        with patch('httpx.AsyncClient', side_effect=lambda **kwargs:
                   real_client(transport=httpx.MockTransport(handler), **kwargs)):
            await BookProcessor(storage).process('layout-book', settings, 'fixed-key')
        self.assertEqual(len(requests), 1)
        return storage.get_pages('layout-book')[0]

    def create_book(self, directory):
        storage = Storage(Path(directory))
        storage.initialize()
        root = storage.books_root / 'layout-book'
        root.mkdir()
        Image.new('RGB', (640, 960), 'white').save(root / 'source.png')
        (root / 'page.png').write_bytes((root / 'source.png').read_bytes())
        storage.create_book('layout-book', '原创布局', 'source.png', [(1, 640, 960, 'page.png')])
        return storage

    async def test_fixed_original_lines_groups_styles_and_annotation_boundary_reach_saved_source(self):
        for protocol in ('openai_responses', 'gemini'):
            with self.subTest(protocol=protocol), tempfile.TemporaryDirectory() as directory:
                storage = self.create_book(directory)
                value = original_printed_page()
                page = await self.process_response(storage, protocol, value)
                self.assertEqual((page.status, page.render_strategy), ('ready', 'source_fidelity'))
                layout = page.layout_source
                self.assertEqual([line.line_id for line in layout.lines],
                                 ['text-1', 'text-2', 'eq-1', 'eq-2', 'eq-3'])
                self.assertEqual([line.latex for line in layout.lines],
                                 [line['latex'] for line in value['layout']['lines']])
                self.assertEqual(layout.equation_groups[0].line_ids, ['eq-1', 'eq-2', 'eq-3'])
                self.assertEqual(layout.equation_groups[0].number.line_id, 'eq-3')
                self.assertEqual(layout.equation_groups[0].align_x, .3)
                self.assertIsNone(layout.lines[0].style.bold)
                self.assertIn(r'\textbf{局部粗体}', layout.lines[0].latex)
                self.assertNotIn(r'\textbf', layout.lines[1].latex)
                self.assertIn(r'\underline{下划线}', layout.lines[1].latex)
                self.assertNotIn('手写补记', ''.join(line.latex for line in layout.lines))
                self.assertTrue(any('后加手写' in reason for reason in layout.review_reasons))
                self.assertEqual(layout.source, page.source_metadata)
                self.assertEqual(layout.source.canonical_width_px, 640)
                self.assertEqual(layout.source.canonical_height_px, 960)
                self.assertEqual(layout.canvas_basis, 'project')
                self.assertAlmostEqual(layout.canvas_width_bp, 210 * 72 / 25.4)
                self.assertAlmostEqual(layout.canvas_height_bp / layout.canvas_width_bp, 1.5)
                self.assertEqual(layout.body_font_basis, 'project')
                self.assertTrue(any('待人工校准' in reason for reason in layout.review_reasons))
                self.assertEqual(layout.generator_version, GENERATOR_VERSION)
                self.assertEqual(layout.generated_content_revision, page.content_revision)
                self.assertEqual(page.text, generate_source_fidelity_latex(layout))
                self.assertIn(r'\documentclass', page.text)

    async def test_complete_document_keeps_its_macros_and_does_not_generate_over_them(self):
        source = (r'\documentclass{ctexbook}' '\n'
                  r'\newcommand{\OriginalTerm}[1]{\textbf{#1}}' '\n'
                  r'\begin{document}\OriginalTerm{原文}\end{document}')
        for protocol in ('openai_responses', 'gemini'):
            with self.subTest(protocol=protocol), tempfile.TemporaryDirectory() as directory:
                storage = self.create_book(directory)
                value = page_response('原文')
                value['body_latex'] = source
                page = await self.process_response(storage, protocol, value)
                self.assertEqual((page.status, page.render_strategy, page.text), ('ready', 'custom_latex', source))
                self.assertEqual(page.layout_source.lines[0].latex, '原文')
                self.assertIsNone(page.generated_content_revision)
                self.assertIsNone(page.layout_source.generated_content_revision)

    async def test_invalid_layout_preserves_the_last_effective_content(self):
        for protocol in ('openai_responses', 'gemini'):
            with self.subTest(protocol=protocol), tempfile.TemporaryDirectory() as directory:
                storage = self.create_book(directory)
                original = await self.process_response(storage, protocol, page_response('已保存原行'))
                value = copy.deepcopy(page_response('错误新原行'))
                value['layout']['lines'][0]['block_id'] = 'undefined'
                page = await self.process_response(storage, protocol, value)
                self.assertEqual(page.status, 'failed')
                self.assertEqual(page.text, original.text)
                self.assertEqual(page.layout_source, original.layout_source)
                self.assertEqual(page.content_revision, original.content_revision)
                self.assertIn('页面结构无效', page.error)

    async def test_blank_content_still_saves_a_source_fidelity_page(self):
        with tempfile.TemporaryDirectory() as directory:
            storage = self.create_book(directory)
            page = await self.process_response(storage, 'openai_responses', page_response(''))
            self.assertEqual((page.status, page.render_strategy), ('ready', 'source_fidelity'))
            self.assertEqual(page.layout_source.lines, [])
            self.assertEqual(page.header_segments + page.footer_segments, [])
            self.assertIn(r'\begin{document}', page.text)

    async def test_unknown_line_position_saves_observation_for_calibration_without_template_fallback(self):
        for protocol in ('openai_responses', 'gemini'):
            with self.subTest(protocol=protocol), tempfile.TemporaryDirectory() as directory:
                storage = self.create_book(directory)
                value = page_response('原行待定位')
                value['layout']['lines'][0].update(bbox=None, baseline=None)
                page = await self.process_response(storage, protocol, value)
                self.assertEqual((page.status, page.render_strategy, page.text),
                                 ('ready', 'source_fidelity', '原行待定位'))
                self.assertIsNone(page.generated_content_revision)
                self.assertIsNone(page.layout_source.generated_content_revision)
                self.assertEqual(page.layout_source.lines[0].latex, '原行待定位')
                self.assertIsNone(page.layout_source.lines[0].bbox)
                self.assertTrue(any('line-1' in reason for reason in page.layout_source.review_reasons))

    async def test_one_hundred_model_review_reasons_survive_three_program_reasons(self):
        with tempfile.TemporaryDirectory() as directory:
            storage = self.create_book(directory)
            value = page_response('原行待定位')
            value['layout']['lines'][0].update(bbox=None, baseline=None)
            reasons = [f'原创复核项 {index}' for index in range(100)]
            value['layout']['review_reasons'] = reasons
            page = await self.process_response(storage, 'openai_responses', value)
            self.assertEqual(page.status, 'ready')
            self.assertEqual(page.layout_source.review_reasons[:100], reasons)
            self.assertEqual(len(page.layout_source.review_reasons), 103)


if __name__ == '__main__':
    unittest.main()
