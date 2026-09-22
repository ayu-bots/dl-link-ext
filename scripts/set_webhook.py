"""Run locally with secrets in the environment; never commit them."""
import os
import re
import httpx


def main():
    token = os.environ['TELEGRAM_BOT_TOKEN']
    secret = os.environ['TELEGRAM_WEBHOOK_SECRET']
    base = os.environ['PUBLIC_BASE_URL'].rstrip('/')
    if not re.fullmatch(r'[A-Za-z0-9_-]{1,256}', secret):
        raise SystemExit('Webhook secret must be 1–256 letters, digits, underscores or hyphens.')
    if not base.startswith('https://'):
        raise SystemExit('PUBLIC_BASE_URL must use HTTPS.')
    try:
        response = httpx.post(f'https://api.telegram.org/bot{token}/setWebhook', json={
            'url': base + '/telegram/webhook', 'secret_token': secret,
            'allowed_updates': ['message'], 'max_connections': 2,
        }, timeout=20)
        if not response.is_success or not response.json().get('ok'):
            raise SystemExit('Webhook registration failed. Check token and public service URL.')
    except httpx.HTTPError:
        raise SystemExit('Could not connect to Telegram.') from None
    print('Telegram webhook registered.')


if __name__ == '__main__':
    main()
