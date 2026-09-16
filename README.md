<div align="center">

# Reddit Reviews

**SEARCH. READ. SERVE.**

[![CI](https://github.com/haider-ecombench/reddit-reviews/actions/workflows/ci.yml/badge.svg)](https://github.com/haider-ecombench/reddit-reviews/actions/workflows/ci.yml)
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

Production runs as a single Docker container on a Linux VPS. The `Dockerfile` installs the stealth browser into `/opt/cache` and runs the service as a non-root user on port 8001; put a reverse proxy with TLS in front of it and set `API_TOKEN`.

```bash
docker build -t reddit-reviews .
docker run -d --restart unless-stopped -p 8001:8001 --env-file .env reddit-reviews
```

Check from the server itself before pointing anything at it; Reddit blocks by IP:

```bash
docker exec <container> uv run --no-sync reddit-reviews search "gymshark reviews" --max 3
```

GitHub Actions CI runs the tests on every push to `main`, then builds the image and exercises `/health`, token enforcement, the `400` path and a live search (a `503` block is accepted there, since runners are datacenter IPs).
