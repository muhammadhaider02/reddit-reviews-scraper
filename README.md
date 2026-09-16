<div align="center">

# Reddit Reviews

**SEARCH. READ. SERVE.**

[![CI](https://github.com/haider-ecombench/reddit-reviews-scraper/actions/workflows/ci.yml/badge.svg)](https://github.com/haider-ecombench/reddit-reviews-scraper/actions/workflows/ci.yml)
[![Python](https://img.shields.io/badge/Python-3.11+-3776AB?logo=python&logoColor=white)](https://python.org)
[![uv](https://img.shields.io/badge/uv-Package_Manager-DE5FE9?logo=uv&logoColor=white)](https://docs.astral.sh/uv/)
[![FastAPI](https://img.shields.io/badge/API-FastAPI-009688?logo=fastapi&logoColor=white)](https://fastapi.tiangolo.com)
[![Scrapling](https://img.shields.io/badge/Browser-Scrapling-FF6F00)](https://github.com/D4Vinci/Scrapling)

Self-hosted Reddit post and comment scraper with an HTTP API.

[Quickstart](#quickstart) · [API](#api) · [Stage 4](#stage-4) · [Configuration](#configuration) · [Development](#development) · [Deployment](#deployment)

</div>

---

## Platform

This repo is a standalone Reddit collection service, the sibling of `trustpilot-reviews`. It replaces the `harshmaur~reddit-scraper` Apify actor in the Stage 4 research workflow: it accepts the same request body and returns the same dataset item shape, so the workflow's Code nodes read it unchanged.

Reddit's JSON endpoints (`/search.json`, `/comments/<id>.json`) answer `403 blocked by network security` to plain HTTP clients and to the stealth browser alike. The regular web pages load in a stealth browser and are server-rendered with the data in element attributes (`search-telemetry-tracker`, `shreddit-post`, `shreddit-comment`), which is what this service reads.

## Quickstart

Requires Python 3.11+, [uv](https://docs.astral.sh/uv/) and, for the Stage 4 node tests, Node.js. Runs as a single FastAPI process.

```bash
uv sync                       # dependencies into a uv-managed venv
uv run scrapling install      # stealth browser (once)

cp .env.example .env          # then set API_TOKEN (see Configuration)

uv run reddit-reviews serve   # HTTP service on :8001
```

Verify with `curl localhost:8001/health` (expects `"status":"ok"`); interactive API docs are at `/docs`.

One-off scrapes from the command line, no server needed:

```bash
uv run reddit-reviews search "Gymshark" "Gymshark reviews" --max 10 --pretty
uv run reddit-reviews thread https://www.reddit.com/r/Gymshark/comments/1vf53rw/ --max-comments 20 --pretty
```

## API

`POST /reddit` with `Authorization: Bearer <API_TOKEN>`. The body decides the mode.

**Search** (replaces `Apify: Reddit Search`)

```json
{ "searchTerms": ["Gymshark", "Gymshark reviews"], "searchSort": "relevance", "searchTime": "all", "maxPostsCount": 10 }
```

**Threads and comments** (replaces `Apify: Reddit Comments`)

```json
{ "startUrls": [{ "url": "https://www.reddit.com/r/Gymshark/comments/1vf53rw/" }], "maxCommentsPerPost": 20, "maxCommentsCount": 160 }
```

| Field | Default | Notes |
|---|---|---|
| `searchTerms` | – | terms run in parallel; posts de-duplicated across terms, in term order |
| `searchSort` | `relevance` | `relevance`, `hot`, `top`, `new`, `comments` |
| `searchTime` | `all` | `all`, `hour`, `day`, `week`, `month`, `year` |
| `maxPostsCount` | `10` | posts per search term (Reddit pages hold ~7, so the cursor is followed) |
| `includeNSFW` | `false` | drop NSFW posts |
| `fullBodies` | `FULL_BODIES` | read each post's thread page for its full body |
| `startUrls` | – | thread links, `[{"url"}]` or strings; any www/old reddit form |
| `maxCommentsPerPost` | `20` | comments kept per thread, in Reddit's "best" order |
| `maxCommentsCount` | none | total comments across the call |

Other Apify fields (`proxy`, `searchComments`, `crawlCommentsPerPost`, `maxCommunitiesCount`, …) and the `maxTotalChargeUsd` / `timeout` query parameters are accepted and ignored. Reddit's spell correction is always disabled so brand names are searched literally.

Returns a JSON array. Posts: `dataType:"post"`, `id`, `parsedId`, `title`, `body`, `bodyIsSnippet`, `communityName` (`r/…`), `subredditName`, `authorName`, `createdAt`, `score`/`upVotes`, `commentsCount`/`numberOfComments`, `postUrl`/`url`, `nsfw`, `searchTerm`. Comments follow their post: `dataType:"comment"`, `id`, `postId`, `parentId`, `body`, `communityName`, `subredditName`, `authorName`, `createdAt`/`commentCreatedAt`, `score`/`upVotes`, `depth`, `url`. Timestamps are ISO 8601 UTC with a `Z`.

Response headers `X-Scrape-Seconds`, `X-Pages-Fetched`, `X-Terms-Failed` (search) and `X-Threads-Missing` / `X-Threads-Failed` (threads) help when debugging a caller.

| Status | Meaning |
|---|---|
| `200` | items, possibly `[]`; also when some terms or threads failed but others worked |
| `400` | no `searchTerms` and no `startUrls`, or no link was a thread |
| `401` | missing or wrong bearer token |
| `503` | every fetch was refused by Reddit or the browser failed |

Deleted or private threads are skipped, never an error. Error bodies are `{"error": {"type", "status", "message", "description"}}`; `503` messages never contain `404`, `not found` or `invalid url`, so Stage 4 treats them as a vendor failure and burns no brand retry.

`GET /health` is unauthenticated and returns request counters.

## Stage 4

The n8n change is limited to the two HTTP Request nodes. Keep their names: `Build Run Telemetry` counts calls by node name.

| Node | Change |
|---|---|
| `Apify: Reddit Search` | URL → `https://<host>/reddit`; authentication → Header Auth credential `Authorization: Bearer <API_TOKEN>`; remove the `maxTotalChargeUsd` and `timeout` query parameters. Body unchanged. |
| `Apify: Reddit Comments` | same |

The node timeouts (290 s / 250 s) stay. `Assess Vendors` gates on the Apify monthly limit; once Trustpilot and Reddit both leave Apify that gate no longer protects anything.

`tests/n8n/` holds verbatim copies of **Sort Reddit Results** and **Fetch Reddit Comments** and a script that replays the whole Reddit chain against a running server, so all of this is tested locally before the workflow is touched. See [tests/n8n/README.md](tests/n8n/README.md).

## Configuration

All configuration is environment variables in `.env`. **`.env.example` is the canonical list.**

| Variable | Default | Purpose |
|---|---|---|
| `API_TOKEN` | *(empty)* | bearer token callers must send; required in production |
| `SCRAPER_PROXY` | *(none)* | proxy URL for the browser; expect to need a residential one on a datacenter host |
| `MAX_CONCURRENCY` | `3` | browser fetches at once, across all requests |
| `FETCH_TIMEOUT_MS` | `45000` | per-page browser timeout |
| `MAX_SEARCH_PAGES` | `3` | pages followed per search term |
| `FULL_BODIES` | `true` | read thread pages for full post bodies |
| `MAX_BODY_FETCHES` | `30` | thread pages read for bodies per search call |
| `SEARCH_BUDGET_S` | `200` | no new body fetch starts after this many seconds |
| `MAX_THREADS` | `10` | thread links read per comments call |
| `RETRY_DELAY_S` | `2` | pause before retrying a refused page |
| `PORT` | `8001` | listen port (8000 is trustpilot-reviews) |

## Development

```bash
uv run pytest            # unit + Stage 4 node tests against saved pages, no network
uv run pytest -m live    # one real search and thread through the stealth browser
```

Layout: `scraper.py` (URLs, fetch, parse, orchestration), `mapping.py` (output shape), `api.py` (FastAPI surface), `config.py` (env). Fixtures in `tests/fixtures/` are real Reddit pages with styles, scripts and SVGs stripped; refresh them if Reddit changes its markup.

Measured on a residential connection: a search page in ~5 s, a thread page in ~8 s. Stage 4's search call (3 terms, 10 posts each, full bodies) takes ~95 s alone and ~185 s when two brands overlap; the comments call (8 threads × 20 comments) ~30 s.

## Deployment

Production runs as a single Docker container on a Linux VPS, behind a TLS reverse proxy.
`docker-compose.yml` is the deployment topology — it carries the memory limits, process
reaping and log rotation that a bare `docker run` would not.

```bash
git clone https://github.com/haider-ecombench/reddit-reviews-scraper.git
cd reddit-reviews-scraper
cp .env.example .env          # set API_TOKEN
docker compose up -d --build  # first build is slow: it downloads Chromium
```

Then point `Caddyfile` at your domain and `caddy reload`. The container publishes on
`127.0.0.1:8001` only, so the reverse proxy is the sole public route in — **note that
`docker run -p 8001:8001` would bypass UFW entirely and expose the API publicly**.

Check from the server itself before pointing anything at it; Reddit blocks by IP, and a
datacenter address will likely need `SCRAPER_PROXY` set to a residential proxy:

```bash
docker compose exec scraper uv run --no-sync reddit-reviews search "gymshark reviews" --max 3
```

### Sizing

Measured against a live container, not estimated:

| | |
|---|---|
| Idle | 43 MiB, 12 PIDs |
| Peak, `MAX_CONCURRENCY=1` | 350 MiB |
| Peak, `MAX_CONCURRENCY=3` | 859 MiB, 358 PIDs |
| Per additional Chromium | ~254 MiB, ~115 threads |

Scrapling launches and tears down a browser per fetch, so memory is spiky, not cumulative;
with `FULL_BODIES=true` a search call is a long run of them, so the peak is held for minutes
rather than touched once. `MAX_CONCURRENCY` keeps the app inside its budget; the `mem_limit`
in `docker-compose.yml` is a blast-radius guard for the host — reaching it means the kernel
OOM-kills the container and drops in-flight scrapes. 2 GB fits the default `MAX_CONCURRENCY=3`
with roughly 2.4× headroom, which leaves a 4 GB VPS room for `trustpilot-reviews` beside it.

If you raise `MAX_CONCURRENCY`, watch both caps: memory runs out around 7–8 concurrent
browsers, and `pids_limit` around 8. A PID ceiling that bites first shows up as opaque
Chromium crashes rather than a clear error, which is why it is set to 1024 rather than 512.

Do not lower `MAX_CONCURRENCY` to save memory. The same 6-post search with full bodies took
**101 s at 3 and 239 s at 1**, against Stage 4's 290 s node timeout — concurrency is a latency
budget here, not only a memory knob. The `Caddyfile` timeouts are sized to the same worst case:
`SEARCH_BUDGET_S` (200 s) plus one in-flight `FETCH_TIMEOUT_MS` (45 s) and a retry pause, so
~250 s, which is why `write` is 300 s.

### Builds

The image is ~4.8 GB, nearly all of it Chromium and its system libraries.

| | |
|---|---|
| Cold build | ~9m40s |
| Rebuild after a code change | **10–15s** (was 4m11s) |
| Build context | 356 kB (was ~260 MB) |

The layer order is what makes the rebuild cheap: dependencies install from the lockfile alone
(`--no-install-project`), and the recursive `chown` — a multi-GB layer, because it covers the
browser cache — sits *above* `COPY src`, so a code edit rewrites only the source and the
project install. `.dockerignore` is what keeps the context at 356 kB: once you have run the
Quickstart, `.venv` alone is 258 MB of host-platform wheels that the image can never use — it
rebuilds dependencies from `uv.lock` inside the image instead.

CI runs on every push to `main`: unit and Stage 4 node tests, then a full image build that
starts the container and exercises `/health`, token enforcement, the `400` path and a live
search (a `503` block is accepted there, since GitHub runners are datacenter IPs).
