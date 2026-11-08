"""Source-grounded enrichment for recipe pages.

No claims of certified allergen safety, no invented nutrients or prices.
Utensils and allergen signals are heuristic, not independently verified labels.
"""
from __future__ import annotations

import csv
import json
import re
import unicodedata
from collections import OrderedDict
from pathlib import Path
from urllib.parse import urljoin, urlparse, urlunparse

from bs4 import BeautifulSoup


def tidy(text):
    return re.sub(r'\s+', ' ', str(text or '')).strip()


def as_number(text):
    m = re.search(r'(?<!\w)(\d+(?:[.,]\d+)?)(?!\w)', str(text or ''))
    return float(m.group(1).replace(',', '.')) if m else None


def normalize_key(text):
    text = unicodedata.normalize('NFKD', str(text or '').casefold())
    return re.sub(r'[^a-z0-9]+', ' ', ''.join(c for c in text if not unicodedata.combining(c))).strip()


def source_field(value, field, kind='site_html'):
    return {'value': value, 'source_kind': kind, 'source_field': field}


def nutrition_details(soup, ld):
    """Extract nutrition values, preserve the reported unit and serving basis.
    GZ DOM is used to supplement JSON-LD (which does not include protein in fixture).
    """
    fields = {
        'calories': ('kcal', 'calories'), 'carbohydrateContent': ('g', 'carbs'),
        'sugarContent': ('g', 'sugars'), 'proteinContent': ('g', 'protein'),
        'fatContent': ('g', 'fat'), 'saturatedFatContent': ('g', 'saturated_fat'),
        'fiberContent': ('g', 'fiber'), 'cholesterolContent': ('mg', 'cholesterol'),
        'sodiumContent': ('mg', 'sodium'),
    }
    label_map = {
        'energia': 'calories', 'carboidrati': 'carbs', 'di cui zuccheri': 'sugars',
        'proteine': 'protein', 'grassi': 'fat', 'di cui saturi': 'saturated_fat',
        'fibre': 'fiber', 'colesterolo': 'cholesterol', 'sodio': 'sodium',
    }
    values = {}
    original = ld.get('nutrition') if isinstance(ld.get('nutrition'), dict) else {}
    for field, (unit, key) in fields.items():
        if original.get(field):
            number = as_number(original[field])
            if number is not None:
                values[key] = {'value': number, 'unit': unit, 'provenance': 'schema.org/Recipe.nutrition.' + field}
    for row in soup.select('.gz-list-macros li'):
        name = row.select_one('.gz-list-macros-name')
        val = row.select_one('.gz-list-macros-value')
        unit = row.select_one('.gz-list-macros-unit')
        if not name or not val:
            continue
        key = label_map.get(tidy(name.get_text(' ', strip=True)).lower())
        numeric = as_number(val.get_text(' ', strip=True))
        if key and numeric is not None:
            values[key] = {'value': numeric,
                           'unit': tidy(unit.get_text(' ', strip=True)) if unit else ('kcal' if key == 'calories' else 'g'),
                           'provenance': 'DOM:.gz-list-macros'}
    per_portion = any('calorie per porzione' in tidy(t.get_text(' ', strip=True)).casefold()
                      for t in soup.select('.gz-text-calories-title'))
    source = None
    disclaimer = soup.select_one('.gz-disclaimer-fornitore')
    if disclaimer:
        text = tidy(disclaimer.get_text(' ', strip=True))
        if 'edamam' in text.casefold():
            source = 'Edamam (come dichiarato nella pagina)'
    return {
        'available': bool(values), 'basis': 'per_serving' if per_portion else 'unknown',
        'values': values, 'provider': source,
        'estimated_by_source': bool(source),
        'disclaimer': 'Valori riportati dalla fonte e indicativi, non calcolati dallo scraper.' if values else None,
    }


def step_media(soup, base, include_images=True):
    steps = []
    for idx, step in enumerate(soup.select('.gz-content-recipe-step'), 1):
        p = step.find('p')
        photos = []
        seen = set()
        for picture in (step.select('picture.gz-content-recipe-step-img') if include_images else []):
            img = picture.find('img')
            if not img:
                continue
            url = img.get('data-src') or img.get('src')
            if not url:
                continue
            absolute = urljoin(base or '', url)
            if absolute in seen:
                continue
            seen.add(absolute)
            photos.append({'url': absolute, 'number': int(picture.get('data-step')) if str(picture.get('data-step', '')).isdigit() else None,
                           'alt': img.get('alt'), 'source': 'DOM:.gz-content-recipe-step-img'})
        if p:
            for badge in p.select('.num-step'):
                badge.decompose()  # remove photo references such as 1, 2, 3, not actual instructions
        entry = {'index': idx, 'text': tidy(p.get_text(' ', strip=True)) if p else None}
        if include_images: entry['images'] = photos
        steps.append(entry)
    return steps


def related_recipes(soup, base, include_images=True):
    recipes = []
    visited = set()
    for card in soup.select('.gz-card-suggested'):
        title_el = card.select_one('.gz-title a[href]')
        if title_el is None:
            continue
        link = urljoin(base or '', title_el.get('href', ''))
        u = urlparse(link)
        link = urlunparse((u.scheme, u.netloc, u.path, '', '', ''))
        title = tidy(title_el.get_text(' ', strip=True)) or title_el.get('title')
        if not title or link in visited:
            continue
        visited.add(link)
        img = card.select_one('.gz-card-image img') if include_images else None
        category = card.select_one('.gz-category')
        record = {'title': title, 'url': link, 'category': tidy(category.get_text(' ', strip=True)) if category else None,
                  'source': 'DOM:.gz-card-suggested'}
        if include_images:
            record['image_url'] = urljoin(base or '', img.get('data-src') or img.get('src')) if img and (img.get('data-src') or img.get('src')) else None
        recipes.append(record)
    return recipes


TOOLS = OrderedDict({
    'mixer': r'\bmixer\b|\bfrullator[ei]\b',
    'pentola': r'\bpentol[ae]\b',
    'padella': r'\bpadell[ae]\b',
    'forno': r'\b(?:in|nel|dal) forno\b|\bforno (?:statico|ventilato|preriscaldato)\b',
    'pirofila': r'\bpirofil[ae]\b',
    'teglia': r'\btegli[ae]\b',
    'grattugia': r'\bgrattugi[ae]\b',
    'mestolo': r'\bmestol[oi]\b',
    'coltello': r'\bcoltell[oi]\b',
    'ciotola': r'\bciotol[ae]\b',
    'spatola': r'\bspatol[ae]\b',
    'cucchiaio': r'\bcucchiai[oi]\b',
    'frusta': r'\bfrust[ae]\b',
    'contenitore': r'\bcontenitor[ei]\b',
})
TECHNIQUES = OrderedDict({
    'tritare': r'\btrit\w*',
    'rosolare': r'\brosol\w*',
    'sfumare': r'\bsfum\w*',
    'mescolare': r'\bmescol\w*',
    'grattugiare': r'\bgrattug\w*',
    'frullare': r'\bfrull\w*',
    'cuocere': r'\bcuoc\w*|\bcuocet\w*|\bcottur\w*',
    'infornare': r'\binforn\w*',
    'tagliare': r'\btagli\w*',
    'versare': r'\bvers\w*',
    'scolare': r'\bscol\w*',
    'bollire': r'\bboll\w*',
    'montare': r'\bmont\w*',
    'amalgamare': r'\bamalgam\w*',
    'raffreddare': r'\braffredd\w*',
    'gratinare': r'\bgratin\w*',
})


def match_actions(steps, patterns):
    detected = []
    for label, pattern in patterns.items():
        in_steps = []
        for s in steps:
            text = s.get('text') or ''
            if re.search(pattern, text, re.I):
                in_steps.append(s['index'])
        if in_steps:
            detected.append({'name': label, 'step_indices': in_steps, 'method': 'keyword_heuristic',
                             'verified': False})
    return detected


# An ingredient-list screening is NOT an allergen declaration or safety guarantee.
# Possible allergens include hidden ingredients in processed foods and cross-contact.
ALLERGEN_PATTERNS = OrderedDict({
    'glutine_cereali': r"\blasagn\w*|\bpast[ae]\b|\bfarina di (?:grano|frumento|segale|orzo)\b|\bgrano\b|\bfrumento\b|\borzo\b|\bsegale\b|\bsemol\w*|\bcous\s*cous\b",
    'uova': r"\buov[oa]\b|\btuorl\w*|\balbum\w*|all['’]uovo",
    'latte': r"\bmozzarell\w*|\bgrana padano\b|\bparmigian\w*|\bformagg\w*|\bricott\w*|\byogurt\b|\bburro\b|\bpanna\b|\bbesciamell\w*|\blatte\b",
    'arachidi': r'\barachid\w*',
    'frutta_a_guscio': r'\bmandorl\w*|\bnocciol\w*|\bnoc[ei]\b|\bpistacch\w*|\banacard\w*|\bpinol\w*',
    'soia': r'\bsoia\b|\btofu\b|\btempeh\b',
    'sesamo': r'\bsesamo\b|\btahin[ai]\b',
    'pesce': r'\btonno\b|\bsalmon\w*|\bmerluzz\w*|\bpesce\b|\bacciugh\w*|\balic[ei]\b',
    'crostacei': r'\bgamber\w*|\bscamp\w*|\bastac\w*|\baragost\w*|\bgranch\w*',
    'molluschi': r'\bcozz[ae]\b|\bvongol\w*|\bcalamar\w*|\bpolp[oi]\b|\bsepp\w*|\bostric\w*',
    'senape': r'\bsenap\w*',
    'sedano': r'\bsedan\w*',
    'lupini': r'\blupin\w*',
    'anidride_solforosa_e_solfiti': r'\bsolfit\w*|\banidride solforosa\b|\bvino\b',
})


def allergen_screening(ingredients):
    matches = []
    for allergen, pattern in ALLERGEN_PATTERNS.items():
        evidence = [x['name'] for x in ingredients if re.search(pattern, x.get('name', ''), re.I)]
        if evidence:
            matches.append({'allergen': allergen, 'ingredient_signals': evidence,
                            'signal_type': 'possible_presence',
                            'notes': 'Verificare etichetta e preparazione; la presenza non è confermata analiticamente.'})
    return {'method': 'non_certifying_ingredient_name_screening', 'signals': matches,
            'absence_can_be_confirmed': False,
            'warning': 'L’assenza di segnali NON indica assenza di allergeni, tracce o contaminazioni crociate.'}


def suggestions_from_source(tips):
    """Only explicit, source-mentioned replacements/additions. Never invent suggestions."""
    out = []
    text = tidy(tips)
    for m in re.finditer(r'\bsostituir\w*\s+(?:la|il|lo|le|i|gli|l[’\'])(?:\s+)?(?P<from>[\w\s]+?)\s+con\s+(?P<to>[^.!?;:]+)', text, re.I):
        item = {'kind': 'substitution', 'from': tidy(m.group('from')),
                'to': tidy(m.group('to')), 'source_text': tidy(m.group(0)),
                'provenance': 'DOM:Consiglio', 'requires_review': True}
        out.append(item)
    for m in re.finditer(r'\b(?:potete|puoi|si può)\s+aggiungere\s+(?P<what>[^.!?;:]+)', text, re.I):
        out.append({'kind': 'addition', 'ingredient_or_note': tidy(m.group('what')),
                    'source_text': tidy(m.group(0)), 'provenance': 'DOM:Consiglio',
                    'requires_review': True,
                    'dietary_warning': 'Rivalutare le diete dichiarate dopo ogni modifica alla ricetta.'})
    return out


def storage_details(text):
    if not text:
        return {'text': None, 'max_days': None}
    m = re.search(r'(?:per|entro|massimo|fino a)\s+(\d+)\s+giorn[oi]', text, re.I)
    return {'text': text, 'max_days': int(m.group(1)) if m else None,
            'max_days_is_extracted': bool(m), 'source': 'DOM:Conservazione'}


def enrich(recipe, html, ld, *, soup=None, include_images=False, instruction_texts=None):
    soup = soup if soup is not None else BeautifulSoup(html, 'lxml')
    base = recipe.get('source_url') or recipe.get('source_domain') or ''
    if base and not base.startswith('http'):
        base = 'https://' + base
    # Analyze the instructions transiently. Do not store their wording or step images.
    steps = step_media(soup, base, include_images=False)
    if not steps and instruction_texts:
        steps = [{'index': i, 'text': text} for i, text in enumerate(instruction_texts, 1)]
    recipe['related_recipes'] = related_recipes(soup, base, include_images=False)
    recipe['nutrition_details'] = nutrition_details(soup, ld)
    recipe['allergen_screening'] = allergen_screening(recipe['ingredients'])
    recipe['diet_claims'] = {'source_declared': recipe.get('diet_tags') or [],
                            'status': 'declared_by_source_not_independently_verified'}
    recipe['utensils_detected'] = match_actions(steps, TOOLS)
    recipe['ingredient_modifications'] = suggestions_from_source(recipe.get('tips'))
    # A suggested optional ingredient does not change the original recipe's tags,
    # but can invalidate them if the user applies the suggestion.
    for modification in recipe['ingredient_modifications']:
        option_text = modification.get('ingredient_or_note', '') if modification['kind'] == 'addition' else modification.get('to', '')
        signals = allergen_screening([{'name': option_text}])['signals']
        modification['potential_added_allergen_signals'] = [sig['allergen'] for sig in signals]
        modification['declared_diet_tags_to_recheck'] = []
        if 'latte' in modification['potential_added_allergen_signals'] and any('VeganDiet' in x for x in recipe.get('diet_tags', [])):
            modification['declared_diet_tags_to_recheck'].append('VeganDiet')
        if 'latte' in modification['potential_added_allergen_signals'] and any('LowLactoseDiet' in x for x in recipe.get('diet_tags', [])):
            modification['declared_diet_tags_to_recheck'].append('LowLactoseDiet')
    recipe['storage_details'] = storage_details(recipe.get('storage'))
    # Explicitly discard fields from older payloads passed to enrich().
    for field in ('instructions', 'instruction_steps', 'instruction_steps_with_photos',
                  'step_images_count', 'techniques_detected', 'images', 'method'):
        recipe.pop(field, None)
    # Number without unit in GialloZafferano DOM (e.g. "Cipolle dorate 1").
    for ingredient in recipe['ingredients']:
        if ingredient.get('quantity') is None and ingredient.get('quantity_text') is None:
            raw = ingredient.get('raw') or ''
            name = ingredient.get('name') or ''
            tail = raw[len(name):].strip() if raw.startswith(name) else ''
            if re.fullmatch(r'\d+(?:[,.]\d+)?', tail):
                ingredient['quantity'] = as_number(tail)
                ingredient['quantity_text'] = tail
                ingredient['unit'] = 'unità'
                ingredient['quantity_source'] = 'DOM:ingredient unitless numeric'
                if ingredient.get('note') == tail: ingredient['note'] = None
    return recipe


UNIT_FACTORS = {'kg': ('g', 1000), 'gr': ('g', 1), 'g': ('g', 1), 'mg': ('g', .001),
                'l': ('ml', 1000), 'litro': ('ml', 1000), 'litri': ('ml', 1000),
                'dl': ('ml', 100), 'cl': ('ml', 10), 'ml': ('ml', 1),
                'unita': ('unit', 1), 'unità': ('unit', 1), 'unità': ('unit', 1),
                'uovo': ('unit', 1), 'uova': ('unit', 1), 'spicchio': ('unit', 1), 'spicchi': ('unit', 1)}


def scale_ingredients(recipe, *, factor=None, servings=None, target_grams=None):
    if sum(v is not None for v in (factor, servings, target_grams)) != 1:
        raise ValueError('Scegli esattamente uno fra factor, servings o target_grams')
    if servings is not None:
        display = recipe.get('yield_display') or ''
        if not re.search(r'\b(person[ae]|porzion[ei])\b', display, re.I):
            raise ValueError('La resa originale non è in persone/porzioni: usa factor')
        original = as_number(display)
        if original is None or original <= 0: raise ValueError('Resa non interpretabile')
        factor = servings / original
    elif target_grams is not None:
        display = recipe.get('yield_display') or ''
        if not re.search(r'\b(g|gramm[oi]|kg|chilogramm[oi])\b', display, re.I):
            raise ValueError('La resa originale non è espressa in peso: usa factor')
        original = as_number(display)
        if original is None or original <= 0: raise ValueError('Resa non interpretabile')
        if re.search(r'\bkg\b|chilogramm', display, re.I): original *= 1000
        factor = target_grams / original
    if factor is None or factor <= 0:
        raise ValueError('Il fattore di scala deve essere positivo')
    rows = []
    for ing in recipe['ingredients']:
        q = ing.get('quantity')
        row = {'name': ing['name'], 'group': ing.get('group'), 'unit': ing.get('unit'),
               'original_quantity': q, 'quantity_scaled': round(float(q)*factor, 3) if q is not None else None,
               'original_text': ing.get('raw'), 'note': ing.get('note'),
               'warning': 'quantità non numerica: da regolare manualmente' if q is None else None}
        rows.append(row)
    return {'factor': round(factor, 6), 'ingredients': rows,
            'notes': 'Stima proporzionale. Tempi, capacità degli utensili e quantità q.b. non scalano automaticamente.'}


def load_prices(filepath):
    """CSV: ingredient, price_eur, quantity, unit, optional note.
    Example: mozzarella,3.50,250,g  => €3.50 per 250g.
    Empty prices are ignored.
    """
    prices = {}
    with open(filepath, newline='', encoding='utf-8-sig') as fh:
        for row in csv.DictReader(fh):
            key = normalize_key(row.get('ingredient'))
            if not key or not str(row.get('price_eur') or '').strip():
                continue
            price = as_number(row['price_eur'])
            amount = as_number(row.get('quantity'))
            unit = normalize_key(row.get('unit'))
            if price is None or price < 0 or amount is None or amount <= 0 or unit not in UNIT_FACTORS:
                raise ValueError(f"Prezzo non valido: {row}")
            base, factor = UNIT_FACTORS[unit]
            prices[key] = {'price_eur': price, 'quantity': amount * factor,
                           'base_unit': base, 'source_note': row.get('note') or 'CSV fornito dall’utente'}
    return prices


def estimate_cost(recipe, prices):
    if not prices:
        return {'status': 'requires_prices', 'currency': 'EUR', 'known_cost_eur': None,
                'total_cost_eur': None, 'missing': [], 'details': [], 'warning': 'Nessun prezzo fornito: nessuna cifra inventata.'}
    subtotal = 0.0
    details, missing = [], []
    for ing in recipe['ingredients']:
        key = normalize_key(ing['name'])
        row = prices.get(key)
        qty = ing.get('quantity')
        unit = normalize_key(ing.get('unit'))
        if qty is None or row is None or unit not in UNIT_FACTORS:
            missing.append({'ingredient': ing['name'], 'reason': 'prezzo_o_quantita_non_disponibile'})
            continue
        base, factor = UNIT_FACTORS[unit]
        if row['base_unit'] != base:
            missing.append({'ingredient': ing['name'], 'reason': 'unita_incompatibili'})
            continue
        cost = round(qty * factor / row['quantity'] * row['price_eur'], 4)
        subtotal += cost
        details.append({'ingredient': ing['name'], 'cost_eur': round(cost, 2),
                        'source_note': row['source_note']})
    complete = not missing
    return {'status': 'complete' if complete else 'partial', 'currency': 'EUR',
            'known_cost_eur': round(subtotal, 2),
            'total_cost_eur': round(subtotal, 2) if complete else None,
            'missing': missing, 'details': details,
            'warning': None if complete else 'Totale non disponibile: alcune quantità o prezzi mancano. Il subtotale è incompleto.'}


def generate_price_template(recipes, destination):
    seen = set()
    with open(destination, 'w', newline='', encoding='utf-8') as fh:
        wr = csv.writer(fh)
        wr.writerow(['ingredient', 'price_eur', 'quantity', 'unit', 'note'])
        for recipe in recipes:
            for ing in recipe['ingredients']:
                key = normalize_key(ing.get('name'))
                if key not in seen:
                    seen.add(key)
                    wr.writerow([ing['name'], '', '', '', ''])
