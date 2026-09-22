# Multi-source Embed Extractor

Lightweight FastAPI service and web UI. **Animexin is the first adapter**; future sources plug into `app/sources/` and its registry.

## Features

- Paste an Animexin episode URL, a `?p=30101` short link, or a server URL ending in `/v/12/`.
- Reads advertised server choices rather than blindly probing server numbers.
- Supports numeric server choices, `/v/N/` links, direct embed URLs, and base64-encoded iframe options.
- Returns the original label, provider name, language, server page, embed URLs, and extraction status for each recognized choice.
- English/Indonesian labels are normalized. “All Sub” is reported as **Multilingual**, not an invented list of languages. Unspecified languages remain empty.
- Partial failures remain visible. Filter by language, copy links, or export JSON.
- No headless browser, video fetching, or third-party embed crawling.

## Run locally

```sh
python -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt
uvicorn app.main:app --host 0.0.0.0 --port 8000
```

Open `/` for the UI or `/docs` for interactive API documentation.

```sh
curl -G http://localhost:8000/api/extract \
  --data-urlencode 'url=https://animexin.dev/supreme-god-emperor-episode-642-indonesia-english-sub/v/12/'
```

Endpoints: `GET /api/extract?url=...`, `GET /api/sources`, `GET /healthz`.

Each response includes `source`, `url`, `title`, `servers`, `complete`, `warnings`, and `cached`. Each server contains `id`, `label`, `server`, `languages`, `page_url`, `embed_urls`, and `status` (`ok`, `unavailable`, or `error`). These are **embed links**, not resolved MP4 download links. Extracted does not mean playable.

## Koyeb deployment

1. Create a Web Service from this repository and select **Dockerfile** deployment.
2. Select the free instance where available for your account, with **one instance**.
3. Set `PORT=8000`, expose HTTP port **8000**, and route `/` to it.
4. Configure a **TCP health check on port 8000**. The app binds to `0.0.0.0`, so Koyeb can connect. Alternatively, use an HTTP check at `/healthz` on port 8000. If you change `PORT`, change the exposed port and health-check port to match.
5. Deploy. No database, persistent disk, secrets, or additional services are needed.

The Dockerfile runs as a non-root user with **one worker**. Requests share a connection pool and a global four-request semaphore. There are at most eight distinct extraction jobs, 64 server choices per episode, a 2 MB upstream response limit, a 55-second extraction deadline, and a 64-entry / five-minute successful-result cache. Concurrent requests for the same episode share a job. These bounds are intended for small instances; actual latency and memory should be measured on Koyeb. Free-instance sleep/cold starts and upstream latency are outside the extractor's control.

Only HTTPS source hosts are fetched; redirects are revalidated. Third-party embed URLs are returned, never fetched. If exposing this publicly at scale, add edge rate limiting/authentication. No CORS wildcard is needed; the UI uses same-origin API calls.

## Tests

```sh
pip install -r requirements-dev.txt
python -m pytest -q
```

Tests use representative HTML fixtures and mocked HTTP, not a dependency on the live site. They cover normalization, language labels, encoded embeds, server selection, player scoping, partial failures, caching, and redirect safety.

## Verification and limitations

The supplied episode's page text was accessible during development and showed the expected named server menu. However, direct HTTPS requests from the development sandbox failed during TLS connection setup, so **live HTML extraction and Koyeb deployment have not been verified**. Test from your deployment before relying on results. If it reports no supported choices, provide a saved episode HTML and a `/v/N/` page HTML to adapt the parser to the live markup.

JavaScript-only embeds, anti-bot challenges, expired links, and provider playback restrictions are not bypassed. Unknown option formats may require adapter updates. A server page without a recognized player container is reported unavailable rather than returning unrelated ad iframes. `complete` refers to recognized advertised choices, not independent proof that every site player was discovered.

## Telegram bot (same Koyeb service)

The optional webhook bot accepts normal messages, photo/video captions, and Telegram hidden text links. Supported inputs include:

- `https://animexin.dev/?p=30101` (WordPress short link)
- `https://animexin.dev/episode-slug/`
- `https://animexin.dev/episode-slug/v/12/`
- A link wrapped in `++...++` or Markdown link syntax

Short links resolve via the final redirect URL or the page's canonical permalink **before** constructing server URLs. Every advertised server is extracted, not just the pasted server. The bot processes the first supported link in each message and shows one inline button per provider (Dailymotion, Mega, etc.). Clicking a provider returns only its links, including all its available language variants, with original labels and explicit language names. Replies are plain text, split below Telegram's length limit. The website and extraction API still return all servers as before.

### Setup (automatic registration)

1. Create a bot with Telegram's **@BotFather**.
2. In Koyeb, set `TELEGRAM_BOT_TOKEN` as a **secret environment variable**.
3. Set `PUBLIC_BASE_URL` to your service's public origin, e.g. `https://your-service.koyeb.app` (no path).
4. Deploy/redeploy. The app registers the webhook automatically on startup, including its automatically derived authentication header. No separate `TELEGRAM_WEBHOOK_SECRET` variable or local registration command is needed.
5. Open `/api/telegram/status`: expect `status: registered`. Then send `/start` or an Animexin link in a **private chat** with your bot. Group privacy mode can prevent delivery of normal group messages.

Webhook registration runs in the background, so it cannot delay the web listener or TCP health checks. Transient registration errors retry every 30 seconds. Missing/invalid configuration is shown in logs and `/api/telegram/status`; correct the environment and redeploy. The status endpoint exposes no token, secret, chat IDs, or messages. It also shows whether an update has arrived and whether the last reply was sent. `registered` means Telegram accepted the configuration, not proof of successful end-to-end delivery.

The optional `scripts/set_webhook.py` remains available for manual registration using the same two environment variables. Do not use this bot token simultaneously with another deployed bot service: webhook registrations will overwrite each other. Do not put tokens in source code or share them in chat.

Webhook work runs in-process after acknowledgement, with at most eight active bot updates and a bounded in-memory duplicate-update list. A process restart can lose acknowledged work; resend the link if a deployment interrupts it. This intentionally lightweight setup is not a durable job queue. Telegram delivery errors are logged without token-bearing URLs; delivery is not automatically retried. For heavy traffic, add a durable queue and rate limiting.

**Verification:** automated tests cover short-link redirects/canonical URLs, wrapped links, captions, hidden links, webhook authentication and duplicate updates. A real Telegram end-to-end test still requires your configured bot and deployed service; no bot credentials were used during development.

TCP readiness does not depend on Telegram configuration or access to Animexin. A passing TCP check confirms the server is listening, not that upstream extraction or Telegram delivery succeeds.

## Koyeb buildpack deployment

Select **Buildpack** with repository root as the working directory. `requirements.txt` provides the dependencies, `.python-version` selects Python 3.12, and `Procfile` declares the web process. If Koyeb asks for an explicit **Run command**, enter:

```sh
uvicorn app.main:app --host 0.0.0.0 --port 8000 --workers 1
```

Set `PORT=8000`, expose HTTP port 8000 with route `/`, and choose TCP health checking on port 8000. For a custom port, change both the explicit command and Koyeb's port settings. Use **one instance and one worker** with the current in-memory job/deduplication implementation. Dockerfile deployment is still supported.

### Bot troubleshooting

- Website works, bot silent: check `/api/telegram/status`, not just `/healthz`.
- `disabled`: missing `TELEGRAM_BOT_TOKEN`.
- `configuration_error`: set `PUBLIC_BASE_URL` to the public HTTPS origin and redeploy.
- `registration_error`: review the sanitized error in status/logs; check the token, URL, and Telegram connectivity.
- `registered`, `last_update: none`: send `/start` in a private chat; ensure the public service is reachable and no other deployment is registering the same bot.
- Update received but delivery failed: check logs/status, verify the bot is not blocked, and retry the message. Upstream extraction may take up to 55 seconds.
- Changing tokens or migrating from older releases: redeploy; automatic registration replaces the old webhook and authentication header.

### Telegram provider buttons

The bot extracts the episode once and groups its results by provider. Selecting **Dailymotion**, for example, returns its English and Indonesian links (plus Multilingual when present), without Mega or other providers' links. Failed variants remain visible with an error instead of disappearing. The original menu stays usable for choosing another provider.

Menus are scoped to their chat, expire after 15 minutes, and are kept in a bounded 64-menu in-memory cache. Restarts or cache eviction invalidate older buttons; the bot asks users to resend the episode URL. In a group, anyone in the same chat can use a menu. No persistent database or additional upstream requests are needed for button clicks.

Redeploy this version to automatically register both `message` and `callback_query` updates with Telegram. No new environment variables are required.
