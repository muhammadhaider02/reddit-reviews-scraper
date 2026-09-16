"""Run Stage 4's own Code nodes (verbatim copies in tests/n8n/) on this service's output.

n8n is read-only for us, so this is how a change here is proven against the workflow before anyone
touches the HTTP nodes. Needs `node` on PATH; skipped otherwise.
"""

import json
import shutil
import subprocess
from pathlib import Path

import pytest
from conftest import fixture

from reddit_reviews.mapping import comment_item, post_item
from reddit_reviews.scraper import parse_search, parse_thread

HERE = Path(__file__).parent
RUNNER = HERE / "n8n" / "run_node.mjs"

pytestmark = pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")

UPSTREAM = {
    "Brand Name": "Gymshark",
    "domain_clean": "gymshark.com",
    "brand_match_tokens": ["gymshark", "gymshark.com"],
    "reddit_search_terms": ["Gymshark reviews"],
}


def run(tmp_path: Path, mode: str, upstream: dict, items) -> dict:
    up, it = tmp_path / f"{mode}_upstream.json", tmp_path / f"{mode}_items.json"
    up.write_text(json.dumps(upstream), encoding="utf-8")
    it.write_text(json.dumps(items), encoding="utf-8")
    out = subprocess.run(["node", str(RUNNER), mode, str(up), str(it)], capture_output=True, text=True, encoding="utf-8", check=True)
    return json.loads(out.stdout)


def search_items() -> list[dict]:
    posts = parse_search(fixture("search_gymshark_reviews.html")).posts + parse_search(fixture("search_gymshark_reviews_page2.html")).posts
    return [post_item(p) for p in posts]


def test_sort_node_reads_search_items(tmp_path):
    out = run(tmp_path, "sort", UPSTREAM, search_items())
    assert out["reddit_request_failed"] is False
    assert out["reddit_raw_post_count"] == 14
    assert out["reddit_post_count"] > 0 and out["reddit_strong_post_count"] > 0
    # replica communities and listings are still caught by Stage 4's own filters
    assert out["reddit_reject_reasons"]["bad_community"] >= 1
    kept = out["reddit_posts"][0]
    assert kept["url"].startswith("https://www.reddit.com/r/") and kept["subreddit"] and kept["age_days"] is not None
    assert 0 < len(out["reddit_comment_targets"]) <= 8


def test_sort_node_sees_empty_array_as_no_results(tmp_path):
    out = run(tmp_path, "sort", UPSTREAM, [])
    assert out["reddit_request_failed"] is False
    assert out["reddit_raw_post_count"] == 0


def test_sort_node_sees_503_body_as_request_failed(tmp_path):
    # With onError=continueRegularOutput n8n hands the node one item carrying the error.
    error_item = {"error": {"type": "ScrapeBlocked", "status": 503, "message": "reddit refused the request (HTTP 403)"}}
    out = run(tmp_path, "sort", UPSTREAM, error_item)
    assert out["reddit_request_failed"] is True
    assert out["reddit_error"]


def test_fetch_comments_node_reads_thread_items(tmp_path):
    sort_out = run(tmp_path, "sort", UPSTREAM, search_items())
    items = []
    for name in ("thread_text_post.html", "thread_image_post.html"):
        t = parse_thread(fixture(name))
        items.append(post_item(t.post))
        items.extend(comment_item(c) for c in t.comments[:20])
    sort_out["reddit_comment_targets"] = ["x", "y"]
    out = run(tmp_path, "comments", sort_out, items)
    assert out["reddit_comment_fetch_failed"] is False
    assert out["reddit_raw_comment_count"] == 13 + 20
    assert out["reddit_comment_count"] > 0
    c = out["reddit_comments"][0]
    assert c["subreddit"] and c["age_days"] is not None and isinstance(c["names_brand"], bool)
    assert out["reddit_outcome"] in {"ok", "thin"}
