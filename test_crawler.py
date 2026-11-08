import unittest as _fixture_unittest
from pathlib import Path as _fixture_Path
if not (_fixture_Path(__file__).parent / "Lasagne-al-forno.html").exists() or not (_fixture_Path(__file__).parent / "Hummus-senza-tahina.html").exists():
    raise _fixture_unittest.SkipTest("Optional third-party HTML fixtures not distributed")
"""Offline functional tests: no requests to GialloZafferano are made."""
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import Mock

from crawler import (CrawlBlocked, PoliteHttp, crawl, discover, export,
                     normalize_url, open_db, sitemap_items, stats)

ROOT = Path(__file__).parent
BASE = 'https://ricette.giallozafferano.it'
LASAGNE = BASE + '/Lasagne-al-forno.html'
HUMMUS = BASE + '/Hummus-senza-tahina.html'


class FakeHttp:
    def __init__(self, assets):
        self.assets = assets
        self.calls = []

    def get(self, url, accept=None, max_bytes=None):
        self.calls.append(url)
        if url not in self.assets:
            raise AssertionError('Unexpected URL ' + url)
        return 200, self.assets[url], {'Content-Type':'text/html; charset=utf-8'}


class SiteCrawlerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = open_db(Path(self.tmp.name) / 'work.sqlite3')
        index = b'''<?xml version="1.0"?><sitemapindex xmlns="http://www.sitemaps.org/schemas/sitemap/0.9"><sitemap><loc>https://ricette.giallozafferano.it/sitemap/a.xml</loc></sitemap></sitemapindex>'''
        pages = f'''<?xml version="1.0"?><urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
        <url><loc>{LASAGNE}</loc><lastmod>2026-10-30</lastmod></url>
        <url><loc>{HUMMUS}</loc><lastmod>2026-10-28</lastmod></url>
        <url><loc>{LASAGNE}</loc><lastmod>2026-10-30</lastmod></url>
        <url><loc>https://blog.giallozafferano.it/another</loc></url>
        <url><loc>https://ricette.giallozafferano.it/ricerca-ricette/x?test=1</loc></url>
        </urlset>'''.encode()
        self.index = BASE + '/sitemap/index.xml'
        self.assets = {
            self.index: index,
            BASE + '/sitemap/a.xml': pages,
            LASAGNE: (ROOT/'Lasagne-al-forno.html').read_bytes(),
            HUMMUS: (ROOT/'Hummus-senza-tahina.html').read_bytes(),
        }
        self.http = FakeHttp(self.assets)

    def tearDown(self):
        self.db.close()
        self.tmp.cleanup()

    def test_nested_discovery_and_deduplication(self):
        r = discover(self.db, self.http, seeds=[self.index])
        self.assertEqual(r['new'], 2)
        self.assertEqual(stats(self.db)['pending'], 2)
        self.assertEqual(stats(self.db)['sitemaps_scanned'], 2)
        self.assertEqual(r['existing'], 1)

    def test_crawl_resume_export_and_enrich(self):
        discover(self.db, self.http, seeds=[self.index])
        first = crawl(self.db, self.http, limit=1)
        self.assertEqual(first['done'], 1)
        self.assertEqual(stats(self.db)['pending'], 1)
        second = crawl(self.db, self.http, limit=0)
        self.assertEqual(second['done'], 1)
        self.assertEqual(stats(self.db)['recipes_saved'], 2)
        self.assertEqual(crawl(self.db, self.http, limit=3)['attempted'], 0)
        out = Path(self.tmp.name)/'recipes.jsonl'
        self.assertEqual(export(self.db,out),2)
        recipes = [json.loads(s) for s in out.read_text().splitlines()]
        self.assertEqual(sorted(len(x['ingredients']) for x in recipes),[11,11])
        self.assertTrue(all('images' not in x and 'step_images_count' not in x for x in recipes))
        self.assertTrue(all('image_url' not in related for recipe in recipes for related in recipe['related_recipes']))
        self.assertEqual(sum(len(x['related_recipes']) for x in recipes),10)
        self.assertEqual([x['title'] for x in recipes],['Hummus senza tahina','Lasagne al forno'])
        self.assertTrue(all('crawl' in r for r in recipes))
        self.assertEqual(len(self.http.calls),4)
        # Re-discovery leaves successfully processed pages out of the work queue.
        discover(self.db, self.http, seeds=[self.index], refresh_days=90)
        self.assertNotIn('pending',stats(self.db))

    def test_sitemap_lastmod_requeues_updated_recipe(self):
        discover(self.db,self.http,seeds=[self.index])
        crawl(self.db,self.http,limit=0)
        self.assets[BASE+'/sitemap/a.xml'] = self.assets[BASE+'/sitemap/a.xml'].replace(
            b'<lastmod>2026-10-30</lastmod>', b'<lastmod>2026-11-02</lastmod>')
        discover(self.db,self.http,seeds=[self.index])
        self.assertEqual(stats(self.db)['pending'],1)

    def test_sitemap_parser_namespace_and_malformed(self):
        self.assertEqual(list(sitemap_items(self.assets[self.index]))[0][0],'sitemap')
        with self.assertRaises(ValueError):
            list(sitemap_items(b'<not-valid>'))

    def test_rejects_unsafe_urls(self):
        self.assertIsNone(normalize_url('https://blog.giallozafferano.it/abc.html'))
        self.assertIsNone(normalize_url('https://ricette.giallozafferano.it/abc.html?foo=1'))
        self.assertIsNone(normalize_url('ftp://ricette.giallozafferano.it/abc.html'))
        self.assertEqual(normalize_url(LASAGNE+'#part'), LASAGNE)

    def test_robots_fail_closed(self):
        r = Mock()
        r.status_code = 403
        s = Mock()
        s.get.return_value = r
        s.headers = {}
        http = PoliteHttp('RecipeArchiveResearchBot/0.6.2',session=s)
        with self.assertRaises(CrawlBlocked):
            http.initialize_robots()
        self.assertEqual(s.get.call_count,1)

    def test_robots_explicit_disallow(self):
        r = Mock()
        r.status_code=200
        r.text='User-agent: *\nDisallow: /\n'
        s=Mock()
        s.get.return_value=r
        s.headers={}
        with self.assertRaises(CrawlBlocked):
            PoliteHttp('RecipeArchiveResearchBot/0.6.2',session=s).initialize_robots()

    def test_rate_limit_does_not_continue(self):
        robots = Mock(status_code=200,text='User-agent: *\nAllow: /')
        limited = Mock(status_code=429, headers={'Retry-After':'600'})
        s = Mock()
        s.get.side_effect = [robots, limited]
        s.headers = {}
        http=PoliteHttp('RecipeArchiveResearchBot/0.6.2',delay=2,session=s,sleep=lambda _:None, monotonic=lambda:100)
        with self.assertRaises(CrawlBlocked):
            http.get(LASAGNE)
        self.assertEqual(s.get.call_count,2)


if __name__=='__main__':
    unittest.main()
