from __future__ import annotations
import unittest as _fixture_unittest
from pathlib import Path as _fixture_Path
if not (_fixture_Path(__file__).parent / "Lasagne-al-forno.html").exists() or not (_fixture_Path(__file__).parent / "Hummus-senza-tahina.html").exists():
    raise _fixture_unittest.SkipTest("Optional third-party HTML fixtures not distributed")
"""Offline integrity, performance and nutrition regressions for v0.8."""

import asyncio
from pathlib import Path
import json
import sqlite3
import tempfile
import time
import unittest

from bs4 import BeautifulSoup

from crawler import BASE, WEB_BASE, audit, open_db, upsert_link, next_pending, PoliteHttp, RobotsPolicy
from catalog_discovery import upsert_listing
from scraper import parse
from integrity import persist, mark_missing, prune_procedures, stripped_recipe
from async_pipeline import AsyncPoliteHttp, Pipeline

HERE=Path(__file__).parent
LAS=BASE+'/Lasagne-al-forno.html'
HUM=BASE+'/Hummus-senza-tahina.html'
ALT=BASE+'/Lasagne-al-forno-alias.html'
CAT=WEB_BASE+'/ricette-cat/Primi/'

class TestV08(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.conn=open_db(Path(self.tmp.name)/'db.sqlite3')
        self.las=(HERE/'Lasagne-al-forno.html').read_text(encoding='utf-8')
        self.hum=(HERE/'Hummus-senza-tahina.html').read_text(encoding='utf-8')
    def tearDown(self):
        self.conn.close()
        self.tmp.cleanup()

    def _save(self,url,html):
        upsert_link(self.conn,url)
        return persist(self.conn,url,{},parse(html,url,include_images=False),'2026-11-07T20:00:00+00:00')

    def test_two_url_aliases_store_one_canonical(self):
        first=self._save(LAS,self.las)
        second=self._save(ALT,self.las)
        self.assertEqual(first['status'],'saved')
        self.assertEqual(second['status'],'unchanged')
        self.assertEqual(self.conn.execute('SELECT COUNT(*) FROM recipes').fetchone()[0],1)
        self.assertEqual(self.conn.execute('SELECT canonical_url FROM recipe_aliases WHERE alias_url=?',(ALT,)).fetchone()[0],LAS)
        self.assertEqual(self.conn.execute('SELECT status FROM urls WHERE url=?',(ALT,)).fetchone()[0],'redirect')

    def test_quality_fail_preserves_valid_recipe(self):
        self._save(LAS,self.las)
        old=self.conn.execute('SELECT json FROM recipes WHERE url=?',(LAS,)).fetchone()[0]
        soup=BeautifulSoup(self.las,'lxml')
        for el in soup.select('script[type="application/ld+json"]'): el.decompose()
        for el in soup.select('dl.gz-list-ingredients'): el.decompose()
        out=self._save(LAS,str(soup))
        self.assertEqual(out['status'],'review')
        self.assertEqual(self.conn.execute('SELECT json FROM recipes WHERE url=?',(LAS,)).fetchone()[0],old)
        self.assertFalse(audit(self.conn)['global_queue_complete'])

    def test_old_pending_prevents_complete_audit(self):
        self._save(LAS,self.las)
        upsert_link(self.conn,HUM)
        self.conn.commit()
        result=audit(self.conn,deep=True)
        self.assertFalse(result['global_queue_complete'])
        self.assertEqual(result['global_unresolved_urls'],1)
        self.assertFalse(result['complete_for_combined_sources'])

    def test_404_stays_retry_and_can_recover(self):
        upsert_link(self.conn,LAS)
        old=next_pending(self.conn)
        mark_missing(self.conn,old,404,'2026-11-07T20:00:00+00:00')
        self.assertEqual(self.conn.execute('SELECT status FROM urls WHERE url=?',(LAS,)).fetchone()[0],'retry')
        result=self._save(LAS,self.las)
        self.assertEqual(result['status'],'saved')
        self.assertEqual(self.conn.execute('SELECT not_found_count FROM urls WHERE url=?',(LAS,)).fetchone()[0],0)

    def test_listing_queue_uses_index_for_sort(self):
        for i in range(300):
            upsert_listing(self.conn,WEB_BASE+f'/ricette-cat/Category{i:03d}/')
        self.conn.commit()
        plan=' '.join(str(tuple(row)) for row in self.conn.execute('EXPLAIN QUERY PLAN SELECT * FROM listing_pages WHERE status=\'pending\' ORDER BY first_seen,url LIMIT 1'))
        self.assertNotIn('TEMP B-TREE',plan)

    def test_nutrients_only_if_present_on_source(self):
        las=parse(self.las,LAS,include_images=False)
        hum=parse(self.hum,HUM,include_images=False)
        self.assertEqual(len(las['nutrition_details']['values']),9)
        self.assertEqual(las['nutrition_details']['values']['protein']['value'],44.1)
        self.assertEqual(las['nutrition_details']['values']['calories']['value'],670.9)
        self.assertFalse(hum['nutrition_details']['available'])
        self.assertEqual(hum['nutrition_details']['values'],{})
        self.assertNotIn('images',hum)
        self.assertNotIn('image_url',str(hum['related_recipes']))

    def test_delay_default_is_four(self):
        from unittest.mock import patch
        from crawler import main
        c=PoliteHttp(user_agent='RecipeTest/1.0')
        self.assertEqual(c.delay,4)
        from crawler import RobotsPolicy
        rob=RobotsPolicy('User-agent: *\nAllow: /\nCrawl-delay: 7')
        self.assertEqual(rob.crawl_delay('RecipeTest/1.0'),7)

    def test_recipe_keeps_utensils_but_never_procedure(self):
        for name, source in (("Lasagne-al-forno.html", LAS), ("Hummus-senza-tahina.html", HUM)):
            result = parse((HERE/name).read_text(encoding='utf-8'), source)
            self.assertTrue(result['utensils_detected'])
            for field in ('instructions','instruction_steps','instruction_steps_with_photos',
                          'techniques_detected','images','step_images_count'):
                self.assertNotIn(field,result)
            self.assertEqual(result['nutrition_details']['available'], name.startswith('Lasagne'))

    def test_jsonld_only_can_identify_utensils_without_steps_in_output(self):
        body = '''<html><head><script type="application/ld+json">
        {"@context":"https://schema.org","@type":"Recipe","name":"Prova frullata",
         "recipeIngredient":["Ceci 100 g","Acqua 50 ml"],
         "recipeInstructions":[{"@type":"HowToStep","text":"Frullare i ceci con un mixer."}]}
        </script></head></html>'''
        recipe = parse(body, LAS)
        self.assertIn('mixer',[x['name'] for x in recipe['utensils_detected']])
        self.assertNotIn('instructions',recipe)
        self.assertNotIn('instruction_steps',recipe)

    def test_prune_legacy_database_and_export_text_only(self):
        from crawler import export
        self._save(LAS,self.las)
        row = self.conn.execute('SELECT json FROM recipes WHERE url=?',(LAS,)).fetchone()
        legacy = json.loads(row['json'])
        legacy['instructions'] = ['Passaggio da cancellare, senza alcuna utilità per il catalogo.']
        legacy['instruction_steps'] = [{'index':1,'text':'Testo da cancellare'}]
        legacy['techniques_detected'] = [{'name':'mescolare'}]
        legacy['images'] = ['https://example.test/photo.jpg']
        legacy['related_recipes']=[{'title':'Altra ricetta','url':'https://example.test/r','image_url':'https://example.test/photo.jpg'}]
        legacy['method']='stir' 
        self.conn.execute('UPDATE recipes SET json=? WHERE url=?',(json.dumps(legacy),LAS))
        self.conn.commit()
        out = Path(self.tmp.name)/'out.jsonl'
        export(self.conn,out)
        self.assertNotIn('Passaggio da cancellare',out.read_text())
        self.assertEqual(prune_procedures(self.conn),1)
        self.assertEqual(prune_procedures(self.conn),0)
        record = json.loads(self.conn.execute('SELECT json FROM recipes WHERE url=?',(LAS,)).fetchone()[0])
        self.assertNotIn('instructions',record)
        self.assertNotIn('method',record)
        self.assertNotIn('image_url',json.dumps(record))
        self.assertNotIn('instruction_steps',record)
        self.assertNotIn('techniques_detected',record)
        self.assertTrue(record['utensils_detected'])
        self.assertEqual(record['quality']['status'],'quality_passed')
        digest = self.conn.execute('SELECT content_hash FROM recipes WHERE url=?',(LAS,)).fetchone()[0]
        self.assertEqual(record['crawl']['content_sha256'],digest)

    def test_deep_audit_detects_legacy_procedure_media(self):
        self._save(LAS,self.las)
        legacy = json.loads(self.conn.execute('SELECT json FROM recipes WHERE url=?',(LAS,)).fetchone()[0])
        legacy['instructions']=['Text not to keep']
        legacy['related_recipes']=[{'url':'https://example.test/recipe','image_url':'https://example.test/photo.jpg'}]
        self.conn.execute('UPDATE recipes SET json=? WHERE url=?',(json.dumps(legacy),LAS))
        self.conn.commit()
        report=audit(self.conn,deep=True)
        self.assertEqual(report['deep_validation']['records_with_procedure_fields'],1)
        self.assertEqual(report['deep_validation']['records_with_image_fields'],1)
        self.assertFalse(report['data_quality_complete'])
        prune_procedures(self.conn)
        report=audit(self.conn,deep=True)
        self.assertEqual(report['deep_validation']['records_with_procedure_fields'],0)
        self.assertEqual(report['deep_validation']['records_with_image_fields'],0)

    def test_legacy_status_gone_migrated_to_retry(self):
        upsert_link(self.conn,LAS)
        self.conn.execute("UPDATE urls SET status='gone',error='HTTP 404',not_found_count=0 WHERE url=?",(LAS,))
        self.conn.commit()
        self.conn.close()
        self.conn=open_db(Path(self.tmp.name)/'db.sqlite3')
        self.assertEqual(self.conn.execute('SELECT status FROM urls WHERE url=?',(LAS,)).fetchone()[0],'retry')

if __name__=='__main__': unittest.main()
