<div align="center">

# Reddit Reviews

**SEARCH. READ. SERVE.**

[![CI](https://github.com/haider-ecombench/reddit-reviews-scraper/actions/workflows/ci.yml/badge.svg)](https://github.com/haider-ecombench/reddit-reviews-scraper/actions/workflows/ci.yml)
[![Python](https://img.shields.io/badge/Python-3.11+-3776AB?logo=python&logoColor=white)](https://python.org)
[![uv](https://img.shields.io/badge/uv-Package_Manager-DE5FE9?logo=uv&logoColor=white)](https://docs.astral.sh/uv/)
[![FastAPI](https://img.shields.io/badge/API-FastAPI-009688?logo=fastapi&logoColor=white)](https://fastapi.tiangolo.com)
[![Scrapling](https://img.shields.io/badge/Browser-Scrapling-FF6F00)](https://github.com/D4Vinci/Scrapling)

Self-hosted Reddit post and comment scraper with an HTTP API.

[Architecture](docs/architecture.md) · [API](docs/api.md) · [Deployment](docs/deployment.md)

</div>

---

## Platform

This repo is a standalone Reddit collection service behind the SmartLead brand-research pipeline, running over its own HTTP API. It is the sibling of `trustpilot-reviews`, which serves Trustpilot the same way, and both stand in for the Apify actors the pipeline used to call.

## Quickstart

Requires Python 3.11+ and [uv](https://docs.astral.sh/uv/). Runs as a single FastAPI process.

```bash
git clone https://github.com/haider-ecombench/reddit-reviews-scraper.git
cd reddit-reviews-scraper

uv sync                               # dependencies into a uv-managed venv
uv run scrapling install              # stealth browser for page scraping (once)

cp .env.example .env                  # then fill in credentials (see Configuration)

uv run reddit-reviews serve           # HTTP service on :8001
```

Verify with `curl localhost:8001/health` (expects `"status":"ok"`); interactive API docs are at `/docs`.

The service is driven over its HTTP API: post search by terms, and threads with their comments by link, in the Apify actor's request and response shape. See [api.md](docs/api.md) for endpoints, auth (`Authorization: Bearer`) and the error contract.

## Configuration

All configuration is environment variables in `.env`. **`.env.example` is the canonical list.** Copy it and fill in your keys; the full reference with defaults lives in [architecture.md](docs/architecture.md#configuration-environment-variables).

Required values are the API bearer token and a residential proxy (Reddit blocks datacenter IPs). The rest tune the browser, the per-call budget and the mobile route.

## Development

```bash
uv run pytest            # unit + Stage 4 node tests against saved pages, no network
uv run pytest -m live    # one real search and thread through the stealth browser
```

## Deployment

Production runs as a single Docker container on the Hostinger VPS that hosts n8n, attached to n8n's Docker network, with nothing published to the host. Deploys are a `git pull` and `docker compose up -d --build`; CI runs on push to `main`. The operational runbook (topology, env, checks and gotchas) is in [deployment.md](docs/deployment.md).
