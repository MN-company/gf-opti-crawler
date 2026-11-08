"""Canonical identity, source-preserving quality checks, and idempotent storage.

This module never guesses nutrition: it stores what the original parser extracted.
"""
from __future__ import annotations
from datetime import datetime, timezone
import hashlib
import json

# Fields from earlier releases that are no longer persisted or exported.
TRANSIENT_FIELDS = frozenset({
    'instructions', 'instruction_steps', 'instruction_steps_with_photos',
    'step_images_count', 'techniques_detected', 'images', 'method',
    'image', 'image_url', 'thumbnail', 'thumbnail_url', 'thumbnailUrl',
    'photo_url', 'photos', 'photographs', 'step_photos',
})


def stripped_recipe(recipe):
    """Remove old preparation/media fields, including media in nested records.

    Keeps source ingredients, nutrients, textual metadata and utensil names.
    Recalculates the stored crawl digest only when stripping changed anything.
    """
    def clean(value):
        if isinstance(value, dict):
            return {key: clean(item) for key, item in value.items()
                    if key not in TRANSIENT_FIELDS}
        if isinstance(value, list):
            return [clean(item) for item in value]
        return value

    result = clean(recipe)
    if result != recipe and isinstance(result.get('crawl'), dict):
        result['crawl'] = dict(result['crawl'])
        digest = hashlib.sha256(json.dumps({k: v for k, v in result.items() if k != 'crawl'},
                                          ensure_ascii=False, sort_keys=True,
                                          separators=(',', ':')).encode('utf-8')).hexdigest()
        result['crawl']['content_sha256'] = digest
    return result


def legacy_field_counts(recipe):
    """Count stale procedure/media keys anywhere within an archived payload."""
    counts = {'procedures': 0, 'media': 0}
    proc = {'instructions', 'instruction_steps', 'instruction_steps_with_photos',
            'techniques_detected', 'method'}

    def walk(value):
        if isinstance(value, dict):
            for k, v in value.items():
                if k in TRANSIENT_FIELDS:
                    counts['procedures' if k in proc else 'media'] += 1
                walk(v)
        elif isinstance(value, list):
            for v in value:
                walk(v)
    walk(recipe)
    return counts


def prune_procedures(conn, *, batch_size=250):
    """Strip legacy payloads in bounded batches. Caller must back up first."""
    updated = 0
    last_url = ''
    while True:
        rows = conn.execute('SELECT url,json FROM recipes WHERE url>? ORDER BY url LIMIT ?',
                            (last_url, batch_size)).fetchall()
        if not rows:
            break
        for row in rows:
            last_url = row['url']
            original = json.loads(row['json'])
            result = stripped_recipe(original)
            if result == original:
                continue
            # Re-evaluate quality after removing the now-optional instructions.
            result['quality'] = assess(result)
            digest = hashlib.sha256(json.dumps({k: v for k, v in result.items() if k != 'crawl'},
                                             ensure_ascii=False, sort_keys=True,
                                             separators=(',', ':')).encode('utf-8')).hexdigest()
            if isinstance(result.get('crawl'), dict):
                result['crawl']['content_sha256'] = digest
            conn.execute('UPDATE recipes SET json=?,content_hash=?,quality_status=? WHERE url=?',
                         (json.dumps(result, ensure_ascii=False, separators=(',', ':')),
                          digest, result['quality']['status'], row['url']))
            updated += 1
        conn.commit()
    return updated


def assess(recipe):
    """Separate fatal extraction errors from source's genuinely optional data."""
    errors=[]
    title = recipe.get('title')
    ingredients = recipe.get('ingredients') or []
    if not isinstance(title,str) or len(title.strip()) < 3:
        errors.append('invalid_title')
    if not isinstance(ingredients,list) or len(ingredients) < 2:
        errors.append('too_few_ingredients')
    elif any(not isinstance(i,dict) or not (i.get('name') or i.get('raw')) for i in ingredients):
        errors.append('empty_ingredient')
    warnings = list(recipe.get('extraction_warning') or [])
    for flag in ('ingredient_count_mismatch_dom_jsonld',):
        if flag in warnings:
            errors.append(flag)
    return {'status':'quality_passed' if not errors else 'needs_review',
            'errors':list(dict.fromkeys(errors)), 'warnings':warnings,
            'nutrition_origin':'source_page_only'}


def resolved_canonical(requested, headers, recipe):
    from crawler import normalize_url
    candidate = normalize_url(recipe.get('source_url') or '')
    redirected = normalize_url(headers.get('X-Crawler-Final-URL') or '')
    return candidate or redirected or requested


def find_saved(conn, url):
    """Follow historical alias chains safely, including chains predating v0.8.2."""
    seen = set()
    current = url
    while current not in seen:
        seen.add(current)
        row = conn.execute('SELECT canonical_url FROM recipe_aliases WHERE alias_url=?',
                           (current,)).fetchone()
        if row is None:
            break
        current = row['canonical_url']
    else:
        return None  # Alias cycle: never claim a resolved recipe.
    return current if conn.execute('SELECT 1 FROM recipes WHERE url=?',
                                   (current,)).fetchone() else None


def persist(conn, requested, headers, recipe, stamp):
    """Save one canonical document; never overwrite a passing record with bad data.

    Records any differing legacy alias content in a conflict table for later inspection.
    """
    from crawler import upsert_link
    recipe = stripped_recipe(recipe)
    quality=assess(recipe)
    if quality['status']!='quality_passed':
        conn.execute('UPDATE urls SET status=?,attempts=attempts+1,error=?,fetched_at=? WHERE url=?',
                     ('review','Extraction quality: '+','.join(quality['errors']),stamp,requested))
        return {'status':'review','quality':quality}
    recipe['quality'] = quality
    canonical=resolved_canonical(requested,headers,recipe)
    recipe['source_url']=canonical
    raw=json.dumps(recipe,ensure_ascii=False,sort_keys=True,separators=(',',':'))
    digest=hashlib.sha256(raw.encode('utf-8')).hexdigest()
    prior=conn.execute('SELECT content_hash FROM recipes WHERE url=?',(canonical,)).fetchone()
    changed = prior is None or prior['content_hash'] != digest
    # Canonical can be absent from urls when only its alias was discovered.
    if canonical!=requested:
        upsert_link(conn,canonical,source='canonical')
    if changed:
        recipe['crawl']={'retrieved_at':stamp,'url':canonical,'content_sha256':digest,
                         'content_type':headers.get('Content-Type') or headers.get('content-type')}
        conn.execute('''INSERT INTO recipes(url,title,content_hash,extracted_at,json,quality_status)
                     VALUES(?,?,?,?,?,?) ON CONFLICT(url) DO UPDATE SET
                     title=excluded.title,content_hash=excluded.content_hash,
                     extracted_at=excluded.extracted_at,json=excluded.json,
                     quality_status=excluded.quality_status''',
                     (canonical,recipe['title'],digest,stamp,
                      json.dumps(recipe,ensure_ascii=False,separators=(',',':')),quality['status']))
    else:
        conn.execute('UPDATE recipes SET quality_status=? WHERE url=?',(quality['status'],canonical))
    # Keep historical differing payloads, instead of silently deleting data.
    if canonical!=requested:
        legacy=conn.execute('SELECT content_hash,json FROM recipes WHERE url=?',(requested,)).fetchone()
        if legacy is not None:
            if legacy['content_hash'] != digest:
                conn.execute('''INSERT OR IGNORE INTO canonical_conflicts(alias_url,canonical_url,legacy_json,created_at)
                             VALUES(?,?,?,?)''',(requested,canonical,legacy['json'],stamp))
            conn.execute('DELETE FROM recipes WHERE url=?',(requested,))
        # Flatten all aliases that transitively pointed at the superseded URL.
        # UNION terminates even if a legacy alias cycle exists.
        ancestors = [r[0] for r in conn.execute('''
            WITH RECURSIVE predecessors(alias_url) AS (
                SELECT alias_url FROM recipe_aliases WHERE canonical_url=?
                UNION
                SELECT a.alias_url FROM recipe_aliases a
                JOIN predecessors p ON a.canonical_url=p.alias_url
            ) SELECT alias_url FROM predecessors''', (requested,)).fetchall()]
        for alias in ancestors:
            if alias != canonical:
                conn.execute('UPDATE recipe_aliases SET canonical_url=?,last_seen=? WHERE alias_url=?',
                             (canonical,stamp,alias))
                conn.execute("UPDATE urls SET redirected_to=? WHERE url=? AND status='redirect'",
                             (canonical,alias))
        conn.execute('''INSERT INTO recipe_aliases(alias_url,canonical_url,first_seen,last_seen)
                     VALUES(?,?,?,?) ON CONFLICT(alias_url) DO UPDATE SET
                     canonical_url=excluded.canonical_url,last_seen=excluded.last_seen''',
                     (requested,canonical,stamp,stamp))
        conn.execute('''UPDATE urls SET status='redirect',attempts=attempts+1,error=NULL,
                     redirected_to=?,fetched_at=?,retry_at=NULL WHERE url=?''',
                     (canonical,stamp,requested))
        # Validators belong to the requested HTTP resource, not the canonical
        # destination, which may have a different ETag or Last-Modified value.
        conn.execute('''UPDATE urls SET etag=COALESCE(?,etag),
                     http_last_modified=COALESCE(?,http_last_modified) WHERE url=?''',
                     (headers.get('ETag') or headers.get('etag'),
                      headers.get('Last-Modified') or headers.get('last-modified'),requested))
    # Every recipe and alias counts as resolved only if its canonical is stored.
    conn.execute('''UPDATE urls SET status='done',error=NULL,fetched_at=?,retry_at=NULL,
                 not_found_count=0,redirected_to=NULL,
                 etag=COALESCE(?,etag),http_last_modified=COALESCE(?,http_last_modified)
                 WHERE url=?''',
                 (stamp,(headers.get('ETag') or headers.get('etag')) if canonical==requested else None,
                  (headers.get('Last-Modified') or headers.get('last-modified')) if canonical==requested else None,canonical))
    if canonical==requested:
        conn.execute('UPDATE urls SET attempts=attempts+1 WHERE url=?',(canonical,))
    return {'status':'saved' if changed else 'unchanged','canonical':canonical,'quality':quality}


def mark_missing(conn,row,code,stamp):
    """Unconfirmed missing URLs never count as completed for audit."""
    count = row['not_found_count'] + 1
    hours = 24 if count==1 and code==404 else (168 if code==404 else 720)
    from datetime import timedelta
    due=(datetime.now(timezone.utc)+timedelta(hours=hours)).isoformat(timespec='seconds')
    conn.execute('''UPDATE urls SET status='retry',attempts=attempts+1,not_found_count=?,
                 error=?,fetched_at=?,retry_at=? WHERE url=?''',
                 (count,f'HTTP {code}: missing unconfirmed (observation {count})',stamp,due,row['url']))
    return 'temporarily_missing'
