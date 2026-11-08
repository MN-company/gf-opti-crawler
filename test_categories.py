import unittest as _fixture_unittest
from pathlib import Path as _fixture_Path
if not (_fixture_Path(__file__).parent / "Lasagne-al-forno.html").exists() or not (_fixture_Path(__file__).parent / "Hummus-senza-tahina.html").exists():
    raise _fixture_unittest.SkipTest("Optional third-party HTML fixtures not distributed")
"""Offline integration tests for GialloZafferano www category discovery (v0.6)."""
from pathlib import Path
import json
import tempfile
import unittest

from crawler import (BASE,WEB_BASE, PoliteHttp, RobotsPolicy, CrawlBlocked, audit,
                     crawl, discover, normalize_url, open_db, stats, requeue_for_review)
from catalog_discovery import discover_categories, scan_categories, listing_links

ROOT=Path(__file__).parent
A=BASE+'/Lasagne-al-forno.html'
B=BASE+'/Hummus-senza-tahina.html'
C=WEB_BASE+'/ricette-cat/Primi/'
D=WEB_BASE+'/ricette-con-ceci/'
P=WEB_BASE+'/ricette-cat/Primi/pagina/2/'
X=WEB_BASE+'/sitemap/portate.xml'
Y=WEB_BASE+'/sitemap/ricerche.xml'
S=BASE+'/sitemap/recipes.xml'

HTML_1=f'''<html><body><main>
<article class="gz-card" data-ets-tipo="R"><h2><a href="{A}">Lasagne</a></h2></article>
<article class="gz-card" data-ets-tipo="R"><a href="{B}">Hummus</a></article>
<nav class="pagination"><a rel="next" href="/ricette-cat/Primi/pagina/2/">2</a>
<a href="/ricette-cat/Primi/?sort=recent">Filtro</a></nav></main></body></html>'''.encode()
HTML_2=f'''<html><script type="application/ld+json">{{"@type":"ItemList", "itemListElement":[{{"@type":"ListItem","item":{{"url":"{A}"}}}}]}}</script></html>'''.encode()

class Offline:
    def __init__(self,mapping):
        self.mapping=mapping
        self.calls=[]
        self.robot=RobotsPolicy('User-agent: *\nAllow: /\nDisallow: /ricette-cat/*?\nDisallow: /ricette-con*/?')
        self.agent='RecipeArchiveResearchBot/0.6'
    def initialize_robots(self):return self.robot
    def get(self,url,**kwargs):
        self.calls.append(url)
        v=self.mapping[url]
        if isinstance(v,Exception):raise v
        if isinstance(v,tuple):return v
        return 200,v,{'Content-Type':'text/html'}

class Categories(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.db=open_db(Path(self.tmp.name)/'recipes.sqlite3')
        self.recipe_http=Offline({S:f'<urlset><url><loc>{A}</loc></url></urlset>'.encode(),
                                 A:(ROOT/'Lasagne-al-forno.html').read_bytes(),
                                 B:(ROOT/'Hummus-senza-tahina.html').read_bytes()})
        self.category_http=Offline({X:f'<urlset><url><loc>{C}</loc><lastmod>2026-11-01</lastmod></url><url><loc>{C}?sort=new</loc></url></urlset>'.encode(),
                                    Y:f'<urlset><url><loc>{D}</loc></url></urlset>'.encode(),
                                    C:HTML_1,D:HTML_2,P:HTML_2})
    def tearDown(self):
        self.db.close()
        self.tmp.cleanup()

    def test_only_listings_not_query_urls(self):
        self.assertEqual(normalize_url(C,'listing'),C)
        self.assertIsNone(normalize_url(C+'?x=1','listing'))
        self.assertIsNone(normalize_url(WEB_BASE+'/admin/','listing'))
        self.assertEqual(normalize_url(X,'sitemap'),X)
        self.assertIsNone(normalize_url(WEB_BASE+'/ricette-cat/Primi/','recipe'))
        self.assertIsNone(normalize_url('https://blog.giallozafferano.it/fake','listing'))

    def test_html_cards_ld_and_pagination(self):
        recipes,pages=listing_links(HTML_1.decode(),C)
        self.assertEqual(recipes,{A,B})
        _,_,skipped=listing_links(HTML_1.decode(),C,include_skip_count=True)
        self.assertEqual(skipped,1)
        self.assertEqual(pages,{P})
        recipes,_=listing_links(HTML_2.decode(),D)
        self.assertEqual(recipes,{A})

    def test_discovery_is_atomic(self):
        self.category_http.mapping[X]=f'<urlset><url><loc>{C}</loc></url><url>'.encode()
        r=discover_categories(self.db,self.category_http,seeds=[X])
        self.assertFalse(r['discovery_complete'])
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM listing_pages').fetchone()[0],0)

    def test_sitemap_nested(self):
        IDX=WEB_BASE+'/sitemap/index.xml'
        self.category_http.mapping[IDX]=f'<sitemapindex><sitemap><loc>{X}</loc></sitemap></sitemapindex>'.encode()
        r=discover_categories(self.db,self.category_http,seeds=[IDX])
        self.assertTrue(r['discovery_complete'])
        self.assertEqual(r['unique_listing_urls'],1)

    def test_category_only_and_exact_combined_audit(self):
        discover(self.db,self.recipe_http,seeds=[S])
        result=discover_categories(self.db,self.category_http,seeds=[X,Y])
        self.assertTrue(result['discovery_complete'])
        self.assertEqual(result['unique_listing_urls'],2)
        scan=scan_categories(self.db,self.category_http,limit=0)
        self.assertEqual(scan['done'],3)
        self.assertEqual(scan['recipe_urls_new'],1)
        self.assertNotIn(C+'?sort=new',self.category_http.calls)
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM urls').fetchone()[0],2)
        pre=audit(self.db)
        self.assertEqual(pre['recipes_found_only_via_categories'],1)
        self.assertEqual(pre['unresolved_recipes_from_categories'],2)
        self.assertFalse(pre['complete_for_combined_sources'])
        crawl(self.db,self.recipe_http,limit=0)
        post=audit(self.db,deep=True)
        self.assertTrue(post['complete_for_combined_sources'])
        self.assertEqual(post['category_discovery']['listings_resolved_percent'],100)
        self.assertEqual(post['saved_recipes'],2)
        self.assertEqual(post['category_query_links_not_visited'],1)

    def test_incomplete_listings_do_not_claim_completion(self):
        discover(self.db,self.recipe_http,seeds=[S])
        discover_categories(self.db,self.category_http,seeds=[X,Y])
        scan_categories(self.db,self.category_http,limit=1)
        crawl(self.db,self.recipe_http,limit=0)
        self.assertFalse(audit(self.db)['complete_for_combined_sources'])

    def test_empty_category_requires_review(self):
        self.category_http.mapping[C]=b'<html><title>Categoria</title><body>No cards here</body></html>'
        discover_categories(self.db,self.category_http,seeds=[X])
        scan=scan_categories(self.db,self.category_http,limit=0)
        self.assertEqual(scan['review'],1)
        self.assertEqual(self.db.execute('SELECT status FROM listing_pages').fetchone()[0],'review')
        self.assertEqual(requeue_for_review(self.db,'review',10,listings=True),1)
        self.assertEqual(self.db.execute('SELECT status FROM listing_pages').fetchone()[0],'pending')

    def test_category_robots_exclusion_is_not_fetched(self):
        self.category_http.robot=RobotsPolicy('User-agent: *\nDisallow: /ricette-cat/\n')
        discover_categories(self.db,self.category_http,seeds=[X])
        scan=scan_categories(self.db,self.category_http,limit=0)
        self.assertEqual(scan['excluded'],1)
        self.assertNotIn(C,self.category_http.calls)
        self.assertFalse(audit(self.db)['complete_for_combined_sources'])

    def test_sitemap_missing_partial(self):
        self.category_http.mapping[Y]=RuntimeError('network dropped')
        r=discover_categories(self.db,self.category_http,seeds=[X,Y])
        self.assertFalse(r['discovery_complete'])
        self.assertEqual(r['errors'],1)

    def test_rediscovery_does_not_repeat_done_categories(self):
        discover_categories(self.db,self.category_http,seeds=[X])
        scan_categories(self.db,self.category_http,limit=0)
        first=len(self.category_http.calls)
        discover_categories(self.db,self.category_http,seeds=[X])
        scan=scan_categories(self.db,self.category_http,limit=0)
        self.assertEqual(scan['attempted'],0)
        self.assertEqual(len(self.category_http.calls),first+1)

if __name__=='__main__':unittest.main()
