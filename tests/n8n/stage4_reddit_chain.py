"""Replay Stage 4's Reddit chain against a running reddit-reviews server, locally.

  Apify: Reddit Search -> Sort Reddit Results -> IF: Has Strong Threads? -> Apify: Reddit Comments
  -> Fetch Reddit Comments -> Reddit Fallback Via Web Search (POST /reddit/mentions when the outcome
  is not 'ok', the leg that replaced Tavily)

The HTTP bodies are the ones Stage 4 sends; the two Code nodes run verbatim through run_node.mjs.
n8n is read-only, so this is the end-to-end check before anyone edits the workflow.

  uv run python tests/n8n/stage4_reddit_chain.py --brand Gymshark --domain gymshark.com \
      --terms "Gymshark" "Gymshark gym clothes" "Gymshark reviews" [--url http://127.0.0.1:8001] [--token ...]
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import httpx

HERE = Path(__file__).parent
RUNNER = HERE / "run_node.mjs"
PROXY = {"useApifyProxy": True, "apifyProxyGroups": ["RESIDENTIAL"]}


def node(mode: str, upstream: dict, items, workdir: Path) -> dict:
    up, it = workdir / f"{mode}_upstream.json", workdir / f"{mode}_items.json"
    up.write_text(json.dumps(upstream), encoding="utf-8")
    it.write_text(json.dumps(items), encoding="utf-8")
    out = subprocess.run(["node", str(RUNNER), mode, str(up), str(it)], capture_output=True, text=True, encoding="utf-8", check=True)
    return json.loads(out.stdout)


def call(client: httpx.Client, url: str, body: dict, timeout_s: int) -> tuple[int, object, float]:
    started = time.time()
    r = client.post(url, json=body, timeout=timeout_s + 10)
    return r.status_code, r.json(), round(time.time() - started, 1)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--brand", required=True)
    ap.add_argument("--domain", required=True)
    ap.add_argument("--terms", nargs="+", required=True, help="the 3 reddit_search_terms Parse Keywords would produce")
    ap.add_argument("--tokens", nargs="+", help="brand_match_tokens exactly as a real run had them (default: derived)")
    ap.add_argument("--url", default="http://127.0.0.1:8001")
    ap.add_argument("--token", default=os.environ.get("API_TOKEN", ""))
    ap.add_argument("--out", help="directory to keep every intermediate JSON in")
    ap.add_argument("--force-fallback", action="store_true", help="run the fallback leg even when the primary outcome is ok")
    args = ap.parse_args()

    workdir = Path(args.out) if args.out else Path(tempfile.mkdtemp(prefix="stage4_"))
    workdir.mkdir(parents=True, exist_ok=True)
    headers = {"Authorization": f"Bearer {args.token}"} if args.token else {}
    domain = args.domain.lower()
    upstream = {
        "Brand Name": args.brand,
        "domain_clean": domain,
        # same construction as Parse Keywords (without Claude's extra brand_keywords)
        "brand_match_tokens": args.tokens
        or sorted({t for t in [args.brand.lower(), args.brand.lower().replace(" ", ""), domain, domain.split(".")[0]] if len(t) >= 3}),
        "reddit_search_terms": args.terms,
        "reddit_query": " | ".join(args.terms),
    }
    report: dict = {"brand": args.brand}

    with httpx.Client(headers=headers) as client:
        search_body = {
            "searchTerms": args.terms, "searchPosts": True, "searchComments": False, "searchCommunities": False,
            "searchSort": "relevance", "searchTime": "all", "maxPostsCount": 10, "crawlCommentsPerPost": False,
            "maxCommentsCount": 0, "maxCommentsPerPost": 0, "maxCommunitiesCount": 0, "includeNSFW": False, "proxy": PROXY,
        }
        status, items, secs = call(client, f"{args.url}/reddit?maxTotalChargeUsd=0.35&timeout=280", search_body, 280)
        (workdir / "search_response.json").write_text(json.dumps(items, indent=2), encoding="utf-8")
        report["search"] = {"status": status, "seconds": secs, "items": len(items) if isinstance(items, list) else 1, "within_280s": secs <= 280}

        sort_out = node("sort", upstream, items, workdir)
        (workdir / "sort_output.json").write_text(json.dumps(sort_out, indent=2), encoding="utf-8")
        report["sort"] = {k: sort_out.get(k) for k in ("reddit_request_failed", "reddit_raw_post_count", "reddit_post_count", "reddit_strong_post_count", "reddit_reject_reasons")}
        targets = sort_out.get("reddit_comment_targets") or []
        report["sort"]["comment_targets"] = len(targets)

        if targets:  # IF: Has Strong Threads? -> true branch
            comments_body = {
                "startUrls": [{"url": u} for u in targets], "crawlCommentsPerPost": True, "maxPostsCount": len(targets),
                "maxCommentsPerPost": 20, "maxCommentsCount": 160, "maxCommunitiesCount": 0, "includeNSFW": False, "proxy": PROXY,
            }
            status, citems, secs = call(client, f"{args.url}/reddit?maxTotalChargeUsd=0.35&timeout=240", comments_body, 240)
            (workdir / "comments_response.json").write_text(json.dumps(citems, indent=2), encoding="utf-8")
            report["comments"] = {"status": status, "seconds": secs, "items": len(citems) if isinstance(citems, list) else 1, "within_240s": secs <= 240}
        else:  # false branch: Merge Comment Paths passes the Sort item straight through
            citems = [sort_out]

        fetch_out = node("comments", sort_out, citems, workdir)
        (workdir / "fetch_output.json").write_text(json.dumps(fetch_out, indent=2), encoding="utf-8")
        report["fetch"] = {k: fetch_out.get(k) for k in ("reddit_outcome", "reddit_has_data", "reddit_post_count", "reddit_comment_count", "reddit_rejected_comments", "reddit_comment_fetch_failed", "reddit_threads_read", "reddit_subreddits")}

        if fetch_out.get("reddit_outcome") != "ok" or args.force_fallback:
            # The fallback node's own call, unauthenticated by design (see MENTIONS_REQUIRE_TOKEN).
            fallback_item = {**upstream, **fetch_out}
            with httpx.Client() as open_client:
                status, mentions, secs = call(open_client, f"{args.url}/reddit/mentions", {"searchTerms": args.terms, "query": upstream["reddit_query"], "maxResults": 15}, 45)
            (workdir / "mentions_response.json").write_text(json.dumps(mentions, indent=2), encoding="utf-8")
            payload = mentions if status == 200 else {"__status": status, **(mentions if isinstance(mentions, dict) else {})}
            fb_out = node("fallback", fallback_item, payload, workdir)
            (workdir / "fallback_output.json").write_text(json.dumps(fb_out, indent=2), encoding="utf-8")
            results = mentions.get("results", []) if isinstance(mentions, dict) else []
            report["fallback"] = {
                "status": status, "seconds": secs, "within_45s": secs <= 45,
                "results": len(results), "threads": [r.get("url") for r in results],
                "kept": fb_out.get("reddit_fallback_count"), "dropped_no_brand_mention": fb_out.get("reddit_fallback_dropped_no_brand_mention"),
                "outcome": fb_out.get("reddit_outcome"), "reason": fb_out.get("reddit_fallback_reason"),
            }

    report["workdir"] = str(workdir)
    print(json.dumps(report, indent=2))
    return 0 if report["fetch"]["reddit_outcome"] not in (None, "request_failed") else 1


if __name__ == "__main__":
    sys.exit(main())
