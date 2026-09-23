"""Brand mentions inside Reddit COMMENTS, in the shape the Tavily fallback node reads.

Workflow 02's `Reddit Fallback Via Web Search` ran one Tavily web search restricted to reddit.com
whenever the primary post search found nothing usable (about 1 brand in 3), and read only
`results[].title`, `results[].url` and `results[].raw_content || content`. Its value was finding
threads where the brand is named in a COMMENT, which the post search misses. Reddit's own comment
search does that directly: `/svc/shreddit/search/?type=comments` is the same server-rendered
partial the post search uses, fetched through the same browser, proxy pool and gate, and every
card carries the matching comment's text, the post title, both ids, the subreddit, the time and
the votes. Measured 23 Sep 2026 from the VPS: 2-5 s a page, and for Howdysnax exactly the three
threads Tavily had found (r/Protein, r/OaklandFood, r/office). Unlike Tavily's `raw_content` the
partial carries no page chrome, which is what produced Tavily's false brand matches ("Related
Answers" naming Safe Hero on a Monster Hunter thread).

The node slices the text it keeps to 900 characters, so `raw_content` leads with the matching
comments and puts the post body (one `mobile.info` call for every thread at once) after them.
Subreddit, time and votes come back as extra fields the node does not read, so the research
output stays what it was: a `web-search-extract` item with a provenance label and no metadata.

Budget: the node's HTTP timeout is 45 s. Page 1 of every term is fetched in parallel and never
skipped; page 2 runs only while the merged threads are still under `maxResults` and a whole
fetch plus the body fill fit before the deadline; the body fill is bounded by whatever is left.
"""

from __future__ import annotations

import html as htmllib
import logging
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FutureTimeout
from dataclasses import dataclass, field
from functools import partial

from scrapling.parser import Selector

from . import mobile
from .config import settings
from urllib.parse import urlsplit

from .scraper import (
    ATTEMPTS,
    BASE,
    NEXT_CURSOR_RE,
    Fetcher,
    RedditError,
    ScrapeBlocked,
    ScrapeFailed,
    _counts,
    _ctx,
    _fetch_cost_s,
    _flat,
    _int,
    _selftext,
    _text,
    build_comment_search_url,
    fetch_html,
    fetch_page,
    iso,
    thread_url,
)

log = logging.getLogger("reddit_reviews.mentions")

# Parse Keywords sends three terms and MAX_CONCURRENCY is three browsers: a fourth term would only
# queue on the gate and eat the budget.
MAX_TERMS = 3
# Time kept back for the one mobile.info call: measured 0.3 s, plus a device's 2-3 s pacing and a
# possible token mint. A page 2 that would leave less than this is not started.
BODY_RESERVE_S = 5.0


@dataclass
class CommentHit:
    comment_id: str  # t1_...
    post_id: str  # t3_...
    title: str  # the post's title, from the tracker context
    subreddit: str
    text: str  # the comment's own text, nothing else
    permalink: str  # /r/<sub>/comments/<id>/<slug>/<comment>/ where the card had it
    thread_link: str  # /r/<sub>/comments/<id>/<slug>/
    created_at: str  # the comment's time, ISO with Z
    score: int  # the comment's votes
    post_score: int = 0
    post_comments: int = 0
    nsfw: bool = False
    position: int = 0
    term: str = ""


@dataclass
class CommentSearchPage:
    hits: list[CommentHit]
    next_url: str | None


@dataclass
class MentionThread:
    post_id: str
    title: str
    url: str
    subreddit: str
    comments: list[CommentHit] = field(default_factory=list)
    created_at: str = ""  # earliest matching comment
    score: int = 0  # best matching comment
    post_score: int = 0
    post_comments: int = 0
    body: str = ""
    body_filled: bool = False
    first_term: str = ""


@dataclass
class MentionsResult:
    threads: list[MentionThread]
    pages_fetched: int
    seconds: float
    failed_terms: dict[str, str] = field(default_factory=dict)
    truncated: bool = False
    bodies_filled: int = 0
    # Threads whose only evidence was the brand phrase used as ordinary words (see Corroboration).
    generic_dropped: int = 0
    # Per term, in request order: (comment hits its pages produced, threads it was first to find).
    term_counts: dict[str, tuple[int, int]] = field(default_factory=dict)


# --------------------------------------------------------------------------- parsing


def _comment_text(body) -> str:
    """The comment's words, plus the address of every link in it. A brand is often named only in
    the href ("apparently <a href=https://www.howdysnax.com/...>this shit is really good!</a>"),
    and the node's gate reads text, so the link is rendered as `text (link: host/path)`. One of
    the three Howdysnax threads Tavily found (r/office) is exactly this case."""
    text = _text(body)
    links: list[str] = []
    for a in body.css("a[href]"):
        href = str(a.attrib.get("href") or "")
        if not href.startswith("http"):
            continue
        parts = urlsplit(href)
        shown = (parts.netloc + parts.path).rstrip("/")
        if shown and shown not in links:
            links.append(shown[:80])
    if links:
        text = f"{text} (link: {', '.join(links[:3])})" if text else f"(link: {', '.join(links[:3])})"
    return text


def parse_comment_search(html: str, term: str = "") -> CommentSearchPage:
    """One page of Reddit's comment search. A result is a `search-telemetry-tracker` whose context
    names a `comment.id`, wrapping a `search-sdui-comment-unit`; the nested trackers inside the
    card repeat the same ids, so the first tracker per comment id is the card root."""
    doc = Selector(html)
    hits: list[CommentHit] = []
    seen: set[str] = set()
    for trk in doc.css("search-telemetry-tracker"):
        ctx = _ctx(trk)
        c = ctx.get("comment") or {}
        p = ctx.get("post") or {}
        cid = str(c.get("id") or "")
        pid = str(c.get("post_id") or p.get("id") or "")
        if not cid.startswith("t1_") or not pid.startswith("t3_") or cid in seen:
            continue
        seen.add(cid)
        units = trk.css('[data-testid="search-sdui-comment-unit"]')
        card = units[0] if units else trk
        sub = str((ctx.get("subreddit") or {}).get("name") or "")

        # The comment's own text lives in its rtjson container; the author line, the time and the
        # vote count are siblings, not children, so nothing has to be stripped off.
        bodies = card.css(f'[id="search-comment-{cid}-post-rtjson-content"]')
        if not bodies:
            content = card.css('[data-testid="search-comment-content"]')
            bodies = content[0].css('[id$="-post-rtjson-content"]') if content else []
        text = _comment_text(bodies[0]) if bodies else ""

        permalink = thread_link = ""
        short = cid.removeprefix("t1_")
        for a in card.css('a[href*="/comments/"]'):
            href = a.attrib.get("href") or ""
            if href.rstrip("/").endswith("/" + short):
                permalink = permalink or href
            else:
                thread_link = thread_link or href
        if not thread_link and sub:
            thread_link = f"/r/{sub}/comments/{pid.removeprefix('t3_')}/"

        content = card.css('[data-testid="search-comment-content"]')
        inner = content[0] if content else card
        ts = inner.css("faceplate-timeago")
        num = inner.css("faceplate-number")
        post_votes, post_comments = _counts(card)
        hits.append(
            CommentHit(
                comment_id=cid,
                post_id=pid,
                title=re.sub(r"\s+", " ", str(p.get("title") or "")).strip(),
                subreddit=sub,
                text=text,
                permalink=permalink,
                thread_link=thread_link,
                created_at=iso(ts[0].attrib.get("ts") if ts else ""),
                score=_int(num[0].attrib.get("number")) if num else 0,
                post_score=post_votes,
                post_comments=post_comments,
                nsfw=bool(p.get("nsfw")),
                position=_int((ctx.get("action_info") or {}).get("position")) or len(hits),
                term=term,
            )
        )
    m = NEXT_CURSOR_RE.search(html)
    next_url = BASE + htmllib.unescape(m.group(1)) if m else None
    return CommentSearchPage(hits=hits, next_url=next_url)


# --------------------------------------------------------------------------- corroboration

STOPWORDS = {
    "the", "a", "an", "and", "or", "of", "for", "to", "in", "on", "at", "by", "with", "from", "co", "inc",
    "llc", "ltd", "company", "store", "shop", "brand", "products", "product", "reviews", "review", "best",
    "your", "our", "my", "its", "this", "that", "home", "use", "daily", "friendly", "free", "premium",
}
_SENTENCE_END = ".!?:;\n\r\"'(“”‘’*-"


@dataclass
class Corroboration:
    """What, beyond the phrase itself, says a comment is about THIS brand.

    An exact-phrase search cannot tell "Safe Hero" the car-escape tool from "a very safe hero" in
    an Overwatch thread, or "The Hero Company" from Hero pens, and the node's gate cannot either:
    it reads text, and the phrase is in the text. Measured 23 Sep 2026 on the 14-brand Tavily
    baseline: without this, Safe Hero rescued 15 gaming comments, The Hero Company 15, Exodus
    Strong 12 (X-Men and Metro: Exodus), against 0 real mentions for all three. A one-word brand
    (Howdysnax, Eskiin) needs none of this. For a multi-word one, a thread is kept when any of:
      - the brand's first significant word is written capitalised mid-sentence ("Hercules
        supplements", "Lyons leather co", "the Longevity store"), the proper-noun signal;
      - a product word from Parse Keywords' `primary_product` / `product_keywords` appears
        ("water softener", "mushroom gummies", "crossbody bag");
      - the brand's domain appears.
    Otherwise it is the phrase as ordinary words and it is dropped. It costs a lowercase mention
    with no product word ("salt free kind water systems work well"): measured, 1 of 15."""

    brand_words: list[str]
    first_word: str  # as written in the term, e.g. "Hercules"; "" when the term is lowercase
    product_words: set[str]
    domain: str

    @classmethod
    def build(cls, brand: str, product_keywords: list[str] | None, primary_product: str | None, domain: str | None) -> "Corroboration | None":
        words = [w for w in re.findall(r"[A-Za-z0-9][A-Za-z0-9'-]*", brand or "") if w.lower() not in STOPWORDS]
        if len([w for w in re.findall(r"[A-Za-z0-9]+", brand or "")]) < 2:
            return None  # one word: distinctive on its own
        first = next((w for w in words if len(w) >= 3), "")
        if not first[:1].isupper():
            first = ""
        brand_stems = {w.rstrip("s") for w in re.findall(r"[a-z0-9]+", (brand or "").lower())}
        vocab: set[str] = set()
        for phrase in [primary_product or ""] + list(product_keywords or []):
            for w in re.findall(r"[a-z0-9]+", str(phrase).lower()):
                if len(w) >= 4 and w not in STOPWORDS and w.rstrip("s") not in brand_stems:
                    vocab.add(w)
        dom = (domain or "").strip().lower().removeprefix("www.")
        if not vocab and not dom:
            return None  # the caller sent no vocabulary: not opted in, every thread goes through
        return cls(brand_words=words, first_word=first, product_words=vocab, domain=dom)

    def holds(self, text: str) -> bool:
        low = text.lower()
        if self.domain and self.domain in low:
            return True
        for w in self.product_words:
            if re.search(r"\b" + re.escape(w) + r"(s|es)?\b", low):
                return True
        if self.first_word:
            for m in re.finditer(r"\b" + re.escape(self.first_word) + r"\b", text):
                before = text[: m.start()].rstrip()
                if before and before[-1] not in _SENTENCE_END:
                    return True  # capitalised, and not because it opens a sentence
        return False


# --------------------------------------------------------------------------- orchestration


def phrase_query(term: str, primary: str) -> str:
    """The comment-search query for a term: the brand name as an exact phrase.

    Unquoted, Reddit ranks comments that contain the words anywhere, and a three-word brand
    loses: measured 23 Sep 2026, "Kind Water Systems" returned 10 comments about Savannah River
    and the Houthis with 0 carrying the phrase, while the quoted form returned 10 of 10 with it.
    Parse Keywords builds term 1 as the brand name and terms 2 and 3 as that name plus a product
    or "reviews", so the brand part is quoted and the rest is left loose for ranking:
    `"Kind Water Systems" water filter`. A term that does not start with the brand is quoted whole."""
    term = term.strip().strip('"')
    primary = primary.strip().strip('"')
    if primary and term.lower() != primary.lower() and term.lower().startswith(primary.lower() + " "):
        return f'"{primary}" {term[len(primary):].strip()}'
    return f'"{term}"'


@dataclass
class _TermHits:
    hits: list[CommentHit]
    pages: int
    truncated: bool


def _mentions_term(
    term: str,
    primary: str,
    max_results: int,
    include_nsfw: bool,
    fetcher: Fetcher,
    deadline: float,
    seen_posts: set[str],
    lock: threading.Lock,
) -> _TermHits:
    """Comment hits for one term. Page 1 always; later pages only while the request as a whole is
    still short of `max_results` threads and a fetch plus the body fill fit before the deadline."""
    url: str | None = build_comment_search_url(phrase_query(term, primary))
    hits: list[CommentHit] = []
    seen: set[str] = set()
    pages = 0
    truncated = False
    while url and pages < settings.mentions_max_pages:
        if pages:
            with lock:
                enough = len(seen_posts) >= max_results
            if enough:
                break
            if time.time() + _fetch_cost_s(1, settings.mentions_fetch_timeout_ms) + BODY_RESERVE_S > deadline:
                truncated = True
                log.info("%r comment search: no time for page %d, keeping %d hit(s)", term, pages + 1, len(hits))
                break
        try:
            html = fetch_page(url, fetcher=fetcher, attempts=ATTEMPTS if pages == 0 else 1)
        except RedditError as e:
            if not pages:
                raise  # nothing collected: the term failed
            log.warning("%r comment page %d failed, keeping the %d hit(s) already collected: %s", term, pages + 1, len(hits), e)
            break
        pages += 1
        page = parse_comment_search(html, term)
        fresh = 0
        for h in page.hits:
            if h.comment_id in seen or (h.nsfw and not include_nsfw):
                continue
            seen.add(h.comment_id)
            hits.append(h)
            fresh += 1
            with lock:
                seen_posts.add(h.post_id)
        with lock:
            merged = len(seen_posts)
        log.info(
            "%r comment page %d: %d hit(s), %d new, %d thread(s) merged so far, next=%s",
            term, pages, len(page.hits), fresh, merged, "yes" if page.next_url else "no",
        )
        if fresh == 0:
            break
        url = page.next_url
    return _TermHits(hits=hits, pages=pages, truncated=truncated)


def fill_mention_bodies(threads: list[MentionThread], deadline: float) -> tuple[int, bool]:
    """Post bodies for every thread in ONE mobile call, bounded by the time left. Returns
    (bodies filled, timed out). Never raises: the comments are the evidence, the body is context."""
    ids = [t.post_id for t in threads]
    pool = ThreadPoolExecutor(max_workers=1)
    future = pool.submit(mobile.info, ids)
    try:
        raw = future.result(timeout=max(0.1, deadline - time.time()))
    except FutureTimeout:
        # The worker keeps its device leased until MOBILE_TIMEOUT_S; one device of three, once.
        pool.shutdown(wait=False)
        log.warning("mentions body fill did not answer before the deadline, returning %d thread(s) without bodies", len(threads))
        return 0, True
    except Exception as e:  # noqa: BLE001 - MobileError and friends; the route is an optimisation
        pool.shutdown(wait=False)
        log.info("mentions body fill skipped: %s", e)
        return 0, False
    pool.shutdown(wait=False)
    filled = 0
    for t in threads:
        r = raw.get(t.post_id) if isinstance(raw, dict) else None
        if not isinstance(r, dict):
            continue
        t.body = _selftext(r)
        t.body_filled = True
        if not t.title:
            t.title = _flat(r.get("title"))
        filled += 1
    return filled, False


def search_mentions(
    terms: list[str],
    max_results: int = 15,
    include_nsfw: bool = False,
    fetcher: Fetcher | None = None,
    budget_s: float | None = None,
    product_keywords: list[str] | None = None,
    primary_product: str | None = None,
    domain: str | None = None,
) -> MentionsResult:
    """Threads where a comment matches any of the terms, merged across terms (the first term to find
    a thread owns it; every term's matching comments are kept), newest evidence first inside each
    thread. Fails only when EVERY term failed; otherwise partial results win."""
    terms = [t for t in dict.fromkeys(re.sub(r"\s+", " ", str(t or "")).strip() for t in terms) if t][:MAX_TERMS]
    if not terms:
        raise ValueError("request must include at least one search term (`searchTerms` or `query`)")
    max_results = max(1, int(max_results))
    started = time.time()
    deadline = started + (settings.mentions_budget_s if budget_s is None else budget_s)
    if fetcher is None:
        fetcher = partial(fetch_html, timeout_ms=settings.mentions_fetch_timeout_ms, gate_deadline=deadline)

    seen_posts: set[str] = set()
    lock = threading.Lock()
    outcomes: dict[str, _TermHits | RedditError] = {}

    def run(term: str) -> None:
        try:
            outcomes[term] = _mentions_term(term, terms[0], max_results, include_nsfw, fetcher, deadline, seen_posts, lock)
        except RedditError as e:
            outcomes[term] = e

    with ThreadPoolExecutor(max_workers=len(terms)) as pool:
        list(pool.map(run, terms))

    threads: dict[str, MentionThread] = {}
    seen_comments: set[str] = set()
    failed: dict[str, str] = {}
    term_counts: dict[str, tuple[int, int]] = {}
    pages = 0
    truncated = False
    for term in terms:
        out = outcomes[term]
        if isinstance(out, RedditError):
            failed[term] = str(out)
            term_counts[term] = (0, 0)
            continue
        pages += out.pages
        truncated = truncated or out.truncated
        first = 0
        for h in out.hits:
            t = threads.get(h.post_id)
            if t is None:
                try:
                    url = thread_url(h.thread_link)
                except ValueError:
                    url = f"{BASE}/comments/{h.post_id.removeprefix('t3_')}/"
                t = MentionThread(
                    post_id=h.post_id, title=h.title, url=url, subreddit=h.subreddit,
                    post_score=h.post_score, post_comments=h.post_comments, first_term=term,
                )
                threads[h.post_id] = t
                first += 1
            if h.comment_id not in seen_comments:
                seen_comments.add(h.comment_id)
                t.comments.append(h)
        term_counts[term] = (len(out.hits), first)
    log.info(
        "comment search: %s -> %d thread(s), %d matching comment(s)",
        "; ".join(f"{t!r} hits={h} threads={n}" + (" FAILED" if t in failed else "") for t, (h, n) in term_counts.items()),
        len(threads), len(seen_comments),
    )
    if len(failed) == len(terms):
        err = outcomes[terms[0]]
        assert isinstance(err, RedditError)
        raise err if isinstance(err, (ScrapeBlocked, ScrapeFailed)) else ScrapeFailed(str(err))

    corroboration = Corroboration.build(terms[0], product_keywords, primary_product, domain)
    generic_dropped = 0
    candidates = list(threads.values())
    if corroboration is not None:
        kept: list[MentionThread] = []
        for t in candidates:
            evidence = " ".join([t.title, *(c.text for c in t.comments)])
            if corroboration.holds(evidence):
                kept.append(t)
            else:
                generic_dropped += 1
        if generic_dropped:
            log.info("%r: %d thread(s) dropped as the phrase used as ordinary words, %d kept", terms[0], generic_dropped, len(kept))
        candidates = kept
    ordered = candidates[:max_results]
    bodies = 0
    if ordered and mobile.available():
        if deadline - time.time() > BODY_RESERVE_S:
            bodies, timed_out = fill_mention_bodies(ordered, deadline)
            truncated = truncated or timed_out
        else:
            truncated = True
            log.info("mentions body fill skipped, %.1fs left of the budget", deadline - time.time())
    for t in ordered:
        # Best evidence first: highest score, then newest. The node keeps the first 900 characters.
        t.comments.sort(key=lambda c: c.created_at, reverse=True)
        t.comments.sort(key=lambda c: -c.score)
        stamps = [c.created_at for c in t.comments if c.created_at]
        t.created_at = min(stamps) if stamps else ""
        t.score = max((c.score for c in t.comments), default=0)
    log.info(
        "mentions terms=%d -> %d thread(s), %d comment(s), bodies=%d, pages=%d, failed=%d, truncated=%s, %.1fs",
        len(terms), len(ordered), sum(len(t.comments) for t in ordered), bodies, pages, len(failed), truncated,
        time.time() - started,
    )
    return MentionsResult(
        threads=ordered,
        pages_fetched=pages,
        seconds=round(time.time() - started, 1),
        failed_terms=failed,
        truncated=truncated,
        bodies_filled=bodies,
        generic_dropped=generic_dropped,
        term_counts=term_counts,
    )
