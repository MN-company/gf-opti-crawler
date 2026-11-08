import unittest as _fixture_unittest
from pathlib import Path as _fixture_Path
if not (_fixture_Path(__file__).parent / "Lasagne-al-forno.html").exists() or not (_fixture_Path(__file__).parent / "Hummus-senza-tahina.html").exists():
    raise _fixture_unittest.SkipTest("Optional third-party HTML fixtures not distributed")
"""Offline regression tests; run: python -m unittest -v test_scraper.py"""
import csv
import tempfile
import unittest
from pathlib import Path

from scraper import parse
from enrichment import load_prices, estimate_cost, scale_ingredients, generate_price_template

HERE = Path(__file__).parent

class Tests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.lasagne = parse((HERE / 'Lasagne-al-forno.html').read_text(encoding='utf-8'))
        cls.hummus = parse((HERE / 'Hummus-senza-tahina.html').read_text(encoding='utf-8'))

    def test_both_have_core_recipe(self):
        for recipe in (self.lasagne, self.hummus):
            self.assertTrue(recipe['has_jsonld_recipe'])
            self.assertEqual(len(recipe['ingredients']), 11)
            self.assertNotIn('instructions', recipe)
            self.assertNotIn('instruction_steps', recipe)
            self.assertNotIn('instruction_steps_with_photos', recipe)
            self.assertTrue(recipe['utensils_detected'])
            self.assertFalse(recipe['extraction_warning'])

    def test_source_nutrients_with_provenance(self):
        nutrit = self.lasagne['nutrition_details']
        self.assertEqual(nutrit['basis'], 'per_serving')
        self.assertEqual(nutrit['values']['protein']['value'], 44.1)
        self.assertEqual(nutrit['values']['calories']['value'], 670.9)
        self.assertTrue(nutrit['provider'].startswith('Edamam'))
        self.assertFalse(self.hummus['nutrition_details']['available'])
        self.assertEqual(self.hummus['nutrition_details']['values'], {})

    def test_only_utensils_and_related(self):
        for rec in [self.lasagne, self.hummus]:
            self.assertEqual(len(rec['related_recipes']), 5)
            for field in ('instructions', 'instruction_steps', 'instruction_steps_with_photos',
                          'step_images_count', 'techniques_detected', 'images'):
                self.assertNotIn(field, rec)
            for tool in rec['utensils_detected']:
                self.assertTrue(tool['name'])
                self.assertEqual(tool['method'], 'keyword_heuristic')

    def test_recipe_tags_not_certified(self):
        self.assertEqual(len(self.hummus['diet_claims']['source_declared']), 4)
        self.assertFalse(self.hummus['allergen_screening']['absence_can_be_confirmed'])
        self.assertIn('glutine_cereali', [x['allergen'] for x in self.lasagne['allergen_screening']['signals']])
        self.assertIn('latte', [x['allergen'] for x in self.lasagne['allergen_screening']['signals']])
        self.assertIn('VeganDiet', self.hummus['ingredient_modifications'][0]['declared_diet_tags_to_recheck'])

    def test_modifications_only_mentioned_by_source(self):
        self.assertEqual(len(self.hummus['ingredient_modifications']), 1)
        self.assertEqual(self.hummus['ingredient_modifications'][0]['kind'], 'addition')
        self.assertEqual(self.lasagne['ingredient_modifications'][0]['from'], 'mozzarella')

    def test_scale_people_and_grams(self):
        scaled = scale_ingredients(self.lasagne, servings=3)
        mozzarella = next(x for x in scaled['ingredients'] if x['name'] == 'Mozzarella')
        self.assertEqual(mozzarella['quantity_scaled'], 150.0)
        scaled = scale_ingredients(self.hummus, target_grams=375)
        chickpeas = next(x for x in scaled['ingredients'] if x['name'] == 'Ceci precotti')
        self.assertEqual(chickpeas['quantity_scaled'], 250.0)
        with self.assertRaises(ValueError):
            scale_ingredients(self.hummus, servings=4)
        with self.assertRaises(ValueError):
            scale_ingredients(self.lasagne, target_grams=400)

    def test_unitless_ingredient(self):
        onions = next(x for x in self.lasagne['ingredients'] if x['name'] == 'Cipolle dorate')
        self.assertEqual(onions['quantity'], 1)
        self.assertEqual(onions['unit'], 'unità')
        self.assertIsNone(onions['note'])

    def test_price_input_incomplete_is_not_fabricated_total(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / 'test_prices.csv'
            with p.open('w', newline='', encoding='utf-8') as fh:
                wr = csv.writer(fh)
                wr.writerow(['ingredient','price_eur','quantity','unit','note'])
                wr.writerow(['Mozzarella', '2.50', '500', 'g', 'TEST ONLY'])
            priced = estimate_cost(self.lasagne, load_prices(p))
            self.assertEqual(priced['status'], 'partial')
            self.assertEqual(priced['known_cost_eur'], 1.5)
            self.assertIsNone(priced['total_cost_eur'])
            self.assertTrue(priced['missing'])
            t = Path(d) / 'template.csv'
            generate_price_template([self.lasagne, self.hummus], t)
            self.assertTrue(t.exists())
            with t.open(encoding='utf-8') as fh:
                self.assertEqual(len(list(csv.DictReader(fh))), 18)

    def test_storage(self):
        self.assertEqual(self.lasagne['storage_details']['max_days'], 2)
        self.assertEqual(self.hummus['storage_details']['max_days'], 2)

if __name__ == '__main__':
    unittest.main()
