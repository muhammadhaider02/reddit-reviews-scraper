"""HTTP surface n8n calls in place of Apify's harshmaur~reddit-scraper run-sync-get-dataset-items endpoint.

POST /reddit  -> JSON array of dataset items, same names Stage 4's Code nodes read from Apify
                 body with `searchTerms` -> post search   (replaces `Apify: Reddit Search`)
                 body with `startUrls`   -> thread + comments (replaces `Apify: Reddit Comments`)
GET  /health  -> counters

Error contract, matched to Stage 4:
  200 []                 nothing found -> `Sort Reddit Results` reports no_results, Tavily fallback runs
  200 [partial]          some terms/threads failed but others worked -> use what we have
  400 {"error": {...}}   neither searchTerms nor startUrls
  503 {"error": {...}}   every fetch was blocked or the browser died -> reddit_request_failed, a vendor
                         failure in Parse Report, so no brand retry is burned
"""

from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager
from typing import Annotated

from fastapi import Depends, FastAPI, Header, HTTPException
from fastapi.responses import JSONResponse
from pydantic import AliasChoices, BaseModel, ConfigDict, Field, field_validator

from . import __version__
from .config import settings
from .mapping import comment_item, post_item
from .mobile import counters as mobile_counters
from .scraper import RedditError, scrape_search, scrape_threads

log = logging.getLogger("reddit_reviews.api")

counters = {"requests": 0, "search": 0, "threads": 0, "ok": 0, "empty": 0, "partial": 0, "blocked": 0, "failed": 0, "bad_request": 0, "truncated": 0, "in_flight": 0}


def _log_egress() -> None:
    """Say which IP we actually leave from, once, at startup.

    A misconfigured proxy and a blocked IP fail identically - both are a 403 from Reddit - so
    without this a wrong credential reads as "Reddit blocked us again" and costs an afternoon.
    Best effort by design: never block startup on it, and never log the credential itself.
    """
    import json
    import urllib.request

    from .scraper import STICKY_PORT_RANGE, proxy_config

    try:
        proxy = proxy_config(STICKY_PORT_RANGE[0])
    except ValueError as e:  # a malformed credential should be loud, but not fatal here
        log.error("SCRAPER_PROXY is unusable, scrapes will run direct and Reddit will block them: %s", e)
        return
    if not proxy:
        log.warning("no SCRAPER_PROXY set - fetches leave from this host's own IP, which Reddit blocks")
        return
    try:
        if isinstance(proxy, dict):
            # Credentials go inline here rather than through ProxyBasicAuthHandler, which does not
            # authenticate a CONNECT tunnel and answers 407. This URL is never logged.
            from urllib.parse import quote

            scheme, _, hostport = proxy["server"].partition("://")
            creds = f"{quote(proxy['username'], safe='')}:{quote(proxy['password'], safe='')}"
            server = f"{scheme}://{creds}@{hostport}"
        else:
            server = proxy
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({"http": server, "https": server}))
        # Vendor-neutral on purpose: this has to keep working whoever SCRAPER_PROXY points at, and a
        # probe hosted by one proxy vendor is a dependency on that vendor outliving our contract.
        with opener.open("https://api.ipify.org?format=json", timeout=20) as r:
            ip = json.loads(r.read()).get("ip")
        log.info("proxy is attached: fetches leave from %s (sticky port %d)", ip, STICKY_PORT_RANGE[0])
    except Exception as e:  # noqa: BLE001 - a failed probe must never stop the service booting
        log.warning("could not confirm the proxy egress IP (%s: %s) - scrapes will still try it", type(e).__name__, e)


@asynccontextmanager
async def lifespan(_: FastAPI):
    if not settings.api_token:
        log.warning("API_TOKEN is not set - the scraper endpoint is unauthenticated")
    await asyncio.to_thread(_log_egress)
    yield


app = FastAPI(title="reddit-reviews", version=__version__, lifespan=lifespan)


class RedditRequest(BaseModel):
    """The Apify actor's input fields, so Stage 4's existing bodies work unchanged. Unknown fields
    (proxy, searchComments, maxCommunitiesCount, ...) are ignored."""

    model_config = ConfigDict(populate_by_name=True, extra="ignore")

    search_terms: list[str] = Field(default_factory=list, validation_alias=AliasChoices("searchTerms", "search_terms", "q"))
    start_urls: list[str] = Field(default_factory=list, validation_alias=AliasChoices("startUrls", "start_urls", "urls"))
    search_sort: str = Field(default="relevance", validation_alias=AliasChoices("searchSort", "sort"))
    search_time: str = Field(default="all", validation_alias=AliasChoices("searchTime", "time"))
    max_posts: int = Field(default=10, ge=1, le=50, validation_alias=AliasChoices("maxPostsCount", "max_posts"))
    max_comments_per_post: int = Field(default=20, ge=0, le=200, validation_alias=AliasChoices("maxCommentsPerPost", "max_comments_per_post"))
    max_comments: int | None = Field(default=None, ge=0, validation_alias=AliasChoices("maxCommentsCount", "max_comments"))
    include_nsfw: bool = Field(default=False, validation_alias=AliasChoices("includeNSFW", "include_nsfw"))
    # Not an Apify field. None = the FULL_BODIES setting decides.
    full_bodies: bool | None = Field(default=None, validation_alias=AliasChoices("fullBodies", "full_bodies"))

    @field_validator("search_terms", mode="before")
    @classmethod
    def _coerce_terms(cls, v):
        if v is None:
            return []
        if isinstance(v, str):
            v = [v]
        return [str(t) for t in v if str(t or "").strip()]

    @field_validator("start_urls", mode="before")
    @classmethod
    def _coerce_urls(cls, v):
        # Apify takes [{"url": "..."}]; plain strings are accepted too.
        if v is None:
            return []
        if isinstance(v, (str, dict)):
            v = [v]
        out = []
        for u in v:
            u = u.get("url") if isinstance(u, dict) else u
            if str(u or "").strip():
                out.append(str(u).strip())
        return out

    @field_validator("max_posts", mode="before")
    @classmethod
    def _clamp_posts(cls, v):
        # Stage 4's comments call sends maxPostsCount = number of targets, which can be 0 on paper.
        n = int(v) if str(v).strip().lstrip("-").isdigit() else 10
        return min(max(n, 1), 50)


def require_token(authorization: Annotated[str | None, Header()] = None) -> None:
    if not settings.api_token:
        return
    if authorization != f"Bearer {settings.api_token}":
        raise HTTPException(status_code=401, detail="invalid or missing bearer token")


def _error_body(exc: Exception, status: int) -> dict:
    return {"error": {"type": type(exc).__name__, "status": status, "message": str(exc), "description": str(exc)}}


@app.post("/reddit", dependencies=[Depends(require_token)])
async def reddit(req: RedditRequest):
    counters["requests"] += 1
    if not req.search_terms and not req.start_urls:
        counters["bad_request"] += 1
        err = ValueError("request must include `searchTerms` or `startUrls`")
        return JSONResponse(status_code=400, content=_error_body(err, 400))

    mode = "threads" if req.start_urls else "search"
    counters[mode] += 1
    counters["in_flight"] += 1
    try:
        if mode == "threads":
            res = await asyncio.to_thread(scrape_threads, req.start_urls, req.max_comments_per_post, req.max_comments)
            items = []
            for t in res.threads:
                items.append(post_item(t.post))
                items.extend(comment_item(c) for c in t.comments)
            partial = bool(res.failed)
            headers = {
                "X-Scrape-Seconds": str(res.seconds),
                "X-Pages-Fetched": str(res.pages_fetched),
                "X-Threads-Missing": str(len(res.missing)),
                "X-Threads-Failed": str(len(res.failed)),
                "X-Truncated": "true" if res.truncated else "false",
                "X-Mobile-Threads": str(res.mobile_threads),
            }
            summary = f"threads={len(res.threads)} comments={len(items) - len(res.threads)} missing={len(res.missing)} failed={len(res.failed)}"
            seconds = res.seconds
        else:
            res = await asyncio.to_thread(
                scrape_search,
                req.search_terms,
                req.max_posts,
                req.search_sort,
                req.search_time,
                req.include_nsfw,
                full_bodies=req.full_bodies,
            )
            items = [post_item(p) for p in res.posts]
            partial = bool(res.failed_terms)
            headers = {
                "X-Scrape-Seconds": str(res.seconds),
                "X-Pages-Fetched": str(res.pages_fetched),
                "X-Terms-Failed": str(len(res.failed_terms)),
                "X-Truncated": "true" if res.truncated else "false",
                "X-Mobile-Posts": str(res.mobile_posts),
            }
            summary = f"terms={len(req.search_terms)} posts={len(items)} failed_terms={len(res.failed_terms)}"
            seconds = res.seconds
    except ValueError as e:
        counters["bad_request"] += 1
        return JSONResponse(status_code=400, content=_error_body(e, 400))
    except RedditError as e:
        counters["blocked" if e.status == 503 else "failed"] += 1
        log.warning("%s %s -> %s: %s", e.status, mode, type(e).__name__, e)
        return JSONResponse(status_code=e.status, content=_error_body(e, e.status))
    except Exception as e:  # noqa: BLE001
        counters["failed"] += 1
        log.exception("unexpected failure in %s", mode)
        return JSONResponse(status_code=500, content=_error_body(e, 500))
    finally:
        counters["in_flight"] -= 1

    counters["partial" if partial else "ok" if items else "empty"] += 1
    # A truncated call is a 200 like any other, so without this counter it is invisible to anything
    # but a per-request header read. A rising number means the budget is biting.
    if res.truncated:
        counters["truncated"] += 1
    log.info("ok %s %s %.1fs", mode, summary, seconds)
    return JSONResponse(content=items, headers=headers)


@app.get("/health")
async def health():
    return {
        "status": "ok",
        "version": __version__,
        "auth": bool(settings.api_token),
        "proxy": bool(settings.proxy),
        "max_concurrency": settings.max_concurrency,
        # The mobile route is an optimisation in front of a working browser path, so its failures are
        # logged and swallowed rather than surfaced as errors. These counters are the only way to see
        # it stop working: `blocked` climbing, or `calls` flat while requests keep arriving, means
        # Reddit closed the door and every call is quietly back on the proxy at ~11x the bandwidth.
        "mobile": {"enabled": settings.mobile_enabled, **mobile_counters},
        **counters,
    }
