"""Discovery from permitted GialloZafferano listing pages (separate from recipes).

Two-phase: sitemap discovery -> persistent listing queue -> scan listing cards.
Only canonical recipe URLs feed the existing recipe queue. No query-string
navigation or paths excluded by robots.txt are requested.
"""
from __future__ import annotations

from collections import Counter, deque
from datetime import datetime, timezone, timedelta
import gzip
import io
import json
import sys
from urllib.parse import urljoin, urlsplit
import uuid

from bs4 import BeautifulSoup

WEB_HOST = 'www.giallozafferano.it'
WEB_BASE = 'https://' + WEB_HOST
DEFAULT_CATEGORY_SITEMAPS = [
    WEB_BASE + '/sitemap/portate.xml',
    WEB_BASE + '/sitemap/ricerche.xml',
]


def current_time():
    return datetime.now(timezone.utc).isoformat(timespec='seconds')


def upsert_listing(conn, url, lastmod=None, refresh_days=45, run_id=None, source_sitemap=None):
    stamp = current_time()
    row = conn.execute('SELECT lastmod, status, scanned_at FROM listing_pages WHERE url=?', (url,)).fetchone()
    if not row:
        conn.execute('''INSERT INTO listing_pages
            (url,lastmod,status,first_seen,last_seen,discovered_run_id,source_sitemap)
            VALUES(?,?,?,?,?,?,?)''',
            (url,lastmod,'pending',stamp,stamp,run_id,source_sitemap))
        return 'new'
    effective = max(row['lastmod'],lastmod) if row['lastmod'] and lastmod else (row['lastmod'] or lastmod)
    updated = bool(row['lastmod'] and effective and effective > row['lastmod'])
    expired = False
    if row['scanned_at'] and refresh_days > 0:
        try:
            expired = (datetime.fromisoformat(stamp) - datetime.fromisoformat(row['scanned_at'])).total_seconds() >= refresh_days*86400
        except (TypeError,ValueError):
            expired = True
    refreshed = updated or expired
    conn.execute('''UPDATE listing_pages SET lastmod=?,last_seen=?,discovered_run_id=COALESCE(?,discovered_run_id),
        status=CASE WHEN ? THEN 'pending' ELSE status END,
        retry_at=CASE WHEN ? THEN NULL ELSE retry_at END,
        source_sitemap=COALESCE(source_sitemap,?) WHERE url=?''',
        (effective,stamp,run_id,int(refreshed),int(refreshed),source_sitemap,url))
    return 'refreshed' if refreshed else 'existing'


def listing_links(html, page_url, *, include_skip_count=False):
    """Extract recipe cards and safe, queryless *listing* pagination links.

    Deliberately no catch-all scan of every anchor on the page: header/footer and
    editorials would inflate coverage with false positives. JSON-LD ItemList is
    an additional structured source for sites with revised card markup.
    """
    from crawler import normalize_url
    soup = BeautifulSoup(html, 'lxml')
    recipes = set()
    pages = set()
    skipped_query_pages = set()
    selectors = ('article.gz-card', '.gz-card[data-recipeid]',
                 '[data-ets-tipo="R"]', '[itemtype*="schema.org/Recipe"]',
                 '.gz-card-suggested')
    for item in soup.select(','.join(selectors)):
        for a in item.select('a[href]'):
            normalized = normalize_url(urljoin(page_url, a['href']), 'recipe')
            if normalized:
                recipes.add(normalized)

    # Some listing layouts have ItemList JSON-LD without a recognizable card.
    import json as _json
    def walk(node):
        if isinstance(node, dict):
            yield node
            for child in node.values():
                yield from walk(child)
        elif isinstance(node, list):
            for child in node:
                yield from walk(child)
    for node in soup.select('script[type="application/ld+json"]'):
        try:
            data = _json.loads(node.get_text() or '')
        except (ValueError,TypeError):
            continue
        for obj in walk(data):
            value = obj.get('url') or obj.get('item')
            if isinstance(value,dict):
                value = value.get('@id') or value.get('url')
            if isinstance(value,str):
                r = normalize_url(urljoin(page_url,value),'recipe')
                if r:
                    recipes.add(r)

    for a in soup.select('a[rel="next"], nav[aria-label*="agin"] a[href], .gz-pagination a[href], .pagination a[href], .gz-pages a[href]'):
        href = a.get('href')
        if not href:
            continue
        target = normalize_url(urljoin(page_url,href),'listing')
        if target and target != page_url:
            pages.add(target)
        elif not target:
            parsed = urlsplit(urljoin(page_url,href))
            if parsed.query and parsed.hostname == WEB_HOST and (
                parsed.path.startswith('/ricette-cat/') or parsed.path.startswith('/ricette-con') or
                parsed.path.startswith('/ricerca-ricette/')):
                skipped_query_pages.add(parsed.path + '?' + parsed.query)
    return (recipes,pages,len(skipped_query_pages)) if include_skip_count else (recipes,pages)


def discover_categories(conn, http, *, seeds=None, refresh_days=45, max_sitemaps=300):
    """Read only public www category/search sitemap files with atomic inserts."""
    from crawler import normalize_url,sitemap_items,MAX_XML_BYTES,CrawlBlocked
    counter = Counter()
    run_id = uuid.uuid4().hex
    conn.execute('INSERT INTO category_discovery_runs(id,started_at) VALUES(?,?)',(run_id,current_time()))
    conn.commit()
    try:
        if seeds is None:
            policy = http.initialize_robots()
            declared = [u for u in policy.site_maps() if urlsplit(u).hostname==WEB_HOST
                        and any(name in urlsplit(u).path.lower() for name in ('portate','ricerche'))]
            seeds = declared or DEFAULT_CATEGORY_SITEMAPS
        paths = list(dict.fromkeys(normalize_url(u,'sitemap') for u in seeds))
        if not paths or any(not x or urlsplit(x).hostname != WEB_HOST for x in paths):
            raise ValueError('Only www sitemap XML files are valid category seeds')
    except Exception as exc:
        conn.execute("UPDATE category_discovery_runs SET finished_at=?,sitemap_errors=1,status='partial' WHERE id=?",(current_time(),run_id))
        conn.execute('INSERT INTO category_run_sitemaps(run_id,url,error) VALUES(?,?,?)',(run_id,http.robot_url if hasattr(http,'robot_url') else WEB_BASE+'/robots.txt',str(exc)[:700]))
        conn.commit()
        raise
    queue = deque(paths)
    checked=set()
    blocker=None
    while queue:
        sitemap=queue.popleft()
        if sitemap in checked:
            continue
        if len(checked)>=max_sitemaps:
            counter['errors']+=1
            blocker=RuntimeError(f'Category sitemap limit {max_sitemaps} reached')
            break
        checked.add(sitemap)
        print(f'CATEGORY SITEMAP FETCH {len(checked)}: {sitemap}', flush=True)
        try:
            code,data,_ = http.get(sitemap,accept='application/xml,text/xml,*/*')
            if code!=200:
                raise RuntimeError(f'HTTP {code}')
            if sitemap.endswith('.gz') and data.startswith(b'\x1f\x8b'):
                with gzip.GzipFile(fileobj=io.BytesIO(data)) as buf:
                    data=buf.read(MAX_XML_BYTES+1)
            if len(data)>MAX_XML_BYTES:
                raise RuntimeError('Category XML exceeds size limit')
            children=[]
            local=Counter()
            entries=0
            conn.execute('SAVEPOINT category_sitemap')
            try:
                for kind,link,lm in sitemap_items(data):
                    if kind=='sitemap':
                        url=normalize_url(link,'sitemap')
                        if url and urlsplit(url).hostname==WEB_HOST:
                            children.append(url)
                    else:
                        url=normalize_url(link,'listing')
                        if url:
                            local[upsert_listing(conn,url,lm,refresh_days,run_id,sitemap)]+=1
                            entries+=1
                conn.execute('RELEASE SAVEPOINT category_sitemap')
            except Exception:
                conn.execute('ROLLBACK TO SAVEPOINT category_sitemap')
                conn.execute('RELEASE SAVEPOINT category_sitemap')
                raise
            queue.extend(children)
            counter.update(local)
            conn.execute('INSERT INTO category_run_sitemaps(run_id,url,listings) VALUES(?,?,?)',(run_id,sitemap,entries))
            conn.commit()
            print(f'CATEGORY SITEMAP {sitemap}: {entries} listing URLs',flush=True)
        except Exception as exc:
            conn.execute('INSERT INTO category_run_sitemaps(run_id,url,error) VALUES(?,?,?)',(run_id,sitemap,str(exc)[:700]))
            conn.commit()
            counter['errors']+=1
            print(f'CATEGORY SITEMAP ERROR {sitemap}: {exc}',file=sys.stderr,flush=True)
            if isinstance(exc,CrawlBlocked):
                blocker=exc
                break
    count=conn.execute('SELECT COUNT(*) FROM listing_pages WHERE discovered_run_id=?',(run_id,)).fetchone()[0]
    status='complete' if checked and not counter['errors'] and not blocker and count>0 else 'partial'
    conn.execute('''UPDATE category_discovery_runs SET finished_at=?,sitemap_count=?,sitemap_errors=?,listing_count=?,status=? WHERE id=?''',
                 (current_time(),len(checked),counter['errors'] or int(bool(blocker)),count,status,run_id))
    conn.commit()
    counter['sitemaps']=len(checked)
    counter['unique_listing_urls']=count
    counter['discovery_complete']=status=='complete'
    if blocker:
        raise blocker
    return counter


def scan_categories(conn, http, *, limit=25, batch_size=25, max_attempts=3):
    """Populate recipe URLs from category cards, with resumable listing queue."""
    from crawler import CrawlBlocked,normalize_url,upsert_link,is_access_challenge
    if limit<0 or batch_size<1 or max_attempts<1:
        raise ValueError('Invalid category scan limits')
    if http.robot is None:
        http.initialize_robots()
    count=Counter()
    dirty=0
    print(f'CATEGORY SCAN START: limit={"ALL" if limit == 0 else limit}, minimum_delay={getattr(http, "delay", "configured")}s/request', flush=True)
    try:
        while limit==0 or count['attempted']<limit:
            row=conn.execute('''SELECT * FROM listing_pages WHERE status='pending' ORDER BY first_seen,url LIMIT 1''').fetchone()
            if row is None:
                row=conn.execute('''SELECT * FROM listing_pages WHERE status='retry' AND retry_at<=? ORDER BY retry_at,url LIMIT 1''',(current_time(),)).fetchone()
            if row is None:
                break
            url=row['url']
            count['attempted']+=1
            stamp=current_time()
            print(f'CATEGORY FETCH {count["attempted"]}/{"ALL" if limit == 0 else limit}: {url}', flush=True)
            if not http.robot.can_fetch(http.agent,url):
                conn.execute("UPDATE listing_pages SET status='excluded',error='Disallowed by robots.txt',scanned_at=? WHERE url=?",(stamp,url))
                count['excluded']+=1
                dirty+=1
                print(f'CATEGORY EXCLUDED {count["attempted"]}: {url}', flush=True)
                if dirty>=batch_size:
                    conn.commit()
                    dirty=0
                continue
            try:
                conditional={}
                if row['etag']:conditional['If-None-Match']=row['etag']
                if row['http_last_modified']:conditional['If-Modified-Since']=row['http_last_modified']
                opts={'accept':'text/html,application/xhtml+xml','max_bytes':8*1024*1024}
                if conditional:opts['conditional_headers']=conditional
                code,data,headers=http.get(url,**opts)
                if code==304:
                    if row['scanned_at'] and row['recipes_found']>0:
                        conn.execute("UPDATE listing_pages SET status='done',scanned_at=?,attempts=attempts+1,error=NULL WHERE url=?",(stamp,url))
                        count['not_modified']+=1
                    else:
                        conn.execute("UPDATE listing_pages SET status='review',attempts=attempts+1,error='304 without saved listing' WHERE url=?",(url,))
                        count['review']+=1
                elif code in (404,410):
                    conn.execute("UPDATE listing_pages SET status='review',scanned_at=?,attempts=attempts+1,error=? WHERE url=?",(stamp,f'HTTP {code}: not yet verified',url))
                    count['review']+=1
                    print(f'CATEGORY REVIEW {count["attempted"]}: HTTP {code}', flush=True)
                elif code==200:
                    html=data.decode('utf-8',errors='replace')
                    if is_access_challenge(html):
                        raise CrawlBlocked('Category page returned an access challenge; stopping.')
                    recipes,pages,skipped=listing_links(html,url,include_skip_count=True)
                    if not recipes:
                        # Do not silently approve a listing that might have changed layout.
                        conn.execute("UPDATE listing_pages SET status='review',attempts=attempts+1,error='No recipe cards or structured recipe links detected',scanned_at=? WHERE url=?",(stamp,url))
                        count['review']+=1
                        print(f'CATEGORY REVIEW {count["attempted"]}: no recipe links found', flush=True)
                    else:
                        for recipe in recipes:
                            result=upsert_link(conn,recipe,source='category',origin=url)
                            if result=='new':
                                count['recipe_urls_new']+=1
                        for page in pages:
                            if not http.robot.can_fetch(http.agent,page):
                                count['pagination_excluded']+=1
                            else:
                                result=upsert_listing(conn,page,run_id=row['discovered_run_id'],source_sitemap=row['source_sitemap'])
                                if result=='new':
                                    count['pagination_new']+=1
                        conn.execute("UPDATE listing_pages SET status='done',scanned_at=?,attempts=attempts+1,error=NULL,recipes_found=?,skipped_query_links=?,etag=COALESCE(?,etag),http_last_modified=COALESCE(?,http_last_modified) WHERE url=?",(stamp,len(recipes),skipped,headers.get('ETag') or headers.get('etag'),headers.get('Last-Modified') or headers.get('last-modified'),url))
                        count['query_links_skipped']+=skipped
                        count['done']+=1
                        count['recipe_urls_seen']+=len(recipes)
                        print(f'CATEGORY OK {count["attempted"]}: links={len(recipes)}, new={count["recipe_urls_new"]}, done={count["done"]}, pending_commit={dirty+1}', flush=True)
                else:
                    raise RuntimeError(f'Category HTTP {code}')
                dirty+=1
            except CrawlBlocked:
                raise
            except Exception as exc:
                attempts=row['attempts']+1
                state='failed' if attempts>=max_attempts else 'retry'
                retry_time = (datetime.now(timezone.utc)+timedelta(hours=min(24,2**max(0,attempts-1)))).isoformat(timespec='seconds') if state=='retry' else None
                conn.execute('UPDATE listing_pages SET status=?,attempts=attempts+1,error=?,retry_at=? WHERE url=?',(state,str(exc)[:700],retry_time,url))
                count[state]+=1
                print(f'CATEGORY {state.upper()} {count["attempted"]}: {str(exc)[:140]}', file=sys.stderr, flush=True)
                dirty+=1
            if dirty>=batch_size:
                conn.commit()
                dirty=0
    finally:
        if dirty:
            conn.commit()
            print(f'CATEGORY CHECKPOINT: committed {dirty} latest listing pages', flush=True)
    print(f'CATEGORY SCAN COMPLETE: {dict(count)}', flush=True)
    return count
