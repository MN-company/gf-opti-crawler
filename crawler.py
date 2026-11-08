"""Polite, resumable sitemap crawler for GialloZafferano recipe pages.

Usage:
    python crawler.py run --limit 10
    python crawler.py run --all
    python crawler.py stats
    python crawler.py export --output data/recipes.jsonl

The crawler can optionally discover recipes via public listing pages on www, obeys robots.txt, respects
rate limits and never tries to bypass 403/429/captchas or authentication.
"""
from __future__ import annotations

import argparse
from collections import Counter, deque
from datetime import datetime, timezone, timedelta
import hashlib
import re
import gzip
import uuid
import io
import json
from pathlib import Path
from contextlib import closing
from integrity import assess, persist, find_saved, mark_missing
import sqlite3
import sys
import time
from urllib.parse import urlsplit, urlunsplit, urljoin, unquote
import xml.etree.ElementTree as ET

import requests
from filelock import FileLock, Timeout as LockTimeout
from scraper import parse
from catalog_discovery import discover_categories, scan_categories
from http_identity import resolve_user_agent

HOST = 'ricette.giallozafferano.it'
BASE = 'https://' + HOST
WEB_HOST = 'www.giallozafferano.it'
WEB_BASE = 'https://' + WEB_HOST
ROBOTS = BASE + '/robots.txt'
DEFAULT_SITEMAPS = [
    BASE + '/sitemap/ricette.xml',
    BASE + '/sitemap/ricette-new.xml',
    BASE + '/sitemap/updated.xml',
]
MAX_XML_BYTES = 60 * 1024 * 1024

class CrawlBlocked(RuntimeError):
    """No permission or rate limited; stop without trying workarounds."""


def now():
    return datetime.now(timezone.utc).isoformat(timespec='seconds')


def localname(tag):
    return str(tag).rsplit('}', 1)[-1]


def normalize_url(url, kind='recipe'):
    """Return canonical allowed URL, or None; ignore query & fragment."""
    try:
        parts = urlsplit(url.strip())
    except ValueError:
        return None
    try:
        if parts.scheme not in ('https','http') or parts.username or parts.password or parts.port:
            return None
        host = (parts.hostname or '').lower()
    except ValueError:
        return None
    if kind == 'recipe':
        if host != HOST:
            return None
        if not parts.path.lower().endswith('.html') or parts.query:
            return None
    elif kind == 'sitemap':
        if host not in (HOST, WEB_HOST) or not parts.path.lower().endswith(('.xml', '.xml.gz')) or parts.query:
            return None
    elif kind == 'listing':
        if host != WEB_HOST or parts.query:
            return None
        if not (parts.path.startswith('/ricette-cat/') or parts.path.startswith('/ricette-con') or parts.path.startswith('/ricerca-ricette/')):
            return None
    else:
        return None
    return urlunsplit(('https', host, parts.path or '/', '', ''))


def sitemap_items(xml_bytes):
    """Stream XML into ('sitemap'|'recipe', url, lastmod) tuples."""
    try:
        reader = ET.iterparse(io.BytesIO(xml_bytes), events=('start', 'end'))
        root = None
        kind = None
        for event, elem in reader:
            tag = localname(elem.tag)
            if root is None and event == 'start':
                root = elem
                if tag == 'sitemapindex':
                    kind = 'sitemap'
                elif tag == 'urlset':
                    kind = 'recipe'
                else:
                    raise ValueError('Unknown sitemap XML root: ' + tag)
            if event == 'end' and tag in ('sitemap', 'url'):
                loc = next((e.text for e in elem if localname(e.tag) == 'loc'), None)
                lastmod = next((e.text for e in elem if localname(e.tag) == 'lastmod'), None)
                if loc:
                    yield ('sitemap' if tag == 'sitemap' else 'recipe', loc.strip(), (lastmod or '').strip() or None)
                elem.clear()
    except ET.ParseError as exc:
        raise ValueError('Invalid sitemap XML') from exc


def open_db(path):
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(p)
    conn.row_factory = sqlite3.Row
    conn.execute('PRAGMA journal_mode=WAL')
    conn.execute('PRAGMA synchronous=NORMAL')
    conn.execute('PRAGMA busy_timeout=5000')
    conn.execute('PRAGMA cache_size=-32768')
    conn.execute('PRAGMA temp_store=MEMORY')
    conn.execute('PRAGMA mmap_size=134217728')
    conn.executescript('''
      CREATE TABLE IF NOT EXISTS urls (
        url TEXT PRIMARY KEY,
        lastmod TEXT,
        status TEXT NOT NULL DEFAULT 'pending',
        attempts INTEGER NOT NULL DEFAULT 0,
        first_seen TEXT NOT NULL,
        last_seen TEXT NOT NULL,
        fetched_at TEXT,
        retry_at TEXT,
        error TEXT,
        etag TEXT,
        http_last_modified TEXT
      );
      CREATE INDEX IF NOT EXISTS idx_urls_status ON urls(status, retry_at, first_seen);
      CREATE INDEX IF NOT EXISTS idx_urls_first_seen ON urls(status, first_seen, url);
      CREATE TABLE IF NOT EXISTS recipes (
        url TEXT PRIMARY KEY,
        title TEXT,
        content_hash TEXT,
        extracted_at TEXT,
        json TEXT NOT NULL,
        quality_status TEXT NOT NULL DEFAULT 'unverified'
      );
      CREATE TABLE IF NOT EXISTS sitemap_scans (
        url TEXT PRIMARY KEY,
        fetched_at TEXT,
        discovered INTEGER NOT NULL DEFAULT 0,
        error TEXT
      );
      CREATE TABLE IF NOT EXISTS discovery_runs (
        id TEXT PRIMARY KEY,
        started_at TEXT NOT NULL,
        finished_at TEXT,
        total_sitemaps INTEGER NOT NULL DEFAULT 0,
        failed_sitemaps INTEGER NOT NULL DEFAULT 0,
        discovered_urls INTEGER NOT NULL DEFAULT 0,
        status TEXT NOT NULL DEFAULT 'running'
      );
      CREATE TABLE IF NOT EXISTS listing_pages (
        url TEXT PRIMARY KEY, lastmod TEXT, status TEXT NOT NULL DEFAULT 'pending',
        attempts INTEGER NOT NULL DEFAULT 0, first_seen TEXT NOT NULL, last_seen TEXT NOT NULL,
        scanned_at TEXT, retry_at TEXT, error TEXT, discovered_run_id TEXT,
        recipes_found INTEGER NOT NULL DEFAULT 0, source_sitemap TEXT,
        skipped_query_links INTEGER NOT NULL DEFAULT 0,
        etag TEXT, http_last_modified TEXT
      );
      CREATE INDEX IF NOT EXISTS idx_listings_queue ON listing_pages(status, retry_at, first_seen);
      CREATE INDEX IF NOT EXISTS idx_listings_pending_order ON listing_pages(status, first_seen, url);
      CREATE INDEX IF NOT EXISTS idx_listings_retry_order ON listing_pages(status, retry_at, url);
      CREATE INDEX IF NOT EXISTS idx_listings_run ON listing_pages(discovered_run_id, status);
      CREATE TABLE IF NOT EXISTS category_discovery_runs (
        id TEXT PRIMARY KEY, started_at TEXT NOT NULL, finished_at TEXT,
        sitemap_count INTEGER NOT NULL DEFAULT 0, sitemap_errors INTEGER NOT NULL DEFAULT 0,
        listing_count INTEGER NOT NULL DEFAULT 0, status TEXT NOT NULL DEFAULT 'running'
      );
      CREATE TABLE IF NOT EXISTS category_run_sitemaps (
        run_id TEXT NOT NULL, url TEXT NOT NULL, listings INTEGER NOT NULL DEFAULT 0,
        error TEXT, PRIMARY KEY(run_id,url)
      );
      CREATE TABLE IF NOT EXISTS recipe_aliases (
        alias_url TEXT PRIMARY KEY, canonical_url TEXT NOT NULL,
        first_seen TEXT NOT NULL,last_seen TEXT NOT NULL
      );
      CREATE INDEX IF NOT EXISTS idx_alias_canonical ON recipe_aliases(canonical_url);
      CREATE TABLE IF NOT EXISTS canonical_conflicts (
        alias_url TEXT NOT NULL, canonical_url TEXT NOT NULL,
        legacy_json TEXT NOT NULL, created_at TEXT NOT NULL,
        PRIMARY KEY(alias_url,canonical_url)
      );
      CREATE TABLE IF NOT EXISTS recipe_sources (
        url TEXT NOT NULL, channel TEXT NOT NULL, origin TEXT NOT NULL,
        discovered_at TEXT NOT NULL, PRIMARY KEY(url,channel,origin)
      );
      CREATE INDEX IF NOT EXISTS idx_recipe_sources_channel ON recipe_sources(channel,url);
      CREATE TABLE IF NOT EXISTS discovery_run_sitemaps (
        run_id TEXT NOT NULL,
        url TEXT NOT NULL,
        discovered INTEGER NOT NULL DEFAULT 0,
        error TEXT,
        PRIMARY KEY(run_id, url)
      );
    ''')
    # In-place migration: preserves databases created with v0.3/v0.4.
    existing = {row['name'] for row in conn.execute('PRAGMA table_info(urls)')}
    for name, definition in (
        ('discovered_run_id', 'TEXT'),
        ('source', "TEXT NOT NULL DEFAULT 'sitemap'"),
        ('not_found_count', 'INTEGER NOT NULL DEFAULT 0'),
        ('redirected_to', 'TEXT'),
    ):
        if name not in existing:
            conn.execute(f'ALTER TABLE urls ADD COLUMN {name} {definition}')
    listing_columns = {row['name'] for row in conn.execute('PRAGMA table_info(listing_pages)')}
    if 'skipped_query_links' not in listing_columns:
        conn.execute('ALTER TABLE listing_pages ADD COLUMN skipped_query_links INTEGER NOT NULL DEFAULT 0')
    for name in ('etag','http_last_modified'):
        if name not in listing_columns:
            conn.execute(f'ALTER TABLE listing_pages ADD COLUMN {name} TEXT')
    recipe_columns = {row['name'] for row in conn.execute('PRAGMA table_info(recipes)')}
    if 'quality_status' not in recipe_columns:
        conn.execute("ALTER TABLE recipes ADD COLUMN quality_status TEXT NOT NULL DEFAULT 'unverified'")
    conn.execute("UPDATE urls SET status='review' WHERE status='not_recipe'")
    # Older versions considered 404/410 definitive. Revisit rather than certify absence.
    conn.execute("UPDATE urls SET status='retry',retry_at=COALESCE(retry_at,?) WHERE status='gone'",
                 ((datetime.now(timezone.utc)+timedelta(days=7)).isoformat(timespec='seconds'),))
    # Stale workers from interrupted sessions are safely requeued.
    conn.execute("UPDATE urls SET status='pending' WHERE status='in_progress'")
    conn.execute("UPDATE listing_pages SET status='pending' WHERE status='in_progress'")
    # v0.4 marked a single 404 as permanent: require a second observation.
    conn.execute("UPDATE urls SET status='retry',not_found_count=1,retry_at=COALESCE(retry_at,?) WHERE status='gone' AND error LIKE 'HTTP 404%' AND not_found_count=0",
                 ((datetime.now(timezone.utc) + timedelta(hours=24)).isoformat(timespec='seconds'),))
    conn.execute('CREATE INDEX IF NOT EXISTS idx_urls_scan ON urls(discovered_run_id, status)')
    conn.commit()
    return conn


def upsert_link(conn, url, lastmod=None, refresh_days=90, timestamp=None, run_id=None, source="sitemap", origin=None):
    timestamp = timestamp or now()
    if origin is not None:
        conn.execute('INSERT OR IGNORE INTO recipe_sources(url,channel,origin,discovered_at) VALUES(?,?,?,?)',
                     (url, source, origin, timestamp))
    old = conn.execute('SELECT status,lastmod,fetched_at FROM urls WHERE url=?', (url,)).fetchone()
    if old is None:
        conn.execute('INSERT INTO urls(url,lastmod,status,first_seen,last_seen,discovered_run_id,source) VALUES(?,?,?,?,?,?,?)',
                     (url, lastmod, 'pending', timestamp, timestamp, run_id, source))
        return 'new'
    # A recipe may occur in several sitemaps with different lastmod values.
    # Never let an older entry overwrite a newer timestamp.
    effective_lastmod = max(lastmod, old['lastmod']) if lastmod and old['lastmod'] else (lastmod or old['lastmod'])
    changed = bool(old['lastmod'] and effective_lastmod and effective_lastmod > old['lastmod'])
    # Don't accept a changed timestamp as evidence of deletion: requeue only.
    expired = False
    if old['fetched_at'] and refresh_days > 0:
        try:
            age = (datetime.fromisoformat(timestamp) - datetime.fromisoformat(old['fetched_at'])).total_seconds()
            expired = age >= refresh_days * 86400
        except (ValueError, TypeError):
            expired = True
    new_status = ('pending' if (changed or expired) and old['status'] not in ('redirect', 'in_progress')
                  else old['status'])
    conn.execute('''UPDATE urls SET lastmod=COALESCE(?,lastmod),last_seen=?,status=?,
                    retry_at=CASE WHEN ? THEN NULL ELSE retry_at END,
                    discovered_run_id=COALESCE(?,discovered_run_id),
                    source=CASE WHEN ?='sitemap' THEN 'sitemap' ELSE source END
                    WHERE url=?''',
                 (effective_lastmod, timestamp, new_status, int(changed or expired), run_id, source, url))
    return 'refreshed' if (changed or expired) else 'existing'


class RobotsPolicy:
    """Small robots.txt policy supporting groups, longest rules, * and $.

    Multiple equally specific matches prefer Allow, as in RFC 9309.
    Unrecognized directives are ignored. This is not an authorization system.
    """
    def __init__(self, text):
        self.groups = []
        self.maps = []
        agents, rules, delay = [], [], None
        for raw in text.splitlines() + ['']:
            line = raw.split('#', 1)[0].strip()
            if not line:
                if agents:
                    self.groups.append((agents, rules, delay))
                agents, rules, delay = [], [], None
                continue
            if ':' not in line:
                continue
            key, value = (v.strip() for v in line.split(':', 1))
            key = key.lower()
            if key == 'sitemap':
                if value:
                    self.maps.append(value)
            elif key == 'user-agent':
                if rules:
                    self.groups.append((agents, rules, delay))
                    agents, rules, delay = [], [], None
                agents.append(value.lower())
            elif agents and key in ('allow', 'disallow') and value:
                rules.append((key == 'allow', unquote(value)))
            elif agents and key == 'crawl-delay':
                try:
                    delay = max(0.0, float(value))
                except ValueError:
                    pass

    def _groups_for(self, user_agent):
        token = user_agent.split('/', 1)[0].lower()
        matches = []
        for agents, rules, delay in self.groups:
            score = max((len(a) for a in agents if a != '*' and a in token), default=-1)
            if score < 0 and '*' in agents:
                score = 0
            if score >= 0:
                matches.append((score, rules, delay))
        if not matches:
            return []
        best = max(item[0] for item in matches)
        return [(rules, delay) for score, rules, delay in matches if score == best]

    def can_fetch(self, user_agent, url):
        parts = urlsplit(url)
        path = unquote(parts.path or '/')
        if parts.query:
            path += '?' + unquote(parts.query)
        matches = []
        for rules, _ in self._groups_for(user_agent):
            for allow, pattern in rules:
                anchored = pattern.endswith('$')
                part = pattern[:-1] if anchored else pattern
                regex = '^' + re.escape(part).replace(r'\*', '.*') + ('$' if anchored else '')
                if re.match(regex, path):
                    # Specificity is based on non-wildcard characters.
                    matches.append((len(part.replace('*', '')), int(allow)))
        return max(matches, default=(0, 1))[1] == 1

    def crawl_delay(self, user_agent):
        delays = [delay for _, delay in self._groups_for(user_agent) if delay is not None]
        return max(delays) if delays else None

    def site_maps(self):
        return list(dict.fromkeys(self.maps))


def is_access_challenge(html):
    """Avoid broad string matches: advertising scripts often mention CAPTCHA."""
    m = re.search(r'<title\b[^>]*>(.*?)</title>', html[:200000], re.I | re.S)
    title = re.sub(r'<[^>]+>', '', m.group(1)).strip().lower() if m else ''
    if any(x in title for x in ('access denied', 'captcha', 'verify you are human',
                                'just a moment', 'too many requests', 'request blocked')):
        return True
    return bool(re.search(r'<form[^>]+(?:id|class)=["\'][^"\']*(?:captcha|challenge-form|cf-chl)',
                          html[:100000], re.I))


class PoliteHttp:
    def __init__(self, user_agent=None, delay=4, timeout=25, session=None, monotonic=None, sleep=None, host=HOST):
        if delay < 2:
            raise ValueError('Delay must be at least 2 seconds.')
        if host not in (HOST, WEB_HOST):
            raise ValueError('Unsupported host for crawler')
        self.host = host
        self.base = 'https://' + host
        self.robot_url = self.base + '/robots.txt'
        self.agent = resolve_user_agent(user_agent)
        self.session = session or requests.Session()
        self.session.headers.update({'User-Agent': self.agent, 'Accept': 'text/html,application/xml,text/xml;q=0.9,*/*;q=0.5'})
        self.delay = delay
        self.timeout = timeout
        self.clock = monotonic or time.monotonic
        self.sleep = sleep or time.sleep
        self.last_request = None
        self.robot = None

    def _pacing(self):
        if self.last_request is not None:
            remaining = self.delay - (self.clock() - self.last_request)
            if remaining > 0:
                self.sleep(remaining)
        self.last_request = self.clock()

    def initialize_robots(self):
        # Fail closed: don't crawl if robots can't be checked.
        self._pacing()
        try:
            r = self.session.get(self.robot_url, timeout=self.timeout)
        except requests.RequestException as exc:
            raise CrawlBlocked(f'Cannot check robots.txt: {exc}') from exc
        if r.status_code != 200:
            raise CrawlBlocked(f'Cannot validate robots.txt (HTTP {r.status_code}); stopping safely.')
        if '<html' in r.text[:2000].lower():
            raise CrawlBlocked('robots.txt returned an HTML page instead of rules')
        rp = RobotsPolicy(r.text)
        probe = self.base + ('/Lasagne-al-forno.html' if self.host == HOST else '/ricette-cat/Primi/')
        if not rp.can_fetch(self.agent, probe):
            raise CrawlBlocked('robots.txt disallows this crawler on required pages.')
        crawl_delay = rp.crawl_delay(self.agent)
        if crawl_delay:
            self.delay = max(self.delay, float(crawl_delay))
        self.robot = rp
        return rp

    def get(self, url, accept=None, max_bytes=MAX_XML_BYTES, conditional_headers=None):
        if self.robot is None:
            self.initialize_robots()
        origin = url
        kind = ('sitemap' if urlsplit(url).path.lower().endswith(('.xml', '.xml.gz'))
                else 'listing' if self.host == WEB_HOST else 'recipe')
        redirects = 0
        while True:
            if urlsplit(url).hostname != self.host or not normalize_url(url, kind=kind):
                raise CrawlBlocked('URL is outside the intended host/scope: ' + url)
            if not self.robot.can_fetch(self.agent, url):
                raise CrawlBlocked('Blocked by robots.txt: ' + url)
            self._pacing()
            headers = {'Accept': accept} if accept else {}
            if conditional_headers:
                headers.update({k: v for k, v in conditional_headers.items()
                                if k in ('If-None-Match', 'If-Modified-Since') and v})
            try:
                r = self.session.get(url, timeout=self.timeout, headers=headers,
                                     stream=True, allow_redirects=False)
            except requests.RequestException as exc:
                raise RuntimeError(f'Network error: {exc}') from exc
            if r.status_code in (301, 302, 303, 307, 308):
                target = urljoin(url, r.headers.get('Location', ''))
                r.close()
                redirects += 1
                if redirects > 3 or target == url:
                    raise RuntimeError(f'Redirect chain too long or cyclic: {origin}')
                normalized = normalize_url(target, kind)
                if normalized is None or urlsplit(normalized).hostname != self.host:
                    raise CrawlBlocked('Unsafe or out-of-scope redirect: ' + target)
                url = normalized
                conditional_headers = None  # Validators belong to the original URL.
                continue
            meta = dict(r.headers)
            if origin != url:
                meta['X-Crawler-Final-URL'] = url
            if r.status_code in (401, 403, 429):
                retry = r.headers.get('Retry-After')
                r.close()
                raise CrawlBlocked(f'Access blocked or rate limited (HTTP {r.status_code}); Retry-After={retry or "unspecified"}; stopping.')
            if r.status_code >= 500:
                r.close()
                raise CrawlBlocked(f'Server error (HTTP {r.status_code}); stopping to avoid repeated load.')
            if r.status_code == 304:
                r.close()
                return 304, b'', meta
            if r.status_code in (404, 410):
                r.close()
                return r.status_code, b'', meta
            if r.status_code != 200:
                r.close()
                raise RuntimeError(f'Unexpected HTTP {r.status_code} for {url}')
            parts = []
            received = 0
            try:
                for chunk in r.iter_content(chunk_size=65536):
                    if not chunk:
                        continue
                    received += len(chunk)
                    if received > max_bytes:
                        raise RuntimeError(f'Download too large (>{max_bytes} bytes): {url}')
                    parts.append(chunk)
            finally:
                r.close()
            return 200, b''.join(parts), meta


def discover(conn, http, *, seeds=None, refresh_days=90, max_sitemaps=300):
    """Discover sitemap URLs with an auditable, atomic per-sitemap scan.

    A broken XML file cannot partially insert links. Incomplete discovery is
    permanently recorded and causes the `run` CLI to stop before crawling.
    """
    checked = set()
    counts = Counter()
    run_id = uuid.uuid4().hex
    started = now()
    conn.execute('INSERT INTO discovery_runs(id,started_at) VALUES(?,?)', (run_id, started))
    conn.commit()
    try:
        if seeds is None:
            # Prefer the site's current published sitemap declarations.
            published = []
            if hasattr(http, 'initialize_robots'):
                robot = http.initialize_robots()
                published = [x for x in (robot.site_maps() or [])
                             if any(key in urlsplit(x).path.lower() for key in ('ricette', 'updated'))]
            seeds = published or DEFAULT_SITEMAPS
        queue = deque(normalize_url(url, 'sitemap') for url in seeds)
    except Exception as exc:
        # A failed robots read cannot leave the previous successful scan looking current.
        conn.execute("UPDATE discovery_runs SET finished_at=?,failed_sitemaps=1,status='partial' WHERE id=?",
                     (now(), run_id))
        conn.execute('INSERT INTO discovery_run_sitemaps(run_id,url,error) VALUES(?,?,?)',
                     (run_id, ROBOTS, str(exc)[:700]))
        conn.commit()
        raise
    blocked = None
    while queue:
        site_map = queue.popleft()
        if not site_map or site_map in checked:
            continue
        if len(checked) >= max_sitemaps:
            blocked = RuntimeError(f'Sitemap limit {max_sitemaps} reached; increase --max-sitemaps after inspection')
            counts['sitemap_errors'] += 1
            break
        checked.add(site_map)
        print(f'SITEMAP FETCH {len(checked)}: {site_map}', flush=True)
        try:
            status, data, _ = http.get(site_map, accept='application/xml,text/xml,*/*')
            if status != 200:
                raise RuntimeError(f'HTTP {status}')
            if site_map.endswith('.xml.gz') and data.startswith(b'\x1f\x8b'):
                # Guard against gzip bombs: never expand more than the cap.
                with gzip.GzipFile(fileobj=io.BytesIO(data)) as compressed:
                    data = compressed.read(MAX_XML_BYTES + 1)
                if len(data) > MAX_XML_BYTES:
                    raise RuntimeError('Uncompressed sitemap exceeds size limit')
            if len(data) > MAX_XML_BYTES:
                raise RuntimeError('Sitemap exceeds size limit')
            records = 0
            nested = []
            local_counts = Counter()
            conn.execute('SAVEPOINT one_sitemap')
            try:
                for kind, link, lm in sitemap_items(data):
                    normalized = normalize_url(link, 'sitemap' if kind == 'sitemap' else 'recipe')
                    if not normalized:
                        continue
                    if kind == 'sitemap':
                        nested.append(normalized)
                    else:
                        local_counts[upsert_link(conn, normalized, lm, refresh_days=refresh_days, run_id=run_id, origin=site_map)] += 1
                        records += 1
                conn.execute('RELEASE SAVEPOINT one_sitemap')
            except Exception:
                conn.execute('ROLLBACK TO SAVEPOINT one_sitemap')
                conn.execute('RELEASE SAVEPOINT one_sitemap')
                raise
            queue.extend(nested)
            counts.update(local_counts)
            conn.execute('INSERT OR REPLACE INTO sitemap_scans(url,fetched_at,discovered,error) VALUES(?,?,?,NULL)',
                         (site_map, now(), records))
            conn.execute('INSERT INTO discovery_run_sitemaps(run_id,url,discovered) VALUES(?,?,?)',
                         (run_id, site_map, records))
            conn.commit()
            print(f'SITEMAP {site_map}: {records} recipe URLs', flush=True)
        except Exception as exc:
            err = str(exc)[:700]
            conn.execute('INSERT OR REPLACE INTO sitemap_scans(url,fetched_at,discovered,error) VALUES(?,?,0,?)',
                         (site_map, now(), err))
            conn.execute('INSERT INTO discovery_run_sitemaps(run_id,url,error) VALUES(?,?,?)',
                         (run_id, site_map, err))
            conn.commit()
            counts['sitemap_errors'] += 1
            print(f'SITEMAP ERROR {site_map}: {err}', file=sys.stderr, flush=True)
            if isinstance(exc, CrawlBlocked):
                blocked = exc
                break
    counts['sitemap_files'] = len(checked)
    unique = conn.execute('SELECT COUNT(*) FROM urls WHERE discovered_run_id=?', (run_id,)).fetchone()[0]
    counts['unique_urls_in_scan'] = unique
    status = 'complete' if not counts['sitemap_errors'] and checked else 'partial'
    conn.execute('UPDATE discovery_runs SET finished_at=?,total_sitemaps=?, failed_sitemaps=?,discovered_urls=?,status=? WHERE id=?',
                 (now(), len(checked), counts['sitemap_errors'], unique, status, run_id))
    conn.commit()
    counts['discovery_complete'] = status == 'complete'
    if blocked is not None:
        raise blocked
    return counts


def next_pending(conn):
    """Indexed lookup: no expensive ORDER BY CASE for the growing URL table."""
    pending = conn.execute('''SELECT * FROM urls WHERE status='pending'
                              ORDER BY first_seen, url LIMIT 1''').fetchone()
    if pending is not None:
        return pending
    return conn.execute('''SELECT * FROM urls WHERE status='retry' AND retry_at<=?
                           ORDER BY retry_at, first_seen LIMIT 1''', (now(),)).fetchone()


def crawl(conn, http, *, limit=10, batch_size=25, browser=None, follow_related=False, max_attempts=4):
    """Extract and batch commits; browser is optional JS rendering fallback.

    All blocked HTTP statuses stop before browser fallback can be considered.
    """
    if batch_size < 1 or max_attempts < 1:
        raise ValueError('batch_size and max_attempts must be positive')
    count = Counter()
    dirty = 0
    try:
        while limit == 0 or count['attempted'] < limit:
            row = next_pending(conn)
            if row is None:
                break
            url = row['url']
            count['attempted'] += 1
            try:
                conditional = {}
                if row['etag']:
                    conditional['If-None-Match'] = row['etag']
                if row['http_last_modified']:
                    conditional['If-Modified-Since'] = row['http_last_modified']
                kw = {'accept': 'text/html,application/xhtml+xml'}
                if conditional:
                    kw['conditional_headers'] = conditional
                code, content, response_headers = http.get(url, **kw)
                stamp = now()
                if code == 304:
                    prior = find_saved(conn,url)
                    if prior:
                        conn.execute("UPDATE urls SET status='done',attempts=attempts+1,error=NULL,fetched_at=?,retry_at=NULL WHERE url=?",
                                     (stamp, url))
                        count['not_modified'] += 1
                    else:
                        # Recover gracefully from a corrupt/incomplete prior archive.
                        conn.execute("UPDATE urls SET status='review',etag=NULL,http_last_modified=NULL,error='304 without saved recipe' WHERE url=?", (url,))
                        count['missing_304_review'] += 1
                elif code in (404, 410):
                    mark_missing(conn,row,code,stamp)
                    count['temporarily_missing'] += 1
                    count['retry'] += 1
                else:
                    html = content.decode('utf-8', errors='replace')
                    if is_access_challenge(html):
                        raise CrawlBlocked('HTTP 200 returned access restriction / challenge; stopping.')
                    recipe = parse(html, url, include_images=False)
                    quality=assess(recipe)
                    if quality['status']!='quality_passed' and browser is not None:
                        html=browser.fetch_html(url)
                        recipe=parse(html,url,include_images=False)
                        count['browser_fallback']+=1
                    result=persist(conn,url,response_headers,recipe,stamp)
                    count[result['status']]+=1
                    if result['status']!='review':
                        count['done']+=1
                        if result['canonical']!=url:count['redirect_aliases']+=1
                        if follow_related:
                            for related in recipe.get('related_recipes',[]):
                                related_url=normalize_url(related.get('url',''))
                                if related_url and related_url!=result['canonical']:
                                    if upsert_link(conn,related_url,source='related',origin=result['canonical'])=='new':
                                        count['related_discovered']+=1
                        print(f"OK {recipe['title']} | ingredients={len(recipe['ingredients'])} | utensils={len(recipe['utensils_detected'])}",flush=True)
                dirty += 1
            except CrawlBlocked:
                count['blocked'] += 1
                raise
            except Exception as exc:
                attempts = row['attempts'] + 1
                final_status = 'failed' if attempts >= max_attempts else 'retry'
                wait_hours = min(24, 2 ** max(0, attempts - 1))
                retry_at = (datetime.now(timezone.utc) + timedelta(hours=wait_hours)).isoformat(timespec='seconds') if final_status == 'retry' else None
                conn.execute("UPDATE urls SET status=?,attempts=attempts+1,error=?,retry_at=? WHERE url=?",
                             (final_status, str(exc)[:700], retry_at, url))
                dirty += 1
                count[final_status] += 1
                print(f'{final_status.upper()} {url}: {exc}', file=sys.stderr, flush=True)
            if dirty >= batch_size:
                conn.commit()
                dirty = 0
    finally:
        # Checkpoint logically completed records even on Ctrl+C or 403/429.
        if dirty:
            conn.commit()
    return count


def export(conn, destination, fmt='jsonl'):
    destination = Path(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    from integrity import stripped_recipe
    cursor = conn.execute('SELECT json FROM recipes ORDER BY url')
    count = 0
    with destination.open('w', encoding='utf-8') as output:
        if fmt == 'json':
            output.write('[\n')
        for row in cursor:
            # Legacy records can still contain preparation; never export it.
            payload = json.dumps(stripped_recipe(json.loads(row['json'])), ensure_ascii=False, separators=(',',':'))
            if fmt == 'json':
                if count:
                    output.write(',\n')
                output.write(payload)
            else:
                output.write(payload + '\n')
            count += 1
        if fmt == 'json':
            output.write('\n]\n')
    return count


def stats(conn):
    counts = {r['status']: r['n'] for r in conn.execute('SELECT status,COUNT(*) n FROM urls GROUP BY status')}
    counts['recipes_saved'] = conn.execute('SELECT COUNT(*) FROM recipes').fetchone()[0]
    counts['sitemaps_scanned'] = conn.execute('SELECT COUNT(*) FROM sitemap_scans').fetchone()[0]
    counts['category_listings'] = conn.execute('SELECT COUNT(*) FROM listing_pages').fetchone()[0]
    counts['category_done'] = conn.execute("SELECT COUNT(*) FROM listing_pages WHERE status='done'").fetchone()[0]
    counts['sitemap_errors'] = conn.execute('SELECT COUNT(*) FROM sitemap_scans WHERE error IS NOT NULL').fetchone()[0]
    return counts


def audit(conn, deep=False):
    """Global coverage accounting: a recent successful run cannot hide old pending URLs.

    This certifies only *discovered* URLs, never the entire website.
    """
    known=stats(conn)
    latest=conn.execute('SELECT * FROM discovery_runs ORDER BY started_at DESC,rowid DESC LIMIT 1').fetchone()
    source_counts={r['source']:r['n'] for r in conn.execute('SELECT source,COUNT(*) n FROM urls GROUP BY source')}
    global_status={r['status']:r['n'] for r in conn.execute('SELECT status,COUNT(*) n FROM urls GROUP BY status')}
    valid_redirect="(u.status='redirect' AND EXISTS (SELECT 1 FROM recipes r WHERE r.url=u.redirected_to))"
    valid_done="(u.status='done' AND EXISTS (SELECT 1 FROM recipes r WHERE r.url=u.url))"
    resolved_predicate=f'({valid_redirect} OR {valid_done})'
    global_total=conn.execute('SELECT COUNT(*) FROM urls').fetchone()[0]
    unresolved_total=conn.execute(f'SELECT COUNT(*) FROM urls u WHERE NOT {resolved_predicate}').fetchone()[0]
    unresolved_by_status={r['status']:r['n'] for r in conn.execute(
        f'SELECT u.status,COUNT(*) n FROM urls u WHERE NOT {resolved_predicate} GROUP BY u.status')}
    missing_done=conn.execute("SELECT COUNT(*) FROM urls u LEFT JOIN recipes r ON r.url=u.url WHERE u.status='done' AND r.url IS NULL").fetchone()[0]
    recent=None
    if latest:
        status={r['status']:r['n'] for r in conn.execute('SELECT status,COUNT(*) n FROM urls WHERE discovered_run_id=? GROUP BY status',(latest['id'],))}
        total=sum(status.values())
        resolved=conn.execute(f'SELECT COUNT(*) FROM urls u WHERE u.discovered_run_id=? AND {resolved_predicate}',(latest['id'],)).fetchone()[0]
        redirect_ok=conn.execute("""SELECT COUNT(*) FROM urls u WHERE discovered_run_id=? AND status='redirect'
                                AND EXISTS (SELECT 1 FROM recipes r WHERE r.url=u.redirected_to)""",(latest['id'],)).fetchone()[0]
        recent={'run_id':latest['id'],'started_at':latest['started_at'],'finished_at':latest['finished_at'],
                'sitemap_status':latest['status'],'sitemaps_read':latest['total_sitemaps'],
                'sitemap_errors':latest['failed_sitemaps'],'unique_urls':total,
                'status_counts':status,'redirects_with_recipe':redirect_ok,'resolved_urls':resolved,
                'resolved_percent':round(100*resolved/total,2) if total else 0}
    last_complete=bool(recent and recent['sitemap_status']=='complete' and recent['unique_urls']>0
                       and recent['resolved_urls']==recent['unique_urls'] and recent['resolved_urls']>0)
    category_latest=conn.execute('SELECT * FROM category_discovery_runs ORDER BY started_at DESC,rowid DESC LIMIT 1').fetchone()
    listing_status={r['status']:r['n'] for r in conn.execute('SELECT status,COUNT(*) n FROM listing_pages GROUP BY status')}
    listing_total=sum(listing_status.values())
    # Excluded, gone or unreviewed listing pages are NOT evidence that all their links were discovered.
    listing_unresolved=listing_total-listing_status.get('done',0)
    category_report=None
    if category_latest:
        current={r['status']:r['n'] for r in conn.execute(
            'SELECT status,COUNT(*) n FROM listing_pages WHERE discovered_run_id=? GROUP BY status',(category_latest['id'],))}
        category_report={'run_id':category_latest['id'],'sitemap_status':category_latest['status'],
                         'sitemap_errors':category_latest['sitemap_errors'],
                         'listings_discovered':sum(current.values()),'status_counts':current,
                         'listings_resolved':current.get('done',0),
                         'listings_resolved_percent':round(100*current.get('done',0)/sum(current.values()),2) if current else 0}
    has_categories=listing_total>0 or bool(category_latest)
    category_complete=bool(not has_categories or (category_latest and category_latest['status']=='complete'
                          and listing_total>0 and listing_unresolved==0))
    only_category=conn.execute("SELECT COUNT(DISTINCT c.url) FROM recipe_sources c WHERE c.channel='category' AND NOT EXISTS (SELECT 1 FROM recipe_sources s WHERE s.url=c.url AND s.channel='sitemap')").fetchone()[0]
    unresolved_category=conn.execute(f"""SELECT COUNT(DISTINCT p.url) FROM recipe_sources p JOIN urls u ON p.url=u.url
                            WHERE p.channel='category' AND NOT {resolved_predicate}""").fetchone()[0]
    invalid_records=incomplete_records=image_fields=procedure_fields=json_warnings=quality_failed=canonical_duplicates=0
    canonical_seen=set()
    if deep:
        from integrity import legacy_field_counts
        for record in conn.execute('SELECT url,json FROM recipes'):
            try:
                item=json.loads(record['json'])
            except (json.JSONDecodeError,TypeError):
                invalid_records+=1;continue
            if not isinstance(item,dict) or not(item.get('title') and item.get('ingredients')):
                incomplete_records+=1;continue
            if assess(item)['status']!='quality_passed':
                quality_failed+=1
            legacy = legacy_field_counts(item)
            if legacy['media']:
                image_fields+=1
            if legacy['procedures']:
                procedure_fields+=1
            if item.get('extraction_warning'):
                json_warnings+=1
            canonical=normalize_url(item.get('source_url') or '')
            if canonical and canonical in canonical_seen:
                canonical_duplicates+=1
            if canonical:
                canonical_seen.add(canonical)
    quality_marked=conn.execute("SELECT COUNT(*) FROM recipes WHERE quality_status='needs_review'").fetchone()[0]
    conflicting_aliases=conn.execute('SELECT COUNT(*) FROM canonical_conflicts').fetchone()[0]
    latest_sitemap_ok=bool(latest and latest['status']=='complete' and latest['failed_sitemaps']==0)
    global_queue_complete=bool(global_total>0 and unresolved_total==0 and known['recipes_saved']>0)
    data_quality_complete=bool(missing_done==0 and quality_marked==0 and conflicting_aliases==0 and
                               (not deep or not(invalid_records or incomplete_records or quality_failed or canonical_duplicates or procedure_fields or image_fields)))
    combined_complete=bool(last_complete and global_queue_complete and category_complete and
                           unresolved_category==0 and data_quality_complete)
    reasons=[]
    if not latest_sitemap_ok: reasons.append('Latest recipe sitemap discovery is absent or incomplete')
    if not last_complete: reasons.append('Latest discovery contains unresolved URLs or no recipes')
    if unresolved_total: reasons.append(f'Global URL queue has {unresolved_total} unresolved records (including historical scans)')
    if listing_unresolved: reasons.append(f'{listing_unresolved} category listings are unscanned, missing or need review')
    if has_categories and not category_complete and not listing_unresolved:
        reasons.append('Category sitemap discovery is incomplete')
    if unresolved_category: reasons.append('Some recipe URLs from categories are unresolved')
    if not data_quality_complete: reasons.append('Saved data needs quality review or canonical conflicts remain')
    return {
        'complete_for_last_discovery':last_complete,
        'complete_for_combined_sources':combined_complete,
        'global_queue_complete':global_queue_complete,
        'data_quality_complete':data_quality_complete,
        'category_discovery':category_report,
        'global_listing_status':listing_status,
        'global_unresolved_listings':listing_unresolved,
        'global_unresolved_urls':unresolved_total,
        'global_unresolved_by_status':unresolved_by_status,
        'global_known_urls':global_total,
        'recipes_found_only_via_categories':only_category,
        'category_query_links_not_visited':conn.execute('SELECT COALESCE(SUM(skipped_query_links),0) FROM listing_pages').fetchone()[0],
        'unresolved_recipes_from_categories':unresolved_category,
        'guarantee_of_entire_website':False,
        'scope':'All discovered recipe URLs across all historical runs and permitted categories; NOT proof of entire website',
        'latest_discovery':recent,'all_known_status':global_status,
        'url_sources':source_counts,'saved_recipes':known['recipes_saved'],
        'done_without_recipe':missing_done,
        'canonical_conflicts':conflicting_aliases,
        'quality_marked_needs_review':quality_marked,
        'deep_validation':({'invalid_json':invalid_records,'incomplete_recipe':incomplete_records,
                             'records_with_image_fields':image_fields,
                             'records_with_procedure_fields':procedure_fields,
                             'records_with_extraction_warnings':json_warnings,
                             'quality_failed':quality_failed,
                             'duplicate_canonical_records':canonical_duplicates} if deep else 'not_run'),
        'reasons_not_complete':list(dict.fromkeys(reasons)),
    }


def export_issues(conn, destination, limit=10000, include_categories=False):
    """Export unresolved URLs so losses cannot disappear behind aggregate counts."""
    destination = Path(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    rows = conn.execute('''SELECT url,status,attempts,error,retry_at,source,last_seen
                           FROM urls u WHERE status NOT IN ('done')
                           AND NOT (status='redirect' AND EXISTS
                                  (SELECT 1 FROM recipes r WHERE r.url=u.redirected_to))
                           ORDER BY status,url LIMIT ?''', (limit,))
    count = 0
    with destination.open('w', encoding='utf-8') as out:
        for row in rows:
            out.write(json.dumps(dict(row), ensure_ascii=False) + '\n')
            count += 1
        if include_categories:
            remaining = max(0, limit - count)
            for row in conn.execute("SELECT url,status,attempts,error,retry_at,source_sitemap,last_seen FROM listing_pages WHERE status NOT IN ('done') ORDER BY status,url LIMIT ?",(remaining,)):
                issue = dict(row)
                issue['kind'] = 'category_listing'
                out.write(json.dumps(issue,ensure_ascii=False)+'\n')
                count += 1
    return count


def backup_db(conn, destination):
    destination = Path(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.exists():
        raise FileExistsError(f'Backup already exists: {destination}')
    with sqlite3.connect(destination) as target:
        conn.backup(target)
    return destination


def requeue_for_review(conn, status, limit, listings=False):
    if status not in ('review', 'failed', 'gone'):
        raise ValueError('Requeue only review, failed or gone URL statuses')
    if listings:
        rows = conn.execute('SELECT url FROM listing_pages WHERE status=? ORDER BY url LIMIT ?', (status, limit)).fetchall()
        conn.executemany("UPDATE listing_pages SET status='pending',attempts=0,retry_at=NULL,error=NULL WHERE url=?",
                         [(r['url'],) for r in rows])
    else:
        rows = conn.execute('SELECT url FROM urls WHERE status=? ORDER BY url LIMIT ?', (status, limit)).fetchall()
        conn.executemany("UPDATE urls SET status='pending',attempts=0,not_found_count=0,retry_at=NULL,error=NULL WHERE url=?",
                         [(r['url'],) for r in rows])
    conn.commit()
    return len(rows)

def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--db', default='data/recipes.sqlite3', help='Persistent SQLite database path')
    subs = ap.add_subparsers(dest='command', required=True)
    for command in ('discover', 'crawl', 'run', 'pipeline'):
        p = subs.add_parser(command)
        p.add_argument('--user-agent', default=None, help='Override the User-Agent defined in .env for this run')
        p.add_argument('--delay', type=float, default=4.0, help='Minimum seconds between HTTP requests (>=2, default 4; obeys stricter crawl-delay)')
        p.add_argument('--timeout', type=float, default=25)
        if command in ('discover', 'run', 'pipeline'):
            if command == 'pipeline':
                p.add_argument('--skip-discovery', action='store_true', help='Resume existing queues without reloading sitemaps')
                p.add_argument('--workers', type=int, choices=range(1,5), default=2, metavar='{1,2,3,4}',help='Concurrent fetch workers per host (default 2; shared host rate limit)')
                p.add_argument('--http-retries', type=int, choices=range(0,6), default=2, metavar='{0..5}', help='Automatic retries for transient HTTP/network failures (default 2)')
                p.add_argument('--max-throttles', type=int, choices=range(1,11), default=3, metavar='{1..10}', help='Host-wide 429/503 circuit breaker threshold (default 3)')
            p.add_argument('--categories', action='store_true', help='Discover listing pages from public category/search sitemaps on www host')
            p.add_argument('--category-sitemap', action='append', help='Optional www listing sitemap; repeatable')
            if command in ('run', 'pipeline'):
                p.add_argument('--category-limit', type=int, default=25, help='Category pages to scan this run; 0 means all (default 25)')
            p.add_argument('--refresh-days', type=int, default=90)
            p.add_argument('--max-sitemaps', type=int, default=300)
            p.add_argument('--sitemap', action='append', help='Custom recipe sitemap; repeatable')
        if command in ('crawl', 'run', 'pipeline'):
            p.add_argument('--batch-size', type=int, default=25, help='Commit every N recipes (default 25)')
            p.add_argument('--max-attempts', type=int, default=4, help='Failures before marking an URL failed')
            p.add_argument('--follow-related', action='store_true', help='Queue related recipe links, restricted to recipe host')
            p.add_argument('--browser', choices=('off','auto'), default='off', help='Use Playwright only when a 200 HTML page has no valid Recipe')
            group = p.add_mutually_exclusive_group()
            group.add_argument('--limit', type=int, default=10, help='Max pages this run (default 10)')
            group.add_argument('--all', action='store_true', help='Process every pending recipe with polite pacing')
    p = subs.add_parser('categories', help='Discover and scan permitted category listing pages')
    p.add_argument('--user-agent', default=None, help='Override the User-Agent defined in .env')
    p.add_argument('--delay', type=float, default=4.0)
    p.add_argument('--timeout', type=float, default=25.0)
    p.add_argument('--refresh-days', type=int, default=45)
    p.add_argument('--max-sitemaps', type=int, default=300)
    p.add_argument('--category-sitemap', action='append')
    p.add_argument('--limit', type=int, default=25, help='0 means all')
    subs.add_parser('stats')
    subs.add_parser('prune-procedures', help='Remove old stored instructions, step photographs and technique fields; makes a backup first')
    p = subs.add_parser('audit')
    p.add_argument('--deep', action='store_true', help='Read every stored recipe to check integrity')
    p.add_argument('--strict', action='store_true', help='Exit 4 unless all discovered recipe URLs and optional listing sources are resolved')
    p.add_argument('--output', help='Optional path for audit JSON report')
    p = subs.add_parser('issues')
    p.add_argument('--output', default='data/issues.jsonl')
    p.add_argument('--limit', type=int, default=10000)
    p.add_argument('--include-categories', action='store_true')
    p = subs.add_parser('backup')
    p.add_argument('--output', default='data/recipes-backup.sqlite3')
    p = subs.add_parser('requeue')
    p.add_argument('--status', choices=('review','failed','gone'), required=True)
    p.add_argument('--listings', action='store_true', help='Requeue category listing pages rather than recipe URLs')
    p.add_argument('--limit', type=int, default=100)
    p = subs.add_parser('export')
    p.add_argument('--output', default='data/recipes.jsonl')
    p.add_argument('--format', choices=('jsonl', 'json'), default='jsonl')
    args = ap.parse_args(argv)
    if hasattr(args, 'limit') and args.limit < (0 if args.command=='categories' else 1):
        ap.error('Use --all for unlimited recipe pages; categories --limit 0 scans all listings')
    if getattr(args, 'batch_size', 1) < 1:
        ap.error('--batch-size must be positive')
    if getattr(args, 'refresh_days', 1) < 0:
        ap.error('--refresh-days must be non-negative')
    if getattr(args,'category_limit',0) < 0 or (args.command=='categories' and args.limit < 0):
        ap.error('Category page limits must be >= 0')
    # One process per SQLite database: prevents two scheduled crawls from fetching
    # the same pending recipe. The OS releases the lock on unexpected termination.
    Path(args.db).parent.mkdir(parents=True, exist_ok=True)
    lock = FileLock(str(args.db) + '.lock', timeout=0)
    try:
        lock.acquire()
    except LockTimeout:
        print('STOP: another crawler is already using this database.', file=sys.stderr)
        return 3
    try:
      with closing(open_db(args.db)) as db:
        try:
            if args.command == 'stats':
                print(json.dumps(stats(db), ensure_ascii=False, indent=2))
            elif args.command == 'prune-procedures':
                from integrity import prune_procedures
                backup_path = Path(args.db).parent / 'recipes-before-prune.sqlite3'
                if backup_path.exists():
                    ap.error(f'Backup already exists at {backup_path}; move it before pruning again')
                backup_db(db, backup_path)
                changed = prune_procedures(db)
                print(f'Updated {changed} older records; automatic backup: {backup_path}')
            elif args.command == 'export':
                n = export(db, args.output, args.format)
                print(f'Exported {n} recipes to {args.output}')
            elif args.command == 'audit':
                result = audit(db, deep=args.deep)
                report_text = json.dumps(result, ensure_ascii=False, indent=2)
                print(report_text)
                if args.output:
                    destination = Path(args.output)
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    destination.write_text(report_text + '\n', encoding='utf-8')
                if args.strict and not result['complete_for_combined_sources']:
                    return 4
            elif args.command == 'issues':
                n = export_issues(db, args.output, args.limit, include_categories=args.include_categories)
                print(f'Exported {n} unresolved URL records to {args.output}')
            elif args.command == 'backup':
                print(f'Created backup: {backup_db(db, args.output)}')
            elif args.command == 'requeue':
                n = requeue_for_review(db, args.status, args.limit, listings=args.listings)
                print(f'Requeued {n} URLs from status {args.status}')
            else:
                client = PoliteHttp(args.user_agent, args.delay, args.timeout)
                if args.command in ('discover','run'):
                    result = discover(db, client, seeds=args.sitemap,
                                      refresh_days=args.refresh_days, max_sitemaps=args.max_sitemaps)
                    print('DISCOVER', json.dumps(dict(result)))
                    if not result['discovery_complete']:
                        print('STOP: incomplete recipe sitemap discovery; consult audit and issues.', file=sys.stderr)
                        return 4
                if args.command == 'categories' or (args.command in ('discover','run','pipeline') and args.categories):
                    listing_client = PoliteHttp(args.user_agent, args.delay, args.timeout, host=WEB_HOST)
                    if args.command != 'pipeline':
                        r = discover_categories(db, listing_client, seeds=args.category_sitemap,
                                                refresh_days=args.refresh_days, max_sitemaps=args.max_sitemaps)
                        print('CATEGORY DISCOVER', json.dumps(dict(r)))
                        if not r['discovery_complete']:
                            print('STOP: category sitemap discovery incomplete; inspect audit.', file=sys.stderr)
                            return 4
                    if args.command in ('run','categories'):
                        cat_limit = args.limit if args.command=='categories' else args.category_limit
                        print(f'CATEGORY PHASE: {"all queued categories" if cat_limit == 0 else str(cat_limit)+" categories"}; recipe crawling begins only after this phase', flush=True)
                        summary = scan_categories(db, listing_client, limit=cat_limit)
                        print('CATEGORY SCAN', json.dumps(dict(summary)))
                if args.command == 'pipeline':
                    import asyncio
                    from async_pipeline import run_pipeline
                    # `client` already performed sitemap discovery/robots preflight.
                    # `listing_client` exists if categories were requested.
                    listing = listing_client if args.categories else None
                    if client.robot is None:
                        client.initialize_robots()
                    if listing is not None and listing.robot is None:
                        listing.initialize_robots()
                    result = asyncio.run(run_pipeline(
                        db, client, listing,
                        category_limit=args.category_limit if args.categories else 0,
                        recipe_limit=0 if args.all else args.limit,
                        batch_size=args.batch_size, max_attempts=args.max_attempts,
                        follow_related=args.follow_related, browser=args.browser,
                        workers=args.workers,http_retries=args.http_retries,
                        max_throttles=args.max_throttles,
                        discover_stream=not args.skip_discovery,
                        recipe_sitemaps=args.sitemap,category_sitemaps=args.category_sitemap,
                        max_sitemaps=args.max_sitemaps,refresh_days=args.refresh_days))
                    print('PIPELINE', json.dumps(result, ensure_ascii=False))
                if args.command in ('crawl','run'):
                    if args.browser == 'auto':
                        from browser_fallback import BrowserFallback
                        with BrowserFallback(timeout_seconds=args.timeout, robots=client.robot or client.initialize_robots(), user_agent=client.agent) as browser:
                            result = crawl(db, client, limit=0 if args.all else args.limit, batch_size=args.batch_size, browser=browser, follow_related=args.follow_related, max_attempts=args.max_attempts)
                    else:
                        result = crawl(db, client, limit=0 if args.all else args.limit, batch_size=args.batch_size, follow_related=args.follow_related, max_attempts=args.max_attempts)
                    print('CRAWL', json.dumps(dict(result)))
                print('STATS', json.dumps(stats(db)))
        except CrawlBlocked as exc:
            print('STOP: ' + str(exc), file=sys.stderr)
            return 2
        except KeyboardInterrupt:
            print('Interrupted: saved progress retained in SQLite.', file=sys.stderr)
            return 130
    finally:
        lock.release()
    return 0


if __name__ == '__main__':
    # Async submodules and category parser must reuse the running CLI module.
    # Otherwise 'from crawler import CrawlBlocked' creates a second class.
    sys.modules.setdefault('crawler', sys.modules[__name__])
    sys.exit(main())
