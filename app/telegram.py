"""Optional Telegram webhook, hosted alongside the extractor (no polling worker)."""
from collections import OrderedDict
import logging
import os
import re
import secrets

import httpx
from fastapi import APIRouter, BackgroundTasks, HTTPException, Request

from app.bot_config import webhook_secret

router = APIRouter()
seen = OrderedDict()
active = set()
log = logging.getLogger(__name__)


def find_link(message):
    candidates = [e.get('url', '') for e in message.get('entities', []) + message.get('caption_entities', [])]
    candidates += re.findall(r'https://(?:www\.)?animexin\.dev/[^\s<>\[\]"\u201d]*',
                             message.get('text', '') or message.get('caption', ''), re.I)
    from app.sources.animexin import normalize
    for candidate in candidates:
        try:
            return normalize(candidate.rstrip(')+*.,!'))
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
        text += '\n'.join(server['embed_urls']) or server.get('error', 'Unavailable')
        text += '\n\n'
    if result['warnings']:
        text += '\n'.join(result['warnings'])
    return [text[i:i + 1800] for i in range(0, len(text), 1800)]


async def handle(update_id, message, token):
    from app.main import extract
    try:
        url = find_link(message)
        if not url:
            texts = ['Send an Animexin episode URL, /v/N/ server URL, or https://animexin.dev/?p=30101. I will extract all advertised servers.']
        else:
            try:
                texts = replies(await extract(url))
            except HTTPException as exc:
                texts = [str(exc.detail)]
        async with httpx.AsyncClient(timeout=15) as client:
            for text in texts:
                response = await client.post(f'https://api.telegram.org/bot{token}/sendMessage', json={
                    'chat_id': message['chat']['id'], 'text': text,
                    'link_preview_options': {'is_disabled': True},
                })
                if not response.is_success or not response.json().get('ok'):
                    # Never log the response URL: it contains the bot token.
                    log.warning('Telegram delivery failed; HTTP status %s', response.status_code)
                    break
    except Exception:
        log.warning('Telegram update processing failed')
    finally:
        active.discard(update_id)


@router.post('/telegram/webhook', include_in_schema=False)
async def webhook(request: Request, background: BackgroundTasks):
    token = os.getenv('TELEGRAM_BOT_TOKEN', '')
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
    message = update.get('message')
    if not isinstance(message, dict) or not isinstance(message.get('chat', {}).get('id'), int):
        return {'ok': True}
    if len(active) >= 8:
        raise HTTPException(429, 'Bot is busy; retry later')
    seen[update_id] = True
    while len(seen) > 256:
        seen.popitem(last=False)
    active.add(update_id)
    background.add_task(handle, update_id, message, token)
    return {'ok': True}
