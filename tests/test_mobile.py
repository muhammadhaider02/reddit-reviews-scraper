"""The mobile route, and the hybrid seam where it meets the browser.

All offline. Reddit's JSON shapes are pinned here as literals rather than fetched, so a change on
their side shows up as a failing contract test instead of as silently emptier reports.
"""

import gzip
import io
import json
import urllib.error
import urllib.request

import pytest
from conftest import FakeReddit, fixture, set_frozen
from test_scraper import search_site

from reddit_reviews import mobile, scraper
from reddit_reviews.config import settings
from reddit_reviews.scraper import (
    ThreadMissing,
    comment_from_raw,
    fill_bodies_mobile,
    post_from_raw,
    post_id_of,
    scrape_search,
    scrape_threads,
)

TEXT_THREAD = "https://www.reddit.com/r/Gymshark/comments/1st816z/"
IMAGE_THREAD = "https://www.reddit.com/r/gymsnark/comments/1pjz4rg/"

RAW_POST = {
    "name": "t3_1st816z",
    "id": "1st816z",
    "title": "Best  Gymshark\nTops review",
    "selftext": "I bought   three\n\nand two shrank.",
    "subreddit": "Gymshark",
    "author": "someone",
    "created_utc": 1789689600.0,  # 2026-09-18T00:00:00Z
    "score": 17,
    "num_comments": 21,
    "permalink": "/r/Gymshark/comments/1st816z/best_gymshark_tops_review/",
    "over_18": False,
}

RAW_COMMENT = {
    "name": "t1_abc123",
    "id": "abc123",
    "link_id": "t3_1st816z",
    "parent_id": "t3_1st816z",
    "body": "Ran  small\nfor me too",
    "subreddit": "Gymshark",
    "author": "commenter",
    "created_utc": 1789693200.0,  # an hour later
    "score": 9,
    "depth": 0,
    "permalink": "/r/Gymshark/comments/1st816z/_/abc123/",
}


@pytest.fixture(autouse=True)
def reset_counters():
    before = dict(mobile.counters)
    yield
    mobile.counters.update(before)


# --------------------------------------------------------------------------- the bridge


def test_post_id_of_reads_every_link_shape_we_are_sent():
    for link, want in [
        (TEXT_THREAD, "1st816z"),
        ("https://old.reddit.com/r/Gymshark/comments/1ST816Z/some_slug/", "1st816z"),
        ("/r/Gymshark/comments/1st816z/", "1st816z"),
        ("https://www.reddit.com/r/Gymshark/comments/1st816z/slug/abc999/", "1st816z"),
    ]:
        assert post_id_of(link) == want


def test_post_id_of_refuses_anything_that_is_not_a_thread():
    with pytest.raises(ValueError):
        post_id_of("https://www.reddit.com/r/Gymshark/")


# --------------------------------------------------------------------------- converters
#
# The two routes must be indistinguishable downstream, so these assert shape, not just presence.


def test_a_mobile_post_looks_exactly_like_a_browser_post():
    browser = scraper.parse_thread(fixture("thread_text_post.html"), TEXT_THREAD).post
    p = post_from_raw(RAW_POST, search_term="gymshark reviews")

    assert p.id.startswith("t3_"), "mapping.py strips a t3_ prefix, so it has to be there"
    assert p.id.startswith("t3_") == browser.id.startswith("t3_")
    # Whitespace is collapsed the way _text() collapses it for HTML; a body must not change shape
    # depending on which route fetched it.
    assert p.title == "Best Gymshark Tops review"
    assert p.body == "I bought three and two shrank."
    assert p.created_at.endswith("Z") and p.created_at == "2026-09-18T00:00:00.000Z"
    assert len(p.created_at) == len(browser.created_at)
    assert p.body_is_snippet is False
    assert p.search_term == "gymshark reviews"
    assert p.nsfw is False


def test_a_mobile_comment_looks_exactly_like_a_browser_comment():
    c = comment_from_raw(RAW_COMMENT)
    assert c is not None
    assert c.id == "t1_abc123" and c.post_id == "t3_1st816z"
    assert c.body == "Ran small for me too"
    assert c.created_at == "2026-09-18T01:00:00.000Z"
    assert c.subreddit == "Gymshark" and c.score == 9 and c.depth == 0


def test_a_comment_with_nothing_to_quote_is_dropped_like_the_browser_drops_it():
    for body in ("[deleted]", "[removed]", "", "   "):
        assert comment_from_raw({**RAW_COMMENT, "body": body}) is None


def test_a_comment_inherits_the_subreddit_when_reddit_omits_it():
    c = comment_from_raw({**RAW_COMMENT, "subreddit": ""}, "Gymshark")
    assert c is not None and c.subreddit == "Gymshark"


def test_an_unparseable_timestamp_becomes_empty_not_an_exception():
    assert post_from_raw({**RAW_POST, "created_utc": None}).created_at == ""
    assert post_from_raw({**RAW_POST, "created_utc": "nonsense"}).created_at == ""


# --------------------------------------------------------------------------- body fill


def _snippet_post(pid="t3_1st816z"):
    return scraper.Post(
        id=pid, title="t", body="a snippet", subreddit="Gymshark", author="a",
        created_at="", score=1, num_comments=1, permalink=TEXT_THREAD, body_is_snippet=True,
    )


def test_one_call_fills_every_body_and_prefers_reddits_live_counts(monkeypatch):
    posts = [_snippet_post()]
    calls = []

    def fake_info(ids):
        calls.append(list(ids))
        return {"t3_1st816z": RAW_POST}

    monkeypatch.setattr(mobile, "info", fake_info)
    assert fill_bodies_mobile(posts) == 1
    assert calls == [["t3_1st816z"]], "all bodies belong in one call; that is the whole saving"
    assert posts[0].body == "I bought three and two shrank."
    assert posts[0].body_is_snippet is False
    assert posts[0].score == 17 and posts[0].num_comments == 21


def test_a_link_post_gets_an_empty_body_because_that_is_what_the_browser_gives_it(monkeypatch):
    posts = [_snippet_post()]
    monkeypatch.setattr(mobile, "info", lambda ids: {"t3_1st816z": {**RAW_POST, "selftext": ""}})
    assert fill_bodies_mobile(posts) == 1
    assert posts[0].body == ""
    assert posts[0].body_is_snippet is False


def test_an_unavailable_route_returns_none_so_the_browser_does_the_whole_job(monkeypatch):
    posts = [_snippet_post()]

    def boom(ids):
        raise mobile.MobileError("nope")

    monkeypatch.setattr(mobile, "info", boom)
    assert fill_bodies_mobile(posts) is None, "None and 0 mean different things to the caller"
    assert posts[0].body_is_snippet is True


def test_zero_answers_is_not_the_same_as_an_unavailable_route(monkeypatch):
    monkeypatch.setattr(mobile, "info", lambda ids: {})
    assert fill_bodies_mobile([_snippet_post()]) == 0


# --------------------------------------------------------------------------- search, end to end


def test_search_fills_bodies_without_fetching_a_single_thread_page(mobile_on, monkeypatch):
    """The point of the hybrid: ~23 proxied page fetches become one call that costs nothing."""
    # search_site() serves the browser BOTH search pages and would serve thread pages too - so a
    # body fetch here would succeed silently. That is the point: the assertion below is that none
    # is even attempted, not that one failed.
    reddit = search_site()
    seen = {}

    def fake_info(ids):
        seen["ids"] = list(ids)
        return {i: {**RAW_POST, "name": i, "selftext": "the full body"} for i in ids}

    monkeypatch.setattr(mobile, "info", fake_info)
    res = scrape_search(["Gymshark reviews"], max_posts=10, fetcher=reddit, full_bodies=True)

    assert res.posts
    assert res.mobile_posts == len(seen["ids"]) > 0
    assert all("/comments/" not in u for u in reddit.calls), reddit.calls
    assert all(not p.body_is_snippet for p in res.posts[: settings.max_body_fetches])
    assert res.pages_fetched == len(reddit.calls)


def test_search_falls_back_to_browser_bodies_when_the_route_is_down(mobile_on, monkeypatch):
    reddit = search_site()

    def boom(ids):
        raise mobile.MobileError("down")

    monkeypatch.setattr(mobile, "info", boom)
    res = scrape_search(["Gymshark reviews"], max_posts=10, fetcher=reddit, full_bodies=True)

    assert res.mobile_posts == 0
    assert any("/comments/" in u for u in reddit.calls), "an outage must cost bandwidth, not results"


def test_search_leaves_the_route_alone_when_it_is_switched_off(monkeypatch):
    reddit = search_site()

    def never(ids):
        raise AssertionError("MOBILE_ENABLED=false must not call the route")

    monkeypatch.setattr(mobile, "info", never)
    res = scrape_search(["Gymshark reviews"], max_posts=10, fetcher=reddit, full_bodies=True)
    assert res.mobile_posts == 0
    assert any("/comments/" in u for u in reddit.calls), "the browser should have done the bodies"


# --------------------------------------------------------------------------- threads, end to end


def test_threads_are_served_entirely_without_the_browser(mobile_on, monkeypatch):
    reddit = FakeReddit({})
    monkeypatch.setattr(mobile, "comments", lambda pid, limit=20, **kw: (RAW_POST, [RAW_COMMENT]))

    res = scrape_threads([TEXT_THREAD], max_comments_per_post=5, fetcher=reddit)

    assert reddit.calls == [], "not one proxied page"
    assert res.mobile_threads == 1 and res.pages_fetched == 0
    assert len(res.threads) == 1
    assert res.threads[0].post.id == "t3_1st816z"
    assert [c.body for c in res.threads[0].comments] == ["Ran small for me too"]


def test_a_thread_the_route_cannot_read_falls_back_to_the_browser(mobile_on, monkeypatch):
    reddit = FakeReddit({TEXT_THREAD: (200, fixture("thread_text_post.html"))})

    def only_fails(pid, limit=20, **kw):
        raise mobile.MobileError("odd shape")

    monkeypatch.setattr(mobile, "comments", only_fails)
    res = scrape_threads([TEXT_THREAD], max_comments_per_post=5, fetcher=reddit)

    assert res.mobile_threads == 0
    assert reddit.calls == [TEXT_THREAD]
    assert len(res.threads) == 1


def test_a_hard_block_stops_the_route_and_hands_the_rest_to_the_browser(mobile_on, monkeypatch):
    reddit = FakeReddit(default=(200, fixture("thread_text_post.html")))
    seen = []

    def blocked(pid, limit=20, **kw):
        seen.append(pid)
        raise mobile.MobileBlocked("reddit blocked this device")

    monkeypatch.setattr(mobile, "comments", blocked)
    res = scrape_threads([TEXT_THREAD, IMAGE_THREAD], max_comments_per_post=5, fetcher=reddit)

    assert seen == ["1st816z"], "a retired device must not be asked again in the same call"
    assert len(reddit.calls) == 2
    assert res.mobile_threads == 0 and len(res.threads) == 2


def test_a_deleted_thread_is_recorded_as_missing_without_spending_a_page(mobile_on, monkeypatch):
    reddit = FakeReddit({})
    monkeypatch.setattr(
        mobile, "comments",
        lambda pid, limit=20, **kw: ({**RAW_POST, "removed_by_category": "deleted"}, []),
    )
    res = scrape_threads([TEXT_THREAD], max_comments_per_post=5, fetcher=reddit)

    assert reddit.calls == []
    assert res.missing == [TEXT_THREAD] and res.threads == []


def test_the_comment_budget_still_applies_to_mobile_results(mobile_on, monkeypatch):
    many = [{**RAW_COMMENT, "name": f"t1_c{i}", "body": f"body {i}"} for i in range(30)]
    monkeypatch.setattr(mobile, "comments", lambda pid, limit=20, **kw: (RAW_POST, many))
    res = scrape_threads([TEXT_THREAD], max_comments_per_post=4, fetcher=FakeReddit({}))
    assert len(res.threads[0].comments) == 4


# --------------------------------------------------------------------------- transport contract


class _Resp(io.BytesIO):
    def __init__(self, payload: bytes, headers: dict, gzipped: bool = False):
        body = gzip.compress(payload) if gzipped else payload
        super().__init__(body)
        self.headers = {"Content-Encoding": "gzip"} if gzipped else {}
        self.headers.update(headers)

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _token_response(headers=None):
    return _Resp(
        json.dumps({"access_token": "t" * 1217, "expires_in": 86399}).encode(),
        {"x-reddit-loid": "LOID1", "x-reddit-session": "SESS1", **(headers or {})},
    )


@pytest.fixture(autouse=True)
def _no_pacing():
    """Settings is a frozen dataclass, so the pacing knobs are poked directly, as elsewhere in the
    suite. Without this every transport test would sit out its real 2-3s inter-call gap."""
    before = settings.mobile_spacing_s, settings.mobile_spacing_jitter_s
    set_frozen(settings, "mobile_spacing_s", 0)
    set_frozen(settings, "mobile_spacing_jitter_s", 0)
    yield
    set_frozen(settings, "mobile_spacing_s", before[0])
    set_frozen(settings, "mobile_spacing_jitter_s", before[1])


def test_the_mint_sends_exactly_what_the_android_app_sends(monkeypatch):
    """Reddit gates this route on client identity. Every header here is load-bearing, and a silent
    drop would read downstream as 'Reddit blocked us' rather than 'we stopped looking like the app'."""
    seen = {}

    def fake_urlopen(req, timeout=None):
        seen["url"] = req.full_url
        seen["headers"] = {k.lower(): v for k, v in req.header_items()}
        seen["body"] = json.loads(req.data)
        return _token_response()

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    d = mobile.Device()
    d.mint()

    assert seen["url"] == mobile.TOKEN_URL
    assert seen["body"] == {"scopes": ["*", "email", "pii"]}
    # base64("ohXpoqrZYub1kg:") - the app's public client id with an empty secret.
    assert seen["headers"]["authorization"] == "Basic b2hYcG9xclpZdWIxa2c6"
    assert seen["headers"]["user-agent"].startswith("Reddit/Version ")
    assert "/Android " in seen["headers"]["user-agent"]
    for h in ("client-vendor-id", "x-reddit-device-id", "x-reddit-retry",
              "x-reddit-compression", "x-reddit-qos", "x-reddit-media-codecs"):
        assert seen["headers"].get(h), f"missing {h}"
    assert d.token and d.loid == "LOID1" and d.session == "SESS1"


def test_a_request_reuses_the_identity_the_token_was_minted_under(monkeypatch):
    calls = []

    def fake_urlopen(req, timeout=None):
        headers = {k.lower(): v for k, v in req.header_items()}
        calls.append((req.full_url, headers))
        if req.full_url == mobile.TOKEN_URL:
            return _token_response()
        return _Resp(json.dumps({"data": {"children": []}}).encode(), {"x-ratelimit-remaining": "98.0"})

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    d = mobile.Device()
    d.get("/api/info", {"id": "t3_x"})

    mint_ua = calls[0][1]["user-agent"]
    _, call_headers = calls[1]
    assert call_headers["user-agent"] == mint_ua, "a token used under a different UA is the giveaway"
    assert call_headers["x-reddit-loid"] == "LOID1"
    assert call_headers["x-reddit-session"] == "SESS1"
    assert call_headers["authorization"].startswith("Bearer ")
    assert "raw_json=1" in calls[1][0], "without it Reddit HTML-escapes text we quote verbatim"
    assert d.remaining == 98.0


def test_gzip_is_decoded_and_the_wire_bytes_are_what_we_count(monkeypatch):
    payload = json.dumps({"data": {"children": [{"kind": "t3", "data": RAW_POST}]}}).encode()

    def fake_urlopen(req, timeout=None):
        if req.full_url == mobile.TOKEN_URL:
            return _token_response()
        return _Resp(payload, {}, gzipped=True)

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    d = mobile.Device()
    d.mint()  # minted first, so the baseline below charges only the call under test
    before = mobile.counters["bytes"]
    out = d.get("/api/info", {"id": "t3_1st816z"})

    assert out["data"]["children"][0]["data"]["name"] == "t3_1st816z"
    charged = mobile.counters["bytes"] - before
    assert 0 < charged < len(payload), "compressed size is what crosses the wire, so it is what we count"


def _http_error(code, headers=None, body=b""):
    return urllib.error.HTTPError("https://oauth.reddit.com/x", code, "err", headers or {}, io.BytesIO(body))


def test_a_403_with_a_back_off_header_is_a_slow_down_and_is_retried(monkeypatch):
    monkeypatch.setattr(mobile.time, "sleep", lambda s: None)
    seq = [_http_error(403, {"Retry-After": "2"})]

    def fake_urlopen(req, timeout=None):
        if req.full_url == mobile.TOKEN_URL:
            return _token_response()
        if seq:
            raise seq.pop(0)
        return _Resp(json.dumps({"ok": 1}).encode(), {})

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    d = mobile.Device()
    assert d.get("/api/info", {"id": "t3_x"}) == {"ok": 1}
    assert d.retired is False, "a rate limit must not retire the device"
    assert mobile.counters["rate_limited"] >= 1


def test_a_403_without_one_retires_the_device_instead_of_hammering_it(monkeypatch):
    body = b"<html>you've been <b>blocked by network security</b></html>"

    def fake_urlopen(req, timeout=None):
        if req.full_url == mobile.TOKEN_URL:
            return _token_response()
        raise _http_error(403, {}, body)

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    d = mobile.Device()
    with pytest.raises(mobile.MobileBlocked):
        d.get("/api/info", {"id": "t3_x"})
    assert d.retired is True
    with pytest.raises(mobile.MobileBlocked):
        d.get("/api/info", {"id": "t3_x"})


def test_an_expired_token_is_reminted_once_and_the_call_succeeds(monkeypatch):
    mints = []
    seq = [_http_error(401)]

    def fake_urlopen(req, timeout=None):
        if req.full_url == mobile.TOKEN_URL:
            mints.append(1)
            return _token_response()
        if seq:
            raise seq.pop(0)
        return _Resp(json.dumps({"ok": 1}).encode(), {})

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    d = mobile.Device()
    assert d.get("/api/info", {"id": "t3_x"}) == {"ok": 1}
    assert len(mints) == 2, "the 401 should have forced exactly one re-mint"


def test_a_200_carrying_an_auth_error_is_treated_as_an_expired_token(monkeypatch):
    mints = []
    seq = [json.dumps({"message": "Unauthorized", "error": 401}).encode()]

    def fake_urlopen(req, timeout=None):
        if req.full_url == mobile.TOKEN_URL:
            mints.append(1)
            return _token_response()
        return _Resp(seq.pop(0) if seq else json.dumps({"ok": 1}).encode(), {})

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    d = mobile.Device()
    assert d.get("/api/info", {"id": "t3_x"}) == {"ok": 1}
    assert len(mints) == 2


def test_a_retired_device_is_never_handed_out_again(monkeypatch):
    pool = mobile._DevicePool(2)
    with pool.lease() as a:
        a.retired = True
        first = id(a)
    with pool.lease() as b:
        assert id(b) != first
    with pool.lease() as c:
        keep = id(c)
    with pool.lease() as d:
        assert id(d) == keep, "a healthy device should be reused, token and all"


def test_info_batches_at_reddits_hundred_id_ceiling(monkeypatch):
    batches = []

    def fake_get(self, path, params):
        batches.append(params["id"].split(","))
        return {"data": {"children": []}}

    monkeypatch.setattr(mobile.Device, "get", fake_get)
    mobile.info([f"t3_{i}" for i in range(250)])
    assert [len(b) for b in batches] == [100, 100, 50]


def test_comments_skips_more_stubs_and_flattens_the_tree(monkeypatch):
    tree = [
        {"data": {"children": [{"kind": "t3", "data": RAW_POST}]}},
        {"data": {"children": [
            {"kind": "t1", "data": {**RAW_COMMENT, "name": "t1_top", "replies": {
                "data": {"children": [
                    {"kind": "t1", "data": {**RAW_COMMENT, "name": "t1_reply", "depth": 1}},
                    {"kind": "more", "data": {"count": 41}},
                ]}
            }}},
            {"kind": "more", "data": {"count": 9}},
        ]}},
    ]
    monkeypatch.setattr(mobile.Device, "get", lambda self, path, params: tree)
    post, flat = mobile.comments("1st816z")
    assert post["name"] == "t3_1st816z"
    assert [c["name"] for c in flat] == ["t1_top", "t1_reply"]


def test_comments_accepts_a_fullname_or_a_bare_id(monkeypatch):
    seen = []

    def fake_get(self, path, params):
        seen.append(path)
        return [{"data": {"children": [{"kind": "t3", "data": RAW_POST}]}}, {"data": {"children": []}}]

    monkeypatch.setattr(mobile.Device, "get", fake_get)
    mobile.comments("t3_1st816z")
    mobile.comments("1st816z")
    assert seen == ["/comments/1st816z", "/comments/1st816z"]
