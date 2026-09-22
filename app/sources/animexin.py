"""animexin source adapter using the shared /v/N/ server menu parser."""
from . import server_menu
from .server_menu import embeds, metadata, web_url

HOSTS = {'animexin.dev', 'www.animexin.dev'}


def normalize(url: str) -> str:
    return server_menu.normalize(url, HOSTS, 'animexin.dev')


def choices(html: str, base: str):
    return server_menu.choices(html, base, normalize)


async def extract(url: str, fetch) -> dict:
    return await server_menu.extract(url, fetch, normalize_url=normalize, source='animexin')
