import asyncio
import tempfile
import threading
import time
import unittest
from io import BytesIO
from pathlib import Path
from unittest.mock import patch
from uuid import uuid4

from fastapi.testclient import TestClient
from PIL import Image

from backend.main import create_app


class ConcurrentProofreadingTests(unittest.TestCase):
    def test_other_pages_can_be_saved_without_overwrite_or_losing_task_state(self):
        for reuse, pause in ((False, False), (True, False), (True, True)):
            with self.subTest(context_reuse=reuse, pause=pause), tempfile.TemporaryDirectory() as directory:
                with TestClient(create_app(Path(directory))) as client:
                    storage = client.app.state.storage
                    book_id = str(uuid4())
                    book_dir = storage.books_root / book_id
                    book_dir.mkdir()
                    image = BytesIO()
                    Image.new('RGB', (32, 24), 'white').save(image, format='PNG')
                    for number in range(1, 5):
                        (book_dir / f'page-{number:04d}.png').write_bytes(image.getvalue())
                    storage.create_book(book_id, '校对并发', 'pages.png', [
                        (number, 32, 24, f'page-{number:04d}.png') for number in range(1, 5)
                    ])
                    base = f'/api/books/{book_id}'
                    self.assertEqual(client.put(base + '/arrangement', json={
                        'file_order': ['legacy'], 'page_order': [1, 2, 3, 4],
                    }).status_code, 200)
                    storage.save_manual_text(book_id, 1, '识别前的正文')
                    storage.save_manual_text(book_id, 2, '原校对正文')
                    attempt = storage.begin_attempt(book_id, 2)
                    storage.finish_attempt(book_id, 2, attempt, (1, 2, 3), True)
                    original = storage.get_pages(book_id)[1]
                    client.put('/api/settings', json={
                        'api_key': 'mock', 'extraction_model': 'mock',
                        'processing_concurrency': 1, 'context_reuse_enabled': reuse,
                        'context_reuse_max_pages': 3,
                    })
                    started, release = threading.Event(), threading.Event()
                    called = []

                    async def recognize(_self, _book_id, number, *_args):
                        called.append(number)
                        if number == 1:
                            started.set()
                            if not await asyncio.to_thread(release.wait, 5):
                                raise AssertionError('识别模拟未释放')
                        return f'OCR {number}'

                    def wait_finished():
                        deadline = time.monotonic() + 5
                        while book_id in client.app.state.running and time.monotonic() < deadline:
                            time.sleep(.01)
                        self.assertNotIn(book_id, client.app.state.running)

                    with patch('backend.pipeline.PageAgent.run', recognize):
                        try:
                            self.assertEqual(client.post(base + '/process', json={'pages': [1, 3, 4]}).status_code, 200)
                            self.assertTrue(started.wait(5))
                            # This also protects a save sent before the UI receives its next poll.
                            self.assertEqual(client.put(base + '/pages/1', json={'text': '不能覆盖'}).status_code, 409)
                            self.assertEqual(storage.get_pages(book_id)[0].text, '识别前的正文')
                            saved = client.put(base + '/pages/2', json={'text': '人工校对正文'})
                            self.assertEqual(saved.status_code, 200)
                            self.assertEqual(saved.json()['usage'], original.usage.model_dump())
                            self.assertEqual(saved.json()['attempts'], original.attempts)
                            self.assertEqual(storage.get_book(book_id).status, 'processing')
                            # Page 3 may already be reserved inside a context-reuse group.
                            fields = [{'kind': 'title', 'text': '人工校对封面'}]
                            saved = client.put(base + '/pages/3', json={
                                'text': '', 'page_kind': 'front_cover', 'cover_fields': fields,
                            })
                            self.assertEqual(saved.status_code, 200)
                            self.assertEqual(client.put(base + '/pages/4', json={
                                'text': '错误请求', 'page_kind': 'content', 'cover_fields': fields,
                            }).status_code, 422)
                            if pause:
                                self.assertEqual(client.post(base + '/pause').status_code, 200)
                                self.assertEqual(client.put(base + '/pages/2', json={'text': '人工校对正文'}).status_code, 200)
                                self.assertEqual(storage.get_book(book_id).status, 'pausing')
                                self.assertEqual(client.put(base + '/pages/1', json={'text': '不能覆盖'}).status_code, 409)
                        finally:
                            release.set()
                        wait_finished()
                        self.assertEqual(called, [1] if pause else [1, 4])
                        pages = storage.get_pages(book_id)
                        self.assertEqual(pages[1].text, '人工校对正文')
                        self.assertEqual(pages[2].cover_fields[0].text, '人工校对封面')
                        self.assertEqual(pages[2].attempts, 0)
                        self.assertEqual(storage.get_book(book_id).status, 'paused' if pause else 'ready')
                        # An explicitly requested later run must still be allowed to replace it.
                        self.assertEqual(client.post(base + '/process', json={'pages': [3]}).status_code, 200)
                        wait_finished()
                        self.assertEqual(called[-1], 3)
                        self.assertEqual(storage.get_pages(book_id)[2].text, 'OCR 3')


if __name__ == '__main__':
    unittest.main()
