"""On-demand rendered-player fallback. Never navigate third-party players."""
import asyncio
import html
import logging
import re
from urllib.parse import urlsplit

from .anime4i import BROWSER_SLOT, LIBRARY_SCRIPTS, valid_embed
from .donghuafun import HOSTS, normalize, decode_player
from .server_menu import web_url

log = logging.getLogger(__name__)
PLAYER_SELECTOR = ('.MacPlayer iframe, #playleft iframe, #player iframe, '
                   '.player-box iframe, .player-video iframe, #player_iframe')


def allowed_request(url, kind):
    try:
        p = urlsplit(url)
        if p.scheme != 'https' or p.username or p.password or p.port not in (None, 443):
            return False
        if p.hostname in HOSTS:
            return kind in ('document', 'script', 'stylesheet', 'xhr', 'fetch')
        return bool(kind == 'script' and p.hostname in LIBRARY_SCRIPTS
                    and re.fullmatch(LIBRARY_SCRIPTS[p.hostname], p.path))
    except ValueError:
        return False


def player_embed(value, base, scoped=False):
    """Accept known provider embeds, or local wrappers inside player containers."""
    try:
        value = html.unescape(value or '')
        url = web_url(value, base)
        if not url:
            return None
        p = urlsplit(url)
        if p.scheme != 'https' or p.port not in (None, 443):
            return None
        if valid_embed(url, 'Dailymotion') or valid_embed(url, 'Ok.ru'):
            return url
        if (p.hostname in ('rumble.com', 'www.rumble.com') and p.path.startswith('/embed/')):
            return url
        if p.hostname == 'mega.nz' and p.path.startswith('/embed/'):
            return url
        if (scoped and p.hostname in HOSTS
                and re.search(r'/(?:static/player/|player/|player\.(?:html|php))', p.path)
                and not re.search(r'(?:buffer|loading|ads?)\.(?:html|php)', p.path)):
            return url
    except ValueError:
        pass
    return None


# This reads already-executed public player state. It does not eval source,
# authenticate, or decrypt access-controlled content.
PLAYER_STATE_JS = """() => {
  const configs = [];
  for (const name of Object.keys(window)) {
    if (!/^player_[a-zA-Z0-9_]+$/.test(name)) continue;
    try {
      const value = window[name];
      if (value && typeof value === 'object' && typeof value.url === 'string')
        configs.push({url: value.url, encrypt: value.encrypt || 0});
    } catch (_) {}
  }
  const mac = window.MacPlayer;
  const playUrl = mac && typeof mac.PlayUrl === 'string' ? mac.PlayUrl : '';
  return {configs: configs.slice(0, 16), playUrl};
}"""


async def rendered_links(page, base):
    state = await page.evaluate(PLAYER_STATE_JS)
    for config in state.get('configs', []):
        link = decode_player(config, base)
        if link:
            return [link], 'player_url'
    # MacPlayer.PlayUrl has already gone through MacCMS's public URL decoder.
    link = decode_player({'url': state.get('playUrl', '')}, base)
    if link:
        return [link], 'player_url'
    for selector, scoped in ((PLAYER_SELECTOR, True), ('iframe', False)):
        for frame in await page.locator(selector).all():
            for attribute in ('src', 'data-src'):
                link = player_embed(await frame.get_attribute(attribute), base, scoped)
                if link:
                    return [link], 'embed'
    return [], 'embed'


async def render_row(browser, row):
    base = row['page_url']
    context = await browser.new_context(service_workers='block', accept_downloads=False)
    blocked = set()
    observed = []
    try:
        await context.route_web_socket('**/*', lambda socket: socket.close())
        async def guard(route):
            request = route.request
            if allowed_request(request.url, request.resource_type):
                try:
                    response = await route.fetch(max_redirects=0, timeout=6000)
                    await route.fulfill(response=response)
                except Exception:
                    await route.abort()
            else:
                if request.resource_type in ('script', 'document'):
                    blocked.add(urlsplit(request.url).hostname or 'unknown')
                await route.abort()
        await context.route('**/*', guard)
        page = await context.new_page()
        page.on('popup', lambda popup: asyncio.create_task(popup.close()))
        def observe(request):
            try:
                if (request.resource_type == 'document' and request.frame.page == page
                        and request.frame.parent_frame is not None):
                    link = player_embed(request.url, base)
                    if link and link not in observed:
                        observed.append(link)
            except Exception:
                pass
        page.on('request', observe)
        await page.goto(base, wait_until='domcontentloaded', timeout=8000)
        if normalize(page.url) != base:
            raise ValueError('Unexpected episode redirect')
        clicked = False
        for _ in range(20):
            links, kind = await rendered_links(page, base)
            if not links and observed:
                links, kind = observed[:1], 'embed'
            if links:
                row.update(embed_urls=links, link_type=kind, status='ok', extraction_method='browser')
                row.pop('error', None)
                return
            if not clicked:
                controls = page.locator('.MacPlayer, #playleft, #player, .player-box').get_by_role(
                    'button', name=re.compile(r'^(play|play video)$', re.I))
                for control in await controls.all():
                    if await control.is_visible():
                        await control.click(timeout=1500, no_wait_after=True)
                        clicked = True
                        break
            await page.wait_for_timeout(250)
        iframe_count = await page.locator('iframe').count()
        row['error'] = (f'No public player URL found after rendering. Diagnostics: {iframe_count} iframe(s), '
                        f'{len(blocked)} blocked external host(s).')
        log.warning('DonghuaFun resource %s: iframe_count=%s; blocked_hosts=%s',
                    row['id'], iframe_count, ','.join(sorted(blocked))[:500])
    except Exception as exc:
        row['error'] = 'Browser could not load this resource. See DonghuaFun diagnostics in service logs.'
        # Class only: browser errors may contain signed URLs or tokens.
        log.warning('DonghuaFun resource %s: browser failure %s', row['id'], type(exc).__name__)
    finally:
        await context.close()


async def resolve_rows(rows):
    from playwright.async_api import async_playwright
    # Same semaphore as Anime4i: at most one Chromium process across sources.
    async with BROWSER_SLOT:
        async with async_playwright() as pw:
            browser = None
            try:
                browser = await pw.chromium.launch(headless=True, args=['--disable-dev-shm-usage'])
                for row in rows[:4]:
                    await render_row(browser, row)
                for row in rows[4:]:
                    row['error'] = 'Browser fallback limit reached (four resources per extraction).'
            except Exception as exc:
                log.warning('DonghuaFun browser launch failed: %s', type(exc).__name__)
                for row in rows:
                    if row['status'] != 'ok':
                        row['error'] = 'Browser fallback unavailable; redeploy the Docker image with Chromium.'
            finally:
                if browser:
                    await browser.close()
