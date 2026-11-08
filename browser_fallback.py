"""Optional Playwright fallback, only for pages whose static HTML lacks a Recipe.

A single Chromium process is reused for the crawl. Resource blocking avoids media,
fonts and trackers; the browser never handles access-denied / rate-limited pages.
"""
from __future__ import annotations

from urllib.parse import urlsplit


class BrowserFallback:
    def __init__(self, timeout_seconds=25, host='ricette.giallozafferano.it', robots=None, user_agent=None):
        self.timeout_seconds = timeout_seconds
        self.host = host
        self.robots = robots
        self.user_agent = user_agent
        self._playwright = None
        self._browser = None
        self._context = None

    def __enter__(self):
        return self

    def _start(self):
        try:
            from playwright.sync_api import sync_playwright
        except ImportError as exc:
            raise RuntimeError('Playwright not installed; run `pip install -r requirements-browser.txt`') from exc
        self._playwright = sync_playwright().start()
        try:
            self._browser = self._playwright.chromium.launch(headless=True)
            self._context = self._browser.new_context(
                service_workers='block', accept_downloads=False,
                **({'user_agent': self.user_agent} if self.user_agent else {}),
            )
            self._context.route('**/*', self._route)
        except Exception:
            self.close()
            raise
        return self

    def _route(self, route):
        request = route.request
        parsed = urlsplit(request.url)
        # Restrict browser to the requested recipe domain, without sidecar requests.
        if (parsed.hostname != self.host or
            (self.robots and not self.robots.can_fetch(self.user_agent or '*', request.url)) or
            request.resource_type in (
            'image', 'media', 'font', 'stylesheet', 'websocket', 'eventsource'
        )):
            return route.abort()
        return route.continue_()

    def fetch_html(self, url):
        if urlsplit(url).hostname != self.host:
            raise ValueError('Browser navigation restricted to recipe domain')
        if self._context is None:
            self._start()
        page = self._context.new_page()
        try:
            response = page.goto(url, wait_until='domcontentloaded', timeout=int(self.timeout_seconds * 1000))
            if response is None:
                raise RuntimeError('Browser navigation has no response')
            status = response.status
            if status in (401, 403, 429) or status >= 500:
                raise RuntimeError(f'Browser navigation stopped at HTTP {status}')
            if status != 200:
                raise RuntimeError(f'Browser navigation failed with HTTP {status}')
            page.locator('script[type="application/ld+json"]').first.wait_for(timeout=3000)
            return page.content()
        finally:
            page.close()

    def close(self):
        if self._context:
            self._context.close()
            self._context = None
        if self._browser:
            self._browser.close()
            self._browser = None
        if self._playwright:
            self._playwright.stop()
            self._playwright = None

    def __exit__(self, exc_type, exc, tb):
        self.close()
