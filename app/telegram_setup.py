"""Register the webhook without delaying HTTP/TCP readiness."""
import asyncio
import logging
import os
from urllib.parse import urlsplit

import httpx
from app.bot_config import webhook_secret

log = logging.getLogger(__name__)


def configuration():
    token = os.getenv('TELEGRAM_BOT_TOKEN', '').strip()
    base = os.getenv('PUBLIC_BASE_URL', '').strip().rstrip('/')
    return token, base


async def setup(app):
    token, base = configuration()
    if not token:
        app.state.telegram_status = {'status': 'disabled', 'detail': 'Set TELEGRAM_BOT_TOKEN in Koyeb and redeploy.'}
        log.warning(app.state.telegram_status['detail'])
        return
    try:
        p = urlsplit(base)
        valid = p.scheme == 'https' and p.hostname and not p.username and not p.password and not p.query and not p.fragment and p.path == ''
    except ValueError:
        valid = False
    if not valid:
        app.state.telegram_status = {'status': 'configuration_error', 'detail': 'Set PUBLIC_BASE_URL to your public HTTPS Koyeb origin, without a path, then redeploy.'}
        log.warning(app.state.telegram_status['detail'])
        return
    # Keep retrying transient startup failures; never hold up the web listener.
    async with httpx.AsyncClient(timeout=15) as client:
        while True:
            try:
                response = await client.post(f'https://api.telegram.org/bot{token}/setWebhook', json={
                    'url': base + '/telegram/webhook', 'secret_token': webhook_secret(token),
                    'allowed_updates': ['message', 'callback_query'], 'max_connections': 2,
                })
                if response.is_success and response.json().get('ok'):
                    app.state.telegram_status = {'status': 'registered', 'detail': 'Telegram accepted the webhook. Send /start in a private chat to test delivery.'}
                    log.warning('Telegram webhook registered successfully')
                    return
                code = response.status_code
                # Never include response URLs, token, or raw Telegram responses.
                detail = f'Telegram rejected webhook registration (HTTP {code}). Check TELEGRAM_BOT_TOKEN and PUBLIC_BASE_URL.'
            except (httpx.HTTPError, ValueError):
                detail = 'Could not reach Telegram to register webhook; retrying in 30 seconds.'
            app.state.telegram_status = {'status': 'registration_error', 'detail': detail}
            log.warning('%s Retrying in 30 seconds.', detail)
            await asyncio.sleep(30)
