"""Shared webhook authentication requiring no extra environment variable."""
import hashlib
import hmac


def webhook_secret(token: str) -> str:
    return hmac.new(token.encode(), b'dl-link-ext:telegram-webhook:v1', hashlib.sha256).hexdigest()
