"""Fetch Reddit search and thread pages with a stealth browser and parse the server-rendered markup.

Reddit's JSON endpoints (`/search.json`, `/comments/<id>.json`) answer 403 "blocked by network security"
to plain HTTP clients AND to the stealth browser, so they are not an option. The regular web pages do
load in the stealth browser, and they are server-rendered with the data in attributes:

  search   /svc/shreddit/search/?q=...  (the page's own infinite-scroll fragment, ~7 posts, ~5 s)
           <search-telemetry-tracker data-faceplate-tracking-context='{"post":{id,title},"subreddit":{name},...}'>
           plus <faceplate-timeago ts>, <faceplate-number> votes/comments, and a cursor link to the next page.
  thread   /r/<sub>/comments/<id>/
           <shreddit-post id comment-count score created-timestamp subreddit-name ...>
           <shreddit-comment thingid depth parentid score created author ...>
           bodies in #<id>-post-rtjson-content and #<thingid>-comment-rtjson-content
"""

from __future__ import annotations

import html as htmllib
import json
import logging
import random
import re
import threading
import time
from concurrent.futures import Future, ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Callable, Iterator
from urllib.parse import urlencode, urlsplit

from scrapling.parser import Selector

from . import mobile
from .config import settings

log = logging.getLogger(__name__)

BASE = "https://www.reddit.com"
SEARCH_PATH = "/svc/shreddit/search/"
SORTS = {"relevance", "hot", "top", "new", "comments"}
TIMES = {"all", "hour", "day", "week", "month", "year"}

# Text Reddit serves instead of content when it refuses us. Kept lowercase.
BLOCK_MARKERS = ("blocked by network security", "whoa there, pardner", "you've been blocked")

THREAD_PATH_RE = re.compile(r"/r/([A-Za-z0-9_]+)/comments/([a-z0-9]+)", re.I)
NEXT_CURSOR_RE = re.compile(r'src="(/svc/shreddit/search/\?[^"]*cursor=[^"]*)"')

# Fetches per page before giving up - fetch_page's default. Named rather than inlined because the
# time budget has to price a fetch before committing to it, and a cost estimate that drifts from
# the real attempt count would silently stop bounding anything.
ATTEMPTS = 2

Fetcher = Callable[[str, "str | None"], "tuple[int, str]"]


class RedditError(Exception):
    """Base class. `status` is the HTTP status the API should answer with."""

    status = 502


class ScrapeBlocked(RedditError):
    """Reddit refused us (IP block, rate limit, interstitial). OUR problem, a vendor failure in Stage 4
    terms, so the message must NOT contain '404', 'not found', 'no such page' or 'invalid url'
    (Stage 4's BRAND_ERROR_RE would burn one of the brand's retries)."""

    status = 503


class ScrapeFailed(RedditError):
    """Browser or network failure. Also a vendor failure from Stage 4's point of view."""

    status = 503


class ThreadMissing(RedditError):
    """A thread page that 404s or renders no post (deleted, private, quarantined). Skipped, never surfaced."""

    status = 404


@dataclass
class Post:
    id: str  # t3_xxxxx
    title: str
    body: str
    subreddit: str
    author: str
    created_at: str
    score: int
    num_comments: int
    permalink: str
    nsfw: bool = False
    # True when `body` is Reddit's search snippet rather than the full text from the thread page.
    body_is_snippet: bool = False
    search_term: str = ""


@dataclass
class Comment:
    id: str  # t1_xxxxx
    post_id: str
    parent_id: str
    body: str
    subreddit: str
    author: str
    created_at: str
    score: int
    depth: int
    permalink: str


@dataclass
class SearchPage:
    posts: list[Post]
    next_url: str | None


@dataclass
class Thread:
    post: Post
    comments: list[Comment]
    url: str


@dataclass
class SearchResult:
    posts: list[Post]
    pages_fetched: int
    seconds: float
    failed_terms: dict[str, str] = field(default_factory=dict)
    # True when SCRAPE_BUDGET_S stopped us early - a page we did not fetch, or a body we did not
    # fill. The posts are real either way, so this is the only thing separating "that is all
    # Reddit had" from "we ran out of time"; both return 200. `failed_terms` is not a substitute,
    # it means a term errored.
    truncated: bool = False
    # Posts whose body came from the mobile API instead of a browser page fetch. Every one of these
    # is a ~2.8 MB proxied page we did not pay for, so it is the number that shows the hybrid working.
    mobile_posts: int = 0
    # Per term, in request order: (posts the term returned under its own quota, posts of those that
    # survived the cross-term merge). Surfaced as X-Term-Counts so a short answer can be read as
    # dedup, a failed term or Reddit having fewer without opening the log.
    term_counts: dict[str, tuple[int, int]] = field(default_factory=dict)
    # Posts returned with an empty `body` after the fill phase. Surfaced as X-Empty-Bodies.
    empty_bodies: int = 0


@dataclass
class ThreadsResult:
    threads: list[Thread]
    pages_fetched: int
    seconds: float
    missing: list[str] = field(default_factory=list)
    failed: dict[str, str] = field(default_factory=dict)
    # As above. Deliberately NOT folded into `failed`: a thread we ran out of time for is not a
    # thread Reddit refused, and Stage 4 treats those differently.
    truncated: bool = False
    # Threads served by the mobile API rather than the browser. As above: the hybrid's receipt.
    mobile_threads: int = 0


# --------------------------------------------------------------------------- small helpers


def _int(v) -> int:
    try:
        return int(float(str(v).strip()))
    except (TypeError, ValueError):
        return 0


def _text(node) -> str:
    return re.sub(r"\s+", " ", node.get_all_text(separator=" ", strip=True)).strip() if node is not None else ""


def iso(ts: str | None) -> str:
    """Reddit writes `2026-04-23T04:06:19.598000+0000`. JavaScript's Date.parse is unreliable on the
    6-digit fraction and colon-less offset, and Stage 4 ages posts with Date.parse, so normalise to
    `2026-04-23T04:06:19.598Z`."""
    raw = (ts or "").strip()
    if not raw:
        return ""
    fixed = re.sub(r"([+-]\d\d)(\d\d)$", r"\1:\2", raw.replace("Z", "+00:00"))
    try:
        dt = datetime.fromisoformat(fixed)
    except ValueError:
        return raw
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _ctx(node) -> dict:
    try:
        return json.loads(node.attrib.get("data-faceplate-tracking-context") or "{}")
    except json.JSONDecodeError:
        return {}


def _title_of(html: str) -> str:
    m = re.search(r"<title>(.*?)</title>", html, re.S)
    return re.sub(r"\s+", " ", m.group(1)).strip()[:120] if m else ""


def is_blocked_page(html: str) -> bool:
    low = html[:400_000].lower()
    return any(m in low for m in BLOCK_MARKERS)


# --------------------------------------------------------------------------- URLs


def build_search_url(term: str, sort: str = "relevance", time_filter: str = "all") -> str:
    sort = sort.lower() if sort and sort.lower() in SORTS else "relevance"
    time_filter = time_filter.lower() if time_filter and time_filter.lower() in TIMES else "all"
    # Reddit silently "corrects" unfamiliar words, and brand names are exactly those (it rewrote a made-up brand
    # into "brand that doesn't exist" and served unrelated posts). Stage 4 relies on literal matching.
    params = {"q": term, "type": "posts", "sort": sort, "t": time_filter, "disableSpellCorrection": "true"}
    return BASE + SEARCH_PATH + "?" + urlencode(params)


def thread_url(raw: str) -> str:
    """Normalise any thread link (www/old/new reddit, with or without a slug or comment id) to its canonical
    www.reddit.com page. Raises ValueError for anything that is not a thread."""
    m = THREAD_PATH_RE.search(str(raw or ""))
    if not m:
        raise ValueError(f"not a reddit thread link: {str(raw)[:120]!r}")
    return f"{BASE}/r/{m.group(1)}/comments/{m.group(2).lower()}/"


def post_id_of(raw: str) -> str:
    """The bare post id out of any thread link. Raises ValueError for anything that is not a thread.

    This is the whole bridge between the two routes: the browser finds posts and knows their URLs,
    and the mobile API is addressed by id.
    """
    m = THREAD_PATH_RE.search(str(raw or ""))
    if not m:
        raise ValueError(f"not a reddit thread link: {str(raw)[:120]!r}")
    return m.group(2).lower()


# --------------------------------------------------------------------------- parsing


def _card_of(node, levels: int = 8):
    """Nearest ancestor holding the whole search result (it is the one that carries the counter row)."""
    cur = node
    for _ in range(levels):
        cur = cur.parent
        if cur is None:
            return None
        if cur.css('[data-testid="search-counter-row"]'):
            return cur
    return None


def _counts(card) -> tuple[int, int]:
    votes = comments = 0
    rows = card.css('[data-testid="search-counter-row"]') if card is not None else []
    if not rows:
        return votes, comments
    numbers = rows[0].css("faceplate-number")
    for i, n in enumerate(numbers):
        label = _text(n.parent).lower()
        value = _int(n.attrib.get("number"))
        if "comment" in label:
            comments = value
        elif "vote" in label:
            votes = value
        elif i == 0:
            votes = value
        else:
            comments = value
    return votes, comments


def parse_search(html: str, term: str = "") -> SearchPage:
    doc = Selector(html)
    posts: list[Post] = []
    seen: set[str] = set()
    for trk in doc.css("search-telemetry-tracker"):
        ctx = _ctx(trk)
        if (ctx.get("action_info") or {}).get("type") != "post":
            continue
        p = ctx.get("post") or {}
        pid = str(p.get("id") or "")
        if not pid.startswith("t3_") or pid in seen:
            continue
        seen.add(pid)
        card = _card_of(trk)

        link = None
        for sel in ('a[data-testid="post-title-text"]', 'a[data-testid="post-title"]'):
            found = card.css(sel) if card is not None else []
            if found:
                link = found[0]
                break
        if link is None:
            found = trk.css("a")
            link = found[0] if found else None
        permalink = (link.attrib.get("href") if link is not None else "") or ""

        # Reddit's snippet is sometimes a matching COMMENT (snippet_id t1_...). Only the post's own text may
        # stand in for its body, or a stranger's reply would be quoted as the poster's words.
        snippet = ""
        candidates = [ctx] + ([_ctx(t) for t in card.css("search-telemetry-tracker")] if card is not None else [])
        for c in candidates:
            s = c.get("search") or {}
            if s.get("snippet") and s.get("snippet_id") == pid:
                snippet = str(s["snippet"])
                break

        ts = card.css("faceplate-timeago") if card is not None else []
        votes, num_comments = _counts(card)
        posts.append(
            Post(
                id=pid,
                title=re.sub(r"\s+", " ", str(p.get("title") or "")).strip(),
                body=re.sub(r"\s+", " ", snippet).strip(),
                subreddit=str((ctx.get("subreddit") or {}).get("name") or ""),
                author=str((ctx.get("profile") or {}).get("name") or ""),
                created_at=iso(ts[0].attrib.get("ts") if ts else ""),
                score=votes,
                num_comments=num_comments,
                permalink=permalink,
                nsfw=bool(p.get("nsfw")),
                body_is_snippet=True,
                search_term=term,
            )
        )
    m = NEXT_CURSOR_RE.search(html)
    next_url = BASE + htmllib.unescape(m.group(1)) if m else None
    return SearchPage(posts=posts, next_url=next_url)


def parse_thread(html: str, url: str = "") -> Thread:
    doc = Selector(html)
    found = doc.css("shreddit-post")
    if not found:
        raise ThreadMissing(f"thread page rendered no post: {url}")
    a = found[0].attrib
    pid = str(a.get("id") or "")
    sub = str(a.get("subreddit-name") or "").removeprefix("r/")
    body = doc.css(f"#{pid}-post-rtjson-content") if pid else []
    post = Post(
        id=pid,
        title=re.sub(r"\s+", " ", str(a.get("post-title") or "")).strip(),
        body=_text(body[0]) if body else "",
        subreddit=sub,
        author=str(a.get("author") or ""),
        created_at=iso(a.get("created-timestamp")),
        score=_int(a.get("score")),
        num_comments=_int(a.get("comment-count")),
        permalink=str(a.get("permalink") or ""),
        nsfw="nsfw" in a,
    )
    comments: list[Comment] = []
    for c in doc.css("shreddit-comment"):
        ca = c.attrib
        tid = str(ca.get("thingid") or "")
        if not tid:
            continue
        # Deleted and removed comments render no body element; they carry nothing to quote.
        text_nodes = doc.css(f"#{tid}-comment-rtjson-content")
        text = _text(text_nodes[0]) if text_nodes else ""
        if not text:
            continue
        comments.append(
            Comment(
                id=tid,
                post_id=str(ca.get("postid") or pid),
                parent_id=str(ca.get("parentid") or pid),
                body=text,
                subreddit=sub,
                author=str(ca.get("author") or ""),
                created_at=iso(ca.get("created")),
                score=_int(ca.get("score")),
                depth=_int(ca.get("depth")),
                permalink=str(ca.get("permalink") or ""),
            )
        )
    return Thread(post=post, comments=comments, url=url)


# ------------------------------------------------------- the same models, from the mobile route
#
# The browser reads attributes off rendered HTML; the app API hands back JSON. Both end up as the
# same `Post` and `Comment`, because everything downstream - mapping.py, Stage 4's Code nodes -
# must not be able to tell which route served a given item. That includes the small things: text is
# whitespace-collapsed here exactly as `_text()` does it for HTML, so a body does not change shape
# depending on which route fetched it.


def _epoch_iso(ts) -> str:
    """Reddit's JSON dates its content with a UNIX float; the HTML carries a formatted string.
    Both become the `...Z` form Stage 4 parses with Date.parse."""
    try:
        return datetime.fromtimestamp(float(ts), timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")
    except (TypeError, ValueError, OSError):
        return ""


def _flat(text) -> str:
    return re.sub(r"\s+", " ", str(text or "")).strip()


def _selftext(raw: dict) -> str:
    """The post's own text, or the original's for a crosspost.

    Reddit gives a crosspost an empty `selftext` and carries the original post under
    `crosspost_parent_list`; the thread page renders that original's text as the body, which is
    what a reader sees and what the actor returned. Every other empty `selftext` is a link, image,
    gallery or video post, and empty is correct for those (the actor's docs: "Empty for link posts").
    """
    text = _flat(raw.get("selftext"))
    if text:
        return text
    parents = raw.get("crosspost_parent_list") or []
    return _flat(parents[0].get("selftext")) if parents and isinstance(parents[0], dict) else ""


def post_from_raw(raw: dict, search_term: str = "") -> Post:
    return Post(
        id=str(raw.get("name") or ("t3_" + str(raw.get("id") or ""))),
        title=_flat(raw.get("title")),
        body=_selftext(raw),
        subreddit=str(raw.get("subreddit") or ""),
        author=str(raw.get("author") or ""),
        created_at=_epoch_iso(raw.get("created_utc")),
        score=_int(raw.get("score")),
        num_comments=_int(raw.get("num_comments")),
        permalink=str(raw.get("permalink") or ""),
        nsfw=bool(raw.get("over_18")),
        body_is_snippet=False,
        search_term=search_term,
    )


def comment_from_raw(raw: dict, subreddit: str = "") -> Comment | None:
    """None for a comment with nothing to quote, matching the browser path, which drops deleted and
    removed comments because they render no body element at all."""
    body = _flat(raw.get("body"))
    if not body or body in ("[deleted]", "[removed]"):
        return None
    return Comment(
        id=str(raw.get("name") or ("t1_" + str(raw.get("id") or ""))),
        post_id=str(raw.get("link_id") or ""),
        parent_id=str(raw.get("parent_id") or ""),
        body=body,
        subreddit=str(raw.get("subreddit") or subreddit),
        author=str(raw.get("author") or ""),
        created_at=_epoch_iso(raw.get("created_utc")),
        score=_int(raw.get("score")),
        depth=_int(raw.get("depth")),
        permalink=str(raw.get("permalink") or ""),
    )


# --------------------------------------------------------------------------- fetching

# One gate for every browser launch in the process, so parallel requests cannot stack up browsers.
_browser_gate = threading.BoundedSemaphore(max(1, settings.max_concurrency))

# Decodo exposes the same residential pool on two kinds of port. 7000 is the ROTATING gateway: a new
# exit IP per request, which would move us mid-scrape and hand Reddit a fresh address for every page.
# The 10001+ band is sticky - the gateway pins one exit IP to that port for the session window - so
# the port number is effectively the session handle, and a different port is the only way to reach a
# different exit. Half-open, so the usable ports are 10001..10099.

# Proxy plumbing lives in `proxy` so the mobile route can share it without importing this module.
# Re-exported because the browser code below, and the tests, address these as scraper.*.
from .proxy import (  # noqa: F401
    ROTATING_GATEWAY_PORTS,
    STICKY_PORT_RANGE,
    _PortPool,
    _port_pool,
    _reject_rotating_gateway,
    proxy_config,
    proxy_url,
)


def _fetch_cost_s(attempts: int = ATTEMPTS) -> float:
    """Worst case wall clock for one fetch_page call: every attempt times out, plus our pauses.

    Callers use this to ask "is there room for a whole fetch" before starting one, rather than
    "is the deadline already past". The difference matters: a fetch begun just under the deadline
    overruns it by its entire cost, which is how a 200s budget turns into a 245s response. Takes
    `attempts` because the three call paths differ - fill_bodies passes 1, the others use ATTEMPTS.
    """
    return attempts * (settings.fetch_timeout_ms / 1000) + (attempts - 1) * settings.retry_delay_s


def _html_of(page) -> str:
    for attr in ("html_content", "body", "text"):
        v = getattr(page, attr, None)
        if v:
            return v.decode("utf-8", "replace") if isinstance(v, bytes) else str(v)
    return ""


def fetch_html(url: str, wait_selector: str | None = None) -> tuple[int, str]:
    """One stealth-browser fetch. Returns (status, html). Raises ScrapeFailed on browser/network errors."""
    from scrapling.fetchers import StealthyFetcher

    kwargs = dict(
        headless=True,
        disable_resources=settings.block_resources,
        block_webrtc=True,
        # Not redundant, and not safe to drop: Scrapling's own default is retries=3, and it
        # multiplies with fetch_page's attempts and the page loop above that - 3 x 2 x 3 = 18
        # navigations for one search term, ~828s worst case against a 290s node timeout. The one
        # retry that matters lives in fetch_page, where it can tell a block from a dead browser;
        # this layer just repeats blindly. fetch_page's `except ScrapeFailed` is now the only
        # crash retry in the stack - deleting it turns one flaky Chromium launch into a 503.
        retries=1,
        # Resolve DNS inside the proxy tunnel. Without it Chromium resolves reddit.com against the
        # container's resolver, which leaks the real egress around a proxy that exists to hide it.
        dns_over_https=True,
        timeout=settings.fetch_timeout_ms,
    )
    # Removed 2026-09-17: humanize / os_randomize / geoip. They are Camoufox-era (scrapling 0.2.x)
    # arguments that do not exist in 0.4.15 - its msgspec validator absorbs unknown keys silently,
    # so they read as active stealth while doing nothing at all. If Reddit starts blocking this
    # server, do not rule them out as already tried; they were never on.
    if wait_selector:
        kwargs.update(wait_selector=wait_selector, wait_selector_state="attached")
    # One sticky exit per fetch, held only while this browser lives. Because fetch_page calls the
    # fetcher once per attempt, a retry automatically lands on a DIFFERENT exit - which is the whole
    # point: Reddit refuses some residential exits outright, and waiting never changes that verdict.
    with _port_pool.lease() as port, _browser_gate:
        proxy = proxy_config(port)
        if proxy:
            kwargs["proxy"] = proxy
        try:
            page = StealthyFetcher.fetch(url, **kwargs)
        except Exception as e:  # noqa: BLE001 - anything from the browser stack is a vendor failure
            # The port, never the credential: logging the proxy dict would put the password in the
            # container log and in every error the API returns.
            via = f" (exit port {port})" if proxy and port else ""
            raise ScrapeFailed(f"browser fetch failed{via}: {type(e).__name__}: {e}"[:300]) from e
    log.debug("fetched %s via exit port %s", url, port if proxy else "direct")
    return int(page.status), _html_of(page)


def fetch_page(url: str, fetcher: Fetcher = fetch_html, wait_selector: str | None = None, attempts: int = ATTEMPTS) -> str:
    """Fetch one page, retrying once when Reddit refuses us. A 404 raises ThreadMissing straight away."""
    last_err: RedditError | None = None
    for attempt in range(1, attempts + 1):
        try:
            status, html = fetcher(url, wait_selector)
        except ScrapeFailed as e:
            last_err = e
        else:
            if status == 404:
                raise ThreadMissing(f"reddit answered HTTP {status} for {url}")
            if status == 200 and not is_blocked_page(html):
                return html
            last_err = ScrapeBlocked(f"reddit refused the request (HTTP {status}), title={_title_of(html)!r}")
        log.warning("attempt %d/%d failed for %s: %s", attempt, attempts, url, last_err)
        if attempt < attempts:
            time.sleep(settings.retry_delay_s)
    assert last_err is not None
    raise last_err


@dataclass
class TermOutcome:
    posts: list[Post]  # the term's answer: up to `max_posts` posts no earlier term returned
    pages: int
    truncated: bool
    raw: int  # every post the term's pages produced, before the earlier terms' posts were set aside


def search_term(
    term: str,
    max_posts: int = 10,
    sort: str = "relevance",
    time_filter: str = "all",
    include_nsfw: bool = False,
    fetcher: Fetcher = fetch_html,
    deadline: float | None = None,
    exclude: Callable[[], set[str]] | None = None,
) -> TermOutcome:
    """Posts for one search term, following Reddit's cursor until `max_posts` posts that no earlier
    term returned, or MAX_SEARCH_PAGES.

    `exclude` answers "which post ids did the earlier terms return"; it may block until those terms
    are final. It is asked once, after page 1 is in hand, so page 1 of every term is still fetched
    in parallel and only the decision to page on waits. Until 21 Sep 2026 each term filled its
    quota on its own and the cross-term merge threw the overlap away afterwards: with Stage 4's
    three terms per brand ("Brand", "Brand product", "Brand reviews") the second and third mostly
    returned what the first already had, and a brand landed on 20-25 distinct posts instead of 30
    (GRIP6 10/8/6 -> 24, Vessel Golf 10/8/4 -> 22, eskiin 10/3/1 -> 14) while every term reported
    a full 10. The quota is now filled after the overlap is set aside, page by page, within the
    same MAX_SEARCH_PAGES.

    `truncated` means the budget stopped us with more pages available, not that anything failed."""
    posts: list[Post] = []
    owned: list[Post] = []
    seen: set[str] = set()
    excluded: set[str] | None = None
    url: str | None = build_search_url(term, sort, time_filter)
    pages = 0
    truncated = False
    fresh = -1
    page_failed = False
    while url and pages < settings.max_search_pages and len(owned) < max_posts:
        # Page 1 is never skipped, however late we already are: returning zero posts would read
        # downstream as "Reddit has nothing for this brand" and trigger the Tavily fallback, which
        # is a data lie. A late answer is recoverable; a wrong one is not.
        if pages and deadline is not None and time.time() + _fetch_cost_s() > deadline:
            truncated = True
            log.info("%r budget exhausted after %d page(s), returning %d post(s) early", term, pages, len(posts))
            break
        # Page 1 gets one more roll at the exit than later pages. A refused exit answers in seconds,
        # not FETCH_TIMEOUT_MS, and page 1 is the whole term: lose it and the term contributes
        # nothing while the call still returns 200. Measured 19 Sep 2026: 3 of 21 first attempts
        # were refused and every retry on a fresh exit succeeded. Later pages keep ATTEMPTS so the
        # budget reservation above (`_fetch_cost_s()`, priced on ATTEMPTS) stays honest.
        try:
            html = fetch_page(url, fetcher=fetcher, attempts=ATTEMPTS + 1 if pages == 0 else ATTEMPTS)
        except RedditError as e:
            if not pages:
                raise  # nothing collected: the term failed, and the caller decides what that means
            # A refused later page used to discard the whole term, page 1 included: the exception
            # left this loop with the posts still in `posts`. Those posts are real; keep them.
            log.warning("%r page %d failed, keeping the %d post(s) already collected: %s", term, pages + 1, len(posts), e)
            page_failed = True
            break
        pages += 1
        page = parse_search(html, term)
        if excluded is None:
            excluded = exclude() if exclude is not None else set()
        fresh = 0
        for p in page.posts:
            if p.id in seen or (p.nsfw and not include_nsfw):
                continue
            seen.add(p.id)
            posts.append(p)
            fresh += 1
            if p.id not in excluded:
                owned.append(p)
        log.info(
            "%r page %d: %d on page, %d new, %d already returned by an earlier term, %d/%d owned, next=%s",
            term, pages, len(page.posts), fresh, len(posts) - len(owned), len(owned), max_posts,
            "yes" if page.next_url else "no",
        )
        if pages == 1 and not page.posts:
            # A 200 that parsed to nothing is either a genuinely empty result or a page Reddit
            # served instead of results without any block marker. Loud, because downstream reads
            # it as "Reddit has nothing for this brand".
            log.warning("%r page 1 parsed to zero posts (title=%r)", term, _title_of(html))
        if fresh == 0:
            break  # an empty or repeating page means the results are exhausted
        url = page.next_url
    # Why the loop ended, so a short term reads as what it was rather than "Reddit had fewer".
    if len(owned) >= max_posts:
        stopped_by = "quota"
    elif truncated:
        stopped_by = "budget"
    elif page_failed:
        stopped_by = "page_failed"
    elif fresh == 0:
        stopped_by = "empty_page"
    elif not url:
        stopped_by = "no_next_page"
    else:
        stopped_by = "max_pages"
    log.info(
        "%r requested=%d returned=%d raw=%d pages=%d stopped_by=%s",
        term, max_posts, min(len(owned), max_posts), len(posts), pages, stopped_by,
    )
    return TermOutcome(posts=owned[:max_posts], pages=pages, truncated=truncated, raw=len(posts))



def scrape_search(
    terms: list[str],
    max_posts: int = 10,
    sort: str = "relevance",
    time_filter: str = "all",
    include_nsfw: bool = False,
    fetcher: Fetcher = fetch_html,
    full_bodies: bool | None = None,
) -> SearchResult:
    """Run every term in parallel. Posts come back in term order, de-duplicated across terms (the first
    term that found a post keeps it), and each term fills its quota with posts the earlier terms did
    not return, paging on within MAX_SEARCH_PAGES to do so. Fails only when EVERY term failed;
    otherwise partial results win."""
    terms = [t for t in dict.fromkeys(re.sub(r"\s+", " ", str(t or "")).strip() for t in terms) if t]
    if not terms:
        raise ValueError("request must include at least one search term (`searchTerms`)")
    full_bodies = settings.full_bodies if full_bodies is None else full_bodies
    started = time.time()
    # One budget for the whole call, shared by every term and then by the body-fill phase below.
    # Terms run in parallel but queue on the browser gate, so the deadline is what stops a slow
    # term from spending the body-fill phase's time as well as its own.
    deadline = started + settings.scrape_budget_s
    outcomes: dict[str, TermOutcome | RedditError] = {}

    def returned_by(earlier: list[Future]) -> Callable[[], set[str]]:
        # What the terms before this one returned. Blocks until they are final, which is what
        # makes "distinct" mean "not returned by an earlier term" rather than "not seen yet by
        # whichever thread happened to run first". A failed earlier term returned nothing.
        def ids() -> set[str]:
            out: set[str] = set()
            for f in earlier:
                try:
                    out.update(p.id for p in f.result().posts)
                except RedditError:
                    pass
            return out
        return ids

    # Every term needs its own worker: a later term waits on the earlier ones, so a pool smaller
    # than the term count could park term 1 behind term 3 and never finish.
    with ThreadPoolExecutor(max_workers=len(terms)) as pool:
        futures: list[Future] = []
        for term in terms:
            futures.append(pool.submit(
                search_term, term, max_posts, sort, time_filter, include_nsfw, fetcher, deadline, returned_by(list(futures)),
            ))
        for term, future in zip(terms, futures):
            try:
                outcomes[term] = future.result()
            except RedditError as e:
                outcomes[term] = e

    posts: list[Post] = []
    seen: set[str] = set()
    pages = 0
    failed: dict[str, str] = {}
    truncated = False
    term_counts: dict[str, tuple[int, int]] = {}
    for term in terms:
        out = outcomes[term]
        if isinstance(out, RedditError):
            failed[term] = str(out)
            term_counts[term] = (0, 0)
            continue
        pages += out.pages
        truncated = truncated or out.truncated
        unique = 0
        for p in out.posts:
            if p.id not in seen:  # belt and braces: the term already set the earlier terms' posts aside
                seen.add(p.id)
                posts.append(p)
                unique += 1
        term_counts[term] = (out.raw, unique)
    # `raw` is every post the term's pages produced; `unique` is what the term contributed after the
    # earlier terms' posts were set aside. The gap is overlap that the term paged past, not a cap.
    log.info(
        "search terms (requested %d each): %s -> %d distinct post(s)",
        max_posts,
        "; ".join(f"{t!r} raw={r} unique={u}" + (" FAILED" if t in failed else "") for t, (r, u) in term_counts.items()),
        len(posts),
    )

    if len(failed) == len(terms):
        err = outcomes[terms[0]]
        assert isinstance(err, RedditError)
        raise err if isinstance(err, (ScrapeBlocked, ScrapeFailed)) else ScrapeFailed(str(err))
    mobile_posts = 0
    if full_bodies and posts:
        wanted = posts[: settings.max_body_fetches]
        # The mobile route fills every body in one call, so try it before spending a browser page on
        # any of them. The fallback is all-or-nothing on purpose: `None` means the route itself was
        # unavailable and the browser should do the whole job, while a number means it answered, and
        # we trust that answer rather than browser-fetching the handful of posts Reddit withheld.
        # Per-post fallback would look more thorough and would quietly reintroduce the bandwidth this
        # exists to remove - a partial mobile answer would put ~25 proxied pages back on the bill.
        done = fill_bodies_mobile(wanted) if mobile.available() else None
        if done is None:
            filled, bodies_truncated = fill_bodies(wanted, fetcher, deadline=deadline)
            pages += filled
            truncated = truncated or bodies_truncated
        else:
            mobile_posts = done
    empty_bodies = sum(1 for p in posts if not p.body)
    snippets = sum(1 for p in posts if p.body_is_snippet)
    log.info("bodies: %d of %d empty after fill, %d still snippet-only", empty_bodies, len(posts), snippets)
    # One line per term, after the fill, so the cap question and the body question are answered
    # side by side without inference: returned_by_source is every post the term's pages produced,
    # after_dedupe is what the term contributed once the earlier terms' posts were set aside (its
    # quota, where Reddit had that many), pages_fetched is how far it paged to get there, and
    # empty_body is counted on the posts this term contributed.
    for term, (returned, unique) in term_counts.items():
        out = outcomes[term]
        pages_for_term = 0 if isinstance(out, RedditError) else out.pages
        empty_for_term = sum(1 for p in posts if p.search_term == term and not p.body)
        log.info(
            "term=%r posts_requested=%d posts_returned_by_source=%d posts_after_dedupe=%d pages_fetched=%d posts_with_empty_body=%d%s",
            term, max_posts, returned, unique, pages_for_term, empty_for_term, " FAILED" if term in failed else "",
        )
    return SearchResult(
        posts=posts,
        pages_fetched=pages,
        seconds=round(time.time() - started, 1),
        failed_terms=failed,
        truncated=truncated,
        mobile_posts=mobile_posts,
        term_counts=term_counts,
        empty_bodies=empty_bodies,
    )


def fill_bodies_mobile(posts: list[Post]) -> int | None:
    """Fill full bodies from the mobile API, in place. Never raises.

    One call covers up to 100 posts, replacing what cost one proxied browser page each - the single
    biggest line on the bandwidth bill.

    Returns the number of posts Reddit answered for, or `None` if the route was unavailable, which
    is the caller's signal to do the whole job with the browser instead. The two are deliberately
    distinguishable: zero can legitimately mean "every one of these was a link post".
    """
    by_id = {p.id: p for p in posts if p.id}
    if not by_id:
        return 0
    try:
        raw = mobile.info(list(by_id))
    except mobile.MobileError as e:
        log.warning("mobile body fill unavailable, falling back to the browser: %s", e)
        return None

    answered = 0
    for name, data in raw.items():
        post = by_id.get(name)
        if post is None:
            continue
        # Assigned even when empty. A link or image post has no self text, and the browser path sets
        # an empty body for exactly those, so anything else would make the two routes disagree.
        post.body = _selftext(data)
        post.body_is_snippet = False
        # Reddit's live figures beat the search index's.
        post.score = _int(data.get("score")) or post.score
        post.num_comments = _int(data.get("num_comments")) or post.num_comments
        answered += 1
    log.info("mobile filled %d of %d post body(ies) in one call", answered, len(by_id))
    return answered


def fill_bodies(posts: list[Post], fetcher: Fetcher = fetch_html, deadline: float | None = None) -> tuple[int, bool]:
    """Replace search snippets with the full post text from each thread page, in place.

    Search results carry no post body (only a snippet, often from a comment), while the Apify actor returned
    the full body. `Sort Reddit Results` gates and scores on brand mentions in the body and the report prompt
    quotes it, so bodies matter. Best effort: a thread that fails, or is still queued when `deadline` passes,
    keeps its snippet, so a busy server answers inside Stage 4's timeout.

    Returns (pages_fetched, truncated). `truncated` means at least one body was skipped for time -
    invisible otherwise, since a skipped post just keeps `body_is_snippet` and downstream cannot
    tell that apart from full bodies being switched off."""
    fetched = 0
    truncated = False
    # fetched/truncated are mutated from pool threads; += is not atomic under the GIL.
    lock = threading.Lock()

    def run(p: Post) -> None:
        nonlocal fetched, truncated
        # attempts=1 below, so one fetch is the whole cost of this body.
        if deadline is not None and time.time() + _fetch_cost_s(1) > deadline:
            with lock:
                truncated = True
            return
        with lock:
            fetched += 1
        try:
            t = parse_thread(fetch_page(thread_url(p.permalink), fetcher=fetcher, wait_selector="shreddit-post", attempts=1))
        except (RedditError, ValueError) as e:
            log.info("body fetch skipped for %s: %s", p.id, e)
            return
        p.body = t.post.body
        p.body_is_snippet = False
        # The thread page is fresher than the search index.
        p.score = t.post.score or p.score
        p.num_comments = t.post.num_comments or p.num_comments

    with ThreadPoolExecutor(max_workers=max(1, settings.max_concurrency)) as pool:
        list(pool.map(run, posts))
    if truncated:
        log.info("body fill stopped at the budget after %d of %d post(s)", fetched, len(posts))
    return fetched, truncated


def scrape_threads(
    urls: list[str],
    max_comments_per_post: int = 20,
    max_comments_total: int | None = None,
    fetcher: Fetcher = fetch_html,
) -> ThreadsResult:
    """Read each thread page in parallel. Missing threads are skipped; fails only when EVERY thread failed
    for a vendor reason (block or browser crash)."""
    canonical: list[str] = []
    for raw in urls:
        try:
            u = thread_url(raw)
        except ValueError:
            log.warning("skipping non-thread url %r", raw)
            continue
        if u not in canonical:
            canonical.append(u)
    if not canonical:
        raise ValueError("request must include at least one reddit thread link (`startUrls`)")
    canonical = canonical[: settings.max_threads]
    started = time.time()
    # Same budget as the search path. Up to MAX_THREADS urls queue on a gate of MAX_CONCURRENCY, so
    # without this the last wave starts long after Stage 4's 250s node timeout has already given up.
    deadline = started + settings.scrape_budget_s
    outcomes: dict[str, Thread | RedditError] = {}
    skipped: list[str] = []
    lock = threading.Lock()

    # The mobile route reads a thread by id, with no ranking involved and no proxy, so it serves the
    # comments call outright. Anything it cannot answer for is left in `pending` and read by the
    # browser below, which is why a change on Reddit's side costs bandwidth rather than results.
    pending = list(canonical)
    mobile_threads = 0
    if mobile.available():
        pending, mobile_threads = _threads_via_mobile(canonical, outcomes, max_comments_per_post)

    def run(u: str) -> None:
        if time.time() + _fetch_cost_s(ATTEMPTS) > deadline:
            # Not an error: we never asked Reddit. Recording it in `failed` would make a clock
            # decision look like a vendor refusal to Stage 4.
            with lock:
                skipped.append(u)
            return
        try:
            outcomes[u] = parse_thread(fetch_page(u, fetcher=fetcher, wait_selector="shreddit-post"), u)
        except RedditError as e:
            outcomes[u] = e

    if pending:
        with ThreadPoolExecutor(max_workers=len(pending)) as pool:
            list(pool.map(run, pending))

    threads: list[Thread] = []
    missing: list[str] = []
    failed: dict[str, str] = {}
    budget = max_comments_total if max_comments_total is not None else 10**9
    for u in canonical:
        out = outcomes.get(u)
        if out is None:  # never attempted - the budget ran out before this one reached the gate
            continue
        if isinstance(out, ThreadMissing):
            missing.append(u)
            continue
        if isinstance(out, RedditError):
            failed[u] = str(out)
            continue
        keep = out.comments[: max(0, min(max_comments_per_post, budget))]
        budget -= len(keep)
        threads.append(Thread(post=out.post, comments=keep, url=u))

    if failed and not threads and not missing:
        err = outcomes[next(iter(failed))]
        assert isinstance(err, RedditError)
        raise err
    if skipped:
        log.info("budget exhausted, %d of %d thread(s) never attempted", len(skipped), len(canonical))
    return ThreadsResult(
        threads=threads,
        # Browser page fetches only: threads we never attempted are not pages we fetched, and neither
        # are threads the mobile route served. `mobile_threads` carries those, so the two numbers stay
        # readable as what they cost - a page here is a proxied megabyte, a mobile call is not.
        pages_fetched=len(pending) - len(skipped),
        seconds=round(time.time() - started, 1),
        missing=missing,
        failed=failed,
        truncated=bool(skipped),
        mobile_threads=mobile_threads,
    )


def _threads_via_mobile(
    canonical: list[str], outcomes: dict[str, Thread | RedditError], max_comments_per_post: int
) -> tuple[list[str], int]:
    """Read what it can through the mobile API, recording results into `outcomes`.

    Returns (urls the browser still has to read, threads served here). Per-thread fallback is right
    here, unlike the body fill: a thread costs one call either way, so retrying a single failure on
    the browser buys a real result for one page, not twenty-five.

    A hard block stops the loop rather than working through the rest, because the device is gone and
    every further call would be a guaranteed failure paid for in latency.
    """
    pending: list[str] = []
    served = 0
    blocked = False
    for url in canonical:
        if blocked:
            pending.append(url)
            continue
        try:
            raw_post, raw_comments = mobile.comments(post_id_of(url), limit=max_comments_per_post)
        except mobile.MobileBlocked as e:
            log.warning("mobile route blocked, the browser takes the rest of this call: %s", e)
            blocked = True
            pending.append(url)
            continue
        except (mobile.MobileError, ValueError) as e:
            log.info("mobile could not read %s, falling back to the browser: %s", url, e)
            pending.append(url)
            continue

        # A deleted or private thread is not a failure to retry on the browser - the browser would
        # render no post either and raise exactly this. Recording it here saves that wasted page.
        if not raw_post or raw_post.get("removed_by_category") or not raw_post.get("id"):
            outcomes[url] = ThreadMissing(f"thread is unavailable: {url}")
            served += 1
            continue

        post = post_from_raw(raw_post)
        comments = [c for c in (comment_from_raw(r, post.subreddit) for r in raw_comments) if c is not None]
        outcomes[url] = Thread(post=post, comments=comments, url=url)
        served += 1
    return pending, served
