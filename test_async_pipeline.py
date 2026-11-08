import unittest as _fixture_unittest
from pathlib import Path as _fixture_Path
if not (_fixture_Path(__file__).parent / "Lasagne-al-forno.html").exists() or not (_fixture_Path(__file__).parent / "Hummus-senza-tahina.html").exists():
    raise _fixture_unittest.SkipTest("Optional third-party HTML fixtures not distributed")
import asyncio
from pathlib import Path
import sqlite3
import tempfile
import time
import unittest

from async_pipeline import AsyncPoliteHttp, Pipeline
from crawler import BASE, WEB_BASE, CrawlBlocked, PoliteHttp, RobotsPolicy, open_db, upsert_link
from catalog_discovery import upsert_listing

ROOT = Path(__file__).parent
LASAGNE = BASE + '/Lasagne-al-forno.html'
HUMMUS = BASE + '/Hummus-senza-tahina.html'
LISTING = WEB_BASE + '/ricette-cat/Primi/'


class MockHttp:
    def __init__(self, host, pages, log, latency=.03):
        self.host = host
        self.pages = pages
        self.log = log
        self.latency = latency
        self.agent = 'RecipeArchiveTestBot/1.0'
        self.robot = RobotsPolicy('User-agent: *\nAllow: /\n')

    async def get(self, url, **kwargs):
        self.log.append((self.host, url, time.monotonic()))
        await asyncio.sleep(self.latency)
        result = self.pages[url]
        if isinstance(result, Exception):
            raise result
        return result

    async def close(self):
        pass


class AsyncPipelineTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.db = open_db(Path(self.directory.name) / 'recipes.sqlite3')
        self.log = []
        self.category_html = f'''<article class="gz-card"><a href="{HUMMUS}">Hummus</a></article>'''
        self.recipe_pages = {
            LASAGNE: (200,(ROOT/'Lasagne-al-forno.html').read_bytes(),{'content-type':'text/html'}),
            HUMMUS: (200,(ROOT/'Hummus-senza-tahina.html').read_bytes(),{'content-type':'text/html'}),
        }

    def tearDown(self):
        self.db.close()
        self.directory.cleanup()

    def seed(self):
        upsert_link(self.db, LASAGNE)
        upsert_listing(self.db, LISTING)
        self.db.commit()

    def test_both_hosts_overlap_and_persist(self):
        self.seed()
        recipe = MockHttp('recipe',self.recipe_pages,self.log)
        category = MockHttp('listing',{LISTING:(200,self.category_html.encode(),{})},self.log)
        result = asyncio.run(Pipeline(self.db,recipe,category,
                                      category_limit=0,recipe_limit=0,batch_size=2).run())
        self.assertEqual(result['categories']['done'],1)
        self.assertEqual(result['recipes']['done'],2)
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM recipes').fetchone()[0],2)
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM urls WHERE status='done'").fetchone()[0],2)
        first_by_host = {host: t for host, _, t in self.log if host not in ()}
        self.assertLess(abs(self.log[0][2]-self.log[1][2]),.12)

    def test_async_browser_fallback_for_incomplete_html(self):
        upsert_link(self.db, LASAGNE)
        self.db.commit()
        class FakeBrowser:
            def __init__(self):
                self.calls = 0
            async def fetch_html(self, url):
                self.calls += 1
                return (ROOT/'Lasagne-al-forno.html').read_text(encoding='utf-8')
            async def close(self):
                pass
        recipe = MockHttp('recipe',{LASAGNE:(200,b'<html><title>empty</title></html>',{})},self.log)
        job = Pipeline(self.db,recipe,recipe_limit=0,batch_size=1)
        stub = FakeBrowser()
        job.browser = stub
        result = asyncio.run(job.run())
        self.assertEqual(stub.calls,1)
        self.assertEqual(result['recipes']['browser_fallback'],1)
        self.assertEqual(result['recipes']['saved'],1)

    def test_no_duplicate_from_listing_and_sitemap(self):
        self.seed()
        category_html = f'''<article class="gz-card"><a href="{LASAGNE}">Same</a></article>'''
        recipe = MockHttp('recipe',self.recipe_pages,self.log)
        category = MockHttp('listing',{LISTING:(200,category_html.encode(),{})},self.log)
        result = asyncio.run(Pipeline(self.db,recipe,category,batch_size=1).run())
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM recipes').fetchone()[0],1)
        self.assertEqual(result['categories']['recipe_urls_new'],0)
        self.assertEqual(result['recipes']['done'],1)

    def test_blocked_category_stops_pipeline(self):
        self.seed()
        recipe = MockHttp('recipe',self.recipe_pages,self.log,latency=.2)
        category = MockHttp('listing',{LISTING:CrawlBlocked('HTTP 429')},self.log,latency=.01)
        with self.assertRaises(CrawlBlocked):
            asyncio.run(Pipeline(self.db,recipe,category,batch_size=1).run())
        self.assertEqual(self.db.execute('SELECT status FROM listing_pages WHERE url=?',(LISTING,)).fetchone()[0],'pending')


if __name__=='__main__':
    unittest.main()

class AsyncHttpTests(unittest.TestCase):
    def test_get_uses_configured_agent_and_rejects_blocked_paths(self):
        import httpx
        async def runner():
            requests = []
            def respond(request):
                requests.append(request)
                return httpx.Response(200,text='ok')
            client = httpx.AsyncClient(transport=httpx.MockTransport(respond))
            checked = PoliteHttp(user_agent='TestArchive/1.0', delay=2)
            checked.robot = RobotsPolicy('User-agent: *\nAllow: /\nDisallow: /blocked/\n')
            checked.last_request = time.monotonic()-3
            checked_agent = AsyncPoliteHttp(checked,client=client)
            try:
                code, content, _ = await checked_agent.get(LASAGNE)
                self.assertEqual((code,content),(200,b'ok'))
                self.assertEqual(requests[0].headers['user-agent'],'TestArchive/1.0')
                with self.assertRaises(CrawlBlocked):
                    await checked_agent.get(BASE + '/blocked/evil.html')
                self.assertEqual(len(requests),1)
            finally:
                await checked_agent.close()
                await client.aclose()
        asyncio.run(runner())

    def test_http_429_retries_after_shared_cooldown(self):
        import httpx
        async def runner():
            seen = []
            def respond(request):
                seen.append(request)
                return (httpx.Response(429, headers={'Retry-After': '0'})
                        if len(seen) == 1 else httpx.Response(200, text='ok'))
            client = httpx.AsyncClient(transport=httpx.MockTransport(respond))
            checked = PoliteHttp(user_agent='TestArchive/1.0', delay=2)
            checked.robot = RobotsPolicy('User-agent: *\nAllow: /\n')
            checked.delay = .001  # Accelerate the mocked test, not production.
            checked.last_request = time.monotonic() - 3
            worker = AsyncPoliteHttp(checked, client=client, retry_base=.01)
            try:
                code, data, _ = await worker.get(LASAGNE)
                self.assertEqual((code, data), (200, b'ok'))
                self.assertEqual(len(seen), 2)
                self.assertGreater(worker.delay, worker.base_delay)
            finally:
                await client.aclose()
        asyncio.run(runner())

class AsyncCliTests(unittest.TestCase):
    def test_resume_does_not_rescan_sitemaps(self):
        from unittest.mock import patch
        import crawler
        from async_pipeline import run_pipeline
        async def fake_run(*args,**kwargs):
            self.assertEqual(kwargs['category_limit'],0)
            self.assertEqual(kwargs['recipe_limit'],0)
            self.assertEqual(kwargs['workers'],4)
            self.assertEqual(kwargs['http_retries'],3)
            self.assertEqual(kwargs['max_throttles'],4)
            return {'categories':{},'recipes':{}}
        def fake_robots(self):
            self.robot=RobotsPolicy('User-agent: *\nAllow: /\n')
            self.last_request=time.monotonic()-self.delay
            return self.robot
        with tempfile.TemporaryDirectory() as d:
            with patch.object(crawler.PoliteHttp,'initialize_robots',fake_robots):
                with patch('async_pipeline.run_pipeline',new=fake_run):
                    rc = crawler.main(['--db',str(Path(d)/'recipes.sqlite3'),
                                       'pipeline','--skip-discovery','--categories',
                                       '--category-limit','0','--all',
                                       '--workers','4','--http-retries','3',
                                       '--max-throttles','4'])
                    self.assertEqual(rc,0)
