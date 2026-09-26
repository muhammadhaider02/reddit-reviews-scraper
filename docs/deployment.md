# Deployment

## Where it runs

| | |
|---|---|
| Host | a Docker host that also runs n8n |
| Checkout | `~/reddit-reviews-scraper`, a clone of `main` |
| Container | `reddit-reviews`, built from the `Dockerfile` by `docker-compose.yml` |
| Network | `n8n_default`, the network n8n's own Compose project created; declared external so this file never owns it |
| Address from n8n | `http://reddit-reviews:8001/reddit` and `/reddit/mentions` |
| Beside it | `trustpilot-reviews` on `8000` and `facebook-ad-library` on `8003`, deployed the same way |
| Proxy | DataImpulse, sticky ports, US exits via the `__cr.us` username suffix |

Nothing is published to the host. Docker publishes straight past UFW, so even `8001:8001` behind the firewall would be public; the service is reachable only from containers on the network, and `API_TOKEN` still applies to `/reddit` so a compromised container cannot drive it freely. There is no domain, certificate or reverse proxy for this service; if the host already runs one on 80/443 for n8n, a second would fail to bind.

## What the compose file sets, and why

| Setting | Value | Reason |
|---|---|---|
| `mem_limit` / `memswap_limit` | 2g / 2g | measured peak 859 MiB at `MAX_CONCURRENCY=3`; swap off so a thrashing browser dies instead of crawling |
| `pids_limit` | 1024 | 358 PIDs measured at concurrency 3; keeps memory, not PIDs, as the binding cap |
| `init: true` | | reaps Chromium's orphaned process trees after a timed-out fetch |
| `shm_size` | 256m | insurance for Chromium paths that ignore `--disable-dev-shm-usage` |
| `stop_grace_period` | 210s | `SCRAPE_BUDGET_S` plus teardown, so a redeploy never kills an in-flight scrape |
| logging | json-file, 10m × 3 | Chromium is verbose enough to fill the disk otherwise |

Do not lower `MAX_CONCURRENCY` to save memory: at 1 the same search took 239 s against the 290 s node timeout.

## First deploy

```bash
git clone https://github.com/automation-ecombench/reddit-reviews-scraper.git ~/reddit-reviews-scraper
cd ~/reddit-reviews-scraper
cp .env.example .env          # set API_TOKEN and SCRAPER_PROXY
docker compose up -d --build  # first build is slow: it downloads Chromium
```

If the network name is wrong the container refuses to start with `network n8n_default declared as external, but could not be found`; `docker network ls` gives the real one.

## Update, rollback, logs

```bash
cd ~/reddit-reviews-scraper
git pull origin main && docker compose up -d --build        # deploy main
git checkout <sha> && docker compose up -d --build           # roll back to a known commit
docker compose logs -f --tail 100                            # follow the service log
```

`.env` is read at container start (`env_file`), so a changed value needs `docker compose up -d`, not a restart of the process inside. Keep a dated copy before editing it.

The container has no `curl`. Read health from inside it with:

```bash
docker exec reddit-reviews python -c "import urllib.request;print(urllib.request.urlopen('http://127.0.0.1:8001/health').read().decode())"
```

## Verifying a deploy

1. The startup log prints `proxy is attached: fetches leave from <ip> (sticky port 10001)`. If it prints a warning instead, the credential is wrong or the vendor is down. A wrong credential fails exactly like being blocked, so read this line before reading anything else.
2. Run a real search from the host:
   ```bash
   docker compose exec scraper uv run --no-sync reddit-reviews search "gymshark reviews" --max 3
   ```
   Expect posts. A `503` means no working proxy.
3. `/health` should show `proxy: true`, `auth: true`, `mobile.enabled: true`.

## Pointing an n8n workflow at it

- **`/reddit`**: an HTTP Request node, `POST http://reddit-reviews:8001/reddit`, authenticated with a Header Auth credential sending `Authorization: Bearer <API_TOKEN>`. Replacing the Apify actor's nodes (`Apify: Reddit Search`, `Apify: Reddit Comments`) means changing only the URL and the credential and dropping Apify's `maxTotalChargeUsd` and `timeout` query parameters; the body is unchanged. Set **On Error** to continue (`continueRegularOutput`) so a `503` arrives as an item carrying `error` for the downstream Code node, and keep the node timeouts (290 s search, 250 s comments) above `SCRAPE_BUDGET_S`.
- **`/reddit/mentions`**: called from a Code node with `this.helpers.httpRequest` (`tests/n8n/reddit_fallback_via_web_search.js` is such a node). It is unauthenticated by default, because a Code node cannot read n8n credentials and the container publishes no ports. `MENTIONS_REQUIRE_TOKEN=true` puts the same bearer check on it, for when the call moves into an HTTP Request node. `/health` reports the setting as `mentions_auth`.

## Rotating credentials

- **`API_TOKEN`**: change it in `.env`, recreate the container, then update the n8n Header Auth credential to match.
- **`SCRAPER_PROXY`**: change it in `.env` and recreate. The rotating-gateway guard runs at startup, so a wrong port fails loudly in the log rather than scraping direct. Handle tokens and credentials in a terminal, not in anything that keeps a transcript.

## What to watch

| Signal | Meaning |
|---|---|
| `/health` `blocked` or `failed` rising | proxy exits refused, or the browser is crashing; read the log for the port |
| `/health` `truncated` rising | `SCRAPE_BUDGET_S` is biting; too many brands in flight on the browser gate |
| `/health` `mobile.blocked` rising, or `mobile.calls` flat | Reddit closed the app route; results survive, bandwidth is ~20x |
| `/health` `mentions` flat while the workflow's fallback fires | the Code node is not reaching this route; check its URL and the container's network |
| Proxy dashboard | ~5 MB per brand on the hybrid; ~90 MB per brand means the app route is down |
| `docker stats` memory near 2g | raise `mem_limit` before raising `MAX_CONCURRENCY`, never the other way round |

## CI

`.github/workflows/ci.yml` runs on pushes to `main`, pull requests and manual dispatch:

- **unit-tests**: `uv run pytest -q` on Python 3.11 with Node 22, so the n8n node tests run.
- **docker-image**: builds the image, starts it with a token minted for that run only, and checks `/health`, that a missing token on `/reddit` is `401`, that an empty body is `400`, that `/reddit/mentions` is open and answers an empty body with `400`, and a live search. GitHub runners are datacenter IPs, so a `503` with `ScrapeBlocked` or `ScrapeFailed` passes; a `200` with posts passes; anything else fails.
