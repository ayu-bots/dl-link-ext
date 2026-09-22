"""DonghuaFun/MacCMS: match episode labels, not nid positions, across resources."""
import asyncio
import base64
import html
import json
import re
from urllib.parse import unquote, urljoin, urlsplit, urlunsplit

from bs4 import BeautifulSoup
from .server_menu import embeds, metadata, web_url

HOSTS = {'donghuafun.com', 'www.donghuafun.com'}
PATH = re.compile(r'/(?:index\.php/)?vod/play/id/([1-9]\d*)/sid/([1-9]\d*)/nid/([1-9]\d*)\.html')


def normalize(url):
    p = urlsplit(url.strip())
    if (p.scheme != 'https' or p.hostname not in HOSTS or p.username or p.password
            or p.port not in (None, 443) or not PATH.fullmatch(p.path)):
        raise ValueError('DonghuaFun requires an episode player URL (/vod/play/id/.../sid/.../nid/....html), not a series detail page. Open the episode on the site and copy its URL.')
    return urlunsplit(('https', 'donghuafun.com', p.path, '', ''))


def episode_key(label):
    clean = re.sub(r'\s+', '', label).casefold()
    match = re.fullmatch(r'(?:ep|episode)0*(\d+)', clean)
    return 'ep' + str(int(match[1])) if match else clean


def resource_choices(content, url):
    soup = BeautifulSoup(content, 'html.parser')
    series, current_sid, _ = PATH.fullmatch(urlsplit(url).path).groups()
    groups = {}
    current_label = None
    for anchor in soup.select('a[href]'):
        try:
            candidate = normalize(urljoin(url, anchor['href']))
        except ValueError:
            continue
        match = PATH.fullmatch(urlsplit(candidate).path)
        if match[1] != series:
            continue
        label = anchor.get_text(' ', strip=True)
        # Exclude Next/Prev navigation links to the same URLs.
        if not re.match(r'^(?:ep(?:isode)?\s*\d|movie\b|special\b)', label, re.I):
            continue
        sid = match[2]
        groups.setdefault(sid, {})[episode_key(label)] = candidate
        if candidate == url:
            current_label = label
    if not current_label:
        raise ValueError('Cannot identify the selected episode in the resource lists; refusing to guess nid mappings.')
    labels = {}
    # Shoutu resource tabs; retain unknown resources as Server N rather than
    # guessing providers or languages from the screenshot.
    tabs = soup.select('.anthology-tab .swiper-slide, .module-tab-item, [role="tab"]')
    ordered_sids = sorted(groups, key=int)
    if len(tabs) == len(ordered_sids):
        for sid, tab in zip(ordered_sids, tabs):
            copy = BeautifulSoup(str(tab), 'html.parser')
            for badge in copy.select('.badge, .count, .num, small'):
                badge.decompose()
            # Counts are frequently standalone child spans, unlike 4K/1080P.
            for span in copy.select('span'):
                if re.fullmatch(r'\d+', span.get_text(strip=True)):
                    span.decompose()
            label = re.sub(r'[\ue000-\uf8ff]', '', copy.get_text(' ', strip=True)).strip()
            if label:
                labels[sid] = label
    rows = []
    for sid in ordered_sids[:64]:
        label = labels.get(sid, f'Server {sid}')
        page = groups[sid].get(episode_key(current_label))
        row = dict(id=sid, label=label, server=label, languages=metadata(label)[1],
                   page_url=page, episode=current_label, embed_urls=[], status='pending')
        if not page:
            row.update(status='unavailable', error=f'{current_label} is not listed for this resource.')
        elif re.search(r'\bvip\b', label, re.I):
            row.update(status='unavailable', error='VIP resource: authenticated extraction is not supported.')
        rows.append(row)
    heading = soup.select_one('h1, h2')
    title = heading.get_text(' ', strip=True) if heading else 'DonghuaFun'
    return f'{title} — {current_label}', rows, len(ordered_sids) > 64


def decode_player(data, base):
    try:
        value = data.get('url')
        if not isinstance(value, str):
            return None
        encoding = str(data.get('encrypt', 0))
        if encoding == '2':
            value = base64.b64decode(value + '=' * (-len(value) % 4), validate=True).decode('utf-8')
        elif encoding not in ('0', '1'):
            return None
        if encoding in ('1', '2'):
            value = unquote(value)
        value = html.unescape(value).strip()
        if not value.startswith(('http://', 'https://', '//')):
            return None
        return web_url(value, base)
    except (ValueError, UnicodeDecodeError, TypeError):
        return None


def player_links(content, base):
    """Parse published MacCMS player JSON without evaluating JavaScript."""
    soup = BeautifulSoup(content, 'html.parser')
    links = []
    for script in soup.find_all('script'):
        text = script.string or script.get_text()
        for assignment in re.finditer(r'\b(?:var\s+)?player_[A-Za-z0-9_]+\s*=\s*(?=\{)', text):
            try:
                data, _ = json.JSONDecoder().raw_decode(text[assignment.end():])
                link = decode_player(data, base)
                if link and link not in links:
                    links.append(link)
            except (ValueError, UnicodeDecodeError, TypeError):
                continue
    if links:
        return links, 'player_url'
    # Scope fallback to the player's container to avoid unrelated ad iframes.
    links = embeds(content, base, scoped=True)
    return links, 'embed'


async def extract(url, fetch):
    url = normalize(url)
    content = await fetch(url)
    title, rows, truncated = resource_choices(content, url)
    browser_candidates = []
    async def resolve(row):
        if row['status'] != 'pending':
            return
        try:
            page = content if row['page_url'] == url else await fetch(row['page_url'])
            links, kind = player_links(page, row['page_url'])
            row.update(embed_urls=links, link_type=kind, status='ok' if links else 'unavailable')
            if not links:
                row['error'] = 'No static URL found; browser fallback has not completed.'
                browser_candidates.append(row)
        except Exception:
            row.update(status='error', error='Could not fetch this resource page.')
    await asyncio.gather(*(resolve(row) for row in rows))
    if browser_candidates:
        from .donghuafun_browser import resolve_rows
        try:
            # Preserve HTTP successes and missing-episode results on timeout.
            await asyncio.wait_for(resolve_rows(browser_candidates), timeout=28)
        except TimeoutError:
            for row in browser_candidates:
                if row['status'] != 'ok':
                    row['error'] = 'Browser extraction time limit reached; no matching public player URL returned.'
    complete = not truncated and all(row['status'] == 'ok' for row in rows)
    warnings = [] if complete else ['Some resources do not list this episode, require login, or could not be extracted.']
    if truncated:
        warnings.append('Only the first 64 resources were processed.')
    return dict(source='donghuafun', url=url, title=title, servers=rows, complete=complete, warnings=warnings)
