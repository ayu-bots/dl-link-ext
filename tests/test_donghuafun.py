"""Representative MacCMS fixtures; not a captured live-site HTML fixture."""
import base64
import json
from urllib.parse import quote
import pytest
from app.sources import donghuafun as source
from app.telegram import find_link, server_menu, menus


def url(sid=1, nid=1):
    return f'https://donghuafun.com/index.php/vod/play/id/12/sid/{sid}/nid/{nid}.html'


def fixture():
    return '''<h2>Shrouding the Heavens</h2><div class="anthology-tab">
    <div class="swiper-slide">4K Ads<span>135</span></div>
    <div class="swiper-slide">ENG 4K-VIP<span>9</span></div>
    <div class="swiper-slide">1080P English gang<span>8</span></div>
    <div class="swiper-slide">4K Indo Rum<span>7</span></div></div>''' + ''.join(
        f'<a href="{url(sid,nid)}">EP{episode}</a>' for sid,nid,episode in [
            (1,1,182),(1,2,181),(2,1,181),(3,1,183),(3,2,182),(4,1,182)]
    ) + '<script>var player_aaaa=' + json.dumps({'url':'https://player.test/embed/current', 'encrypt':0}) + ';</script>'


def test_normalize_and_telegram():
    assert source.normalize(url()+'?tracking=1') == url()
    assert find_link({'text':f'++[{url()}]({url()})++'}) == url()
    for bad in ['https://donghuafun.com/', 'https://donghuafun.com/index.php/vod/detail/id/12.html',
                url().replace('donghuafun.com','evil.test'), url().replace('sid/1','sid/0')]:
        with pytest.raises(ValueError): source.normalize(bad)


@pytest.mark.asyncio
async def test_match_episode_not_position():
    calls = []
    async def fetch(page):
        calls.append(page)
        if page == url(): return fixture()
        return '<script>var player_aaaa=' + json.dumps({'url':'https://rumble.com/embed/test', 'encrypt':0}) + ';</script>'
    result = await source.extract(url(), fetch)
    assert result['source'] == 'donghuafun'
    assert result['title'] == 'Shrouding the Heavens — EP182'
    assert set(calls) == {url(),url(3,2),url(4,1)}
    assert url(2,1) not in calls and url(3,1) not in calls
    rows = result['servers']
    assert rows[1]['status'] == 'unavailable' and rows[1]['page_url'] is None
    assert 'EP182' in rows[1]['error']
    assert rows[2]['label'] == '1080P English gang' and rows[2]['languages'] == ['English']
    assert rows[3]['languages'] == ['Indonesian']
    assert not result['complete']
    markup = server_menu(result, 42)
    assert markup['inline_keyboard'][0][0]['text'] == '4K Ads'
    menus.clear()


@pytest.mark.parametrize('encoding', [0,1,2])
def test_player_json_decoding(encoding):
    link = 'https://mega.nz/embed/abc#key'
    value = quote(link, safe='') if encoding else link
    if encoding == 2: value = base64.b64encode(value.encode()).decode()
    content = '<script>var player_aaaa=' + json.dumps({'encrypt':encoding,'url':value,'url_next':'https://ads.test/'}) + ';</script>'
    assert source.player_links(content,url()) == ([link],'player_url')


@pytest.mark.parametrize('value', ['https://t.co/ad', 'javascript:alert(1)', 'video-id-only'])
def test_reject_ads_or_non_urls(value):
    content = '<script>var player_aaaa=' + json.dumps({'url':value}) + ';</script>'
    assert source.player_links(content,url())[0] == []


def test_scoped_iframe_fallback():
    content = '<iframe src="https://ads.test/"></iframe><div id="player"><iframe src="https://ok.ru/videoembed/1"></iframe></div>'
    assert source.player_links(content,url()) == (['https://ok.ru/videoembed/1'],'embed')


def test_no_guessing_and_vip_status():
    with pytest.raises(ValueError): source.resource_choices('<h1>Login required</h1>',url())
    _,rows,_ = source.resource_choices(fixture(),url(1,2))
    assert rows[1]['label'] == 'ENG 4K-VIP'
    assert rows[1]['status'] == 'unavailable' and 'authenticated' in rows[1]['error']


def test_api_dispatch(monkeypatch):
    from fastapi.testclient import TestClient
    import app.main as main
    main.cache.clear()
    async def fetch(page):
        return fixture()
    monkeypatch.setattr(main,'fetch',fetch)
    with TestClient(main.app) as client:
        response = client.get('/api/extract',params={'url':url()})
        assert response.status_code == 200
        assert response.json()['source'] == 'donghuafun'
        assert response.json()['servers'][0]['embed_urls'] == ['https://player.test/embed/current']


@pytest.mark.asyncio
async def test_browser_fallback_only_for_missing_public_urls(monkeypatch):
    from app.sources import donghuafun_browser as browser
    calls = []
    async def fetch(page):
        if page == url():
            return fixture().split('<script>')[0]  # dynamic player on 4K Ads
        return '<div id="player"><iframe src="https://ok.ru/videoembed/123"></iframe></div>'
    async def resolve(rows):
        calls.extend(row['page_url'] for row in rows)
        for row in rows:
            row.update(embed_urls=['https://rumble.com/embed/rendered'], status='ok', link_type='embed')
            row.pop('error',None)
    monkeypatch.setattr(browser,'resolve_rows',resolve)
    result = await source.extract(url(),fetch)
    assert calls == [url()]
    assert result['servers'][0]['embed_urls'] == ['https://rumble.com/embed/rendered']
    assert result['servers'][1]['status'] == 'unavailable'  # not EP182
    assert result['servers'][2]['embed_urls'] == ['https://ok.ru/videoembed/123']


@pytest.mark.asyncio
async def test_browser_timeout_preserves_http_success(monkeypatch):
    from app.sources import donghuafun_browser as browser
    async def fetch(page):
        return fixture() if page == url() else '<div>JavaScript player</div>'
    async def timeout(rows):
        raise TimeoutError()
    monkeypatch.setattr(browser,'resolve_rows',timeout)
    result = await source.extract(url(),fetch)
    assert result['servers'][0]['status'] == 'ok'
    assert 'time limit' in result['servers'][2]['error']
    assert not result['complete']


@pytest.mark.parametrize('value,kind,expected', [
    ('https://donghuafun.com/static/js/player.js','script',True),
    ('https://donghuafun.com/static/player/dplayer.html','document',True),
    ('https://code.jquery.com/jquery-3.7.1.min.js','script',True),
    ('https://code.jquery.com/jquery-3.7.1.min.js','document',False),
    ('https://t.co/ad','document',False),
    ('https://ok.ru/videoembed/123','document',False),
    ('http://127.0.0.1/admin','fetch',False),
    ('https://donghuafun.com/ep.mp4','media',False),
])
def test_rendered_request_policy(value,kind,expected):
    from app.sources.donghuafun_browser import allowed_request
    assert allowed_request(value,kind) == expected


def test_rendered_embed_validation():
    from app.sources.donghuafun_browser import player_embed
    assert player_embed('https://rumble.com/embed/abc',url())
    assert player_embed('https://t.co/ad',url(),True) is None
    assert player_embed('https://evil.test/player/abc',url(),True) is None
    assert player_embed('/static/player/dplayer.html?url=abc',url(),True)
    assert player_embed('/static/player/buffer.html',url(),True) is None
    assert player_embed('/static/player/dplayer.html',url(),False) is None


@pytest.mark.asyncio
async def test_runtime_macplayer_url():
    from app.sources.donghuafun_browser import rendered_links
    class Page:
        async def evaluate(self, script):
            return {'configs':[], 'playUrl':'https://ok.ru/videoembed/123'}
    assert await rendered_links(Page(),url()) == (['https://ok.ru/videoembed/123'],'player_url')


@pytest.mark.asyncio
async def test_runtime_encoded_config():
    from app.sources.donghuafun_browser import rendered_links
    class Page:
        async def evaluate(self, script):
            return {'configs':[{'url':quote('https://rumble.com/embed/xyz',safe=''),'encrypt':1}], 'playUrl':''}
    assert await rendered_links(Page(),url()) == (['https://rumble.com/embed/xyz'],'player_url')
