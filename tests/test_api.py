import pytest
from conftest import fixture, set_frozen
from fastapi.testclient import TestClient

from reddit_reviews import api
from reddit_reviews.scraper import ScrapeBlocked, SearchResult, ThreadsResult, parse_search, parse_thread

# Stage 4's `Apify: Reddit Search` body, verbatim apart from the expression being resolved.
STAGE4_SEARCH_BODY = {
    "searchTerms": ["Gymshark", "Gymshark gym clothes", "Gymshark reviews"],
    "searchPosts": True,
    "searchComments": False,
    "searchCommunities": False,
    "searchSort": "relevance",
    "searchTime": "all",
    "maxPostsCount": 10,
    "crawlCommentsPerPost": False,
    "maxCommentsCount": 0,
    "maxCommentsPerPost": 0,
    "maxCommunitiesCount": 0,
    "includeNSFW": False,
    "proxy": {"useApifyProxy": True, "apifyProxyGroups": ["RESIDENTIAL"]},
}

# Stage 4's `Apify: Reddit Comments` body.
STAGE4_COMMENTS_BODY = {
    "startUrls": [
        {"url": "https://www.reddit.com/r/Gymshark/comments/1st816z/best_gymshark_tops_review/"},
        {"url": "https://www.reddit.com/r/gymsnark/comments/1pjz4rg/should_i_buy_gymshark_or_not_is_the_quality_good/"},
    ],
    "crawlCommentsPerPost": True,
    "maxPostsCount": 2,
    "maxCommentsPerPost": 20,
    "maxCommentsCount": 160,
    "maxCommunitiesCount": 0,
    "includeNSFW": False,
    "proxy": {"useApifyProxy": True, "apifyProxyGroups": ["RESIDENTIAL"]},
}

# The exact field lookups the Stage 4 Code nodes perform.
SORT_NODE_POST_FIELDS = {"dataType", "title", "body", "communityName", "createdAt", "commentsCount", "score", "parsedId", "postUrl"}
FETCH_NODE_COMMENT_FIELDS = {"dataType", "body", "commentCreatedAt", "score", "subredditName"}


@pytest.fixture
def client():
    set_frozen(api.settings, "api_token", "")
    with TestClient(api.app) as c:
        yield c


def canned_search(*_a, **_k):
    return SearchResult(posts=parse_search(fixture("search_gymshark_reviews.html"), "Gymshark reviews").posts, pages_fetched=1, seconds=1.0)


def canned_threads(*_a, **_k):
    threads = [parse_thread(fixture("thread_text_post.html")), parse_thread(fixture("thread_image_post.html"))]
    return ThreadsResult(threads=threads, pages_fetched=2, seconds=2.0)


def test_stage4_search_body_verbatim(client, monkeypatch):
    seen = {}

    def fake(terms, max_posts, sort, time_filter, include_nsfw, full_bodies=None):
        seen.update(terms=terms, max_posts=max_posts, sort=sort, time_filter=time_filter, include_nsfw=include_nsfw, full_bodies=full_bodies)
        return canned_search()

    monkeypatch.setattr(api, "scrape_search", fake)
    r = client.post("/reddit?maxTotalChargeUsd=0.35&timeout=280", json=STAGE4_SEARCH_BODY)
    assert r.status_code == 200
    assert seen == {
        "terms": ["Gymshark", "Gymshark gym clothes", "Gymshark reviews"],
        "max_posts": 10,
        "sort": "relevance",
        "time_filter": "all",
        "include_nsfw": False,
        "full_bodies": None,
    }
    items = r.json()
    assert len(items) == 7
    assert all(SORT_NODE_POST_FIELDS <= set(i) and i["dataType"] == "post" for i in items)
    assert r.headers["X-Pages-Fetched"] == "1"


def test_stage4_comments_body_verbatim(client, monkeypatch):
    seen = {}

    def fake(urls, per_post, total):
        seen.update(urls=urls, per_post=per_post, total=total)
        return canned_threads()

    monkeypatch.setattr(api, "scrape_threads", fake)
    r = client.post("/reddit", json=STAGE4_COMMENTS_BODY)
    assert r.status_code == 200
    assert seen == {"urls": [u["url"] for u in STAGE4_COMMENTS_BODY["startUrls"]], "per_post": 20, "total": 160}
    items = r.json()
    posts = [i for i in items if i["dataType"] == "post"]
    comments = [i for i in items if i["dataType"] == "comment"]
    assert len(posts) == 2 and len(comments) == 13 + 25
    assert all(FETCH_NODE_COMMENT_FIELDS <= set(c) for c in comments)
    # post first, then its own comments
    assert items[0]["dataType"] == "post" and items[1]["postId"] == items[0]["id"]


def test_empty_result_is_200_empty_array(client, monkeypatch):
    monkeypatch.setattr(api, "scrape_search", lambda *a, **k: SearchResult(posts=[], pages_fetched=3, seconds=1.0))
    r = client.post("/reddit", json={"searchTerms": ["nothing"]})
    assert r.status_code == 200 and r.json() == []


def test_blocked_is_503_with_error_item(client, monkeypatch):
    def fake(*a, **k):
        raise ScrapeBlocked("reddit refused the request (HTTP 403)")

    monkeypatch.setattr(api, "scrape_search", fake)
    r = client.post("/reddit", json={"searchTerms": ["Gymshark"]})
    assert r.status_code == 503
    body = r.json()
    assert body["error"]["type"] == "ScrapeBlocked"
    # Stage 4 spots a failed request as ONE item carrying error|message and no dataType/title.
    assert "dataType" not in body and "title" not in body


def test_400_without_terms_or_urls(client):
    r = client.post("/reddit", json={"maxPostsCount": 10, "searchTerms": []})
    assert r.status_code == 400
    assert "searchTerms" in r.json()["error"]["message"]


def test_400_for_links_that_are_not_threads(client):
    r = client.post("/reddit", json={"startUrls": [{"url": "https://example.com/"}]})
    assert r.status_code == 400


def test_string_forms_accepted(client, monkeypatch):
    seen = {}
    monkeypatch.setattr(api, "scrape_search", lambda terms, *a, **k: seen.setdefault("terms", terms) and canned_search())
    monkeypatch.setattr(api, "scrape_threads", lambda urls, *a, **k: seen.setdefault("urls", urls) and canned_threads())
    assert client.post("/reddit", json={"searchTerms": "Gymshark"}).status_code == 200
    assert client.post("/reddit", json={"startUrls": ["https://www.reddit.com/r/a/comments/b1/"]}).status_code == 200
    assert seen == {"terms": ["Gymshark"], "urls": ["https://www.reddit.com/r/a/comments/b1/"]}


def test_bearer_token_enforced(monkeypatch):
    set_frozen(api.settings, "api_token", "s3cret")
    try:
        monkeypatch.setattr(api, "scrape_search", canned_search)
        with TestClient(api.app) as c:
            body = {"searchTerms": ["Gymshark"]}
            assert c.post("/reddit", json=body).status_code == 401
            assert c.post("/reddit", json=body, headers={"Authorization": "Bearer nope"}).status_code == 401
            assert c.post("/reddit", json=body, headers={"Authorization": "Bearer s3cret"}).status_code == 200
            assert c.get("/health").status_code == 200  # health stays open for uptime checks
    finally:
        set_frozen(api.settings, "api_token", "")


def test_health_counters(client, monkeypatch):
    monkeypatch.setattr(api, "scrape_search", canned_search)
    before = client.get("/health").json()
    client.post("/reddit", json={"searchTerms": ["Gymshark"]})
    after = client.get("/health").json()
    assert after["ok"] == before["ok"] + 1
    assert after["search"] == before["search"] + 1
    assert after["in_flight"] == 0
