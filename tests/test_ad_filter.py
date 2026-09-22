import base64
import pytest
from app.sources import animexin, luciferdonghua
from app.sources.server_menu import web_url, embeds


@pytest.mark.parametrize('url', [
    'https://t.co/', 'https://t.co/ad123', 'http://t.co/ad',
    '//t.co/ad', 'https://T.CO/ad', 'https://www.t.co/ad',
    'https://t.co./ad',
])
def test_block_shortener(url):
    assert web_url(url, 'https://luciferdonghua.in/episode/') is None


def test_preserve_video_hosts():
    for url in ['https://www.dailymotion.com/embed/video/x', 'https://mega.nz/embed/x#key',
                'https://rumble.com/embed/x/', 'https://vidhide.test/embed/x',
                'https://ok.ru/videoembed/123', 'https://player.test/embed/t.co/video']:
        assert web_url(url, 'https://luciferdonghua.in/episode/') == url


@pytest.mark.parametrize('source,base', [
    (animexin, 'https://animexin.dev/episode/'),
    (luciferdonghua, 'https://luciferdonghua.in/episode/'),
])
@pytest.mark.asyncio
async def test_filter_ads_in_server_and_encoded_options(source, base):
    mixed = '<iframe src="https://t.co/ad"></iframe><iframe src="https://www.dailymotion.com/embed/video/x"></iframe>'
    assert embeds('<div id="pembed">' + mixed + '</div>', base, True) == ['https://www.dailymotion.com/embed/video/x']
    encoded = base64.b64encode(mixed.encode()).decode()
    html = f'<select><option value="1">Eng Dailymotion</option><option value="{encoded}">Indo Dailymotion</option><option value="https://t.co/ad">Advertisement</option></select>'
    async def fetch(url):
        return html if url == base else '<div id="pembed">' + mixed + '</div>'
    result = await source.extract(base, fetch)
    assert result['complete']
    assert len(result['servers']) == 2
    assert all(row['embed_urls'] == ['https://www.dailymotion.com/embed/video/x'] for row in result['servers'])
