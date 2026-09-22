"""Optional Telegram webhook, hosted alongside the extractor (no polling worker)."""
from collections import OrderedDict
import logging
import os
import re
import secrets
import time
from urllib.parse import urlsplit

import httpx
from fastapi import APIRouter, BackgroundTasks, HTTPException, Request

from app.bot_config import webhook_secret

router = APIRouter()
seen = OrderedDict()
active = set()
menus = OrderedDict()
MENU_TTL = 900
MENU_LIMIT = 64
log = logging.getLogger(__name__)


def find_link(message):
    candidates = [e.get('url', '') for e in message.get('entities', []) + message.get('caption_entities', [])]
    candidates += re.findall(r'https://[^\s<>\[\]"\u201d]*',
                             message.get('text', '') or message.get('caption', ''), re.I)
    from app.sources import SOURCES
    for candidate in candidates:
        try:
            candidate = candidate.rstrip(')+*.,!')
            source = next((s for s in SOURCES.values() if urlsplit(candidate).hostname in s.HOSTS), None)
            if source:
                return source.normalize(candidate)
        except ValueError:
            continue
    return None


def replies(result):
    # Plain text avoids Telegram Markdown/HTML injection. Conservative chunk
    # size also leaves room for UTF-16 surrogate pairs in Telegram's limit.
    text = result['title'] + '\n\n'
    for server in result['servers']:
        text += server['label'] + '\n'
        text += 'Language: ' + (', '.join(server['languages']) or 'Unspecified') + '\n'
        if server.get('link_type') == 'player_url' and server['embed_urls']:
            text += 'Published player URL (not resolved to a final embed):\n'
        text += '\n'.join(server['embed_urls']) or server.get('error', 'Unavailable')
        text += '\n\n'
    if result['warnings']:
        text += '\n'.join(result['warnings'])
    return [text[i:i + 1800] for i in range(0, len(text), 1800)]


def server_menu(result, chat_id):
    now = time.monotonic()
    for key in list(menus):
        if now - menus[key]['created'] >= MENU_TTL:
            del menus[key]
    providers = list(dict.fromkeys(row['server'] for row in result['servers']))
    key = secrets.token_hex(8)
    menus[key] = {'created': now, 'chat_id': chat_id, 'result': result, 'providers': providers}
    while len(menus) > MENU_LIMIT:
        menus.popitem(last=False)
    buttons = [{'text': name[:60], 'callback_data': f'srv:{key}:{i}'} for i, name in enumerate(providers)]
    return {'inline_keyboard': [buttons[i:i+2] for i in range(0, len(buttons), 2)]}


async def send(client, token, chat_id, text, markup=None):
    from app.main import app
    payload = {'chat_id': chat_id, 'text': text, 'link_preview_options': {'is_disabled': True}}
    if markup:
        payload['reply_markup'] = markup
    response = await client.post(f'https://api.telegram.org/bot{token}/sendMessage', json=payload)
    if not response.is_success or not response.json().get('ok'):
        app.state.telegram_last_delivery = f'failed (HTTP {response.status_code})'
        log.warning('Telegram delivery failed; HTTP status %s', response.status_code)
        return False
    app.state.telegram_last_delivery = 'sent'
    return True


async def handle_callback(update_id, query, token):
    from app.main import app
    try:
        chat_id = query['message']['chat']['id']
        match = re.fullmatch(r'srv:([0-9a-f]{16}):([0-9]{1,2})', query.get('data', ''))
        menu = menus.get(match[1]) if match else None
        index = int(match[2]) if match else -1
        valid = (menu is not None and menu['chat_id'] == chat_id
                 and time.monotonic() - menu['created'] < MENU_TTL
                 and 0 <= index < len(menu['providers']))
        async with httpx.AsyncClient(timeout=15) as client:
            # Stop the Telegram button spinner before sending the links.
            await client.post(f'https://api.telegram.org/bot{token}/answerCallbackQuery', json={
                'callback_query_id': query['id'],
                'text': 'Showing selected server' if valid else 'Menu expired or unavailable. Send the episode link again.',
                'show_alert': not valid,
            })
            if not valid:
                return
            provider = menu['providers'][index]
            result = menu['result']
            selected = [row for row in result['servers'] if row['server'] == provider]
            filtered = {**result, 'servers': selected, 'warnings': []}
            for text in replies(filtered):
                if not await send(client, token, chat_id, text):
                    break
    except Exception:
        app.state.telegram_last_delivery = 'failed during callback processing or connection to Telegram'
        log.warning('Telegram callback processing failed')
    finally:
        active.discard(update_id)


async def handle(update_id, message, token):
    from app.main import app, extract
    try:
        url = find_link(message)
        command = (message.get('text', '').split() or [''])[0].split('@')[0].lower()
        markup = None
        if command in ('/start', '/help') or not url:
            texts = ['Send an Animexin, Lucifer Donghua, Anime4i or DonghuaFun episode URL, /v/N/ server URL, or supported short link. Choose a server button to get its links in all available languages.']
        else:
            try:
                result = await extract(url)
                markup = server_menu(result, message['chat']['id'])
                texts = [result['title'][:500] + '\n\nChoose a server below. Each button returns all available languages for that provider.\nButtons expire after 15 minutes or a service restart.'
                         + ('\nSome servers could not be extracted; select them to see their status.' if result['warnings'] else '')]
            except HTTPException as exc:
                texts = [str(exc.detail)]
        async with httpx.AsyncClient(timeout=15) as client:
            for text in texts:
                if not await send(client, token, message['chat']['id'], text, markup):
                    break
    except Exception:
        app.state.telegram_last_delivery = 'failed during processing or connection to Telegram'
        log.warning('Telegram update processing failed')
    finally:
        active.discard(update_id)


@router.post('/telegram/webhook', include_in_schema=False)
async def webhook(request: Request, background: BackgroundTasks):
    token = os.getenv('TELEGRAM_BOT_TOKEN', '').strip()
    if not token:
        raise HTTPException(503, 'Telegram bot is not configured')
    secret = webhook_secret(token)
    if not secrets.compare_digest(request.headers.get('X-Telegram-Bot-Api-Secret-Token', ''), secret):
        raise HTTPException(403, 'Invalid webhook secret')
    body = bytearray()
    async for chunk in request.stream():
        body.extend(chunk)
        if len(body) > 64_000:
            raise HTTPException(413, 'Update too large')
    import json
    try:
        update = json.loads(body)
        update_id = update['update_id']
        if not isinstance(update_id, int):
            raise ValueError()
    except (ValueError, KeyError, TypeError):
        raise HTTPException(400, 'Invalid Telegram update')
    if update_id in seen or update_id in active:
        return {'ok': True}
    query = update.get('callback_query')
    message = query.get('message') if isinstance(query, dict) else update.get('message')
    if not isinstance(message, dict) or not isinstance(message.get('chat', {}).get('id'), int):
        return {'ok': True}
    if len(active) >= 8:
        raise HTTPException(429, 'Bot is busy; retry later')
    seen[update_id] = True
    while len(seen) > 256:
        seen.popitem(last=False)
    active.add(update_id)
    request.app.state.telegram_last_update = 'received'
    if isinstance(query, dict):
        background.add_task(handle_callback, update_id, query, token)
    else:
        background.add_task(handle, update_id, message, token)
    return {'ok': True}


@router.get('/api/telegram/status')
async def telegram_status(request: Request):
    # No token, webhook secret, chat IDs, or message content is exposed.
    return {
        **getattr(request.app.state, 'telegram_status', {'status': 'starting'}),
        'last_update': getattr(request.app.state, 'telegram_last_update', 'none'),
        'last_delivery': getattr(request.app.state, 'telegram_last_delivery', 'none'),
    }
