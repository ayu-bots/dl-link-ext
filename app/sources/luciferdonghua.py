"""luciferdonghua source adapter using the shared /v/N/ server menu parser."""
from . import server_menu
from .server_menu import embeds, metadata, web_url

HOSTS = {'luciferdonghua.in', 'www.luciferdonghua.in'}


def normalize(url: str) -> str:
    return server_menu.normalize(url, HOSTS, 'luciferdonghua.in')


def choices(html: str, base: str):
    return server_menu.choices(html, base, normalize)


async def extract(url: str, fetch) -> dict:
    return await server_menu.extract(url, fetch, normalize_url=normalize, source='luciferdonghua')
