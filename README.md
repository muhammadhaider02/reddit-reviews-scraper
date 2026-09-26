<div align="center">

# Reddit Reviews

**SEARCH. READ. SERVE.**

[![CI](https://github.com/automation-ecombench/reddit-reviews-scraper/actions/workflows/ci.yml/badge.svg)](https://github.com/automation-ecombench/reddit-reviews-scraper/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![Python](https://img.shields.io/badge/Python-3.11+-3776AB?logo=python&logoColor=white)](https://python.org)
[![uv](https://img.shields.io/badge/uv-Package_Manager-DE5FE9?logo=uv&logoColor=white)](https://docs.astral.sh/uv/)
[![FastAPI](https://img.shields.io/badge/API-FastAPI-009688?logo=fastapi&logoColor=white)](https://fastapi.tiangolo.com)
[![Scrapling](https://img.shields.io/badge/Browser-Scrapling-FF6F00)](https://github.com/D4Vinci/Scrapling)

Self-hosted Reddit post, comment and mention scraper with an HTTP API.

[Architecture](docs/architecture.md) · [API](docs/api.md) · [Deployment](docs/deployment.md)

</div>

---

## What it is

A FastAPI service that reads Reddit through a headless stealth browser (Scrapling/Playwright) and Reddit's own app API, both over residential sticky proxies.

- `POST /reddit` is a drop-in for the Apify actor `harshmaur~reddit-scraper`: post search by `searchTerms`, threads with their comments by `startUrls`, in the actor's request and row shape.
- `POST /reddit/mentions` searches Reddit's comment index for threads where a brand is named in a reply, and answers in Tavily's search response shape.

It was built as one of three services behind an n8n brand-research pipeline, beside `trustpilot-reviews` (port 8000) and `facebook-ad-library` (port 8003), and replaces the paid Apify actor and a Tavily web search there. It listens on 8001.

## Quickstart

Requires Python 3.11+, [uv](https://docs.astral.sh/uv/) and a residential proxy with sticky ports. `SCRAPER_PROXY` is optional in the code (without it the service logs a warning and fetches from the host's own IP), but Reddit blocks datacenter IPs, so on a server it is required in practice.

```bash
git clone https://github.com/automation-ecombench/reddit-reviews-scraper.git
cd reddit-reviews-scraper

uv sync                               # dependencies into a uv-managed venv
uv run scrapling install              # stealth browser (once)

cp .env.example .env                  # set API_TOKEN and SCRAPER_PROXY

uv run reddit-reviews serve           # HTTP service on :8001
```

Verify with `curl localhost:8001/health` (expects `"status":"ok"`); interactive API docs are at `/docs`. The same code runs without a server:

```bash
uv run reddit-reviews search "Gymshark" "Gymshark reviews" --max 10 --pretty
```

See [api.md](docs/api.md) for the request bodies, response rows, headers, auth (`Authorization: Bearer`) and the error contract.

## Configuration

All configuration is environment variables in `.env`; **`.env.example` is the canonical list**, each value beside the measurement its default rests on. The reference table is in [architecture.md](docs/architecture.md#configuration-environment-variables).

`API_TOKEN` (empty disables auth, local testing only) and `SCRAPER_PROXY` (a URL or `host:port:user:pass`, on a sticky port; the rotating gateways are refused at startup) are the two to set. The rest tune the browser, the per-call budget, the mobile route and the mentions route.

## Development

```bash
uv run pytest            # 158 tests against saved pages and the n8n node copies, no network
uv run pytest -m live    # one real search and thread through the stealth browser
```

The n8n node tests (`tests/test_n8n_nodes.py`) need Node 22. `tests/n8n/research_reddit_chain.py` replays the whole n8n Reddit chain against a running server.

## Deployment

One Docker container on n8n's Docker network, nothing published to the host. Deploys are `git pull` and `docker compose up -d --build`; CI runs on pushes to `main` and on pull requests. The runbook (topology, compose settings, checks, what to watch) is in [deployment.md](docs/deployment.md).

## Disclaimer

This service reads public Reddit pages. You are responsible for using it in line with Reddit's terms and the law where you operate. It is not affiliated with, endorsed by or sponsored by Reddit, Apify or Tavily.

## License

[MIT](LICENSE)
