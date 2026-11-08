import sqlite3
import tempfile
from pathlib import Path
import unittest
from unittest.mock import patch
from crawler import open_db, upsert_link
from integrity import find_saved, persist

BASE='https://ricette.giallozafferano.it/'
A,B,C=[BASE+x+'.html' for x in ('Alias-A','Alias-B','Canonical-C')]

class Regression082(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.conn=open_db(Path(self.tmp.name)/'test.sqlite3')
        for url in (A,B,C): upsert_link(self.conn,url)
    def tearDown(self):
        self.conn.close()
        self.tmp.cleanup()
    def test_legacy_alias_chain_resolves(self):
        self.conn.execute("INSERT INTO recipes(url,title,json,quality_status) VALUES(?,?,?,?)",(C,'C','{}','quality_passed'))
        self.conn.execute("INSERT INTO recipe_aliases(alias_url,canonical_url,first_seen,last_seen) VALUES(?,?,?,?)",(A,B,'t','t'))
        self.conn.execute("INSERT INTO recipe_aliases(alias_url,canonical_url,first_seen,last_seen) VALUES(?,?,?,?)",(B,C,'t','t'))
        self.assertEqual(find_saved(self.conn,A),C)
    def test_alias_cycle_not_resolved(self):
        self.conn.execute("INSERT INTO recipe_aliases(alias_url,canonical_url,first_seen,last_seen) VALUES(?,?,?,?)",(A,B,'t','t'))
        self.conn.execute("INSERT INTO recipe_aliases(alias_url,canonical_url,first_seen,last_seen) VALUES(?,?,?,?)",(B,A,'t','t'))
        self.assertIsNone(find_saved(self.conn,A))

if __name__=='__main__': unittest.main()
