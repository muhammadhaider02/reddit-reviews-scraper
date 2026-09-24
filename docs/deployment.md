# Deployment

## Where it runs

| | |
|---|---|
| Host | the Hostinger VPS that runs the self-hosted n8n at `n8n.srv1980669.hstgr.cloud` |
| Checkout | `/opt/reddit-reviews-scraper`, a clone of `main` |
| Container | `reddit-reviews`, built from the `Dockerfile` by `docker-compose.yml` |
| Network | `n8n_default`, the network n8n's own Compose project created; declared external so this file never owns it |
| Address from n8n | `http://reddit-reviews:8001/reddit` |
| Beside it | `trustpilot-reviews` on `8000`, deployed the same way |
| Proxy | DataImpulse, sticky ports, US exits via the `__cr.us` username suffix |

Nothing is published to the host. Docker publishes straight past UFW, so even `8001:8001` behind the firewall would be public; the service is reachable only from containers on the network, and `API_TOKEN` still applies so a compromised container cannot drive it freely. The VPS already runs Traefik on 80/443 for n8n. There is no domain, no certificate and no reverse proxy for this service, and adding one would fail to bind.

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
git clone https://github.com/haider-ecombench/reddit-reviews-scraper.git /opt/reddit-reviews-scraper
cd /opt/reddit-reviews-scraper
cp .env.example .env          # set API_TOKEN and SCRAPER_PROXY
docker compose up -d --build  # first build is slow: it downloads Chromium
```

If the network name is wrong the container refuses to start with `network n8n_default declared as external, but could not be found`; `docker network ls` gives the real one.

## Update, rollback, logs

```bash
cd /opt/reddit-reviews-scraper
git pull origin main && docker compose up -d --build        # deploy main
git checkout <sha> && docker compose up -d --build           # roll back to a known commit
docker compose logs -f --tail 100                            # follow the service log
```

`.env` is read at container start (`env_file`), so a changed value needs `docker compose up -d`, not a restart of the process inside. Keep a dated copy before editing it (`.env.bak.<epoch>` is the convention used so far).

The container has no `curl`. Read health from inside it with:

```bash
docker exec reddit-reviews python -c "import urllib.request;print(urllib.request.urlopen('http://127.0.0.1:8001/health').read().decode())"
```

## Verifying a deploy

1. The startup log prints `proxy is attached: fetches leave from <ip> (sticky port 10001)`. If it prints a warning instead, the credential is wrong or the vendor is down. A wrong credential fails exactly like being blocked, so read this line before reading anything else.
2. Run a real search from the server:
   ```bash
   docker compose exec scraper uv run --no-sync reddit-reviews search "gymshark reviews" --max 3
   ```
   Expect posts. A `503` means no working proxy.
3. `/health` should show `proxy: true`, `auth: true`, `mobile.enabled: true`.

## Rotating credentials

- **`API_TOKEN`**: change it in `.env`, recreate the container, then update the n8n Header Auth credential `reddit-scraper` to match. Both scraper credentials in n8n are Header Auth with `Authorization: Bearer <token>`.
- **`SCRAPER_PROXY`**: change it in `.env` and recreate. The rotating-gateway guard runs at startup, so a wrong port fails loudly in the log rather than scraping direct. Retrieve tokens and credentials in a terminal, not in anything that keeps a transcript.

## Testing against the pipeline

The production workflow is not edited. Every change is exercised first in the n8n workflow **`scraper-testing`** (`0q7jtSF7FG0cbyBe`) on the same instance, which drives this container through the pipeline's own Reddit chain:

```
Loop Over Brands → Parse Keywords → Reddit Search → Sort Reddit Results
  → Has Strong Threads? → Reddit Comments → Fetch Reddit Comments → (loop) → Collect Results
```

- `Reddit Search` and `Reddit Comments` are HTTP Request nodes posting Stage 4's verbatim bodies to `http://reddit-reviews:8001/reddit` with the `reddit-scraper` credential, 290 s and 250 s timeouts, `onError: continueRegularOutput`.
- `Sort Reddit Results` and `Fetch Reddit Comments` are verbatim copies of the Stage 4 Code nodes (the same copies live in `tests/n8n/`).
- The loop runs one brand at a time because `Sort Reddit Results` reads `$('Parse Keywords').first()`; a batch would score every brand against the first brand's tokens.
- `Brand List` is the brand source. It has held the 103-brand Google Sheet and now holds the 26 brands that still have a real Apify result in the pipeline's data table, with that baseline on each item, so `Collect Results` reports hybrid and Apify side by side.
- `Parse Keywords` reconstructs the terms as brand, brand plus product, brand plus `reviews`. The pipeline does not store the terms Claude wrote, and it does not store Claude's `brand_keywords` either, so this harness under-keeps posts whose brand is written with a space on Reddit. Check the raw rows before blaming the scraper.

Run it from the n8n UI or over MCP (`execute_workflow`, manual mode). A 26-brand run takes about 14 minutes; 103 brands about 54. `Collect Results` on the loop's done output carries the totals and a per-brand table.

## What to watch

| Signal | Meaning |
|---|---|
| `/health` `blocked` or `failed` rising | proxy exits refused, or the browser is crashing; read the log for the port |
| `/health` `truncated` rising | `SCRAPE_BUDGET_S` is biting; too many brands in flight on the browser gate |
| `/health` `mobile.blocked` rising, or `mobile.calls` flat | Reddit closed the app route; results survive, bandwidth is ~20x |
| `/health` `mentions` flat while `Build Run Telemetry`'s `tavily_calls` climbs | the fallback node is not reaching this route; check the node's URL and the container's network |
| Proxy dashboard | ~5 MB per brand on the hybrid; ~90 MB per brand means the app route is down |
| `docker stats` memory near 2g | raise `mem_limit` before raising `MAX_CONCURRENCY`, never the other way round |

## Cutting the production workflow over

Not applied, and not to be applied without a decision. Recorded so the shape of the change is known. It is limited to the two HTTP Request nodes; keep their names, because `Build Run Telemetry` counts calls by node name.

| Node | Change |
|---|---|
| `Apify: Reddit Search` | URL → `http://reddit-reviews:8001/reddit`; authentication → the `reddit-scraper` Header Auth credential; drop the `maxTotalChargeUsd` and `timeout` query parameters. Body unchanged. |
| `Apify: Reddit Comments` | same |
| `Reddit Fallback Via Web Search` (Code node, workflow 02) | done 23 Sep 2026 (version `8ec36b32`): the `httpRequest` inside it → `POST http://reddit-reviews:8001/reddit/mentions` with `{ searchTerms, query, maxResults: 15, productKeywords, primaryProduct, domain }`, no Authorization header (the route is open, see `MENTIONS_REQUIRE_TOKEN`); gate, mapping, outcome strings and the `tavily_calls` / `tavily_errors` keys stay, because `Build Run Telemetry` sums those by name. `tests/n8n/reddit_fallback_via_web_search.js` is the live code. Before any change to it or to the route, run `scraper-testing`'s `Start Fallback Test` lane (14 Tavily-baseline brands, real Parse Keywords output) and read `Collect Fallback Results`. |

`Assess Vendors` gates on the Apify monthly limit; once Trustpilot and Reddit both leave Apify that gate protects nothing. Adding `reddit_search_terms` to the `Save Research Bundle` mapping would make future runs reproducible.

## CI

`.github/workflows/ci.yml` runs on pushes to `main`, pull requests and manual dispatch:

- **unit-tests**: `uv run pytest -q` on Python 3.11 with Node 22, so the Stage 4 node tests run.
- **docker-image**: builds the image, starts it with a token minted for that run only, and checks `/health`, that a missing token is `401`, that an empty body is `400`, and a live search. GitHub runners are datacenter IPs, so a `503` with `ScrapeBlocked` or `ScrapeFailed` passes; a `200` with posts passes; anything else fails.
