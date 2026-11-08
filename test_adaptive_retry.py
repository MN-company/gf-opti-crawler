"""Offline regression tests for bounded retry and shared per-host pacing."""
import asyncio
from datetime import datetime, timedelta, timezone
from email.utils import format_datetime
import time
import unittest

import httpx

from async_pipeline import AsyncBrowserFallback, AsyncPoliteHttp
from crawler import BASE, CrawlBlocked, PoliteHttp, RobotsPolicy

URL = BASE + '/Lasagne-al-forno.html'


class AdaptiveRetryTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.clients = []

    async def asyncTearDown(self):
        for client in self.clients:
            await client.aclose()

    def make_worker(self, respond, *, delay=.01, retries=2, max_throttles=3,
                    retry_base=.001, recovery_successes=25):
        checked = PoliteHttp(user_agent='TestArchive/1.0', delay=2)
        checked.robot = RobotsPolicy('User-agent: *\nAllow: /\n')
        checked.delay = delay  # Tests only: avoid real-time sleeps.
        checked.last_request = time.monotonic() - 5
        client = httpx.AsyncClient(transport=httpx.MockTransport(respond))
        self.clients.append(client)
        return AsyncPoliteHttp(checked, client=client, max_retries=retries,
                               max_throttles=max_throttles, retry_base=retry_base,
                               recovery_successes=recovery_successes)

    async def test_retry_after_delta_and_http_date(self):
        self.assertEqual(AsyncPoliteHttp.parse_retry_after('120'), 120)
        self.assertEqual(AsyncPoliteHttp.parse_retry_after('0'), 0)
        self.assertIsNone(AsyncPoliteHttp.parse_retry_after('not a date'))
        value = format_datetime(datetime.now(timezone.utc) + timedelta(seconds=12), usegmt=True)
        parsed = AsyncPoliteHttp.parse_retry_after(value)
        self.assertGreater(parsed, 10)
        self.assertLessEqual(parsed, 12)

    async def test_429_then_success_honors_shared_pause(self):
        starts = []
        async def respond(request):
            starts.append(time.monotonic())
            if len(starts) == 1:
                return httpx.Response(429, headers={'Retry-After': '0'})
            return httpx.Response(200, text='ok')
        worker = self.make_worker(respond, delay=.025)
        status, body, _ = await worker.get(URL)
        self.assertEqual((status, body), (200, b'ok'))
        self.assertEqual(len(starts), 2)
        self.assertGreaterEqual(starts[1] - starts[0], .045)
        self.assertGreater(worker.delay, worker.base_delay)

    async def test_503_then_success(self):
        seen = []
        async def respond(request):
            seen.append(1)
            return httpx.Response(503, headers={'Retry-After': '0'}) if len(seen) == 1 else httpx.Response(200, text='ok')
        worker = self.make_worker(respond)
        self.assertEqual((await worker.get(URL))[0], 200)
        self.assertEqual(len(seen), 2)

    async def test_403_never_retried(self):
        seen = []
        def respond(request):
            seen.append(1)
            return httpx.Response(403)
        worker = self.make_worker(respond)
        with self.assertRaisesRegex(CrawlBlocked, 'access denied'):
            await worker.get(URL)
        self.assertEqual(len(seen), 1)

    async def test_repeated_429_opens_circuit_breaker(self):
        seen = []
        def respond(request):
            seen.append(1)
            return httpx.Response(429, headers={'Retry-After': '0'})
        worker = self.make_worker(respond, delay=.005)
        with self.assertRaisesRegex(CrawlBlocked, 'circuit breaker'):
            await worker.get(URL)
        self.assertEqual(len(seen), 3)
        self.assertEqual(worker.throttle_strikes, 3)

    async def test_502_transient_retries_but_not_indefinitely(self):
        seen = []
        def respond(request):
            seen.append(1)
            return httpx.Response(502)
        worker = self.make_worker(respond, retries=2)
        with self.assertRaisesRegex(RuntimeError, 'after 3 attempts'):
            await worker.get(URL)
        self.assertEqual(len(seen), 3)

    async def test_network_error_retries(self):
        seen = []
        def respond(request):
            seen.append(1)
            if len(seen) == 1:
                raise httpx.ConnectTimeout('temporary', request=request)
            return httpx.Response(200, text='recovered')
        worker = self.make_worker(respond)
        self.assertEqual((await worker.get(URL))[1], b'recovered')
        self.assertEqual(len(seen), 2)

    async def test_four_workers_share_one_host_pacing(self):
        starts = []
        async def respond(request):
            starts.append(time.monotonic())
            await asyncio.sleep(.01)
            return httpx.Response(200, text='ok')
        worker = self.make_worker(respond, delay=.025)
        responses = await asyncio.gather(*(worker.get(URL) for _ in range(4)))
        self.assertEqual(len(responses), 4)
        self.assertTrue(all(r[0] == 200 for r in responses))
        self.assertTrue(all(b-a >= .020 for a,b in zip(starts, starts[1:])), starts)

    async def test_shared_cooldown_holds_another_worker(self):
        starts = []
        async def respond(request):
            starts.append(time.monotonic())
            if len(starts) == 1:
                return httpx.Response(429, headers={'Retry-After': '0'})
            return httpx.Response(200, text='ok')
        worker = self.make_worker(respond, delay=.03)
        first = asyncio.create_task(worker.get(URL))
        await asyncio.sleep(.005)
        second = asyncio.create_task(worker.get(URL))
        await asyncio.gather(first, second)
        self.assertEqual(len(starts), 3)
        self.assertGreaterEqual(starts[1] - starts[0], .05)

    async def test_browser_429_retries_with_shared_cooldown(self):
        worker = self.make_worker(lambda _: httpx.Response(200), delay=.005)
        calls = []
        class Response:
            def __init__(self, status):
                self.status = status
                self.headers = {'retry-after': '0'}
        class Page:
            async def goto(self, url, **kwargs):
                calls.append('goto')
                return Response(429 if len(calls) == 1 else 200)
            async def content(self):
                return '<html>recovered</html>'
            async def close(self):
                calls.append('close')
        class Context:
            async def new_page(self):
                return Page()
        fallback = AsyncBrowserFallback(worker)
        fallback.context = Context()
        result = await fallback.fetch_html(URL)
        self.assertEqual(result, '<html>recovered</html>')
        self.assertEqual(calls.count('goto'), 2)
        self.assertEqual(worker.metrics['http_429'], 1)

    async def test_browser_subrequests_use_shared_pacing(self):
        worker = self.make_worker(lambda _: httpx.Response(200, text='ok'))
        calls = []
        async def reserve():
            calls.append('reserved')
        worker.reserve = reserve
        fallback = AsyncBrowserFallback(worker)
        class Request:
            url = URL
            resource_type = 'script'
        class Route:
            request = Request()
            async def continue_(self):
                calls.append('continued')
            async def abort(self):
                calls.append('aborted')
        await fallback._route(Route())
        self.assertEqual(calls, ['reserved','continued'])
        calls.clear()
        Request.resource_type = 'document'
        await fallback._route(Route())
        self.assertEqual(calls, ['reserved','continued'])  # Unknown document frame: pace safely

    async def test_sustained_success_restores_base_delay(self):
        worker = self.make_worker(lambda _: httpx.Response(200, text='ok'),
                                  delay=.005, recovery_successes=2)
        await worker.note_throttle(429, '0')
        self.assertGreater(worker.delay, worker.base_delay)
        await worker.note_success()
        await worker.note_success()
        self.assertEqual(worker.delay, worker.base_delay)
        self.assertEqual(worker.throttle_strikes, 0)


if __name__ == '__main__':
    unittest.main()
