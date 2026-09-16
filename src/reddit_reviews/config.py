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
    # Optional proxy URL passed straight to Scrapling. Apify ran this actor on RESIDENTIAL proxies, and
    # Reddit's "blocked by network security" wall is IP-based, so a datacenter host will likely need one.
    proxy: str | None = os.environ.get("SCRAPER_PROXY", "").strip() or None
    # Browser fetches allowed at once, across ALL requests. Stage 4 sends 3 search terms, then up to 8 threads.
    max_concurrency: int = _env_int("MAX_CONCURRENCY", 3)
    # Per-page browser timeout. Stage 4 allows 280s for the search call and 240s for the comments call.
    fetch_timeout_ms: int = _env_int("FETCH_TIMEOUT_MS", 45_000)
    # Reddit search returns about 7 posts per page. Hard cap on pages followed per search term.
    max_search_pages: int = _env_int("MAX_SEARCH_PAGES", 3)
    # Search results carry only a snippet. When on, each found post's thread page is read for its full body,
    # as the Apify actor returned. Costs one page per post (~30 for Stage 4) but keeps scoring and quotes intact.
    full_bodies: bool = _env_bool("FULL_BODIES", True)
    # Hard cap on thread pages read for bodies per search call.
    max_body_fetches: int = _env_int("MAX_BODY_FETCHES", 30)
    # Seconds into a search call after which no NEW body fetch starts (posts keep their snippet). Stage 4 gives
    # the call 280s; one brand alone takes ~95s, but overlapping runs share the browser gate and slow down.
    search_budget_s: int = _env_int("SEARCH_BUDGET_S", 200)
    # Hard cap on thread URLs read per call, whatever the caller sends.
    max_threads: int = _env_int("MAX_THREADS", 10)
    # Pause before retrying a blocked page, seconds.
    retry_delay_s: float = float(os.environ.get("RETRY_DELAY_S", "2"))
    host: str = os.environ.get("HOST", "0.0.0.0")
    # 8001 so it can run next to trustpilot-reviews (8000) on the same machine.
    port: int = _env_int("PORT", 8001)


settings = Settings()
