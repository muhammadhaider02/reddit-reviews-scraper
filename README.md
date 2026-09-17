<div align="center">

# Reddit Reviews

**SEARCH. READ. SERVE.**

[![CI](https://github.com/haider-ecombench/reddit-reviews-scraper/actions/workflows/ci.yml/badge.svg)](https://github.com/haider-ecombench/reddit-reviews-scraper/actions/workflows/ci.yml)
[![Python](https://img.shields.io/badge/Python-3.11+-3776AB?logo=python&logoColor=white)](https://python.org)
[![uv](https://img.shields.io/badge/uv-Package_Manager-DE5FE9?logo=uv&logoColor=white)](https://docs.astral.sh/uv/)
[![FastAPI](https://img.shields.io/badge/API-FastAPI-009688?logo=fastapi&logoColor=white)](https://fastapi.tiangolo.com)
[![Scrapling](https://img.shields.io/badge/Browser-Scrapling-FF6F00)](https://github.com/D4Vinci/Scrapling)

Self-hosted Reddit post and comment scraper with an HTTP API.

</div>

---

## Platform

This repo is a standalone Reddit collection service, the sibling of `trustpilot-reviews`. Given search terms it reads Reddit's search pages with a stealth browser and returns the matching posts; given thread links it returns each thread's post and comments. It serves the result over a small authenticated HTTP API that automation workflows call like any other data service.

A real browser is required. Reddit's JSON endpoints (`/search.json`, `/comments/<id>.json`) answer `403 blocked by network security` to plain HTTP clients and to the stealth browser alike. The regular web pages are server-rendered with the data in element attributes (`search-telemetry-tracker`, `shreddit-post`, `shreddit-comment`), which is what this service reads.

## Quickstart

Requires Python 3.11+, [uv](https://docs.astral.sh/uv/) and, for the Stage 4 node tests, Node.js. Runs as a single FastAPI process.

```bash
git clone https://github.com/haider-ecombench/reddit-reviews-scraper.git
cd reddit-reviews-scraper

uv sync                       # dependencies into a uv-managed venv
uv run scrapling install      # stealth browser (once)

cp .env.example .env          # then set API_TOKEN (see Configuration)

uv run reddit-reviews serve   # HTTP service on :8001
```

Verify with `curl localhost:8001/health` (expects `"status":"ok"`); interactive API docs are at `/docs`.

The service is driven over its HTTP API: `POST /reddit` takes `Authorization: Bearer <API_TOKEN>` and either `searchTerms` (post search) or `startUrls` (threads and their comments), while `GET /health` is unauthenticated and returns request counters. See `/docs` for the request fields, the response shape and the error contract.

One-off scrapes from the command line, no server needed:

```bash
uv run reddit-reviews search "Gymshark" "Gymshark reviews" --max 10 --pretty
uv run reddit-reviews thread https://www.reddit.com/r/Gymshark/comments/1vf53rw/ --max-comments 20 --pretty
```

## Configuration

All configuration is environment variables in `.env`. **`.env.example` is the canonical list.** Copy it and fill it in; every variable is documented there alongside the measurement its default is based on.

`API_TOKEN` is the bearer token callers must send, and is required in production. `SCRAPER_PROXY` is worth setting early: Reddit blocks by IP, and a datacenter address will likely need a residential proxy. The rest tune the browser: concurrency, per-page timeout, page and thread caps, resource blocking and full-body fetching.

`SCRAPE_BUDGET_S` bounds a single call's wall clock. Past it the service returns the posts, bodies and threads it has already collected and sets an `X-Truncated` response header, rather than running on past the caller's own timeout holding a browser nobody is waiting for.

## Development

```bash
uv run pytest            # 54 unit + Stage 4 node tests against saved pages, no network
uv run pytest -m live    # one real search and thread through the stealth browser
```

Layout: `scraper.py` (URLs, fetch, parse, orchestration), `mapping.py` (output shape), `api.py` (FastAPI surface), `config.py` (env). Fixtures in `tests/fixtures/` are real Reddit pages with styles, scripts and SVGs stripped; refresh them if Reddit changes its markup.

`tests/n8n/` holds verbatim copies of the **Sort Reddit Results** and **Fetch Reddit Comments** Code nodes and a script that replays the whole Reddit chain against a running server, so the workflow's own JavaScript is tested here before the workflow is touched. See [tests/n8n/README.md](tests/n8n/README.md).

Measured on a residential connection: a search page in ~5 s, a thread page in ~8 s. Stage 4's search call (3 terms, 10 posts each, full bodies) takes ~95 s alone and ~185 s when two brands overlap; the comments call (8 threads × 20 comments) ~30 s.

## Deployment

Production runs as a single Docker container on the same VPS as n8n, attached to n8n's Docker network. `docker-compose.yml` is the deployment topology. It carries the memory limits, process reaping, shutdown grace and log rotation that a bare `docker run` would not, and its comments record the measurements each limit is sized against.

```bash
git clone https://github.com/haider-ecombench/reddit-reviews-scraper.git
cd reddit-reviews-scraper
cp .env.example .env          # set API_TOKEN
docker compose up -d --build  # first build is slow: it downloads Chromium
```

n8n reaches the service by container name over the shared network:

```
http://reddit-reviews:8001/reddit
```

Nothing is published to the host and nothing is reachable from the internet, so there is no domain, no TLS certificate and no reverse proxy to maintain for this service. The VPS already runs Traefik on 80/443 for n8n's own UI; adding a second proxy would fail to bind. `API_TOKEN` still applies and is still worth setting, so a compromised container on the network cannot drive the scraper freely.

The network is declared external in `docker-compose.yml` as `n8n_default`, the default Compose creates for n8n's project. If that name differs the container refuses to start and says so; `docker network ls` gives the real one.

Check from the server itself before pointing the workflow at it — Reddit blocks by IP, and a datacenter address will likely need `SCRAPER_PROXY`:

```bash
docker compose exec scraper uv run --no-sync reddit-reviews search "gymshark reviews" --max 3
```

**Switching Stage 4 over** is limited to the two HTTP Request nodes. Keep their names: `Build Run Telemetry` counts calls by node name.

| Node | Change |
|---|---|
| `Apify: Reddit Search` | URL → `http://reddit-reviews:8001/reddit`; authentication → Header Auth credential `Authorization: Bearer <API_TOKEN>`; remove the `maxTotalChargeUsd` and `timeout` query parameters. Body unchanged. |
| `Apify: Reddit Comments` | same |

The node timeouts (290 s / 250 s) stay, and `SCRAPE_BUDGET_S` is sized to sit under both. `Assess Vendors` gates on the Apify monthly limit; once Trustpilot and Reddit both leave Apify that gate no longer protects anything.

CI runs on pushes to `main` and on pull requests: unit and Stage 4 node tests, plus a full image build that starts the container and exercises `/health`, token enforcement, the `400` path and a live search (a `503` block is accepted there, since GitHub runners are datacenter IPs).
