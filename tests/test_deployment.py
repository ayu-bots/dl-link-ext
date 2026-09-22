from pathlib import Path


def test_browser_image_uses_supported_debian_release():
    dockerfile = Path('Dockerfile').read_text()
    assert 'FROM python:3.12-slim-bookworm\n' in dockerfile
    assert 'playwright install --with-deps chromium' in dockerfile
    assert 'PLAYWRIGHT_BROWSERS_PATH=/ms-playwright' in dockerfile
    assert 'USER appuser' in dockerfile
