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


@pytest.mark.asyncio
async def test_automatic_webhook_registration(monkeypatch):
    from types import SimpleNamespace
    import app.telegram_setup as setup
    from app.bot_config import webhook_secret
    monkeypatch.setenv('TELEGRAM_BOT_TOKEN', 'test-token')
    monkeypatch.setenv('PUBLIC_BASE_URL', 'https://example.koyeb.app')
    calls = []
    class Client:
        def __init__(self, **kwargs): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *args): pass
        async def post(self, url, json):
            calls.append(json)
            return httpx.Response(200, json={'ok': True})
    monkeypatch.setattr(setup.httpx, 'AsyncClient', Client)
    instance = SimpleNamespace(state=SimpleNamespace())
    await setup.setup(instance)
    assert instance.state.telegram_status['status'] == 'registered'
    assert calls[0]['url'] == 'https://example.koyeb.app/telegram/webhook'
    assert calls[0]['allowed_updates'] == ['message', 'callback_query']
    assert calls[0]['secret_token'] == webhook_secret('test-token')
    monkeypatch.delenv('PUBLIC_BASE_URL')
    await setup.setup(instance)
    assert instance.state.telegram_status['status'] == 'configuration_error'


@pytest.mark.parametrize('text', ['/start', '++https://animexin.dev/?p=30101++'])
def test_webhook_sends_reply(monkeypatch, text):
    import app.telegram as bot
    import app.main as main
    from app.bot_config import webhook_secret
    bot.seen.clear()
    bot.active.clear()
    monkeypatch.setenv('TELEGRAM_BOT_TOKEN', 'test-token')
    monkeypatch.delenv('PUBLIC_BASE_URL', raising=False)
    sent = []
    class Client:
        def __init__(self, **kwargs): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *args): pass
        async def post(self, url, json):
            sent.append(json)
            return httpx.Response(200, json={'ok': True})
    async def extract(url):
        assert url == 'https://animexin.dev/?p=30101'
        return {'title': 'Episode', 'warnings': [], 'servers': [
            {'server': 'Mega', 'label': 'English Mega', 'languages':['English'], 'embed_urls':['https://mega.nz/embed/test']}
        ]}
    monkeypatch.setattr(bot.httpx, 'AsyncClient', Client)
    monkeypatch.setattr(main, 'extract', extract)
    with TestClient(app) as client:
        r = client.post('/telegram/webhook', json={'update_id': 700, 'message': {'chat': {'id': 42}, 'text': text}},
                        headers={'X-Telegram-Bot-Api-Secret-Token': webhook_secret('test-token')})
        assert r.status_code == 200
        assert sent[0]['chat_id'] == 42
        assert ('Send an Animexin' if text == '/start' else 'Choose a server') in sent[0]['text']
        if text != '/start':
            assert sent[0]['reply_markup']['inline_keyboard'][0][0]['text'] == 'Mega'
            assert 'https://mega.nz/embed/test' not in sent[0]['text']
        status = client.get('/api/telegram/status').json()
        assert status['last_update'] == 'received'
        assert 'test-token' not in str(status)


@pytest.mark.parametrize('mode', ['valid', 'expired', 'wrong_chat', 'invalid'])
def test_provider_button_callback(monkeypatch, mode):
    import app.telegram as bot
    from app.bot_config import webhook_secret
    bot.menus.clear()
    bot.seen.clear()
    bot.active.clear()
    monkeypatch.setenv('TELEGRAM_BOT_TOKEN', 'test-token')
    monkeypatch.delenv('PUBLIC_BASE_URL', raising=False)
    result = {'title': 'Episode', 'warnings': [], 'servers': [
        {'server':'Dailymotion', 'label':'Eng Dailymotion', 'languages':['English'], 'embed_urls':['https://dm.test/eng']},
        {'server':'Mega', 'label':'Eng Mega', 'languages':['English'], 'embed_urls':['https://mega.test/eng']},
        {'server':'Dailymotion', 'label':'Indo Dailymotion', 'languages':['Indonesian'], 'embed_urls':['https://dm.test/indo']},
        {'server':'Dailymotion', 'label':'All Sub Dailymotion', 'languages':['Multilingual'], 'embed_urls':[], 'error':'Unavailable'},
    ]}
    markup = bot.server_menu(result, 42)
    buttons = [b for row in markup['inline_keyboard'] for b in row]
    assert [b['text'] for b in buttons] == ['Dailymotion', 'Mega']
    assert all(len(b['callback_data'].encode()) <= 64 for b in buttons)
    data = buttons[0]['callback_data']
    if mode == 'expired':
        next(iter(bot.menus.values()))['created'] -= bot.MENU_TTL
    if mode == 'invalid':
        data = 'srv:bad:999'
    calls = []
    class Client:
        def __init__(self, **kwargs): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *args): pass
        async def post(self, url, json):
            calls.append((url.rsplit('/', 1)[-1], json))
            return httpx.Response(200, json={'ok': True})
    monkeypatch.setattr(bot.httpx, 'AsyncClient', Client)
    with TestClient(app) as client:
        update = {'update_id': 900, 'callback_query': {'id':'query-id', 'data':data,
                  'message': {'chat': {'id': 99 if mode == 'wrong_chat' else 42}}}}
        headers = {'X-Telegram-Bot-Api-Secret-Token': webhook_secret('test-token')}
        assert client.post('/telegram/webhook', json=update, headers=headers).status_code == 200
        assert calls[0][0] == 'answerCallbackQuery'
        if mode == 'valid':
            text = calls[1][1]['text']
            assert 'https://dm.test/eng' in text and 'Language: English' in text
            assert 'https://dm.test/indo' in text and 'Language: Indonesian' in text
            assert 'Language: Multilingual' in text and 'Unavailable' in text
            assert 'mega.test' not in text
            assert len(bot.menus) == 1  # Other buttons remain usable.
        else:
            assert len(calls) == 1 and calls[0][1]['show_alert']
        count = len(calls)
        client.post('/telegram/webhook', json=update, headers=headers)
        assert len(calls) == count  # Telegram redelivery is deduplicated.


def test_menu_cache_bounded():
    import app.telegram as bot
    bot.menus.clear()
    result = {'title':'Episode', 'servers':[{'server':'Mega'}]}
    for _ in range(bot.MENU_LIMIT + 5):
        bot.server_menu(result, 42)
    assert len(bot.menus) == bot.MENU_LIMIT
    bot.menus.clear()
