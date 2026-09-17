import time

import pytest
from conftest import FakeReddit, fixture, set_frozen

from reddit_reviews import scraper
from reddit_reviews.config import settings
from reddit_reviews.scraper import (
    ScrapeBlocked,
    ScrapeFailed,
    build_search_url,
    iso,
    is_blocked_page,
    parse_search,
    parse_thread,
    scrape_search,
    scrape_threads,
    thread_url,
)

TEXT_THREAD = "https://www.reddit.com/r/Gymshark/comments/1st816z/"
IMAGE_THREAD = "https://www.reddit.com/r/gymsnark/comments/1pjz4rg/"


# --------------------------------------------------------------------------- helpers


def test_iso_normalises_reddit_timestamps_for_javascript():
    assert iso("2026-04-23T04:06:19.598000+0000") == "2026-04-23T04:06:19.598Z"
    assert iso("2026-04-23T04:06:19Z") == "2026-04-23T04:06:19.000Z"
    assert iso("") == ""
    assert iso("garbage") == "garbage"


def test_search_url_is_literal_and_sanitised():
    url = build_search_url("Hairbrella reviews", sort="NOPE", time_filter="year")
    assert url.startswith("https://www.reddit.com/svc/shreddit/search/?")
    assert "q=Hairbrella+reviews" in url and "type=posts" in url
    assert "sort=relevance" in url and "t=year" in url
    assert "disableSpellCorrection=true" in url


@pytest.mark.parametrize(
    "raw",
    [
        "https://www.reddit.com/r/Gymshark/comments/1st816z/best_gymshark_tops_review/",
        "https://old.reddit.com/r/Gymshark/comments/1st816z/",
        "/r/Gymshark/comments/1ST816Z/best/comment/ohwwsg0/",
    ],
)
def test_thread_url_canonicalises(raw):
    assert thread_url(raw) == TEXT_THREAD


def test_thread_url_rejects_non_threads():
    with pytest.raises(ValueError):
        thread_url("https://www.reddit.com/r/Gymshark/")


def test_block_page_detected():
    assert is_blocked_page(fixture("blocked.html"))
    assert not is_blocked_page(fixture("search_gymshark_reviews.html"))
    assert not is_blocked_page(fixture("thread_text_post.html"))


# --------------------------------------------------------------------------- parsing


def test_parse_search_page():
    page = parse_search(fixture("search_gymshark_reviews.html"), "Gymshark reviews")
    assert len(page.posts) == 7
    assert len({p.id for p in page.posts}) == 7
    assert page.next_url and page.next_url.startswith("https://www.reddit.com/svc/shreddit/search/?")
    assert "&amp;" not in page.next_url and "cursor=" in page.next_url

    downfall = next(p for p in page.posts if p.id == "t3_1vf53rw")
    assert downfall.title == "Gymshark downfall"
    assert downfall.subreddit == "Gymshark"
    assert (downfall.score, downfall.num_comments) == (19, 32)
    assert downfall.created_at == "2026-08-04T09:09:44.076Z"
    assert downfall.permalink == "/r/Gymshark/comments/1vf53rw/gymshark_downfall/"
    assert downfall.body and downfall.body_is_snippet
    assert all(p.search_term == "Gymshark reviews" and p.created_at and p.permalink for p in page.posts)


def test_search_snippet_from_a_comment_is_never_used_as_the_post_body():
    page = parse_search(fixture("search_gymshark_reviews.html"))
    reps = next(p for p in page.posts if p.id == "t3_1strbjn")
    # Reddit matched a comment in this thread (snippet_id t1_...), so the post has no body of its own here.
    assert reps.body == ""


def test_parse_search_follow_page():
    page = parse_search(fixture("search_gymshark_reviews_page2.html"))
    assert len(page.posts) == 7
    first = {p.id for p in parse_search(fixture("search_gymshark_reviews.html")).posts}
    assert not first & {p.id for p in page.posts}


def test_parse_thread_text_post():
    t = parse_thread(fixture("thread_text_post.html"), TEXT_THREAD)
    p = t.post
    assert (p.id, p.subreddit, p.title) == ("t3_1st816z", "Gymshark", "Best Gymshark Tops review")
    assert (p.score, p.num_comments) == (17, 21)
    assert p.created_at == "2026-04-23T04:06:19.598Z"
    assert p.body.startswith("Onyx V5 Hoodie: Looks really good")
    assert not p.body_is_snippet
    assert len(t.comments) == 13
    c = t.comments[0]
    assert (c.id, c.depth, c.parent_id, c.author) == ("t1_ohwwsg0", 0, "t3_1st816z", "Randomlogicuser")
    assert c.body == "Gonna bulk soon?"
    reply = t.comments[1]
    assert reply.depth == 1 and reply.parent_id == "t1_ohwwsg0"
    assert all(x.body and x.created_at and x.subreddit == "Gymshark" for x in t.comments)


def test_parse_thread_image_post_has_no_body_but_keeps_comments():
    t = parse_thread(fixture("thread_image_post.html"), IMAGE_THREAD)
    assert t.post.body == ""
    assert t.post.num_comments == 36
    assert len(t.comments) == 25


def test_parse_thread_without_post_is_missing():
    with pytest.raises(scraper.ThreadMissing):
        parse_thread("<html><body>nothing here</body></html>")


# --------------------------------------------------------------------------- orchestration


def search_site(extra: dict | None = None) -> FakeReddit:
    page1 = fixture("search_gymshark_reviews.html")
    next_url = parse_search(page1).next_url
    pages = {
        build_search_url("Gymshark reviews"): (200, page1),
        next_url: (200, fixture("search_gymshark_reviews_page2.html")),
        build_search_url("Hairbrella"): (200, fixture("search_hairbrella.html")),
        # every body fetch lands on the text thread; good enough to prove bodies are replaced
        "https://www.reddit.com/r/*": (200, fixture("thread_text_post.html")),
    }
    pages.update(extra or {})
    return FakeReddit(pages)


def test_search_follows_cursor_until_max_and_skips_bodies_when_off():
    site = search_site()
    res = scrape_search(["Gymshark reviews"], max_posts=10, fetcher=site, full_bodies=False)
    assert len(res.posts) == 10
    assert res.pages_fetched == 2
    assert all(p.body_is_snippet for p in res.posts)
    assert len(site.calls) == 2


def test_search_stops_at_max_search_pages():
    site = search_site()
    before = settings.max_search_pages
    set_frozen(settings, "max_search_pages", 1)
    try:
        res = scrape_search(["Gymshark reviews"], max_posts=50, fetcher=site, full_bodies=False)
    finally:
        set_frozen(settings, "max_search_pages", before)
    assert len(res.posts) == 7 and res.pages_fetched == 1


def test_search_dedupes_across_terms_and_keeps_term_order():
    site = search_site()
    res = scrape_search(["Hairbrella", "Gymshark reviews", "Hairbrella"], max_posts=7, fetcher=site, full_bodies=False)
    assert len(res.posts) == 14
    assert [p.search_term for p in res.posts[:7]] == ["Hairbrella"] * 7
    assert len({p.id for p in res.posts}) == 14


def test_search_fills_full_bodies_from_thread_pages():
    site = search_site()
    res = scrape_search(["Hairbrella"], max_posts=7, fetcher=site, full_bodies=True)
    assert all(not p.body_is_snippet for p in res.posts)
    assert all(p.body.startswith("Onyx V5 Hoodie") for p in res.posts)
    assert res.pages_fetched == 1 + 7


def test_body_fetches_stop_at_the_deadline():
    site = search_site()
    posts = parse_search(fixture("search_gymshark_reviews.html")).posts
    assert scraper.fill_bodies(posts, site, deadline=0) == (0, True)
    assert all(p.body_is_snippet for p in posts)
    assert site.calls == []


def test_body_fetches_reserve_room_rather_than_racing_the_deadline():
    """A deadline still in the future but too close to fit a whole fetch must stop us.

    The old check was `time.time() > deadline`, which let a body fetch start at t=199 and run its
    full cost past the caller's own timeout. Reserving the cost up front is what bounds the call.
    """
    site = search_site()
    posts = parse_search(fixture("search_gymshark_reviews.html")).posts
    # Comfortably in the future, but less than one fetch's worst case away.
    deadline = time.time() + scraper._fetch_cost_s(1) - 1
    assert scraper.fill_bodies(posts, site, deadline=deadline) == (0, True)
    assert site.calls == []


def test_a_healthy_body_fill_is_not_flagged_as_truncated():
    site = search_site()
    posts = parse_search(fixture("search_gymshark_reviews.html")).posts
    fetched, truncated = scraper.fill_bodies(posts, site, deadline=time.time() + 10_000)
    assert fetched == len(posts)
    assert truncated is False


def test_body_fetch_failure_keeps_snippet():
    site = search_site({"https://www.reddit.com/r/*": (403, fixture("blocked.html"))})
    res = scrape_search(["Gymshark reviews"], max_posts=7, fetcher=site, full_bodies=True)
    assert len(res.posts) == 7
    downfall = next(p for p in res.posts if p.id == "t3_1vf53rw")
    assert downfall.body_is_snippet and downfall.body


def test_search_partial_failure_returns_what_worked():
    site = search_site({build_search_url("Hairbrella"): (403, fixture("blocked.html"))})
    res = scrape_search(["Hairbrella", "Gymshark reviews"], max_posts=7, fetcher=site, full_bodies=False)
    assert len(res.posts) == 7
    assert list(res.failed_terms) == ["Hairbrella"]


def test_search_all_blocked_raises_vendor_error():
    site = FakeReddit(default=(403, fixture("blocked.html")))
    with pytest.raises(ScrapeBlocked) as e:
        scrape_search(["Gymshark", "Gymshark reviews"], fetcher=site)
    # retried once per term
    assert len(site.calls) == 4
    # must not look like a brand-side error to Stage 4's BRAND_ERROR_RE
    import re

    assert not re.search(r"\b404\b|not found|no such (company|business|page)|invalid (url|domain)", str(e.value), re.I)


def test_blocked_page_served_with_200_still_counts_as_blocked():
    site = FakeReddit(default=(200, fixture("blocked.html")))
    with pytest.raises(ScrapeBlocked):
        scrape_search(["Gymshark"], fetcher=site)


def test_browser_crash_is_retried_then_raised():
    calls = []

    def crashing(url, wait_selector=None):
        calls.append(url)
        raise ScrapeFailed("browser fetch failed: TimeoutError")

    with pytest.raises(ScrapeFailed):
        scrape_search(["Gymshark"], fetcher=crashing)
    assert len(calls) == 2


def test_search_requires_a_term():
    with pytest.raises(ValueError):
        scrape_search(["  ", ""], fetcher=FakeReddit())


def test_threads_caps_comments_per_post_and_total():
    site = FakeReddit({TEXT_THREAD: (200, fixture("thread_text_post.html")), IMAGE_THREAD: (200, fixture("thread_image_post.html"))})
    res = scrape_threads([TEXT_THREAD, IMAGE_THREAD + "slug/"], max_comments_per_post=10, max_comments_total=15, fetcher=site)
    assert [len(t.comments) for t in res.threads] == [10, 5]
    assert [t.post.id for t in res.threads] == ["t3_1st816z", "t3_1pjz4rg"]


def test_threads_skip_missing_and_bad_links():
    site = FakeReddit({TEXT_THREAD: (200, fixture("thread_text_post.html"))})
    res = scrape_threads([TEXT_THREAD, "https://www.reddit.com/r/x/comments/gone1/", "https://example.com/nope"], fetcher=site)
    assert len(res.threads) == 1
    assert res.missing == ["https://www.reddit.com/r/x/comments/gone1/"]
    assert not res.failed


def test_threads_all_blocked_raises():
    site = FakeReddit(default=(403, fixture("blocked.html")))
    with pytest.raises(ScrapeBlocked):
        scrape_threads([TEXT_THREAD, IMAGE_THREAD], fetcher=site)


def test_threads_partial_block_keeps_the_rest():
    site = FakeReddit({TEXT_THREAD: (200, fixture("thread_text_post.html")), IMAGE_THREAD: (403, fixture("blocked.html"))})
    res = scrape_threads([TEXT_THREAD, IMAGE_THREAD], fetcher=site)
    assert len(res.threads) == 1 and list(res.failed) == [IMAGE_THREAD]


def test_threads_require_a_thread_link():
    with pytest.raises(ValueError):
        scrape_threads(["https://example.com"], fetcher=FakeReddit())


# --------------------------------------------------------------------------- time budget


def test_search_truncates_when_another_page_will_not_fit(budget):
    budget(1)  # far below the ~92s one page is allowed to cost
    site = search_site()

    # max_posts=10 needs a second page: page 1 carries 7. The budget is what stops it, not the cap.
    res = scrape_search(["Gymshark reviews"], max_posts=10, fetcher=site, full_bodies=False)

    # Page 2 is never even attempted, and what page 1 found comes back as a normal result.
    assert len(site.calls) == 1
    assert res.pages_fetched == 1
    assert res.posts
    assert res.truncated is True
    assert res.failed_terms == {}, "running out of time is not a term failing"


def test_search_budget_never_skips_the_first_page(budget):
    budget(0)  # no budget at all
    site = search_site()

    # Returning zero posts would read to Stage 4 as "Reddit has nothing for this brand" and fire
    # the Tavily fallback. Page 1 is always attempted, however late we are.
    res = scrape_search(["Gymshark reviews"], max_posts=10, fetcher=site, full_bodies=False)
    assert len(site.calls) == 1
    assert len(res.posts) == 7
    assert res.truncated is True


def test_search_is_not_truncated_on_a_normal_run():
    site = search_site()
    res = scrape_search(["Gymshark reviews"], max_posts=10, fetcher=site, full_bodies=False)
    assert res.pages_fetched == 2
    assert res.truncated is False, "a healthy search must never be flagged as truncated"


def test_threads_truncate_without_looking_like_a_vendor_failure(budget):
    budget(0)
    site = FakeReddit({TEXT_THREAD: (200, fixture("thread_text_post.html")), IMAGE_THREAD: (200, fixture("thread_image_post.html"))})

    res = scrape_threads([TEXT_THREAD, IMAGE_THREAD], fetcher=site)

    assert site.calls == [], "nothing should be fetched once the budget is gone"
    assert res.truncated is True
    # The distinction that matters downstream: a clock decision is not Reddit refusing us, so it
    # must not land in `failed` and must not raise ScrapeBlocked.
    assert res.failed == {}
    assert res.missing == []
    assert res.pages_fetched == 0


def test_threads_are_not_truncated_on_a_normal_run():
    site = FakeReddit({TEXT_THREAD: (200, fixture("thread_text_post.html")), IMAGE_THREAD: (200, fixture("thread_image_post.html"))})
    res = scrape_threads([TEXT_THREAD, IMAGE_THREAD], fetcher=site)
    assert len(res.threads) == 2
    assert res.truncated is False


# --------------------------------------------------------------------------- proxy


@pytest.fixture
def proxy_value():
    """Set SCRAPER_PROXY on the frozen settings singleton, restoring it afterwards."""
    before = settings.proxy
    yield lambda v: set_frozen(settings, "proxy", v)
    set_frozen(settings, "proxy", before)


def test_proxy_config_keeps_a_password_containing_colons(proxy_value):
    # Decodo passwords routinely contain ':'. An uncapped split would truncate the password at the
    # first one and authenticate with a prefix, which reads downstream as "Reddit blocked us".
    proxy_value("us.decodo.com:10001:user:pa:ss:word")
    assert scraper.proxy_config() == {
        "server": "http://us.decodo.com:10001",
        "username": "user",
        "password": "pa:ss:word",
    }


def test_proxy_config_port_override_picks_the_exit(proxy_value):
    proxy_value("us.decodo.com:10001:user:pw")
    assert scraper.proxy_config(10042)["server"] == "http://us.decodo.com:10042"


def test_proxy_config_refuses_the_rotating_gateway(proxy_value):
    # Port 7000 hands out a new exit IP per request, which would move us mid-scrape.
    proxy_value("us.decodo.com:7000:user:pw")
    with pytest.raises(ValueError, match="rotating gateway"):
        scraper.proxy_config()


def test_proxy_config_passes_a_plain_url_through(proxy_value):
    # Someone using a non-Decodo proxy should keep working; rotation simply does not apply.
    proxy_value("http://user:pw@some.proxy:8080")
    assert scraper.proxy_config(10042) == "http://user:pw@some.proxy:8080"


def test_proxy_config_is_none_when_unset(proxy_value):
    proxy_value(None)
    assert scraper.proxy_config() is None


def test_proxy_config_rejects_a_malformed_credential(proxy_value):
    # Better to stop than to quietly scrape from the blocked server IP.
    proxy_value("us.decodo.com:10001:user")
    with pytest.raises(ValueError):
        scraper.proxy_config()


def test_port_pool_never_hands_the_same_port_to_two_holders():
    pool = scraper._PortPool(10001, 10004)  # 3 ports
    with pool.lease() as a, pool.lease() as b, pool.lease() as c:
        assert len({a, b, c}) == 3, "two concurrent browsers on one exit IP is a fingerprint"
        assert all(10001 <= p < 10004 for p in (a, b, c))
        with pool.lease() as d:
            assert d is None, "exhaustion yields None rather than a duplicate"
    # everything returned
    with pool.lease() as e:
        assert e is not None


def test_port_pool_releases_on_exception():
    pool = scraper._PortPool(10001, 10002)  # exactly one port
    with pytest.raises(RuntimeError):
        with pool.lease() as first:
            assert first == 10001
            raise RuntimeError("boom")
    with pool.lease() as again:
        assert again == 10001, "a crashed fetch must not leak its exit port"


def test_sticky_range_is_wider_than_the_browser_gate():
    lo, hi = scraper.STICKY_PORT_RANGE
    assert hi - lo > settings.max_concurrency, "the pool must never exhaust under normal concurrency"


# --------------------------------------------------------------------------- browser contract


def test_fetch_html_pins_the_scrapling_kwargs(monkeypatch):
    import scrapling.fetchers as fetchers

    seen = {}

    class FakePage:
        status = 200
        html_content = "<html></html>"

    class FakeFetcher:
        @classmethod
        def fetch(cls, url, **kwargs):
            seen.update(kwargs)
            return FakePage()

    monkeypatch.setattr(fetchers, "StealthyFetcher", FakeFetcher)
    status, _ = scraper.fetch_html("https://www.reddit.com/r/Gymshark/comments/1st816z/")

    assert status == 200
    # Scrapling's own default is 3, and it multiplies with every retry layer above it.
    assert seen["retries"] == 1
    # Camoufox-era names that scrapling 0.4.x swallows silently - they must not drift back in.
    assert not {"humanize", "os_randomize", "geoip"} & set(seen)
    assert seen["block_webrtc"] is True, "this one is real, unlike the three above"
    assert seen["disable_resources"] is settings.block_resources
    # DNS inside the tunnel, or the container's resolver leaks the egress a proxy exists to hide.
    assert seen["dns_over_https"] is True


def test_fetch_html_sends_the_proxy_as_a_dict_on_a_sticky_port(monkeypatch, proxy_value):
    """Scrapling accepts a URL too, but parses it with urlparse, which does no percent-decoding -
    a password containing '%', '@' or ':' would authenticate wrong. The dict does no string surgery."""
    import scrapling.fetchers as fetchers

    seen = {}

    class FakePage:
        status = 200
        html_content = "<html></html>"

    class FakeFetcher:
        @classmethod
        def fetch(cls, url, **kwargs):
            seen.update(kwargs)
            return FakePage()

    monkeypatch.setattr(fetchers, "StealthyFetcher", FakeFetcher)
    proxy_value("us.decodo.com:10001:user:pw")
    scraper.fetch_html("https://www.reddit.com/x")

    proxy = seen["proxy"]
    assert isinstance(proxy, dict), "a URL string would be re-parsed lossily by scrapling"
    assert set(proxy) == {"server", "username", "password"}
    assert proxy["username"] == "user" and proxy["password"] == "pw"
    lo, hi = scraper.STICKY_PORT_RANGE
    port = int(proxy["server"].rsplit(":", 1)[1])
    assert lo <= port < hi, "the fetch must leave from a drawn sticky port, not the configured one"


def test_fetch_html_omits_proxy_entirely_when_unset(monkeypatch, proxy_value):
    import scrapling.fetchers as fetchers

    seen = {}

    class FakePage:
        status = 200
        html_content = "<html></html>"

    class FakeFetcher:
        @classmethod
        def fetch(cls, url, **kwargs):
            seen.update(kwargs)
            return FakePage()

    monkeypatch.setattr(fetchers, "StealthyFetcher", FakeFetcher)
    proxy_value(None)
    scraper.fetch_html("https://www.reddit.com/x")
    assert "proxy" not in seen, "passing proxy=None differs from omitting it only by noise"


def test_a_failed_fetch_names_the_port_but_never_the_credential(monkeypatch, proxy_value):
    import scrapling.fetchers as fetchers

    class FakeFetcher:
        @classmethod
        def fetch(cls, url, **kwargs):
            raise RuntimeError("Target closed")

    monkeypatch.setattr(fetchers, "StealthyFetcher", FakeFetcher)
    proxy_value("us.decodo.com:10001:user:sup3rs3cret")

    with pytest.raises(ScrapeFailed) as ei:
        scraper.fetch_html("https://www.reddit.com/x")
    msg = str(ei.value)
    assert "exit port" in msg, "the port is what makes a bad exit diagnosable"
    assert "sup3rs3cret" not in msg and "user" not in msg, "credentials must never reach a log or an API error"


def test_fetch_html_passes_the_wait_selector_only_when_asked(monkeypatch):
    import scrapling.fetchers as fetchers

    seen = {}

    class FakePage:
        status = 200
        html_content = "<html></html>"

    class FakeFetcher:
        @classmethod
        def fetch(cls, url, **kwargs):
            seen.clear()
            seen.update(kwargs)
            return FakePage()

    monkeypatch.setattr(fetchers, "StealthyFetcher", FakeFetcher)

    scraper.fetch_html("https://www.reddit.com/x")
    assert "wait_selector" not in seen

    scraper.fetch_html("https://www.reddit.com/x", "shreddit-post")
    assert seen["wait_selector"] == "shreddit-post"
    assert seen["wait_selector_state"] == "attached"


@pytest.mark.live
def test_live_search_and_thread():
    res = scrape_search(["Gymshark reviews"], max_posts=3, full_bodies=False)
    assert res.posts, res
    t = scrape_threads([res.posts[0].permalink], max_comments_per_post=5)
    assert t.threads and t.threads[0].post.id == res.posts[0].id
