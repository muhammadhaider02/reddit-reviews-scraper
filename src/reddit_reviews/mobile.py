"""Reddit's anonymous Android-app API - the half of the work the browser should not be doing.

Where the line falls, and why it falls there:

Reddit's *web* search (`/svc/shreddit/search/`, what the browser reads) and the *app* search
(`oauth.reddit.com/search`) are different indexes, not one index sorted two ways. Measured
18 Sep 2026 across three brands, the app's top-100 contained 2 of the 30 posts the web search put
in its top-10 - and across 103 brands the two routes agreed on well under half the posts. So the app
cannot serve search without changing what Stage 4 reads.

`search()` therefore exists but is OFF by default, behind SEARCH_ROUTE=mobile. It is there to
measure that difference on real brands rather than argue about it, and because it costs nothing at
all: no browser, no proxy, bodies included in the response. It is not a cheaper way to get the same
results; it is a different result set that happens to be free.

Fetching a post or its comments BY ID involves no ranking at all, and there the app API is
strictly better: one `/api/info` call returned all 30 browser-found ids with full `selftext` in
27 KB and 0.35s, and `/comments` answered every one of them in ~0.2s. That is the ~23 body
fetches plus the 8 thread fetches - the bulk of the proxy bill and nearly all of the wall clock -
moved onto a route that needs no proxy and no browser. It works from a datacenter IP because
Reddit cannot IP-gate the endpoint its own app calls from every mobile carrier on earth; it gates
on client identity instead, and that identity is a public constant baked into every install.

The unit of identity is a "device", not a request: one User-Agent, two UUIDs, one qos figure, the
token minted with them, and the loid/session pair Reddit returned. Never mix parts between
devices - a token minted under one User-Agent and used under another is exactly the inconsistency
worth fingerprinting. One call from Stage 4 leases one device for all of its sub-requests, so
Reddit sees one phone reading about one brand.

Transport only, by design: it returns Reddit's raw JSON and imports nothing from `scraper`, which
is what keeps the two free of a cycle. `scraper` owns the conversion to `Post`/`Comment` and owns
the decision to fall back to the browser when this route is unavailable.

Stdlib HTTP on purpose. The runtime dependency list stays as it was, and gzip is handled here so
the bytes we count are the bytes that actually crossed the wire.
"""

from __future__ import annotations

import base64
import contextlib
import gzip
import json
import logging
import random
import threading
import time
import urllib.error
import urllib.request
import uuid
from collections.abc import Iterator
from urllib.parse import urlencode

from .config import settings

log = logging.getLogger("reddit_reviews.mobile")

# The Reddit Android app's public OAuth client id, with an empty secret. Not a credential of ours:
# it ships inside every copy of the app. Taken from Redlib, which has run this in production since 2024.
CLIENT_ID = "ohXpoqrZYub1kg"
TOKEN_URL = "https://www.reddit.com/auth/v2/oauth/access-token/loid"
API = "https://oauth.reddit.com"

# Real Android app version strings; Reddit rejects invented ones. A list of 150 lives at
# https://github.com/redlib-org/redlib/blob/main/src/oauth_resources.rs if more spread is wanted.
APP_VERSIONS = (
    "Version 2024.22.1/Build 1652272",
    "Version 2024.23.1/Build 1665606",
    "Version 2024.24.1/Build 1682520",
    "Version 2024.25.0/Build 1693595",
    "Version 2024.25.2/Build 1700401",
)

# `/api/info` takes up to 100 fullnames per call.
INFO_BATCH = 100

# Reddit's own wording on the block page, the same marker the browser path looks for.
BLOCK_MARKER = "blocked by network security"

counters = {"mints": 0, "calls": 0, "bytes": 0, "rate_limited": 0, "unauthorized": 0, "blocked": 0, "errors": 0}
_counters_lock = threading.Lock()


class MobileError(Exception):
    """This route could not answer. Always recoverable by the caller falling back to the browser."""


class MobileBlocked(MobileError):
    """Reddit refused the device or the IP outright. The device is retired, not retried."""


def _bump(name: str, by: int = 1) -> None:
    with _counters_lock:
        counters[name] += by


def _read(resp) -> tuple[bytes, dict]:
    """Read a response, count the bytes that actually crossed the wire, and gunzip if needed."""
    raw = resp.read()
    _bump("bytes", len(raw))
    headers = dict(resp.headers)
    if (resp.headers.get("Content-Encoding") or "").lower() == "gzip":
        try:
            return gzip.decompress(raw), headers
        except OSError:
            return raw, headers
    return raw, headers


class Device:
    """One fake phone: a stable identity plus the anonymous token minted under it."""

    def __init__(self) -> None:
        self.ua = f"Reddit/{random.choice(APP_VERSIONS)}/Android {random.randint(9, 14)}"
        self.vendor_id = str(uuid.uuid4())
        self.device_id = str(uuid.uuid4())
        self.qos = f"{random.randint(1000, 100000) / 1000:.3f}"
        self.token = ""
        self.loid = ""
        self.session = ""
        self.expires_at = 0.0
        # Reddit's budget is per token. Start optimistic; every response overwrites it.
        self.remaining = 100.0
        self.reset_at = 0.0
        self.retired = False
        self._last_call = 0.0

    # ----------------------------------------------------------------- token

    @property
    def fresh(self) -> bool:
        return bool(self.token) and time.time() < self.expires_at and self.remaining >= settings.mobile_min_budget

    def mint(self) -> None:
        """Ask Reddit for an account-less token, the way the app does on first launch."""
        basic = base64.b64encode(f"{CLIENT_ID}:".encode()).decode()
        req = urllib.request.Request(
            TOKEN_URL,
            method="POST",
            data=json.dumps({"scopes": ["*", "email", "pii"]}).encode(),
            headers={
                "User-Agent": self.ua,
                "Authorization": f"Basic {basic}",
                "Content-Type": "application/json; charset=UTF-8",
                "client-vendor-id": self.vendor_id,
                "X-Reddit-Device-Id": self.device_id,
                "x-reddit-retry": "algo=no-retries",
                "x-reddit-compression": "1",
                "x-reddit-qos": self.qos,
                "x-reddit-media-codecs": "available-codecs=video/avc, video/hevc",
                "Accept-Encoding": "gzip",
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=settings.mobile_timeout_s) as r:
                body, headers = _read(r)
        except urllib.error.HTTPError as e:
            raw = e.read()
            _bump("bytes", len(raw))
            if e.code == 403 and BLOCK_MARKER in raw[:400_000].decode("utf-8", "ignore").lower():
                self.retired = True
                _bump("blocked")
                raise MobileBlocked("reddit blocked the token mint") from e
            _bump("errors")
            raise MobileError(f"token mint failed: http {e.code}") from e
        except OSError as e:  # DNS, TLS, timeout
            _bump("errors")
            raise MobileError(f"token mint failed: {type(e).__name__}: {e}") from e

        try:
            data = json.loads(body)
            self.token = data["access_token"]
        except (json.JSONDecodeError, KeyError, TypeError) as e:
            _bump("errors")
            raise MobileError("token mint returned no access_token") from e
        # Refresh two minutes early so a long call cannot expire mid-flight.
        self.expires_at = time.time() + int(data.get("expires_in", 86400)) - 120
        self.loid = headers.get("x-reddit-loid", "")
        self.session = headers.get("x-reddit-session", "")
        self.remaining = 100.0
        _bump("mints")
        log.info("minted an anonymous device token (expires in %ss)", data.get("expires_in"))

    # ----------------------------------------------------------------- requests

    def _headers(self) -> dict[str, str]:
        h = {
            "User-Agent": self.ua,
            "Authorization": f"Bearer {self.token}",
            "Accept": "*/*",
            "Accept-Encoding": "gzip",
        }
        if self.loid:
            h["x-reddit-loid"] = self.loid
        if self.session:
            h["x-reddit-session"] = self.session
        return h

    def _pace(self) -> None:
        """Space calls out, and honour any back-off a previous response asked for.

        Reddit's ceiling is 100 per 10 minutes per token; this keeps us well under it and keeps the
        traffic shaped like someone reading rather than a loop.
        """
        gap = settings.mobile_spacing_s + random.uniform(0, settings.mobile_spacing_jitter_s)
        wait = max(self.reset_at - time.time(), self._last_call + gap - time.time())
        if wait > 0:
            time.sleep(wait)

    def get(self, path: str, params: dict) -> dict | list:
        """One API call, with the recovery rules Reddit's failure modes actually need.

        Retries exist only for the two cases where a retry is known to help - an expired token
        (re-mint) and an explicit rate-limit back-off. A hard block retires the device instead:
        waiting does not clear it, and hammering it is what turns a device block into an IP block.
        """
        if self.retired:
            raise MobileBlocked("device is retired")
        for attempt in range(2):
            if not self.fresh:
                self.mint()
            self._pace()
            url = f"{API}{path}?{urlencode({**params, 'raw_json': 1})}"
            req = urllib.request.Request(url, headers=self._headers())
            self._last_call = time.time()
            try:
                with urllib.request.urlopen(req, timeout=settings.mobile_timeout_s) as r:
                    body, headers = _read(r)
                _bump("calls")
                self._read_limits(headers)
                payload = json.loads(body)
            except urllib.error.HTTPError as e:
                self._on_http_error(e, attempt)
                continue
            except OSError as e:
                _bump("errors")
                raise MobileError(f"{path}: {type(e).__name__}: {e}") from e
            except json.JSONDecodeError as e:
                _bump("errors")
                raise MobileError(f"{path}: response was not JSON") from e

            # A 200 carrying an auth error is Reddit's way of saying the token died mid-window.
            if isinstance(payload, dict) and payload.get("error") == 401:
                _bump("unauthorized")
                self.token = ""
                if attempt == 0:
                    continue
                raise MobileError(f"{path}: unauthorized even after a fresh token")
            return payload
        raise MobileError(f"{path}: gave up after 2 attempts")

    def _read_limits(self, headers: dict) -> None:
        raw = headers.get("x-ratelimit-remaining")
        if raw is not None:
            with contextlib.suppress(ValueError, TypeError):
                self.remaining = float(raw)

    def _on_http_error(self, e: urllib.error.HTTPError, attempt: int) -> None:
        """Classify a failure and either arrange a retry or raise. Never returns for a hard block."""
        raw = e.read()
        _bump("bytes", len(raw))
        retry_after = e.headers.get("Retry-After") or e.headers.get("x-ratelimit-reset")
        # A 403 *with* a back-off header is a slow-down. A 403 without one, carrying Reddit's block
        # page, is a refusal of this device - a distinction worth getting right, because the first is
        # fixed by waiting and the second is made worse by it.
        if e.code == 429 or (e.code == 403 and retry_after):
            _bump("rate_limited")
            with contextlib.suppress(ValueError, TypeError):
                self.reset_at = time.time() + min(float(retry_after or 60), 120.0)
            if attempt == 0:
                return
            raise MobileError(f"rate limited, resets in {retry_after}s") from e
        if e.code == 403:
            self.retired = True
            _bump("blocked")
            blocked = BLOCK_MARKER in raw[:400_000].decode("utf-8", "ignore").lower()
            raise MobileBlocked("reddit blocked this device" if blocked else "403 with no retry hint") from e
        if e.code == 401:
            _bump("unauthorized")
            self.token = ""
            if attempt == 0:
                return
            raise MobileError("unauthorized even after a fresh token") from e
        if 500 <= e.code < 600 and attempt == 0:
            self.reset_at = time.time() + 1.0
            return
        _bump("errors")
        raise MobileError(f"http {e.code}") from e


# --------------------------------------------------------------------------- device pool


class _DevicePool:
    """Hands out one device per call and keeps it for that call's whole conversation.

    Leasing per call rather than per request is the point: Reddit then sees one phone reading about
    one brand, which is the unit of identity this route is built on. A retired device is dropped on
    its way back in, so a block costs one call rather than the service.
    """

    def __init__(self, size: int) -> None:
        self._free: list[Device] = []
        self._lock = threading.Lock()
        self._slots = threading.BoundedSemaphore(max(1, size))

    @contextlib.contextmanager
    def lease(self) -> Iterator[Device]:
        self._slots.acquire()
        try:
            with self._lock:
                device = self._free.pop() if self._free else Device()
            try:
                yield device
            finally:
                with self._lock:
                    if not device.retired:
                        self._free.append(device)
        finally:
            self._slots.release()


_pool = _DevicePool(settings.mobile_devices)


def available() -> bool:
    """Whether callers should try this route at all. The switch exists so a change on Reddit's side
    is a one-line env edit and a restart, not a redeploy."""
    return bool(settings.mobile_enabled)


# --------------------------------------------------------------------------- the two calls we need


def info(fullnames: list[str]) -> dict[str, dict]:
    """Look up posts by fullname (`t3_xxx`). Returns {fullname: raw post}; missing ids are absent.

    This is what replaces the body-fill phase: up to 100 posts, with full `selftext`, in one call.
    """
    ids = [i for i in dict.fromkeys(fullnames) if i]
    if not ids:
        return {}
    out: dict[str, dict] = {}
    with _pool.lease() as device:
        for start in range(0, len(ids), INFO_BATCH):
            batch = ids[start : start + INFO_BATCH]
            payload = device.get("/api/info", {"id": ",".join(batch)})
            if not isinstance(payload, dict):
                raise MobileError("/api/info returned an unexpected shape")
            for child in payload.get("data", {}).get("children", []):
                data = child.get("data") or {}
                name = data.get("name")
                if child.get("kind") == "t3" and name:
                    out[name] = data
    return out


def search(term: str, limit: int = 10, sort: str = "relevance", time_filter: str = "all") -> list[dict]:
    """Search posts through the app API. Returns raw `t3` post dicts, bodies included.

    Only reachable when SEARCH_ROUTE=mobile. Read the module docstring first: this is a different
    index from the web search, not a cheaper route to the same answer.

    One call covers the whole term - Reddit caps `limit` at 100 and Stage 4 asks for 10 - so there is
    no cursor to follow and no per-page cost. The bodies arrive with the results, which is why this
    route needs no body-fill phase at all.

    Raises MobileError on a short listing. Reddit's documented soft block is a 200 carrying almost
    nothing, and treating that as "the brand has no discussion" would silently empty a report.
    """
    payload = None
    with _pool.lease() as device:
        payload = device.get("/search", {"q": term, "sort": sort, "t": time_filter,
                                         "limit": max(1, min(int(limit), 100)), "type": "link"})
    if not isinstance(payload, dict):
        raise MobileError("/search returned an unexpected shape")
    children = payload.get("data", {}).get("children", [])
    posts = [c["data"] for c in children if c.get("kind") == "t3" and c.get("data")]
    if limit >= 10 and len(posts) < 3:
        raise MobileError(f"short listing for {term!r} ({len(posts)} posts) - treating as a soft block")
    return posts


def comments(post_id: str, limit: int = 20, depth: int = 2, sort: str = "top") -> tuple[dict, list[dict]]:
    """Read one thread by id. Returns (raw post, raw comments flattened in display order).

    `post_id` is the bare id (`1whp5vw`), which is what a thread URL carries. Reddit answers with two
    listings: the post, then the comment tree. "more" stubs are skipped - they are pointers to
    unloaded replies rather than content, and following them costs a call per stub for progressively
    less relevant text.
    """
    pid = str(post_id or "").strip().removeprefix("t3_")
    if not pid:
        raise MobileError("comments() needs a post id")
    with _pool.lease() as device:
        payload = device.get(f"/comments/{pid}", {"limit": limit, "depth": depth, "sort": sort})
    if not isinstance(payload, list) or len(payload) < 2:
        raise MobileError(f"/comments/{pid} returned an unexpected shape")
    try:
        post = payload[0]["data"]["children"][0]["data"]
    except (KeyError, IndexError, TypeError) as e:
        raise MobileError(f"/comments/{pid} carried no post") from e

    flat: list[dict] = []

    def walk(listing) -> None:
        if not isinstance(listing, dict):
            return
        for child in listing.get("data", {}).get("children", []):
            if child.get("kind") != "t1":
                continue
            data = child.get("data") or {}
            flat.append(data)
            walk(data.get("replies"))

    walk(payload[1])
    return post, flat
