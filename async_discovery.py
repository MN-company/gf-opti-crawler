"""Progressive sitemap producers sharing the pipeline's rate-limited HTTP clients.

All sitemap writes are atomic per file. Producers can discover URLs while workers
extract them. No other database worker or second process is required.
"""
from __future__ import annotations
from collections import Counter, deque
from datetime import datetime, timezone
import asyncio
import gzip
import io
import sys
import uuid
from urllib.parse import urlsplit
from crawler import (
    BASE, WEB_HOST, DEFAULT_SITEMAPS, MAX_XML_BYTES, normalize_url,
    sitemap_items, upsert_link, now, CrawlBlocked,
)
from catalog_discovery import DEFAULT_CATEGORY_SITEMAPS, upsert_listing


async def produce_sitemaps(conn,http,*,kind='recipe',seeds=None,max_sitemaps=300,refresh_days=90,on_new=None):
    if kind not in ('recipe','listing'):
        raise ValueError('invalid sitemap producer')
    is_recipe=kind=='recipe'
    table='discovery_runs' if is_recipe else 'category_discovery_runs'
    scans='discovery_run_sitemaps' if is_recipe else 'category_run_sitemaps'
    run_id=uuid.uuid4().hex
    conn.execute(f'INSERT INTO {table}(id,started_at) VALUES(?,?)',(run_id,now()))
    conn.commit()
    if seeds is None:
        published=http.robot.site_maps() or []
        if is_recipe:
            declared=[u for u in published if urlsplit(u).hostname==http.host and
                      any(token in urlsplit(u).path.lower() for token in ('ricette','updated'))]
            seeds=declared or DEFAULT_SITEMAPS
        else:
            declared=[u for u in published if urlsplit(u).hostname==WEB_HOST and
                      any(token in urlsplit(u).path.lower() for token in ('portate','ricerche'))]
            seeds=declared or DEFAULT_CATEGORY_SITEMAPS
    queue=deque(normalize_url(u,'sitemap') for u in seeds)
    checked=set()
    counts=Counter()
    blocker=None
    while queue:
        sitemap=queue.popleft()
        if not sitemap or sitemap in checked:
            continue
        if urlsplit(sitemap).hostname!=http.host:
            counts['errors']+=1;continue
        if len(checked)>=max_sitemaps:
            counts['errors']+=1;blocker=RuntimeError(f'Sitemap cap {max_sitemaps} reached');break
        checked.add(sitemap)
        print(f'{kind.upper()} SITEMAP FETCH {len(checked)}: {sitemap}',flush=True)
        try:
            code,data,_=await http.get(sitemap,accept='application/xml,text/xml,*/*',max_bytes=MAX_XML_BYTES)
            if code!=200:
                raise RuntimeError(f'Sitemap HTTP {code}')
            if sitemap.endswith('.gz') and data.startswith(b'\x1f\x8b'):
                with gzip.GzipFile(fileobj=io.BytesIO(data)) as gz:
                    data=gz.read(MAX_XML_BYTES+1)
            if len(data)>MAX_XML_BYTES:
                raise RuntimeError('Sitemap expanded beyond limit')
            children=[]
            added=0
            conn.execute('SAVEPOINT progressive_sitemap')
            try:
                for typ,link,lastmod in sitemap_items(data):
                    if typ=='sitemap':
                        url=normalize_url(link,'sitemap')
                        if url and urlsplit(url).hostname==http.host:
                            children.append(url)
                    else:
                        url=normalize_url(link,kind)
                        if url:
                            if is_recipe:
                                result=upsert_link(conn,url,lastmod,refresh_days=refresh_days,
                                                   run_id=run_id,origin=sitemap)
                            else:
                                result=upsert_listing(conn,url,lastmod,refresh_days=refresh_days,
                                                      run_id=run_id,source_sitemap=sitemap)
                            added+=1
                conn.execute('RELEASE SAVEPOINT progressive_sitemap')
            except BaseException:
                conn.execute('ROLLBACK TO SAVEPOINT progressive_sitemap')
                conn.execute('RELEASE SAVEPOINT progressive_sitemap')
                raise
            queue.extend(children)
            if is_recipe:
                conn.execute('INSERT INTO discovery_run_sitemaps(run_id,url,discovered,error) VALUES(?,?,?,NULL)',
                             (run_id,sitemap,added))
                conn.execute('INSERT OR REPLACE INTO sitemap_scans(url,fetched_at,discovered,error) VALUES(?,?,?,NULL)',
                             (sitemap,now(),added))
            else:
                conn.execute('INSERT INTO category_run_sitemaps(run_id,url,listings,error) VALUES(?,?,?,NULL)',
                             (run_id,sitemap,added))
            conn.commit()
            counts['listed']+=added
            if added and on_new:
                on_new.set()
            print(f'{kind.upper()} SITEMAP OK {len(checked)}: {added} URLs',flush=True)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            error=str(exc)[:700]
            counts['errors']+=1
            if is_recipe:
                conn.execute('INSERT INTO discovery_run_sitemaps(run_id,url,error) VALUES(?,?,?)',(run_id,sitemap,error))
                conn.execute('INSERT OR REPLACE INTO sitemap_scans(url,fetched_at,discovered,error) VALUES(?,?,0,?)',
                             (sitemap,now(),error))
            else:
                conn.execute('INSERT INTO category_run_sitemaps(run_id,url,error) VALUES(?,?,?)',(run_id,sitemap,error))
            conn.commit()
            print(f'{kind.upper()} SITEMAP ERROR: {sitemap}: {error}',file=sys.stderr,flush=True)
            if isinstance(exc,CrawlBlocked):
                blocker=exc
                break
    unique=conn.execute(f'SELECT COUNT(*) FROM {"urls" if is_recipe else "listing_pages"} WHERE discovered_run_id=?',
                        (run_id,)).fetchone()[0]
    status='complete' if checked and not counts['errors'] and unique>0 else 'partial'
    if is_recipe:
        conn.execute('''UPDATE discovery_runs SET finished_at=?,total_sitemaps=?,failed_sitemaps=?,
                     discovered_urls=?,status=? WHERE id=?''',(now(),len(checked),counts['errors'],unique,status,run_id))
    else:
        conn.execute('''UPDATE category_discovery_runs SET finished_at=?,sitemap_count=?,sitemap_errors=?,
                     listing_count=?,status=? WHERE id=?''',(now(),len(checked),counts['errors'],unique,status,run_id))
    conn.commit()
    counts.update({'sitemaps':len(checked),'unique':unique,'discovery_complete':status=='complete'})
    if blocker:
        raise blocker
    return dict(counts)
