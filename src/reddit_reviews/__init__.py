"""Self-hosted Reddit scraper, a drop-in for the Apify actor used by an n8n research workflow."""

import argparse
import json
import logging
import sys

__version__ = "0.1.0"


def _cmd_serve(args: argparse.Namespace) -> int:
    import uvicorn

    from .config import settings

    uvicorn.run(
        "reddit_reviews.api:app",
        host=args.host or settings.host,
        port=args.port or settings.port,
        log_level="info",
    )
    return 0


def _print(payload: dict, pretty: bool) -> None:
    # ensure_ascii keeps this safe on a cp1252 Windows console.
    print(json.dumps(payload, indent=2 if pretty else None, ensure_ascii=True))


def _cmd_search(args: argparse.Namespace) -> int:
    from .mapping import post_item
    from .scraper import RedditError, scrape_search

    try:
        res = scrape_search(args.terms, args.max, args.sort, args.time)
    except RedditError as e:
        print(json.dumps({"error": {"type": type(e).__name__, "status": e.status, "message": str(e)}}), file=sys.stderr)
        return 2
    _print({"seconds": res.seconds, "pages_fetched": res.pages_fetched, "failed_terms": res.failed_terms, "items": [post_item(p) for p in res.posts]}, args.pretty)
    return 0


def _cmd_thread(args: argparse.Namespace) -> int:
    from .mapping import comment_item, post_item
    from .scraper import RedditError, scrape_threads

    try:
        res = scrape_threads(args.urls, args.max_comments)
    except RedditError as e:
        print(json.dumps({"error": {"type": type(e).__name__, "status": e.status, "message": str(e)}}), file=sys.stderr)
        return 2
    items = []
    for t in res.threads:
        items.append(post_item(t.post))
        items.extend(comment_item(c) for c in t.comments)
    _print({"seconds": res.seconds, "missing": res.missing, "failed": res.failed, "items": items}, args.pretty)
    return 0


def main(argv: list[str] | None = None) -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    parser = argparse.ArgumentParser(prog="reddit-reviews")
    sub = parser.add_subparsers(dest="cmd", required=True)

    serve = sub.add_parser("serve", help="run the HTTP service n8n calls")
    serve.add_argument("--host")
    serve.add_argument("--port", type=int)
    serve.set_defaults(func=_cmd_serve)

    search = sub.add_parser("search", help="search posts from the command line and print JSON")
    search.add_argument("terms", nargs="+", help='one or more search terms, e.g. "gymshark reviews"')
    search.add_argument("--max", type=int, default=10, help="posts per term")
    search.add_argument("--sort", default="relevance")
    search.add_argument("--time", default="all")
    search.add_argument("--pretty", action="store_true")
    search.set_defaults(func=_cmd_search)

    thread = sub.add_parser("thread", help="read threads and their comments from the command line and print JSON")
    thread.add_argument("urls", nargs="+")
    thread.add_argument("--max-comments", type=int, default=20, help="comments per thread")
    thread.add_argument("--pretty", action="store_true")
    thread.set_defaults(func=_cmd_thread)

    args = parser.parse_args(argv)
    sys.exit(args.func(args))
