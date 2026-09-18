"""Runtime configuration, read once from the environment."""

import os
from dataclasses import dataclass

from dotenv import load_dotenv

load_dotenv()


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name, "").strip()
    return int(raw) if raw else default


def _env_bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name, "").strip().lower()
    if not raw:
        return default
    return raw in ("1", "true", "yes", "on")


@dataclass(frozen=True)
class Settings:
    # Shared secret n8n sends as `Authorization: Bearer <token>`. Empty = no auth (local testing only).
    api_token: str = os.environ.get("API_TOKEN", "").strip()
    # Residential proxy. Not optional on a datacenter host: Reddit's "blocked by network security"
    # wall is IP-based, and measured 2026-09-17, this VPS gets a 403 block page for every request
    # while a residential IP gets 200 for the same URL, same second - curl, Chromium and Firefox
    # alike, so it is the address and not the browser. The Apify actor this replaces paid for
    # RESIDENTIAL proxies for the same reason.
    # Two forms, parsed by scraper.proxy_config(): a plain URL (http://user:pass@host:port), or
    # Decodo's `host:port:user:pass`. Use a STICKY port (10001+), never the rotating gateway (7000).
    proxy: str | None = os.environ.get("SCRAPER_PROXY", "").strip() or None
    # Browser fetches allowed at once, across ALL requests. Stage 4 sends 3 search terms, then up to 8 threads.
    max_concurrency: int = _env_int("MAX_CONCURRENCY", 3)
    # Per-page browser timeout. Stage 4 allows 290s for the search call and 250s for the comments call
    # (read from the live workflow's node timeouts, u3698BQra0i9oK4b).
    fetch_timeout_ms: int = _env_int("FETCH_TIMEOUT_MS", 45_000)
    # We only read the server-rendered HTML, so images, fonts, CSS and media are pure memory and
    # bandwidth cost in the browser. Blocking them is the single biggest memory lever we have.
    # Flip to false if Reddit starts serving the stripped page a challenge.
    block_resources: bool = _env_bool("BLOCK_RESOURCES", True)
    # Reddit search returns about 7 posts per page. Hard cap on pages followed per search term.
    max_search_pages: int = _env_int("MAX_SEARCH_PAGES", 3)
    # Search results carry only a snippet. When on, each found post's thread page is read for its full body,
    # as the Apify actor returned. Costs one page per post (~30 for Stage 4) but keeps scoring and quotes intact.
    full_bodies: bool = _env_bool("FULL_BODIES", True)
    # Hard cap on thread pages read for bodies per search call.
    max_body_fetches: int = _env_int("MAX_BODY_FETCHES", 30)
    # Total wall-clock budget for one call, search or comments. Nothing cancels a request once it
    # starts - Starlette does not cancel handlers on client disconnect, and the browser runs in a
    # thread that cannot be cancelled - so a caller that gives up does NOT free the browser. This is
    # what frees it: no new page, body or thread is started that cannot finish inside the budget,
    # and the call returns what it already has with `truncated` set. One brand alone takes ~95s, but
    # overlapping runs share the browser gate and slow down. Must stay under BOTH Stage 4 node
    # timeouts (search 290s, comments 250s), and under docker-compose.yml's stop_grace_period.
    scrape_budget_s: int = _env_int("SCRAPE_BUDGET_S", 200)
    # Hard cap on thread URLs read per call, whatever the caller sends.
    max_threads: int = _env_int("MAX_THREADS", 10)
    # ----------------------------------------------------------------- the mobile route
    # Reddit's own Android app reads through an anonymous OAuth token that is gated on client
    # identity, not on IP - so it answers this datacenter host without a proxy at all. We use it for
    # everything fetched BY ID (post bodies, comment trees) and never for search, because the app's
    # search is a different index: measured 18 Sep 2026, its top-100 held 2 of the 30 posts the web
    # search put in its top-10. See mobile.py. Turning this off falls the whole service back to the
    # browser, which still works - it just costs ~11x the proxy bandwidth.
    mobile_enabled: bool = _env_bool("MOBILE_ENABLED", True)
    # Devices (fake phones) that may be in flight at once. One call leases one device for all of its
    # sub-requests, so this caps concurrent CALLS on this route, not requests.
    mobile_devices: int = _env_int("MOBILE_DEVICES", 3)
    # Re-mint when a token's 100-per-10-minutes budget falls this low. A fresh token gets a fresh budget.
    mobile_min_budget: float = float(os.environ.get("MOBILE_MIN_BUDGET", "10"))
    # Minimum gap between two calls on one device, plus up to this much jitter. Reddit's ceiling is
    # 100 per 10 min per token; 8 thread reads at ~2.5s apart is ~20s, well inside Stage 4's 250s.
    mobile_spacing_s: float = float(os.environ.get("MOBILE_SPACING_S", "2"))
    mobile_spacing_jitter_s: float = float(os.environ.get("MOBILE_SPACING_JITTER_S", "1"))
    # Per-request timeout. These calls answer in ~0.2s; this is only for a hung socket.
    mobile_timeout_s: int = _env_int("MOBILE_TIMEOUT_S", 25)
    # Pause before retrying a blocked page, seconds.
    retry_delay_s: float = float(os.environ.get("RETRY_DELAY_S", "2"))
    host: str = os.environ.get("HOST", "0.0.0.0")
    # 8001 so it can run next to trustpilot-reviews (8000) on the same machine.
    port: int = _env_int("PORT", 8001)


settings = Settings()
