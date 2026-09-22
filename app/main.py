import asyncio
from collections import OrderedDict
from contextlib import asynccontextmanager
from pathlib import Path
import time
from urllib.parse import urljoin, urlsplit

import httpx
from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse

from app.sources import SOURCES

MAX_BODY = 2_000_000
CACHE_TTL = 300
cache = OrderedDict()
inflight = {}
slots = asyncio.Semaphore(4)


@asynccontextmanager
async def lifespan(app):
    async with httpx.AsyncClient(timeout=httpx.Timeout(12, connect=5),
                                limits=httpx.Limits(max_connections=4, max_keepalive_connections=4),
                                headers={'User-Agent': 'Mozilla/5.0 (compatible; LinkExtractor/1.0)'}) as client:
        app.state.client = client
        yield


app = FastAPI(title='Multi-source Embed Extractor', lifespan=lifespan)


class Page(str):
    def __new__(cls, text, url):
        obj = super().__new__(cls, text)
        obj.url = url
        return obj


async def fetch(url):
    # Only source pages are fetched, never third-party embeds or arbitrary URLs.
    allowed = set().union(*(s.HOSTS for s in SOURCES.values()))
    async with slots:
        for _ in range(4):
            p = urlsplit(url)
            if p.scheme != 'https' or p.hostname not in allowed or p.username or p.password or p.port not in (None, 443):
                raise ValueError('Unsafe upstream URL or redirect')
            async with app.state.client.stream('GET', url) as response:
                if response.is_redirect:
                    url = urljoin(url, response.headers['location'])
                    continue
                response.raise_for_status()
                chunks = bytearray()
                async for chunk in response.aiter_bytes():
                    chunks.extend(chunk)
                    if len(chunks) > MAX_BODY:
                        raise ValueError('Upstream page exceeds size limit')
                return Page(chunks.decode('utf-8', errors='replace'), str(response.url))
        raise ValueError('Too many upstream redirects')


@app.get('/', include_in_schema=False)
async def index():
    return FileResponse(Path(__file__).with_name('index.html'))


@app.get('/healthz')
async def health():
    return {'status': 'ok'}


@app.get('/api/sources')
async def sources():
    return {'sources': [{'name': name, 'hosts': sorted(s.HOSTS)} for name, s in SOURCES.items()]}


async def run(source, normalized):
    try:
        result = await asyncio.wait_for(source.extract(normalized, fetch), timeout=55)
        # Don't retain upstream outages or partially resolved results.
        if result['complete']:
            cache[normalized] = (time.monotonic(), result)
            cache.move_to_end(normalized)
            while len(cache) > 64:
                cache.popitem(last=False)
        return result
    finally:
        inflight.pop(normalized, None)


@app.get('/api/extract')
async def extract(url: str = Query(..., max_length=2048)):
    try:
        source = next((s for s in SOURCES.values() if urlsplit(url.strip()).hostname in s.HOSTS), None)
        if source is None:
            raise ValueError('Unsupported source. Currently supported: animexin.dev')
        normalized = source.normalize(url)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    cached = cache.get(normalized)
    if cached and time.monotonic() - cached[0] < CACHE_TTL:
        cache.move_to_end(normalized)
        return {**cached[1], 'cached': True}
    if normalized not in inflight:
        if len(inflight) >= 8:
            raise HTTPException(429, 'Extractor is busy; retry shortly.', headers={'Retry-After': '5'})
        inflight[normalized] = asyncio.create_task(run(source, normalized))
    try:
        return {**await asyncio.shield(inflight[normalized]), 'cached': False}
    except TimeoutError as exc:
        raise HTTPException(504, 'Source timed out. Retry shortly.') from exc
    except (httpx.HTTPError, ValueError) as exc:
        raise HTTPException(502, 'Could not extract source. It may be unavailable, blocked, or have changed markup.') from exc


from app.telegram import router as telegram_router
app.include_router(telegram_router)
