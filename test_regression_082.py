"""Self-contained regressions: no network access or third-party HTML fixtures."""
import asyncio
import sqlite3
import tempfile
from pathlib import Path
from types import SimpleNamespace
import unittest

from crawler import RobotsPolicy, open_db, upsert_link
from integrity import find_saved, persist
from async_pipeline import AsyncBrowserFallback

BASE = 'https://ricette.giallozafferano.it/'
A, B, C = [BASE + x + '.html' for x in ('Alias-A', 'Alias-B', 'Canonical-C')]
STAMP = '2026-11-08T12:00:00+00:00'


def valid_recipe(canonical):
    return {
        'title': 'Ricetta di prova',
        'source_url': canonical,
        'ingredients': [{'name': 'acqua'}, {'name': 'farina'}],
    }


class Regression082(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.conn = open_db(Path(self.tmp.name) / 'test.sqlite3')
        for url in (A, B, C):
            upsert_link(self.conn, url)

    def tearDown(self):
        self.conn.close()
        self.tmp.cleanup()

    def test_legacy_alias_chain_resolves(self):
        self.conn.execute(
            "INSERT INTO recipes(url,title,json,quality_status) VALUES(?,?,?,?)",
            (C, 'C', '{}', 'quality_passed'))
        self.conn.execute(
            "INSERT INTO recipe_aliases(alias_url,canonical_url,first_seen,last_seen) VALUES(?,?,?,?)",
            (A, B, 't', 't'))
        self.conn.execute(
            "INSERT INTO recipe_aliases(alias_url,canonical_url,first_seen,last_seen) VALUES(?,?,?,?)",
            (B, C, 't', 't'))
        self.assertEqual(find_saved(self.conn, A), C)

    def test_alias_cycle_not_resolved(self):
        self.conn.execute(
            "INSERT INTO recipe_aliases(alias_url,canonical_url,first_seen,last_seen) VALUES(?,?,?,?)",
            (A, B, 't', 't'))
        self.conn.execute(
            "INSERT INTO recipe_aliases(alias_url,canonical_url,first_seen,last_seen) VALUES(?,?,?,?)",
            (B, A, 't', 't'))
        self.assertIsNone(find_saved(self.conn, A))

    def test_sitemap_refresh_preserves_in_progress(self):
        self.conn.execute(
            "UPDATE urls SET status='in_progress',lastmod=?,fetched_at=? WHERE url=?",
            ('2026-10-01', '2026-10-01T00:00:00+00:00', A))
        upsert_link(self.conn, A, lastmod='2026-11-01',
                    timestamp=STAMP, refresh_days=1)
        row = self.conn.execute('SELECT status,lastmod FROM urls WHERE url=?', (A,)).fetchone()
        self.assertEqual(row['status'], 'in_progress')
        self.assertEqual(row['lastmod'], '2026-11-01')

    def test_alias_flatten_and_http_validators_belong_to_requested(self):
        self.conn.execute(
            "INSERT INTO recipe_aliases(alias_url,canonical_url,first_seen,last_seen) VALUES(?,?,?,?)",
            (A, B, STAMP, STAMP))
        self.conn.execute(
            "UPDATE urls SET status='redirect',redirected_to=? WHERE url=?", (B, A))
        headers = {'ETag': '"requested-etag"',
                   'Last-Modified': 'Sun, 08 Nov 2026 12:00:00 GMT'}
        result = persist(self.conn, B, headers, valid_recipe(C), STAMP)
        self.assertEqual(result['canonical'], C)
        self.assertEqual(find_saved(self.conn, A), C)
        self.assertEqual(self.conn.execute(
            'SELECT canonical_url FROM recipe_aliases WHERE alias_url=?',
            (A,)).fetchone()['canonical_url'], C)
        self.assertEqual(self.conn.execute(
            'SELECT redirected_to FROM urls WHERE url=?',
            (A,)).fetchone()['redirected_to'], C)
        b_row = self.conn.execute(
            'SELECT etag,http_last_modified,status FROM urls WHERE url=?',
            (B,)).fetchone()
        c_row = self.conn.execute(
            'SELECT etag,http_last_modified,status FROM urls WHERE url=?',
            (C,)).fetchone()
        self.assertEqual(b_row['etag'], '"requested-etag"')
        self.assertEqual(b_row['http_last_modified'], headers['Last-Modified'])
        self.assertEqual(b_row['status'], 'redirect')
        self.assertIsNone(c_row['etag'])
        self.assertIsNone(c_row['http_last_modified'])
        self.assertEqual(c_row['status'], 'done')


class BrowserPacingRegression082(unittest.IsolatedAsyncioTestCase):
    async def test_only_first_main_document_uses_prereserved_slot(self):
        calls = []

        class FakeHttp:
            agent = 'OfflineTest/1.0'
            robot = RobotsPolicy('User-agent: *\nAllow: /\n')

            async def reserve(self):
                calls.append('reserve')

        fallback = AsyncBrowserFallback(FakeHttp())

        class FakeRoute:
            def __init__(self, *, resource='document', parent=None, redirected=None):
                self.request = SimpleNamespace(
                    url=A,
                    resource_type=resource,
                    is_navigation_request=lambda: resource == 'document',
                    frame=SimpleNamespace(parent_frame=parent),
                    redirected_from=redirected)

            async def continue_(self):
                calls.append('continue')

            async def abort(self):
                calls.append('abort')

        await fallback._route(FakeRoute())
        self.assertEqual(calls, ['continue'])

        calls.clear()
        await fallback._route(FakeRoute(parent=object()))
        self.assertEqual(calls, ['reserve', 'continue'])

        calls.clear()
        await fallback._route(FakeRoute(redirected=object()))
        self.assertEqual(calls, ['reserve', 'continue'])

        calls.clear()
        await fallback._route(FakeRoute(resource='script'))
        self.assertEqual(calls, ['reserve', 'continue'])


if __name__ == '__main__':
    unittest.main()
