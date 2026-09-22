"""Animexin adapter: parse advertised choices, never probe arbitrary servers."""
import asyncio
import base64
import re
from urllib.parse import parse_qs, urljoin, urlsplit, urlunsplit

from bs4 import BeautifulSoup

HOSTS = {'animexin.dev', 'www.animexin.dev'}


def normalize(url: str) -> str:
    p = urlsplit(url.strip())
    if p.scheme != 'https' or p.hostname not in HOSTS or p.username or p.password or p.port not in (None, 443):
        raise ValueError('Use an HTTPS Animexin episode URL on animexin.dev.')
    post = parse_qs(p.query).get('p', [])
    if p.path in ('', '/') and len(post) == 1 and re.fullmatch(r'[1-9][0-9]*', post[0]):
        return f'https://animexin.dev/?p={post[0]}'
    path = re.sub(r'/v/\d+/?$', '/', p.path)
    if not re.fullmatch(r'/[a-zA-Z0-9_-]+/', path.rstrip('/') + '/'):
        raise ValueError('Provide an episode URL, not the homepage or a nested path.')
    return urlunsplit(('https', 'animexin.dev', path.rstrip('/') + '/', '', ''))


def web_url(value: str, base: str) -> str | None:
    value = value.strip()
    if not value or value.startswith(('javascript:', 'data:', 'about:')):
        return None
    url = urljoin(base, value)
    p = urlsplit(url)
    return url if p.scheme in ('https', 'http') and p.hostname and not p.username and not p.password else None


def embeds(html: str, base: str, scoped: bool = False) -> list[str]:
    soup = BeautifulSoup(html, 'html.parser')
    roots = soup.select('#pembed, #embed_holder, .player-embed, .video-content, .player-content, #player, .video-player') if scoped else [soup]
    # On full pages, avoid collecting unrelated advertisement iframes.
    result = []
    for root in roots:
        for tag in root.select('iframe, embed, video, video source'):
            url = web_url(tag.get('src') or tag.get('data-src') or tag.get('data-lazy-src') or '', base)
            if url and url not in result:
                result.append(url)
    return result


def metadata(label: str) -> tuple[str, list[str]]:
    lower = label.lower()
    languages = []
    if re.search(r'\b(english|eng)\b', lower):
        languages.append('English')
    if re.search(r'\b(indonesia|indonesian|indo)\b', lower):
        languages.append('Indonesian')
    if re.search(r'\ball\s+sub\b', lower):
        languages.append('Multilingual')
    server = next((name for pattern, name in [
        (r'dailymotion', 'Dailymotion'), (r'mega', 'Mega'), (r'odysee', 'Odysee'),
        (r'ok\.?ru', 'Ok.ru'), (r'dtube', 'Dtube'), (r'rumble', 'Rumble'),
        (r'streamwish', 'Streamwish'), (r'dood', 'Dood')
    ] if re.search(pattern, lower)), label)
    return server, languages


def choices(html: str, base: str) -> tuple[str, list[dict], bool]:
    soup = BeautifulSoup(html, 'html.parser')
    heading = soup.select_one('h1')
    rows = []
    seen = set()
    nodes = soup.select('select option, a[href*="/v/"], [data-em]')
    for node in nodes:
        label = node.get_text(' ', strip=True)
        raw = str(node.get('data-em') or node.get('value') or node.get('href') or '').strip()
        if not raw or not label or re.search(r'select.*server|choose.*server', label, re.I):
            continue
        links = []
        page = None
        number = None
        if raw.isdigit():
            number = int(raw)
            page = f'{base}v/{number}/'
        elif re.search(r'/v/\d+/?(?:[?#].*)?$', raw):
            candidate = urljoin(base, raw)
            try:
                if normalize(candidate) != base:
                    continue
            except ValueError:
                continue
            number = int(re.search(r'/v/(\d+)', candidate)[1])
            page = f'{base}v/{number}/'
        else:
            decoded = raw
            if not raw.startswith(('<', 'http', '//')):
                try:
                    decoded = base64.b64decode(raw + '=' * (-len(raw) % 4), validate=True).decode()
                except (ValueError, UnicodeDecodeError):
                    continue
            links = embeds(decoded, base) if '<' in decoded else []
            if not links and decoded.startswith(('https://', 'http://', '//')):
                link = web_url(decoded, base)
                if link:
                    links = [link]
            if not links:
                continue
        key = (label, page, tuple(links))
        if key in seen:
            continue
        seen.add(key)
        server, languages = metadata(label)
        rows.append(dict(id=str(number) if number is not None else str(len(rows) + 1), label=label,
                         server=server, languages=languages, page_url=page or base, embed_urls=links,
                         status='ok' if links else 'pending'))
    truncated = len(rows) > 64
    return heading.get_text(' ', strip=True) if heading else 'Animexin episode', rows[:64], truncated


async def extract(url: str, fetch) -> dict:
    # A pasted server URL still requests every server for the episode.
    url = normalize(url)
    html = await fetch(url)
    if urlsplit(url).query:
        # Resolve WordPress short links before building /v/N/ URLs.
        candidates = [getattr(html, 'url', '')]
        soup = BeautifulSoup(html, 'html.parser')
        canonical = soup.select_one('link[rel="canonical"]')
        if canonical:
            candidates.append(urljoin(url, canonical.get('href', '')))
        for candidate in candidates:
            try:
                resolved = normalize(candidate)
                if not urlsplit(resolved).query:
                    url = resolved
                    break
            except ValueError:
                continue
        else:
            raise ValueError('Could not resolve the short link to an episode permalink.')
    title, rows, truncated = choices(html, url)
    warnings = ['More than 64 choices found; remaining choices were not fetched.'] if truncated else []
    if not rows:
        raise ValueError('No supported server choices found. The page may be blocked or its markup may have changed.')

    async def resolve(row):
        if row['status'] == 'ok':
            return
        try:
            content = await fetch(row['page_url'])
            row['embed_urls'] = embeds(content, row['page_url'], scoped=True)
            row['status'] = 'ok' if row['embed_urls'] else 'unavailable'
            if not row['embed_urls']:
                row['error'] = 'No static player embed found; this server may require JavaScript.'
        except Exception:
            row['status'] = 'error'
            row['error'] = 'Server page could not be retrieved.'

    await asyncio.gather(*(resolve(row) for row in rows))
    if any(row['status'] != 'ok' for row in rows):
        warnings.append('Some servers could not be extracted; their labels are retained below.')
    return dict(source='animexin', url=url, title=title, servers=rows, warnings=warnings,
                complete=not truncated and all(row['status'] == 'ok' for row in rows))
