import os
import subprocess
import sys
import unittest
from unittest.mock import patch, Mock
from crawler import PoliteHttp
from http_identity import resolve_user_agent, DEFAULT_USER_AGENT


class IdentityTests(unittest.TestCase):
    def test_default_without_email(self):
        with patch.dict(os.environ, {'RECIPE_SCRAPER_USER_AGENT': ''}):
            with self.assertRaises(ValueError):
                resolve_user_agent()
        with patch.dict(os.environ, {}, clear=True):
            self.assertEqual(resolve_user_agent(), DEFAULT_USER_AGENT)

    def test_custom_env_and_override(self):
        with patch.dict(os.environ, {'RECIPE_SCRAPER_USER_AGENT': 'MyRecipeBot/2.0'}):
            self.assertEqual(resolve_user_agent(), 'MyRecipeBot/2.0')
            self.assertEqual(resolve_user_agent('CustomBot/3.1'), 'CustomBot/3.1')
            fake_session = Mock(headers={})
            client = PoliteHttp(None, session=fake_session)
            self.assertEqual(client.agent, 'MyRecipeBot/2.0')
            self.assertEqual(fake_session.headers['User-Agent'], 'MyRecipeBot/2.0')

    def test_injection_rejected(self):
        for agent in ('', '  ', 'x\r\nInjected: yes', 'x' * 257):
            with self.subTest(agent=agent):
                with self.assertRaises(ValueError):
                    resolve_user_agent(agent)

    def test_cli_no_contact_requirement(self):
        output = subprocess.run([sys.executable,'crawler.py','crawl','--help'],capture_output=True,text=True)
        self.assertEqual(output.returncode, 0, output.stderr)
        self.assertIn('--user-agent',output.stdout)
        self.assertNotIn('--contact',output.stdout)

if __name__=='__main__':
    unittest.main()
