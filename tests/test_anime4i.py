import pytest
from app.sources import anime4i
from app.telegram import find_link

BASE = 'https://anime4i.com/martial-master-episode-694-english-subtitles'
DM = 'https://geo.dailymotion.com/player.html?video=k1TJxVjOaQy1EeKjpZo&autoplay=true&mute=false'
OK = 'https://ok.ru/videoembed/16520496941687'


def test_urls_and_source_intake():
    assert anime4i.normalize(BASE + '/') == BASE
    assert find_link({'text': '++' + BASE + '++'}) == BASE
    for url in ['https://anime4i.com/anime/series', BASE + '/v/1/', 'https://evil.test/episode', 'https://anime4i.com:8000/episode']:
        with pytest.raises(ValueError):
            anime4i.normalize(url)


@pytest.mark.parametrize('url,provider,expected', [
    (DM, 'Dailymotion', True), (OK, 'Ok.ru', True),
    (DM.replace('&', '&amp;'), 'Dailymotion', True),
    (DM, 'Ok.ru', False), (OK, 'Dailymotion', False),
    ('https://t.co/ad', 'Dailymotion', False),
    ('https://ok.ru.evil.test/videoembed/123', 'Ok.ru', False),
    ('https://ok.ru@127.0.0.1/videoembed/123', 'Ok.ru', False),
])
def test_provider_validation(url, provider, expected):
    assert anime4i.valid_embed(url, provider) == expected


@pytest.mark.parametrize('url,kind,expected', [
    (BASE, 'document', True), ('https://anime4i.com/wp-admin/admin-ajax.php', 'xhr', True),
    ('https://anime4i.com/player.js', 'script', True),
    ('https://t.co/ad', 'document', False), (OK, 'document', False),
    ('https://evil.test/ad.js', 'script', False),
    ('https://anime4i.com/video.mp4', 'media', False),
    ('http://169.254.169.254/latest', 'document', False),
])
def test_browser_network_policy(url, kind, expected):
    assert anime4i.allowed_request(url, kind) == expected


class Element:
    def __init__(self, page, kind): self.page, self.kind = page, kind
    @property
    def first(self): return self
    async def count(self): return 1
    async def is_visible(self): return True
    async def click(self, **kwargs): self.page.events.append(self.kind)
    async def get_attribute(self, attr): return self.page.embed
    async def all(self):
        return [self] if self.kind == 'play' or (self.kind == 'frame' and 'play' in self.page.events) else []
    def get_by_role(self, *args, **kwargs): return Element(self.page, 'play')
    def get_by_text(self, *args, **kwargs): return Element(self.page, 'play')


class Page:
    def __init__(self, embed): self.embed, self.events = embed, []
    def get_by_role(self, *args, **kwargs): return Element(self, 'server')
    def locator(self, selector): return Element(self, 'article' if selector == 'main article' else 'frame')
    async def wait_for_timeout(self, ms): pass


@pytest.mark.asyncio
@pytest.mark.parametrize('provider,embed', [('Dailymotion', DM), ('Ok.ru', OK)])
async def test_click_server_then_play_then_extract(provider, embed):
    page = Page(embed)
    await anime4i.select_provider(page, provider, '.*')
    assert await anime4i.load_player(page, provider) == embed
    assert page.events == ['server', 'play']


@pytest.mark.asyncio
async def test_ad_iframe_cannot_be_returned():
    page = Page('https://t.co/advertisement')
    assert await anime4i.load_player(page, 'Dailymotion') is None


@pytest.mark.parametrize('url,kind,allowed', [
    ('https://code.jquery.com/jquery-3.7.1.min.js', 'script', True),
    ('https://ajax.googleapis.com/ajax/libs/jquery/3.7.1/jquery.min.js', 'script', True),
    ('https://cdnjs.cloudflare.com/ajax/libs/jquery/3.7.1/jquery.min.js', 'script', True),
    ('https://code.jquery.com/jquery-3.7.1.min.js', 'document', False),
    ('https://code.jquery.com/advertising.js', 'script', False),
    ('https://code.jquery.com.evil.test/jquery-3.7.1.min.js', 'script', False),
])
def test_explicit_script_exceptions(url, kind, allowed):
    assert anime4i.allowed_request(url, kind) == allowed


@pytest.mark.asyncio
async def test_lazy_iframe_with_different_layout():
    class Frame:
        async def get_attribute(self, attr):
            return OK if attr == 'data-src' else ''
    class Frames:
        def __init__(self, selector): self.selector = selector
        async def all(self): return [Frame()] if self.selector == 'iframe' else []
    class DifferentLayout:
        def locator(self, selector): return Frames(selector)
    assert await anime4i.read_player(DifferentLayout(), 'Ok.ru') == OK
    assert await anime4i.read_player(DifferentLayout(), 'Dailymotion') is None


@pytest.mark.asyncio
async def test_image_based_play_overlay():
    events = []
    class Image:
        async def is_visible(self): return True
        async def click(self, **kwargs): events.append('poster clicked')
    class Controls:
        def __init__(self, images=False): self.images = images
        def get_by_role(self, *args, **kwargs): return Controls()
        def get_by_text(self, *args, **kwargs): return Controls()
        async def all(self): return [Image()] if self.images else []
    class ImagePage:
        def locator(self, selector): return Controls('img[alt=' in selector)
    assert await anime4i.click_play(ImagePage())
    assert events == ['poster clicked']


def test_capture_iframe_request_not_popup_or_ad():
    from types import SimpleNamespace as NS
    page = object()
    captured = []
    def req(url, owner=page, parent=True):
        return NS(url=url, resource_type='document', frame=NS(page=owner, parent_frame=object() if parent else None))
    anime4i.capture_request(req(DM), page, 'Dailymotion', captured)
    assert captured == [DM]
    captured.clear()
    for request in [req(DM, parent=False), req(DM, owner=object()), req('https://t.co/ad'), req(OK)]:
        anime4i.capture_request(request, page, 'Dailymotion', captured)
    assert captured == []


@pytest.mark.asyncio
async def test_transient_iframe_request_fallback():
    page = Page('https://t.co/ad')
    assert await anime4i.load_player(page, 'Dailymotion', [DM]) == DM
    assert page.events == []
