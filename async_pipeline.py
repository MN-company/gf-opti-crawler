"""Bounded asynchronous, two-host pipeline for recipe/category discovery.

Network requests are async, while a single asyncio event loop owns the SQLite
connection. Recipe and listing tasks overlap; each host has its own pacing.
No IP or user-agent rotation; access denials stop the pipeline.
Transient failures receive bounded retries; rate limits impose shared cooldowns.
"""
from __future__ import annotations

import asyncio
from collections import Counter
from datetime import datetime, timezone, timedelta
import hashlib
from email.utils import parsedate_to_datetime
import random
import json
import re
import sys
import time
from urllib.parse import urljoin, urlsplit

import httpx

from catalog_discovery import current_time, listing_links, upsert_listing
from crawler import (
    BASE, HOST, WEB_HOST, CrawlBlocked, MAX_XML_BYTES, PoliteHttp,
    is_access_challenge, next_pending, normalize_url, now, upsert_link,
)
from scraper import parse
from integrity import assess, persist, find_saved, mark_missing

MAX_HTML_BYTES = 8 * 1024 * 1024


class AsyncPoliteHttp:
    """Per-host adaptive pacing, shared cooldown and bounded HTTP retries.

    The condition is released while waiting, so a Retry-After response can
    immediately postpone *all* workers, including workers already waiting.
    A 401/403/CAPTCHA never triggers a retry or an identity change.
    """

    def __init__(self, checked: PoliteHttp, *, client=None, max_retries=2,
                 max_throttles=3, retry_base=3.0, recovery_successes=25):
        if checked.robot is None:
            raise ValueError('Robots must be checked before starting the pipeline')
        if not 0 <= max_retries <= 5:
            raise ValueError('max_retries must be between 0 and 5')
        if max_throttles < 1:
            raise ValueError('max_throttles must be positive')
        self.host = checked.host
        self.agent = checked.agent
        self.robot = checked.robot
        self.base_delay = checked.delay
        self.delay = checked.delay
        self.timeout = checked.timeout
        self.next_at = (checked.last_request or time.monotonic()) + checked.delay
        self.cooldown_until = 0.0
        self.max_retries = max_retries
        self.max_throttles = max_throttles
        self.retry_base = retry_base
        self.recovery_successes = recovery_successes
        self.throttle_strikes = 0
        self.good_streak = 0
        self.metrics = Counter()
        self.client = client or httpx.AsyncClient(
            headers={'User-Agent': self.agent,
                     'Accept': 'text/html,application/xhtml+xml,application/xml,*/*;q=0.5'},
            timeout=httpx.Timeout(self.timeout),
            limits=httpx.Limits(max_connections=4, max_keepalive_connections=4),
            follow_redirects=False,
        )
        self._owned = client is None
        self.condition = asyncio.Condition()

    async def close(self):
        if self._owned:
            await self.client.aclose()

    async def reserve(self):
        """Reserve one request start, respecting current pacing and cooldown."""
        async with self.condition:
            while True:
                wait = max(self.next_at, self.cooldown_until) - time.monotonic()
                if wait <= 0:
                    self.next_at = time.monotonic() + self.delay
                    self.metrics['requests_started'] += 1
                    return
                try:
                    await asyncio.wait_for(self.condition.wait(), timeout=wait)
                except asyncio.TimeoutError:
                    pass

    @staticmethod
    def parse_retry_after(value):
        """HTTP Retry-After supports delta-seconds and RFC 7231 HTTP dates."""
        if value is None:
            return None
        try:
            seconds = float(value.strip())
            if 0 <= seconds < float('inf'):
                return seconds
        except (ValueError, AttributeError):
            pass
        try:
            when = parsedate_to_datetime(value)
            if when.tzinfo is None:
                when = when.replace(tzinfo=timezone.utc)
            return max(0.0, (when - datetime.now(timezone.utc)).total_seconds())
        except (ValueError, TypeError, IndexError, OverflowError):
            return None

    async def note_throttle(self, status, retry_after=None):
        """Throttle every worker on this host; repeated limits open the breaker."""
        async with self.condition:
            self.throttle_strikes += 1
            self.metrics[f'http_{status}'] += 1
            self.good_streak = 0
            self.delay = min(max(self.base_delay * 8, self.base_delay), self.delay * 2)
            requested = self.parse_retry_after(retry_after)
            fallback = (30.0 if status == 429 else 15.0) * (2 ** min(self.throttle_strikes - 1, 4))
            pause = max(self.delay, fallback if requested is None else requested)
            self.cooldown_until = max(self.cooldown_until, time.monotonic() + pause)
            self.condition.notify_all()
            strikes = self.throttle_strikes
        print(f'HTTP BACKOFF host={self.host} status={status} pause={pause:.1f}s '
              f'delay={self.delay:.1f}s strikes={strikes}/{self.max_throttles}',
              file=sys.stderr, flush=True)
        if strikes >= self.max_throttles:
            self.metrics['circuit_breakers'] += 1
            raise CrawlBlocked(f'HTTP {status} repeated {strikes} times on {self.host}; '
                               'circuit breaker opened; saved progress can be resumed later. '
                               f'Retry-After={retry_after or "unspecified"}')

    async def note_success(self):
        """Recover cautiously after sustained success; never below robots/base."""
        async with self.condition:
            self.metrics['successful_responses'] += 1
            self.good_streak += 1
            if self.good_streak < self.recovery_successes:
                return
            self.good_streak = 0
            self.throttle_strikes = 0
            if self.delay > self.base_delay:
                self.delay = max(self.base_delay, self.delay / 2)
                self.condition.notify_all()
                print(f'HTTP RECOVER host={self.host} delay={self.delay:.1f}s',
                      flush=True)

    async def _transient_retry(self, attempt, reason):
        """Short, bounded per-request retry for network errors and HTTP 5xx."""
        self.metrics['transient_retries'] += 1
        wait = min(60.0, self.retry_base * (2 ** max(0, attempt - 1)))
        wait += random.uniform(0, wait * 0.2)
        print(f'HTTP RETRY host={self.host} attempt={attempt}/{self.max_retries} '
              f'reason={reason} wait={wait:.1f}s', file=sys.stderr, flush=True)
        await asyncio.sleep(wait)

    async def get(self, url, *, accept='text/html,application/xhtml+xml',
                  max_bytes=MAX_HTML_BYTES, conditional_headers=None):
        origin = url
        redirects = 0
        failures = 0
        while True:
            kind = ('sitemap' if urlsplit(url).path.lower().endswith(('.xml', '.xml.gz'))
                    else 'listing' if self.host == WEB_HOST else 'recipe')
            if urlsplit(url).hostname != self.host or normalize_url(url, kind) is None:
                raise CrawlBlocked('URL outside approved scope: ' + url)
            if not self.robot.can_fetch(self.agent, url):
                raise CrawlBlocked('URL disallowed by robots.txt: ' + url)
            await self.reserve()
            headers = {'Accept': accept, 'User-Agent': self.agent}
            if conditional_headers:
                headers.update({k: v for k, v in conditional_headers.items()
                                if k in ('If-None-Match', 'If-Modified-Since') and v})
            try:
                async with self.client.stream('GET', url, headers=headers) as response:
                    status = response.status_code
                    meta = dict(response.headers)
                    if status in (301, 302, 303, 307, 308):
                        target = urljoin(url, response.headers.get('Location', ''))
                        redirects += 1
                        if redirects > 3 or target == url:
                            raise RuntimeError('Redirect loop or chain too long: ' + origin)
                        target = normalize_url(target, kind)
                        if not target or urlsplit(target).hostname != self.host:
                            raise CrawlBlocked('Unsafe redirect from ' + url)
                        url = target
                        conditional_headers = None
                        continue
                    if status in (401, 403):
                        raise CrawlBlocked(f'HTTP {status}; access denied; stopped without retry.')
                    if status in (429, 503):
                        await response.aclose()
                        await self.note_throttle(status, meta.get('retry-after'))
                        if failures >= self.max_retries:
                            raise CrawlBlocked(f'HTTP {status} on {self.host} after '
                                               f'{failures + 1} attempts; stopped safely. '
                                               f'Retry-After={meta.get("retry-after", "unspecified")}')
                        failures += 1
                        continue  # reserve() waits for the host-wide cooldown
                    if status in (500, 502, 504):
                        if failures >= self.max_retries:
                            raise RuntimeError(f'HTTP {status} after {failures + 1} attempts: {url}')
                        failures += 1
                        await response.aclose()
                        await self._transient_retry(failures, f'HTTP {status}')
                        continue
                    if status in (304, 404, 410):
                        await self.note_success()
                        return status, b'', meta
                    if status != 200:
                        raise RuntimeError(f'Unexpected HTTP {status}: {url}')
                    chunks = []
                    total = 0
                    async for piece in response.aiter_bytes(chunk_size=65536):
                        total += len(piece)
                        if total > max_bytes:
                            raise RuntimeError(f'HTML exceeds maximum size: {url}')
                        chunks.append(piece)
                    if origin != url:
                        meta['X-Crawler-Final-URL'] = url
                    await self.note_success()
                    return status, b''.join(chunks), meta
            except httpx.RequestError as exc:
                if failures >= self.max_retries:
                    raise RuntimeError(f'Network error after {failures + 1} attempts: {exc}') from exc
                failures += 1
                await self._transient_retry(failures, type(exc).__name__)


class AsyncBrowserFallback:
    """Shared Playwright browser started only when static parsing fails."""

    def __init__(self, http: AsyncPoliteHttp):
        self.http = http
        self.playwright = self.browser = self.context = None
        self.startup_lock=asyncio.Lock()

    async def fetch_html(self, url):
        if not self.http.robot.can_fetch(self.http.agent, url):
            raise CrawlBlocked('Browser fallback URL disallowed by robots.txt')
        async with self.startup_lock:
            if self.context is None:
                try:
                    from playwright.async_api import async_playwright
                except ImportError as exc:
                    raise RuntimeError('Install the browser image for --browser auto') from exc
                self.playwright = await async_playwright().start()
                try:
                    self.browser = await self.playwright.chromium.launch(headless=True)
                    self.context = await self.browser.new_context(
                        user_agent=self.http.agent, service_workers='block', accept_downloads=False)
                    await self.context.route('**/*', self._route)
                except Exception:
                    await self.close()
                    raise
        for attempt in range(self.http.max_retries + 1):
            await self.http.reserve()
            page = await self.context.new_page()
            try:
                response = await page.goto(url, wait_until='domcontentloaded',
                                           timeout=int(self.http.timeout * 1000))
                if response is None:
                    raise RuntimeError('Browser returned no response')
                if response.status in (429, 503):
                    await self.http.note_throttle(response.status,
                                                  response.headers.get('retry-after'))
                    if attempt >= self.http.max_retries:
                        raise CrawlBlocked(f'Browser HTTP {response.status}; retry limit reached; stopped.')
                    continue  # Shared host cooldown applies before the next navigation.
                if response.status in (401, 403) or response.status >= 500:
                    raise CrawlBlocked(f'Browser HTTP {response.status}; stopped.')
                if response.status != 200:
                    raise RuntimeError(f'Browser HTTP {response.status}')
                html = await page.content()
                await self.http.note_success()
                return html
            finally:
                await page.close()
        raise AssertionError('Unreachable browser retry state')

    async def _route(self, route):
        request = route.request
        if (urlsplit(request.url).hostname != HOST or
            not self.http.robot.can_fetch(self.http.agent, request.url) or
            request.resource_type in ('image','media','font','stylesheet','websocket','eventsource')):
            await route.abort()
        else:
            # Only the first top-level navigation has already been reserved by
            # fetch_html(). Iframe documents and redirected documents need pacing.
            # Redirected requests have redirected_from set by Playwright.
            try:
                first_main_document = (request.resource_type == 'document'
                    and request.is_navigation_request()
                    and request.frame.parent_frame is None
                    and request.redirected_from is None)
            except Exception:
                first_main_document = False
            if not first_main_document:
                await self.http.reserve()
            await route.continue_()

    async def close(self):
        if self.context:
            await self.context.close()
            self.context = None
        if self.browser:
            await self.browser.close()
            self.browser = None
        if self.playwright:
            await self.playwright.stop()
            self.playwright = None


class Pipeline:
    def __init__(self, conn, recipe_http, listing_http=None, *, category_limit=0,
                 recipe_limit=0, batch_size=25, max_attempts=4, follow_related=False,
                 browser='off', progress_every=5, workers=2):
        self.conn = conn
        self.workers = max(1, min(int(workers), 4))
        self.recipe_http = recipe_http
        self.listing_http = listing_http
        self.category_limit = category_limit
        self.recipe_limit = recipe_limit
        self.batch_size = batch_size
        self.max_attempts = max_attempts
        self.follow_related = follow_related
        self.browser_mode = browser
        self.progress_every = progress_every
        self.cat_counts = Counter()
        self.recipe_counts = Counter()
        self.dirty = 0
        self.category_done = asyncio.Event()
        self.recipe_sitemaps_done = asyncio.Event()
        self.listing_sitemaps_done = asyncio.Event()
        self.recipe_sitemaps_done.set()
        self.listing_sitemaps_done.set()
        self.new_recipes = asyncio.Event()
        self.started = time.monotonic()
        self._category_workers_active=0
        self.browser = AsyncBrowserFallback(recipe_http) if browser == 'auto' else None
        self.timings = Counter()

    def checkpoint(self):
        self.dirty += 1
        if self.dirty >= self.batch_size:
            self.conn.commit()
            self.dirty = 0

    def log(self, kind, count):
        if count == 1 or count % self.progress_every == 0:
            elapsed = max(0.001, time.monotonic()-self.started)
            print(f'PIPELINE {kind} processed={count} categories={self.cat_counts["attempted"]}'
                  f' recipes={self.recipe_counts["attempted"]} saved={self.recipe_counts["saved"]}'
                  f' elapsed={elapsed:.0f}s throughput={60*(self.cat_counts["attempted"]+self.recipe_counts["attempted"])/elapsed:.2f}/min',
                  flush=True)

    async def _category_once(self, row):
        url = row['url']
        stamp = now()
        if not self.listing_http.robot.can_fetch(self.listing_http.agent, url):
            self.conn.execute("UPDATE listing_pages SET status='excluded',error='Disallowed by robots.txt',scanned_at=? WHERE url=?", (stamp,url))
            self.cat_counts['excluded'] += 1
            return
        try:
            conditions={}
            if row['etag']: conditions['If-None-Match']=row['etag']
            if row['http_last_modified']: conditions['If-Modified-Since']=row['http_last_modified']
            code, data, meta = await self.listing_http.get(url,conditional_headers=conditions)
            if code == 304:
                if row['scanned_at'] and row['recipes_found']>0:
                    self.conn.execute("UPDATE listing_pages SET status='done',scanned_at=?,attempts=attempts+1,error=NULL WHERE url=?",(stamp,url))
                    self.cat_counts['not_modified']+=1
                else:
                    self.conn.execute("UPDATE listing_pages SET status='review',attempts=attempts+1,error='304 without validated listing' WHERE url=?",(url,))
                    self.cat_counts['review']+=1
            elif code in (404, 410):
                self.conn.execute("UPDATE listing_pages SET status='review',scanned_at=?,attempts=attempts+1,error=? WHERE url=?", (stamp,f'HTTP {code}: unverified listing removal',url))
                self.cat_counts['review'] += 1
            elif code == 200:
                html = data.decode('utf-8', errors='replace')
                if is_access_challenge(html):
                    raise CrawlBlocked('Category page returned access challenge')
                recipes, pages, skipped = await asyncio.to_thread(listing_links, html, url, include_skip_count=True)
                if not recipes:
                    self.conn.execute("UPDATE listing_pages SET status='review',attempts=attempts+1,error='No recipe cards',scanned_at=? WHERE url=?", (stamp,url))
                    self.cat_counts['review'] += 1
                else:
                    added = 0
                    for recipe_url in recipes:
                        if upsert_link(self.conn, recipe_url, source='category', origin=url) == 'new':
                            added += 1
                    for page_url in pages:
                        if self.listing_http.robot.can_fetch(self.listing_http.agent, page_url):
                            upsert_listing(self.conn,page_url,run_id=row['discovered_run_id'],source_sitemap=row['source_sitemap'])
                        else:
                            self.cat_counts['pagination_excluded'] += 1
                    self.conn.execute("UPDATE listing_pages SET status='done',scanned_at=?,attempts=attempts+1,error=NULL,recipes_found=?,skipped_query_links=?,etag=COALESCE(?,etag),http_last_modified=COALESCE(?,http_last_modified) WHERE url=?", (stamp,len(recipes),skipped,meta.get('etag') or meta.get('ETag'),meta.get('last-modified') or meta.get('Last-Modified'),url))
                    self.cat_counts['done'] += 1
                    self.cat_counts['recipe_urls_new'] += added
                    if added:
                        self.new_recipes.set()
            else:
                raise RuntimeError(f'Category HTTP {code}')
        except CrawlBlocked:
            raise
        except Exception as exc:
            attempts = row['attempts']+1
            state = 'failed' if attempts >= self.max_attempts else 'retry'
            retry_at = (datetime.now(timezone.utc)+timedelta(hours=min(24,2**max(0,attempts-1)))).isoformat(timespec='seconds') if state == 'retry' else None
            self.conn.execute('UPDATE listing_pages SET status=?,attempts=attempts+1,error=?,retry_at=? WHERE url=?', (state,str(exc)[:700],retry_at,url))
            self.cat_counts[state] += 1
            print(f'CATEGORY {state.upper()}: {url}: {str(exc)[:120]}', file=sys.stderr, flush=True)

    async def categories(self):
        if self.listing_http is None:
            self.category_done.set()
            return
        self._category_workers_active += 1
        try:
            while not self.category_limit or self.cat_counts['attempted'] < self.category_limit:
                row = self.conn.execute("SELECT * FROM listing_pages WHERE status='pending' ORDER BY first_seen,url LIMIT 1").fetchone()
                if row is None:
                    row = self.conn.execute("SELECT * FROM listing_pages WHERE status='retry' AND retry_at<=? ORDER BY retry_at,url LIMIT 1", (now(),)).fetchone()
                if row is None:
                    if self.listing_sitemaps_done.is_set():
                        break
                    await asyncio.sleep(.25)
                    continue
                self.conn.execute("UPDATE listing_pages SET status='in_progress' WHERE url=?",(row['url'],))
                await self._category_once(row)
                self.cat_counts['attempted'] += 1
                self.checkpoint()
                self.log('categories', self.cat_counts['attempted'])
        finally:
            self._category_workers_active -= 1
            if self._category_workers_active==0:
                self.category_done.set()
                self.new_recipes.set()

    async def _recipe_once(self, row):
        url = row['url']
        stamp = now()
        try:
            conditions = {}
            if row['etag']:
                conditions['If-None-Match'] = row['etag']
            if row['http_last_modified']:
                conditions['If-Modified-Since'] = row['http_last_modified']
            code, data, headers = await self.recipe_http.get(url, conditional_headers=conditions)
            if code == 304:
                if find_saved(self.conn,url):
                    self.conn.execute("UPDATE urls SET status='done',attempts=attempts+1,error=NULL,fetched_at=?,retry_at=NULL WHERE url=?",(stamp,url))
                    self.recipe_counts['not_modified'] += 1
                else:
                    self.conn.execute("UPDATE urls SET status='review',etag=NULL,http_last_modified=NULL,error='304 without saved recipe' WHERE url=?",(url,))
                    self.recipe_counts['review'] += 1
            elif code in (404,410):
                mark_missing(self.conn,row,code,stamp)
                self.recipe_counts['temporarily_missing']+=1
            else:
                html = data.decode('utf-8', errors='replace')
                if is_access_challenge(html):
                    raise CrawlBlocked('HTML access challenge; stopping')
                recipe = await asyncio.to_thread(parse, html, url, include_images=False)
                quality=assess(recipe)
                if quality['status']!='quality_passed' and self.browser is not None:
                    html = await self.browser.fetch_html(url)
                    if is_access_challenge(html):
                        raise CrawlBlocked('Browser returned access challenge')
                    recipe = await asyncio.to_thread(parse, html, url, include_images=False)
                    self.recipe_counts['browser_fallback'] += 1
                result=persist(self.conn,url,headers,recipe,stamp)
                self.recipe_counts[result['status']]+=1
                if result['status']!='review':
                    self.recipe_counts['done']+=1
                    if result['canonical']!=url:self.recipe_counts['redirect_aliases']+=1
                    if self.follow_related:
                        for related in recipe.get('related_recipes',[]):
                            related_url=normalize_url(related.get('url',''))
                            if related_url and related_url!=result['canonical']:
                                if upsert_link(self.conn,related_url,source='related',origin=result['canonical'])=='new':
                                    self.recipe_counts['related_discovered']+=1
        except CrawlBlocked:
            raise
        except Exception as exc:
            attempts = row['attempts']+1
            state = 'failed' if attempts >= self.max_attempts else 'retry'
            retry_at = (datetime.now(timezone.utc)+timedelta(hours=min(24,2**max(0,attempts-1)))).isoformat(timespec='seconds') if state == 'retry' else None
            self.conn.execute("UPDATE urls SET status=?,attempts=attempts+1,error=?,retry_at=? WHERE url=?",(state,str(exc)[:700],retry_at,url))
            self.recipe_counts[state] += 1
            print(f'RECIPE {state.upper()}: {url}: {str(exc)[:120]}',file=sys.stderr,flush=True)

    async def recipes(self):
        while not self.recipe_limit or self.recipe_counts['attempted'] < self.recipe_limit:
            row = next_pending(self.conn)
            if row is None:
                if self.category_done.is_set() and self.recipe_sitemaps_done.is_set():
                    break
                self.new_recipes.clear()
                try:
                    await asyncio.wait_for(self.new_recipes.wait(), timeout=3)
                except asyncio.TimeoutError:
                    pass
                continue
            self.conn.execute("UPDATE urls SET status='in_progress' WHERE url=?",(row['url'],))
            await self._recipe_once(row)
            self.recipe_counts['attempted'] += 1
            self.checkpoint()
            self.log('recipes', self.recipe_counts['attempted'])

    async def run(self, *, discover_stream=False, recipe_sitemaps=None, category_sitemaps=None,
                  max_sitemaps=300, refresh_days=90):
        print('PIPELINE START: category and recipe collection overlap; one writer, separate host pacing '
              f'workers_per_host={self.workers} '
              f'recipe_delay={getattr(self.recipe_http, "base_delay", "unknown")} '
              f'listing_delay={getattr(self.listing_http, "base_delay", "unknown") if self.listing_http else "off"} '
              f'http_retries={getattr(self.recipe_http, "max_retries", "unknown")}', flush=True)
        category_workers = self.workers if not self.category_limit else 1
        recipe_workers = self.workers if not self.recipe_limit else 1
        category_jobs=[asyncio.create_task(self.categories()) for _ in range(category_workers)]
        recipe_jobs=[asyncio.create_task(self.recipes()) for _ in range(recipe_workers)]
        jobs=category_jobs+recipe_jobs
        if discover_stream:
            from async_discovery import produce_sitemaps
            self.recipe_sitemaps_done.clear()
            async def produce_recipes():
                try:
                    return await produce_sitemaps(self.conn,self.recipe_http,kind='recipe',seeds=recipe_sitemaps,
                        max_sitemaps=max_sitemaps,refresh_days=refresh_days,on_new=self.new_recipes)
                finally:
                    self.recipe_sitemaps_done.set()
                    self.new_recipes.set()
            jobs.insert(0,asyncio.create_task(produce_recipes()))
            if self.listing_http is not None:
                self.listing_sitemaps_done.clear()
                async def produce_listings():
                    try:
                        return await produce_sitemaps(self.conn,self.listing_http,kind='listing',seeds=category_sitemaps,
                            max_sitemaps=max_sitemaps,refresh_days=refresh_days,on_new=self.new_recipes)
                    finally:
                        self.listing_sitemaps_done.set()
                jobs.insert(1,asyncio.create_task(produce_listings()))
        try:
            await asyncio.gather(*jobs)
        except BaseException:
            # Stop the other host as soon as any host is blocked or interrupted.
            for job in jobs:
                if not job.done():
                    job.cancel()
            await asyncio.gather(*jobs, return_exceptions=True)
            raise
        finally:
            self.conn.execute("UPDATE urls SET status='pending' WHERE status='in_progress'")
            self.conn.execute("UPDATE listing_pages SET status='pending' WHERE status='in_progress'")
            self.conn.commit()
            self.dirty = 0
            if self.browser is not None:
                await self.browser.close()
            await self.recipe_http.close()
            if self.listing_http is not None:
                await self.listing_http.close()
        return {'categories':dict(self.cat_counts),'recipes':dict(self.recipe_counts),
                'http': {'recipe': dict(getattr(self.recipe_http, 'metrics', {})),
                         'listing': dict(getattr(self.listing_http, 'metrics', {})) if self.listing_http else {}}}


async def run_pipeline(conn, recipe_checked, listing_checked=None, *, discover_stream=False,
                       recipe_sitemaps=None,category_sitemaps=None,max_sitemaps=300,
                       refresh_days=90,http_retries=2,max_throttles=3,**kwargs):
    recipe_http = AsyncPoliteHttp(recipe_checked, max_retries=http_retries,
                                  max_throttles=max_throttles)
    listing_http = (AsyncPoliteHttp(listing_checked, max_retries=http_retries,
                                    max_throttles=max_throttles) if listing_checked else None)
    return await Pipeline(conn,recipe_http,listing_http,**kwargs).run(
        discover_stream=discover_stream,recipe_sitemaps=recipe_sitemaps,
        category_sitemaps=category_sitemaps,max_sitemaps=max_sitemaps,
        refresh_days=refresh_days)
