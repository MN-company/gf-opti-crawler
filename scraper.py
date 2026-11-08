"""Universal recipe HTML parser: JSON-LD first, DOM fallback, regex for quantities.
Run: python scraper.py file.html [file2.html...] --out recipes.json
Optional live URL fetching: python scraper.py https://example.com/recipe --out recipes.json
Respect website terms, robots.txt and rate limits when fetching online.
"""
from __future__ import annotations
import argparse, json, re, sys
from enrichment import enrich, load_prices, estimate_cost, scale_ingredients, generate_price_template
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse
from bs4 import BeautifulSoup

AMOUNT = re.compile(r'(?<!\w)(\d+(?:[.,]\d+)?(?:\s*[-–]\s*\d+(?:[.,]\d+)?)?)\s*(kg|g|gr|mg|ml|cl|dl|l|litri?|cucchiai?|cucchiaini?|spicch(?:io|i)|pezz(?:o|i)|fette?|pizzichi?|rametti?|foglie?|uova?|tazze?)\b',re.I)
QB = re.compile(r'\bq\s*\.?\s*b\s*\.?\b',re.I)

def clean(value):
    return re.sub(r'\s+', ' ', str(value or '')).strip()

def types(obj):
    ts = obj.get('@type', [])
    return [ts] if isinstance(ts,str) else ts if isinstance(ts,list) else []

def walk_json(obj):
    if isinstance(obj,dict):
        yield obj
        for v in obj.values(): yield from walk_json(v)
    elif isinstance(obj,list):
        for v in obj: yield from walk_json(v)

def structured_recipe(soup):
    for tag in soup.select('script[type="application/ld+json"]'):
        try: obj = json.loads(tag.get_text())
        except (json.JSONDecodeError,TypeError): continue
        for node in walk_json(obj):
            if 'Recipe' in types(node): return node
    return {}

def section(soup, label):
    for h in soup.select('h2,h3,h4'):
        if clean(h.get_text(' ',strip=True)).casefold()==label.casefold():
            sibling=h.find_next_sibling()
            if sibling:
                return clean(sibling.get_text(' ',strip=True))
    return None

def ingredient_detail(raw, name=None, group=None, extra=None):
    match = AMOUNT.search(raw)
    qb = bool(QB.search(raw))
    quantity = float(match.group(1).replace(',','.')) if match and '-' not in match.group(1) and '–' not in match.group(1) else None
    unit = match.group(2) if match else None
    if not name:
        if match: name = clean(raw[:match.start()]+' '+raw[match.end():])
        else: name = QB.sub('',raw).strip()
    if extra and match:
        extra = extra.replace(match.group(0), '').strip()
    if extra and qb:
        extra = QB.sub('',extra).strip(' .,;:')
    return {'name':clean(name),'quantity':quantity,'unit':unit,'quantity_text':clean(match.group(0)) if match else ('q.b.' if qb else None),'raw':clean(raw),'group':group,'note':clean(extra).strip(' .,;:') or None}

def dom_ingredients(soup):
    found=[]
    for dl in soup.select('dl.gz-list-ingredients'):
        heading=dl.select_one('dt.gz-title-ingredients')
        group=clean(heading.get_text(' ',strip=True)) if heading else None
        for dd in dl.select('dd.gz-ingredient'):
            a=dd.find('a'); sp=dd.find('span')
            name=clean(a.get_text(' ',strip=True)) if a else None
            extra=clean(sp.get_text(' ',strip=True)) if sp else None
            raw=clean(dd.get_text(' ',strip=True))
            found.append(ingredient_detail(raw,name,group,extra))
    return found

def flatten_instructions(instructions):
    out=[]
    for v in instructions if isinstance(instructions,list) else [instructions]:
        if isinstance(v,str): out.append(clean(v))
        elif isinstance(v,dict):
            if 'text' in v:out.append(clean(v['text']))
            elif 'itemListElement' in v:out.extend(flatten_instructions(v['itemListElement']))
    return [s for s in out if s]

def faq_data(soup):
    out=[]
    for tag in soup.select('script[type="application/ld+json"]'):
        try:obj=json.loads(tag.get_text())
        except (json.JSONDecodeError,TypeError): continue
        for node in walk_json(obj):
            if 'FAQPage' in types(node):
                for q in node.get('mainEntity',[]):
                    if isinstance(q,dict):out.append({'question':q.get('name'),'answer':q.get('acceptedAnswer',{}).get('text') if isinstance(q.get('acceptedAnswer'),dict) else None})
    return out

def parse(html, source=None, *, include_images=False):
    soup=BeautifulSoup(html,'lxml'); ld=structured_recipe(soup)
    def meta(sel,attr='content'):
        tag=soup.select_one(sel)
        return tag.get(attr) if tag else None
    canonical=meta('link[rel="canonical"]','href') or meta('meta[property="og:url"]') or source
    ingredients=dom_ingredients(soup)
    if not ingredients: ingredients=[ingredient_detail(str(i)) for i in ld.get('recipeIngredient',[])]
    steps=flatten_instructions(ld.get('recipeInstructions',[]))
    if not steps:
        for h in soup.select('h2,h3'):
            if clean(h.get_text(' ',strip=True)).casefold().startswith('come preparare'):
                nxt=h.find_next_sibling()
                if nxt: steps=[clean(p.get_text(' ',strip=True)) for p in nxt.select('p') if clean(p.get_text(' ',strip=True))]
                break
    diff=None
    yield_display=None
    for tag in soup.select('.gz-name-featured-data'):
        if 'dosi per:' in tag.get_text(' ',strip=True).casefold():
            yield_display=clean(tag.get_text(' ',strip=True)).split(':',1)[-1].strip()
        if 'difficoltà' in tag.get_text(' ',strip=True).casefold():
            b=tag.find('strong');diff=clean(b.get_text(' ',strip=True)) if b else clean(tag.get_text(' ',strip=True)).split(':')[-1].strip()
    info={
      'source_url':canonical,'source_domain':urlparse(canonical or '').netloc or None,
      'site_recipe_id':soup.body.get('data-recipe') if soup.body else None,
      'title':ld.get('name') or meta('meta[property="og:title"]') or (clean(soup.h1.get_text(' ',strip=True)) if soup.h1 else None),
      'description':ld.get('description') or meta('meta[name="description"]'),
      'author':ld.get('author'),'date_published':ld.get('datePublished'),'date_modified':ld.get('dateModified'),
      'category':ld.get('recipeCategory'),'keywords':ld.get('keywords'),
      'difficulty':diff,'estimated_cost':ld.get('estimatedCost'),
      'prep_time_iso':ld.get('prepTime'),'cook_time_iso':ld.get('cookTime'),'total_time_iso':ld.get('totalTime'),
      'yield_value':ld.get('recipeYield'),'yield_display':yield_display,'yield_note':next((clean(x.get_text(' ',strip=True)) for x in soup.select('dt.gz-title-ingredients') if 'per circa' in x.get_text(' ',strip=True).casefold()),None),
      'ingredients':ingredients,'nutrition':ld.get('nutrition'),
      'rating':ld.get('aggregateRating'),'diet_tags':ld.get('suitableForDiet',[]),
      'presentation':section(soup,'PRESENTAZIONE'),
      'storage':section(soup,'Conservazione'),'tips':section(soup,'Consiglio'),
      'faq':faq_data(soup),'has_jsonld_recipe':bool(ld),
      'extraction_warning':[]
    }
    if not info['title']:info['extraction_warning'].append('missing_title')
    if not ingredients:info['extraction_warning'].append('missing_ingredients')
    if not ld:info['extraction_warning'].append('no_recipe_jsonld')
    schema_ingredients = ld.get('recipeIngredient') or []
    if isinstance(schema_ingredients, list) and schema_ingredients and soup.select('dl.gz-list-ingredients dd.gz-ingredient'):
        if len(schema_ingredients) != len(ingredients):
            info['extraction_warning'].append('ingredient_count_mismatch_dom_jsonld')
    if isinstance(info['yield_value'],(float,int)) and not info['yield_note'] and not info['yield_display']:
        info['extraction_warning'].append('yield_unit_unknown')
    # Preparation text is used only in memory to recognize kitchen utensils.
    # Do not persist instructions, numbered steps or step photographs.
    return enrich(info, html, ld, soup=soup, include_images=False, instruction_texts=steps)

def load_source(location):
    if location.startswith(('https://','http://')):
        import requests
        from http_identity import resolve_user_agent
        response=requests.get(location,timeout=20,headers={'User-Agent':resolve_user_agent()})
        response.raise_for_status()
        return response.text,location
    path=Path(location)
    return path.read_text(encoding='utf-8'),str(path)

def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('sources', nargs='+')
    parser.add_argument('--out', default='recipes.json')
    parser.add_argument('--prices', help='CSV prezzi: ingredient,price_eur,quantity,unit,note')
    parser.add_argument('--price-template', help='Scrive un modello CSV senza prezzi inventati')
    scaling = parser.add_mutually_exclusive_group()
    scaling.add_argument('--scale', type=float, help='Moltiplicatore delle quantità (es. 0.5)')
    scaling.add_argument('--servings', type=float, help='Numero desiderato di porzioni, solo ricette con resa per persone')
    scaling.add_argument('--target-grams', type=float, help='Resa desiderata in grammi, solo ricette con resa in peso')
    args = parser.parse_args()
    items = []
    prices = load_prices(args.prices) if args.prices else {}
    failed = 0
    for source in args.sources:
        try:
            html, ref = load_source(source)
            result = parse(html, ref)
            result['cost_estimate'] = estimate_cost(result, prices)
            if args.scale is not None or args.servings is not None or args.target_grams is not None:
                try:
                    result['scaled'] = scale_ingredients(result, factor=args.scale,
                        servings=args.servings, target_grams=args.target_grams)
                except ValueError as error:
                    result['scaled'] = {'error': str(error)}
            items.append(result)
            print(f"OK: {result['title']} | ingredients={len(result['ingredients'])} | "
                  f"utensils={len(result['utensils_detected'])} | "
                  f"related={len(result['related_recipes'])} | nutrition={len(result['nutrition_details']['values'])} | "
                  f"warnings={result['extraction_warning']}")
        except Exception as e:
            failed += 1
            print(f'ERROR {source}: {e}', file=sys.stderr)
    Path(args.out).write_text(json.dumps(items, ensure_ascii=False, indent=2), encoding='utf-8')
    if args.price_template:
        generate_price_template(items, args.price_template)
    print(f'Esportate {len(items)} ricette in {args.out}; errori: {failed}')
    if failed: sys.exit(1)

if __name__=='__main__':main()
