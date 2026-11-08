import unittest as _fixture_unittest
from pathlib import Path as _fixture_Path
if not (_fixture_Path(__file__).parent / "Lasagne-al-forno.html").exists() or not (_fixture_Path(__file__).parent / "Hummus-senza-tahina.html").exists():
    raise _fixture_unittest.SkipTest("Optional third-party HTML fixtures not distributed")
"""Regression tests for the v0.4 text-only and incremental pipeline."""
import json
from pathlib import Path
import tempfile
import unittest

from crawler import open_db, upsert_link, crawl, PoliteHttp
from scraper import parse
from unittest.mock import Mock

HERE = Path(__file__).parent
URL = 'https://ricette.giallozafferano.it/Lasagne-al-forno.html'
HTML = (HERE / 'Lasagne-al-forno.html').read_bytes()


class ConditionalFake:
    def __init__(self):
        self.calls = []
        self.mode = 'html'

    def get(self, url, accept=None, max_bytes=None, conditional_headers=None):
        self.calls.append((url, conditional_headers))
        if self.mode == '304':
            if conditional_headers and conditional_headers.get('If-None-Match') == '"etag1"':
                return 304, b'', {'ETag': '"etag1"'}
            raise AssertionError('Expected conditional request')
        if self.mode == 'broken':
            return 200, b'<html><body>render later</body></html>', {'Content-Type': 'text/html'}
        return 200, HTML, {'Content-Type': 'text/html', 'ETag': '"etag1"'}


class FakeBrowser:
    def __init__(self):
        self.calls = []

    def fetch_html(self, url):
        self.calls.append(url)
        return HTML.decode('utf-8')


class TestPerformance(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db = open_db(Path(self.temp.name)/'test.sqlite3')
        upsert_link(self.db, URL)
        self.db.commit()
        self.http = ConditionalFake()

    def tearDown(self):
        self.db.close()
        self.temp.cleanup()

    def requeue(self):
        self.db.execute("UPDATE urls SET status='pending' WHERE url=?", (URL,))
        self.db.commit()

    def test_304_and_text_only_and_no_rewrite(self):
        r = crawl(self.db, self.http, limit=1, batch_size=25)
        self.assertEqual(r['saved'], 1)
        record = self.db.execute('SELECT json,extracted_at FROM recipes WHERE url=?', (URL,)).fetchone()
        data = json.loads(record['json'])
        self.assertNotIn('images', data)
        self.assertNotIn('instruction_steps_with_photos', data)
        self.assertNotIn('instructions', data)
        self.assertNotIn('instruction_steps', data)
        self.assertNotIn('techniques_detected', data)
        self.assertTrue(data['utensils_detected'])
        self.assertNotIn('step_images_count', data)
        self.assertEqual(len(data['ingredients']), 11)
        self.assertEqual(len(data['related_recipes']), 5)
        self.assertNotIn('image_url', data['related_recipes'][0])
        self.requeue()
        self.http.mode = '304'
        r = crawl(self.db, self.http, limit=1)
        self.assertEqual(r['not_modified'], 1)
        self.assertEqual(self.http.calls[-1][1]['If-None-Match'], '"etag1"')
        after = self.db.execute('SELECT json,extracted_at FROM recipes WHERE url=?', (URL,)).fetchone()
        self.assertEqual(record['json'], after['json'])
        self.assertEqual(record['extracted_at'], after['extracted_at'])

    def test_no_rewrite_even_if_server_returns_200(self):
        self.assertEqual(crawl(self.db, self.http, limit=1)['saved'], 1)
        self.requeue()
        r = crawl(self.db, self.http, limit=1)
        self.assertEqual(r['unchanged'], 1)
        self.assertEqual(r['saved'], 0)

    def test_browser_fallback_only_when_unstructured_200(self):
        browser = FakeBrowser()
        self.http.mode = 'broken'
        r = crawl(self.db, self.http, limit=1, browser=browser)
        self.assertEqual(r['browser_fallback'], 1)
        self.assertEqual(r['done'], 1)
        self.assertEqual(browser.calls, [URL])

    def test_persistent_connection_and_conditional_headers(self):
        response = Mock()
        response.status_code = 304
        response.headers = {'ETag': '"etag1"'}
        response.close = Mock()
        robots = Mock(status_code=200, text='User-agent: *\nAllow: /')
        sess = Mock()
        sess.get.side_effect = [robots, response]
        sess.headers = {}
        client = PoliteHttp('RecipeArchiveResearchBot/0.6.2', delay=2, session=sess, monotonic=lambda: 100, sleep=lambda _: None)
        code, content, headers = client.get(URL, conditional_headers={'If-None-Match':'"etag1"'})
        self.assertEqual(code, 304)
        self.assertEqual(content, b'')
        self.assertEqual(sess.get.call_args.kwargs['headers']['If-None-Match'], '"etag1"')

    def test_db_index_and_mode(self):
        pragmas = self.db.execute('PRAGMA journal_mode').fetchone()[0]
        self.assertEqual(pragmas, 'wal')
        self.assertEqual(self.db.execute('PRAGMA synchronous').fetchone()[0], 1)
        names = {r[1] for r in self.db.execute('PRAGMA index_list(urls)')}
        self.assertIn('idx_urls_first_seen', names)


if __name__=='__main__':
    unittest.main()
