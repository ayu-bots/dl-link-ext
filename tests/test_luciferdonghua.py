"""Representative menu fixtures based on the supplied screenshot (not live HTML)."""
import pytest
from fastapi.testclient import TestClient
from app.sources import luciferdonghua as source
from app.telegram import find_link, server_menu, menus
from app.main import app, cache

BASE = 'https://luciferdonghua.in/shrouding-the-heavens-episode-182-lucifer-donghua/'
LABELS = [
    '[4K] Indo + Eng [Dailymotion Little Ads - Server]',
    '[4K] Eng [RUMBLE - Server]',
    '[4K] Eng+Indo [VID HIDE- Server]',
    '[4K] Eng [OK.RU - Server]',
    '[1080p] Eng [With Ads - Server]',
]


def menu():
    # Mix numeric, absolute and relative server choices; duplicate mobile menu.
    values = ['1', BASE + 'v/2/', '/shrouding-the-heavens-episode-182-lucifer-donghua/v/3/', '4', '5']
    options = ''.join(f'<option value="{value}">{label}</option>' for value, label in zip(values, LABELS))
    return '<h1>Episode 182</h1>' + ('<select><option value="">Select Video Server</option>' + options + '</select>') * 2


def test_lucifer_labels():
    title, rows, truncated = source.choices(menu(), BASE)
    assert len(rows) == 5 and not truncated
    assert [row['server'] for row in rows] == ['Dailymotion', 'Rumble', 'VidHide', 'Ok.ru', 'With Ads']
    assert rows[0]['languages'] == rows[2]['languages'] == ['English', 'Indonesian']
    assert [row['label'] for row in rows] == LABELS
    assert rows[-1]['languages'] == ['English']
    buttons = server_menu({'servers':rows}, 1)
    assert len([b for group in buttons['inline_keyboard'] for b in group]) == 5
    menus.clear()


@pytest.mark.asyncio
async def test_lucifer_all_servers():
    calls = []
    async def fetch(url):
        calls.append(url)
        if url == BASE:
            return menu()
        number = url.rstrip('/').split('/')[-1]
        return f'<iframe src="https://ads.test/"></iframe><div id="pembed"><iframe src="https://player.test/embed/{number}"></iframe></div>'
    result = await source.extract(BASE + 'v/3/', fetch)
    assert result['source'] == 'luciferdonghua' and result['complete']
    assert set(calls) == {BASE, *(BASE + f'v/{n}/' for n in range(1,6))}
    assert len(calls) == 6
    for row in result['servers']:
        assert row['embed_urls'] == [f"https://player.test/embed/{row['id']}"]


def test_lucifer_input_and_isolation():
    assert source.normalize(BASE.rstrip('/')) == BASE
    assert source.normalize(BASE + 'v/2/') == BASE
    assert find_link({'text': f'++[{BASE}]({BASE})++'}) == BASE
    assert find_link({'entities':[{'type':'text_link', 'url': BASE}]}) == BASE
    with pytest.raises(ValueError):
        source.normalize('https://animexin.dev/episode/')
    with pytest.raises(ValueError):
        source.normalize('https://luciferdonghua.in.evil.test/episode/')
    _, rows, _ = source.choices('<select><option value="https://animexin.dev/episode/v/1/">Eng Mega</option></select>', BASE)
    assert rows == []


def test_lucifer_api(monkeypatch):
    import app.main as main
    cache.clear()
    async def fetch(url):
        if url == BASE:
            return menu()
        return '<div id="pembed"><iframe src="https://player.test/embed/one"></iframe></div>'
    monkeypatch.setattr(main, 'fetch', fetch)
    with TestClient(app) as client:
        names = [s['name'] for s in client.get('/api/sources').json()['sources']]
        assert names == ['animexin', 'luciferdonghua', 'anime4i', 'donghuafun']
        response = client.get('/api/extract', params={'url': BASE.rstrip('/')})
        assert response.status_code == 200
        assert response.json()['source'] == 'luciferdonghua'
        assert len(response.json()['servers']) == 5
