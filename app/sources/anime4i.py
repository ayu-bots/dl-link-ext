"""Isolated, bounded browser extraction for Anime4i's click-to-load players."""
import asyncio
import html
import re
from urllib.parse import urlsplit, urlunsplit

HOSTS = {'anime4i.com', 'www.anime4i.com'}
BROWSER_SLOT = asyncio.Semaphore(1)
PLAYER_XPATH = '/html/body/main/div/div[3]/article/div[2]/div/div[3]/div/div/div[1]/iframe'
PROVIDERS = [('Dailymotion', r'^dailymotion$'), ('Ok.ru', r'^ok\.?ru$')]


def normalize(url):
    p = urlsplit(url.strip())
    if (p.scheme != 'https' or p.hostname not in HOSTS or p.username or p.password
            or p.port not in (None, 443) or not re.fullmatch(r'/[a-zA-Z0-9_-]+/?', p.path)):
        raise ValueError('Use an HTTPS Anime4i episode URL, not a series page or /v/ URL.')
    return urlunsplit(('https', 'anime4i.com', p.path.rstrip('/'), '', ''))


def valid_embed(url, provider):
    try:
        p = urlsplit(html.unescape(url or ''))
        if p.scheme != 'https' or p.username or p.password or p.port not in (None, 443):
            return False
        host = (p.hostname or '').lower()
        if provider == 'Dailymotion':
            return ((host == 'geo.dailymotion.com' and p.path == '/player.html') or
                    (host in ('www.dailymotion.com', 'dailymotion.com') and p.path.startswith('/embed/video/')))
        return host == 'ok.ru' and p.path.startswith('/videoembed/')
    except ValueError:
        return False


def allowed_request(url, resource_type):
    """Never fetch third-party players/ads, media, images, or external scripts."""
    try:
        p = urlsplit(url)
        return (p.scheme == 'https' and p.hostname in HOSTS and not p.username and not p.password
                and p.port in (None, 443) and resource_type in ('document', 'script', 'stylesheet', 'xhr', 'fetch'))
    except ValueError:
        return False


async def guard(route):
    request = route.request
    if allowed_request(request.url, request.resource_type):
        # Do not let the network client auto-follow a redirect outside the
        # allowlist. Fulfill the 3xx as-is; any browser follow-up is intercepted.
        response = await route.fetch(max_redirects=0, timeout=10000)
        await route.fulfill(response=response)
    else:
        await route.abort()


async def select_provider(page, provider, pattern):
    regex = re.compile(pattern, re.I)
    for role in ('button', 'tab', 'link'):
        control = page.get_by_role(role, name=regex).first
        if await control.count() and await control.is_visible():
            await control.click(timeout=2500, no_wait_after=True)
            return
    # Custom div/span buttons may not have an accessible role.
    control = page.get_by_text(regex, exact=True).first
    if await control.count() and await control.is_visible():
        await control.click(timeout=2500, no_wait_after=True)
        return
    for select in await page.locator('select').all():
        for option in await select.locator('option').all():
            if regex.fullmatch((await option.inner_text()).strip()):
                await select.select_option(value=await option.get_attribute('value'), timeout=2500)
                return
    raise ValueError(f'{provider} server control not found')


async def read_player(page, provider):
    # Scope to the player and require a provider-specific embed host/path. An
    # advertising iframe cannot qualify, even if it occupies the supplied XPath.
    for selector in ('xpath=' + PLAYER_XPATH,
                     '#pembed iframe, #embed_holder iframe, #player iframe, .video-content iframe, .player-content iframe, .video-player iframe',
                     'main article iframe'):
        for frame in await page.locator(selector).all():
            url = html.unescape(await frame.get_attribute('src') or '')
            if valid_embed(url, provider):
                return url
    return None


async def load_player(page, provider):
    # Wait for asynchronous server switching, clicking only an explicit play
    # control in the episode article (never click inside third-party iframes).
    clicked = False
    for _ in range(20):
        embed = await read_player(page, provider)
        if embed:
            return embed
        if not clicked:
            play = page.locator('main article').get_by_role('button', name=re.compile(r'^(play|play video)$', re.I)).first
            if not await play.count():
                play = page.locator('main article').get_by_text(re.compile(r'^play video$', re.I)).first
            if await play.count() and await play.is_visible():
                await play.click(timeout=2000, no_wait_after=True)
                clicked = True
        await page.wait_for_timeout(250)
    return None


async def extract(url, fetch):
    from playwright.async_api import async_playwright
    url = normalize(url)
    rows = []
    title = 'Anime4i episode'
    # Launch on demand; no idle Chromium process consuming free-tier memory.
    async with BROWSER_SLOT:
        async with async_playwright() as pw:
            try:
                browser = await pw.chromium.launch(headless=True, args=['--disable-dev-shm-usage'])
            except Exception as exc:
                raise ValueError('Chromium unavailable. Deploy using the Dockerfile with Playwright installed.') from exc
            try:
                for provider, pattern in PROVIDERS:
                    row = dict(id=str(len(rows) + 1), label=provider, server=provider, languages=[],
                               page_url=url, embed_urls=[], status='unavailable')
                    rows.append(row)
                    # Fresh context per server prevents returning the previous
                    # server's iframe and discards cookies/service worker state.
                    context = await browser.new_context(service_workers='block', accept_downloads=False)
                    await context.route_web_socket('**/*', lambda socket: socket.close())
                    await context.route('**/*', guard)
                    page = await context.new_page()
                    page.on('popup', lambda popup: asyncio.create_task(popup.close()))
                    try:
                        await page.goto(url, wait_until='domcontentloaded', timeout=12000)
                        if normalize(page.url) != url:
                            raise ValueError('Unexpected episode redirect')
                        heading = page.locator('h1').first
                        if await heading.count():
                            title = (await heading.inner_text())[:500]
                        await select_provider(page, provider, pattern)
                        embed = await load_player(page, provider)
                        if embed:
                            row['embed_urls'] = [embed]
                            row['status'] = 'ok'
                        else:
                            row['error'] = 'Player embed did not appear after selecting this server.'
                    except Exception:
                        row['status'] = 'error'
                        row['error'] = 'Could not load or select this server; the page may be blocked or its controls changed.'
                    finally:
                        await context.close()
            finally:
                await browser.close()
    # The episode title explicitly advertises subtitles; do not infer from the
    # provider itself. Keep language unspecified on titles without a language.
    from .server_menu import metadata
    languages = metadata(title)[1]
    for row in rows:
        row['languages'] = languages
    complete = all(row['status'] == 'ok' for row in rows)
    return dict(source='anime4i', url=url, title=title, servers=rows, complete=complete,
                warnings=[] if complete else ['Some Anime4i servers could not be extracted.'])
