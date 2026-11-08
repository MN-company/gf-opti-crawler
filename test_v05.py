import unittest as _fixture_unittest
from pathlib import Path as _fixture_Path
if not (_fixture_Path(__file__).parent / "Lasagne-al-forno.html").exists() or not (_fixture_Path(__file__).parent / "Hummus-senza-tahina.html").exists():
    raise _fixture_unittest.SkipTest("Optional third-party HTML fixtures not distributed")
"""Regression tests for v0.5 completeness and no-loss guarantees. Offline only."""
from pathlib import Path
import json
import gzip
import sqlite3
import tempfile
import unittest
from unittest.mock import Mock

from bs4 import BeautifulSoup
from crawler import (BASE, CrawlBlocked, PoliteHttp, audit, backup_db, crawl,
                     discover, export_issues, open_db, requeue_for_review,
                     upsert_link)
from scraper import parse

ROOT = Path(__file__).parent
A = BASE + '/Lasagne-al-forno.html'
B = BASE + '/Hummus-senza-tahina.html'
S = BASE + '/sitemap/test.xml'

class OfflineHttp:
    def __init__(self, mapping):
        self.mapping = mapping
        self.calls = []
    def get(self, url, **kwargs):
        self.calls.append((url,kwargs))
        result = self.mapping[url]
        if isinstance(result, Exception):
            raise result
        if callable(result):
            return result()
        if isinstance(result, bytes):
            return 200,result,{'Content-Type':'text/html'}
        return result

class Tests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = open_db(Path(self.tmp.name)/'recipes.sqlite3')
        self.sitemap = f'<urlset><url><loc>{A}</loc></url><url><loc>{B}</loc></url></urlset>'.encode()
        self.http = OfflineHttp({S:self.sitemap, A:(ROOT/'Lasagne-al-forno.html').read_bytes(),
                                 B:(ROOT/'Hummus-senza-tahina.html').read_bytes()})
    def tearDown(self):
        self.db.close()
        self.tmp.cleanup()

    def test_audit_false_before_discovery_and_until_crawled(self):
        self.assertFalse(audit(self.db)['complete_for_last_discovery'])
        r=discover(self.db,self.http,seeds=[S])
        self.assertTrue(r['discovery_complete'])
        self.assertEqual(audit(self.db)['latest_discovery']['unique_urls'], 2)
        self.assertFalse(audit(self.db)['complete_for_last_discovery'])
        crawl(self.db,self.http,limit=0)
        self.assertTrue(audit(self.db, deep=True)['complete_for_last_discovery'])
        self.assertEqual(audit(self.db, deep=True)['saved_recipes'],2)
        self.assertEqual(audit(self.db, deep=True)['deep_validation']['incomplete_recipe'],0)

    def test_invalid_sitemap_rolls_back_all_urls_and_marks_partial(self):
        broken=f'<urlset><url><loc>{A}</loc></url><url>'.encode()
        self.http.mapping[S]=broken
        r=discover(self.db,self.http,seeds=[S])
        self.assertFalse(r['discovery_complete'])
        self.assertEqual(r['unique_urls_in_scan'],0)
        self.assertEqual(self.db.execute('SELECT count(*) FROM urls').fetchone()[0],0)
        self.assertEqual(audit(self.db)['latest_discovery']['sitemap_errors'],1)

    def test_one_failed_sitemap_not_mistaken_for_complete_scan(self):
        self.http.mapping[BASE+'/sitemap/missing.xml']=RuntimeError('timeout')
        r=discover(self.db,self.http,seeds=[S,BASE+'/sitemap/missing.xml'])
        self.assertFalse(r['discovery_complete'])
        self.assertEqual(r['unique_urls_in_scan'],2)
        self.assertEqual(audit(self.db)['latest_discovery']['sitemap_status'],'partial')

    def test_schema_less_html_with_dom_is_supported(self):
        soup=BeautifulSoup((ROOT/'Lasagne-al-forno.html').read_text(), 'lxml')
        for tag in soup.select('script[type="application/ld+json"]'):tag.decompose()
        self.http.mapping[A]=str(soup).encode('utf-8')
        upsert_link(self.db,A)
        self.db.commit()
        r=crawl(self.db,self.http,limit=1)
        self.assertEqual(r['done'],1)
        recipe=json.loads(self.db.execute('SELECT json FROM recipes WHERE url=?',(A,)).fetchone()[0])
        self.assertFalse(recipe['has_jsonld_recipe'])
        self.assertEqual(len(recipe['ingredients']),11)
        self.assertNotIn('instructions', recipe)
        self.assertTrue(recipe['utensils_detected'])

    def test_no_recipe_goes_to_review_not_silent_done(self):
        self.http.mapping[A]=b'<html><head><title>Not a recipe</title></head><body></body></html>'
        upsert_link(self.db,A)
        self.db.commit()
        r=crawl(self.db,self.http,limit=1)
        self.assertEqual(r['review'],1)
        self.assertEqual(audit(self.db)['all_known_status']['review'],1)
        self.assertEqual(requeue_for_review(self.db,'review',100),1)
        self.assertEqual(audit(self.db)['all_known_status']['pending'],1)

    def test_multiple_404_remain_unconfirmed_and_retryable(self):
        self.http.mapping[A]=(404,b'',{})
        upsert_link(self.db,A)
        self.db.commit()
        r=crawl(self.db,self.http,limit=1)
        self.assertEqual(r['retry'],1)
        self.db.execute("UPDATE urls SET retry_at='2000-01-01T00:00:00+00:00'")
        self.db.commit()
        r=crawl(self.db,self.http,limit=1)
        self.assertEqual(r['temporarily_missing'],1)
        self.assertEqual(self.db.execute('SELECT status FROM urls').fetchone()[0],'retry')
        self.assertEqual(self.db.execute('SELECT not_found_count FROM urls').fetchone()[0],2)

    def test_retries_are_bounded_and_issues_export(self):
        self.http.mapping[A]=RuntimeError('broken connection')
        upsert_link(self.db,A)
        self.db.commit()
        crawl(self.db,self.http,limit=1,max_attempts=2)
        self.db.execute("UPDATE urls SET retry_at='2000-01-01T00:00:00+00:00'")
        self.db.commit()
        crawl(self.db,self.http,limit=1,max_attempts=2)
        self.assertEqual(audit(self.db)['all_known_status']['failed'],1)
        path=Path(self.tmp.name)/'issues.jsonl'
        self.assertEqual(export_issues(self.db,path),1)
        self.assertEqual(json.loads(path.read_text())['status'],'failed')

    def test_304_without_recipe_does_not_loop_or_mark_done(self):
        self.http.mapping[A]=(304,b'',{})
        upsert_link(self.db,A)
        self.db.execute("UPDATE urls SET etag='xyz' WHERE url=?",(A,))
        self.db.commit()
        r=crawl(self.db,self.http,limit=0)
        self.assertEqual(r['attempted'],1)
        self.assertEqual(audit(self.db)['all_known_status']['review'],1)

    def test_related_links_discovery_opt_in(self):
        upsert_link(self.db,A)
        self.db.commit()
        crawl(self.db,self.http,limit=1, follow_related=True)
        n=self.db.execute("SELECT COUNT(*) FROM urls WHERE source='related'").fetchone()[0]
        self.assertGreater(n,0)
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM urls WHERE status='done'").fetchone()[0],1)

    def test_backup_is_sqlite_and_keeps_recipes(self):
        discover(self.db,self.http,seeds=[S]);crawl(self.db,self.http,limit=0)
        dest=Path(self.tmp.name)/'backup.sqlite3'
        backup_db(self.db,dest)
        with sqlite3.connect(dest) as snapshot:
            self.assertEqual(snapshot.execute('SELECT COUNT(*) FROM recipes').fetchone()[0],2)

    def test_sitemap_conflicting_lastmod_preserves_newest(self):
        upsert_link(self.db,A,'2026-11-05')
        upsert_link(self.db,A,'2026-10-01')
        self.assertEqual(self.db.execute('SELECT lastmod FROM urls').fetchone()[0],'2026-11-05')

    def test_gzipped_sitemap(self):
        gz_url = BASE + '/sitemap/test.xml.gz'
        self.http.mapping[gz_url] = gzip.compress(self.sitemap)
        result = discover(self.db,self.http,seeds=[gz_url])
        self.assertTrue(result['discovery_complete'])
        self.assertEqual(result['unique_urls_in_scan'],2)

    def test_redirect_alias_is_not_exported_as_a_duplicate_recipe(self):
        upsert_link(self.db,A)
        self.db.commit()
        self.http.mapping[A] = (200,(ROOT/'Hummus-senza-tahina.html').read_bytes(),
                                {'X-Crawler-Final-URL':B,'Content-Type':'text/html'})
        r = crawl(self.db,self.http,limit=1)
        self.assertEqual(r['redirect_aliases'],1)
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM recipes').fetchone()[0],1)
        self.assertEqual(self.db.execute('SELECT url FROM recipes').fetchone()[0],B)
        self.assertEqual(self.db.execute('SELECT status FROM urls WHERE url=?',(A,)).fetchone()[0],'redirect')
        self.assertEqual(self.db.execute('SELECT status FROM urls WHERE url=?',(B,)).fetchone()[0],'done')

    def test_robots_denied_redirect_does_not_follow(self):
        from requests.models import Response
        class FakeSession:
            headers={}
            def __init__(self):self.calls=[]
            def get(self,url,**kwargs):
                self.calls.append(url)
                if url.endswith('robots.txt'):
                    return Mock(status_code=200,text='User-agent: *\nAllow: /\nDisallow: /private/\n')
                return Mock(status_code=302,headers={'Location':'/private/secret.html'},close=lambda:None)
        ss=FakeSession()
        http=PoliteHttp('RecipeArchiveResearchBot/0.6.2',delay=2,session=ss,sleep=lambda n:None,monotonic=lambda:100)
        with self.assertRaises(CrawlBlocked):http.get(A)
        self.assertEqual(len(ss.calls),2)

if __name__=='__main__':unittest.main()

class Policies(unittest.TestCase):
    def test_robot_longest_rule_and_wildcards(self):
        from crawler import RobotsPolicy
        rules = RobotsPolicy('''User-agent: *
Allow: /
Disallow: /private/
Allow: /private/public/
Disallow: /*.pdf$
Sitemap: https://ricette.giallozafferano.it/sitemap/test.xml
''')
        ua='RecipeArchiveResearchBot/0.5'
        self.assertFalse(rules.can_fetch(ua,BASE+'/private/x.html'))
        self.assertTrue(rules.can_fetch(ua,BASE+'/private/public/a.html'))
        self.assertFalse(rules.can_fetch(ua,BASE+'/documents/a.pdf'))
        self.assertTrue(rules.can_fetch(ua,BASE+'/documents/a.pdf.html'))
        self.assertEqual(len(rules.site_maps()),1)

    def test_robot_specific_agent_overrides_generic_group(self):
        from crawler import RobotsPolicy
        rules = RobotsPolicy('''User-agent: *
Disallow: /recipe/

User-agent: RecipeArchiveResearchBot
Allow: /recipe/
Disallow: /recipe/private/
''')
        ua='RecipeArchiveResearchBot/0.5'
        self.assertTrue(rules.can_fetch(ua,BASE+'/recipe/a.html'))
        self.assertFalse(rules.can_fetch(ua,BASE+'/recipe/private/x.html'))

    def test_successful_same_host_redirect(self):
        from crawler import RobotsPolicy
        class Response:
            def __init__(self,status, location=None):
                self.status_code=status
                self.headers={'Location':location} if location else {'Content-Type':'text/html'}
            def close(self):pass
            def iter_content(self,chunk_size=65536):yield b'<html>content</html>'
        class Session:
            headers={}
            def __init__(self):self.calls=[]
            def get(self,url,**kwargs):
                self.calls.append(url)
                if url==A:return Response(301,'/Hummus-senza-tahina.html')
                if url==B:return Response(200)
                raise AssertionError(url)
        ss=Session()
        http=PoliteHttp('RecipeArchiveResearchBot/0.6.2',delay=2,session=ss,sleep=lambda n:None,monotonic=lambda:100)
        http.robot=RobotsPolicy('User-agent: *\nAllow: /')
        status,body,headers=http.get(A)
        self.assertEqual(status,200)
        self.assertEqual(headers['X-Crawler-Final-URL'],B)
        self.assertEqual(len(ss.calls),2)
