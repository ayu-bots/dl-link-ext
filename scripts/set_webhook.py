"""Run locally with secrets in the environment; never commit them."""
import os
import sys
from pathlib import Path
import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app.bot_config import webhook_secret


def main():
    token = os.environ['TELEGRAM_BOT_TOKEN']
    secret = webhook_secret(token)
    base = os.environ['PUBLIC_BASE_URL'].rstrip('/')
    if not base.startswith('https://'):
        raise SystemExit('PUBLIC_BASE_URL must use HTTPS.')
    try:
        response = httpx.post(f'https://api.telegram.org/bot{token}/setWebhook', json={
            'url': base + '/telegram/webhook', 'secret_token': secret,
            'allowed_updates': ['message', 'callback_query'], 'max_connections': 2,
        }, timeout=20)
        if not response.is_success or not response.json().get('ok'):
            raise SystemExit('Webhook registration failed. Check token and public service URL.')
    except httpx.HTTPError:
        raise SystemExit('Could not connect to Telegram.') from None
    print('Telegram webhook registered.')


if __name__ == '__main__':
    main()
