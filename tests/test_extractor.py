import base64
import asyncio
import pytest
import httpx
from fastapi.testclient import TestClient
from app.sources.animexin import normalize, choices, extract, embeds, metadata
from app.main import app, cache

BASE = 'https://animexin.dev/test-episode/'


def test_normalize():
    assert normalize(BASE + 'v/12/?x=1') == BASE
    for url in ['http://animexin.dev/x/', 'https://evil.test/x/', 'https://animexin.dev@127.0.0.1/x/', 'https://animexin.dev:8080/x/', 'https://animexin.dev/']:
        with pytest.raises(ValueError):
            normalize(url)


def test_choices():
    encoded = base64.b64encode(b'<iframe src="https://mega.nz/embed/abc#key"></iframe>').decode()
    html = f'''<h1>Episode</h1><select><option value="">Select Video Server</option>
    <option value="12">Hardsub English Rumble AX</option>
    <option value="{encoded}">Hardsub Indonesia Mega AX</option>
    <option value="/test-episode/v/4/">All Sub Player Dailymotion AX</option></select>'''
    title, rows, truncated = choices(html, BASE)
    assert title == 'Episode' and not truncated
    assert len(rows) == 3
    assert rows[0]['page_url'] == BASE + 'v/12/'
    assert rows[1]['embed_urls'] == ['https://mega.nz/embed/abc#key']
    assert rows[1]['languages'] == ['Indonesian']
    assert rows[2]['languages'] == ['Multilingual']


def test_player_scope():
    html = '<iframe src="https://ads.test/"></iframe><div id="pembed"><iframe data-src="//www.dailymotion.com/embed/video/x"></iframe></div>'
    assert embeds(html, BASE, True) == ['https://www.dailymotion.com/embed/video/x']
    assert embeds('<iframe src="javascript:alert(1)"></iframe>', BASE) == []
    assert metadata('Dtube Eng AX') == ('Dtube', ['English'])


@pytest.mark.asyncio
async def test_partial_results():
    async def fetch(url):
        if url == BASE:
            return '<select><option value="1">Eng Mega</option><option value="12">Indo Rumble</option></select>'
        if url.endswith('/1/'):
            return '<div id="pembed"><iframe src="https://mega.nz/embed/x"></iframe></div>'
        raise httpx.ConnectError('offline')
    data = await extract(BASE, fetch)
    assert not data['complete']
    assert [s['status'] for s in data['servers']] == ['ok', 'error']
    assert data['warnings']


def test_api(monkeypatch):
    import app.main as main
    cache.clear()
    calls = []
    async def fetch(url):
        calls.append(url)
        return '<select><option value="https://mega.nz/embed/test">Eng Mega</option></select>'
    monkeypatch.setattr(main, 'fetch', fetch)
    with TestClient(app) as client:
        assert client.get('/healthz').status_code == 200
        assert client.get('/').status_code == 200
        assert client.get('/api/extract', params={'url':'https://127.0.0.1/'}).status_code == 400
        first = client.get('/api/extract', params={'url': BASE}).json()
        assert first['complete'] and not first['cached']
        assert client.get('/api/extract', params={'url':BASE+'v/12/'}).json()['cached']
        assert calls == [BASE]


@pytest.mark.asyncio
async def test_redirect_security():
    import app.main as main
    def handler(request):
        return httpx.Response(302, headers={'location':'http://169.254.169.254/latest/meta-data/'})
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        app.state.client = client
        with pytest.raises(ValueError, match='Unsafe'):
            await main.fetch(BASE)


@pytest.mark.asyncio
async def test_all_numbered_servers_from_single_server_input():
    # Intentionally unordered and non-contiguous: don't stop at gaps or assume
    # that the first/default Dailymotion player represents every server.
    servers = {
        1: ('Hardsub English Dailymotion AX', 'https://www.dailymotion.com/embed/video/one', 'Dailymotion', ['English']),
        3: ('Hardsub Indonesia Odysee AX', 'https://odysee.com/$/embed/three', 'Odysee', ['Indonesian']),
        2: ('Hardsub Indonesia Mega AX', 'https://mega.nz/embed/two#key', 'Mega', ['Indonesian']),
        12: ('Hardsub English Rumble AX', 'https://rumble.com/embed/twelve/', 'Rumble', ['English']),
        16: ('All Sub Player Dood AX', 'https://dood.test/e/sixteen', 'Dood', ['Multilingual']),
    }
    calls = []

    async def fetch(url):
        calls.append(url)
        if url == BASE:
            return '<select>' + ''.join(
                f'<option value="{BASE}v/{number}/">{entry[0]}</option>'
                for number, entry in servers.items()
            ) + '</select>'
        number = int(url.rstrip('/').split('/')[-1])
        return f'<div id="pembed"><iframe src="{servers[number][1]}"></iframe></div>'

    result = await extract(BASE + 'v/12/', fetch)
    assert result['complete']
    assert result['url'] == BASE
    assert set(calls) == {BASE, *(f'{BASE}v/{n}/' for n in servers)}
    assert len(calls) == len(servers) + 1
    assert len(result['servers']) == len(servers)
    for row in result['servers']:
        label, embed, provider, languages = servers[int(row['id'])]
        assert row['label'] == label
        assert row['embed_urls'] == [embed]
        assert row['server'] == provider
        assert row['languages'] == languages


@pytest.mark.asyncio
@pytest.mark.parametrize('redirect', [True, False])
async def test_short_link(redirect):
    from app.main import Page
    short = 'https://animexin.dev/?p=30101'
    assert normalize(short) == short
    calls = []
    async def fetch(url):
        calls.append(url)
        if url == short:
            html = f'<link rel="canonical" href="{BASE}"><select><option value="12">Eng Mega</option></select>'
            return Page(html, BASE if redirect else short)
        assert url == BASE + 'v/12/'
        return '<div id="pembed"><iframe src="https://mega.nz/embed/test"></iframe></div>'
    result = await extract(short, fetch)
    assert result['complete'] and result['url'] == BASE
    assert calls == [short, BASE + 'v/12/']


def test_telegram_links():
    from app.telegram import find_link, replies
    short = 'https://animexin.dev/?p=30101'
    assert find_link({'text': f'++[{short}]({short})++'}) == short
    assert find_link({'text': f'watch {BASE}v/12/'}) == BASE
    assert find_link({'text': 'watch', 'entities': [{'type': 'text_link', 'url': short}]}) == short
    assert find_link({'caption': short}) == short
    assert find_link({'text': 'https://evil.test/'}) is None
    assert all(len(x) <= 1800 for x in replies({'title':'X' * 5000, 'servers':[], 'warnings':[]}))


def test_telegram_webhook(monkeypatch):
    import app.telegram as bot
    bot.seen.clear()
    bot.active.clear()
    monkeypatch.setenv('TELEGRAM_BOT_TOKEN', 'test-only')
    from app.bot_config import webhook_secret
    monkeypatch.delenv('TELEGRAM_WEBHOOK_SECRET', raising=False)
    calls = []
    async def handle(uid, message, token):
        calls.append(uid)
        bot.active.discard(uid)
    monkeypatch.setattr(bot, 'handle', handle)
    with TestClient(app) as client:
        body = {'update_id': 123, 'message': {'chat': {'id': 42}, 'text': 'https://animexin.dev/?p=30101'}}
        assert client.post('/telegram/webhook', json=body).status_code == 403
        for _ in range(2):
            assert client.post('/telegram/webhook', json=body, headers={'X-Telegram-Bot-Api-Secret-Token':webhook_secret('test-only')}).status_code == 200
        assert calls == [123]


def test_webhook_registration_uses_automatic_secret(monkeypatch):
    from scripts import set_webhook
    from app.bot_config import webhook_secret
    monkeypatch.setenv('TELEGRAM_BOT_TOKEN', 'test-token')
    monkeypatch.setenv('PUBLIC_BASE_URL', 'https://example.koyeb.app/')
    monkeypatch.delenv('TELEGRAM_WEBHOOK_SECRET', raising=False)
    def post(url, json, timeout):
        assert json['url'] == 'https://example.koyeb.app/telegram/webhook'
        assert json['secret_token'] == webhook_secret('test-token')
        assert webhook_secret('other-token') != json['secret_token']
        return httpx.Response(200, json={'ok': True})
    monkeypatch.setattr(set_webhook.httpx, 'post', post)
    set_webhook.main()
