# API

One endpoint that answers the two bodies Stage 4 used to send to Apify, in the actor's row shape. Interactive docs are at `/docs`.

## The Apify contract

| | Apify | This service |
|---|---|---|
| Endpoint | `POST https://api.apify.com/v2/acts/harshmaur~reddit-scraper/run-sync-get-dataset-items` | `POST http://reddit-reviews:8001/reddit` |
| Auth | n8n `apifyApi` credential | n8n Header Auth credential sending `Authorization: Bearer <API_TOKEN>` |
| Query string | `?maxTotalChargeUsd=0.35&timeout=280` | none |
| Success | JSON array, one object per dataset row | same |
| Failure | one object carrying `error` or `message`, no `dataType` | same |
| Node timeout | 290 s search, 250 s comments | unchanged; `SCRAPE_BUDGET_S` sits under both |

The node option `onError: continueRegularOutput` is what lets an error body reach the downstream Code node instead of stopping the run. Keep it.

### Call 1: `Apify: Reddit Search`

Sent verbatim by Stage 4, `proxy` and the comment fields included. The terms are written per brand by Claude in the keyword step, in this order: the brand alone, brand plus primary product, brand plus `reviews`.

```json
{
  "searchTerms": ["<brand>", "<brand> <primary product>", "<brand> reviews"],
  "searchPosts": true,
  "searchComments": false,
  "searchCommunities": false,
  "searchSort": "relevance",
  "searchTime": "all",
  "maxPostsCount": 10,
  "crawlCommentsPerPost": false,
  "maxCommentsCount": 0,
  "maxCommentsPerPost": 0,
  "maxCommunitiesCount": 0,
  "includeNSFW": false,
  "proxy": { "useApifyProxy": true, "apifyProxyGroups": ["RESIDENTIAL"] }
}
```

Honoured: `searchTerms`, `searchSort`, `searchTime`, `maxPostsCount` (per term, so 30 is the ceiling per brand), `includeNSFW`. Everything else is accepted and ignored.

### Call 2: `Apify: Reddit Comments`

Fired only when `Sort Reddit Results` produced at least one strong thread with comments; the `IF: Has Strong Threads?` false branch skips it.

```json
{
  "startUrls": [{ "url": "<post url>" }],
  "crawlCommentsPerPost": true,
  "maxPostsCount": "<number of startUrls>",
  "maxCommentsPerPost": 20,
  "maxCommentsCount": 160,
  "maxCommunitiesCount": 0,
  "includeNSFW": false,
  "proxy": { "useApifyProxy": true, "apifyProxyGroups": ["RESIDENTIAL"] }
}
```

Honoured: `startUrls`, `maxCommentsPerPost`, `maxCommentsCount`. `maxPostsCount` is ignored here; it is only the link count. Up to `MAX_THREADS` (10) links are read; Stage 4 sends at most 8.

### Where the two differ from the actor

- Posts are de-duplicated across terms; the first term that found a post keeps it and is recorded in `searchTerm`. Stage 4 never de-duplicates, so a post the actor returned under two terms counted twice in its `posts_found` and could be kept twice.
- `[deleted]`, `[removed]` and empty comments are dropped before they are returned. Stage 4 would drop them anyway.
- A deleted or private thread is skipped and counted in `X-Threads-Missing`; it is never an error, because the actor would return nothing for it either.
- A term or thread that fails does not fail the call while any other succeeded. The response is still `200`, with the failure counted in a header.

## Request fields

`POST /reddit` takes a JSON body. The mode is decided by which of the first two fields is present; sending neither is a `400`.

| Field | Aliases | Default | Notes |
|---|---|---|---|
| `searchTerms` | `search_terms`, `q` | | list or single string; blank entries dropped, whitespace collapsed |
| `startUrls` | `start_urls`, `urls` | | list of `{"url": ...}` or plain strings; non-thread links are skipped |
| `searchSort` | `sort` | `relevance` | `relevance`, `hot`, `top`, `new`, `comments` |
| `searchTime` | `time` | `all` | `all`, `hour`, `day`, `week`, `month`, `year` |
| `maxPostsCount` | `max_posts` | `10` | per term, clamped to 1–50 |
| `maxCommentsPerPost` | `max_comments_per_post` | `20` | 0–200 |
| `maxCommentsCount` | `max_comments` | *(none)* | cap on the whole call |
| `includeNSFW` | `include_nsfw` | `false` | |
| `fullBodies` | `full_bodies` | `FULL_BODIES` | not an Apify field; `false` returns search snippets and skips the body fill |

## Response rows

### Post

One per post in the search response, and one per thread at the head of a comments response.

| Field | Value | Read downstream by |
|---|---|---|
| `dataType` | `"post"` | `Sort Reddit Results`, to identify a row as a post |
| `title` | | relevance scoring, the report |
| `body` | full selftext; empty for image and link posts, as on Reddit | brand matching, 800 chars stored, 400 into the prompt |
| `bodyIsSnippet` | `true` only when the body fill was skipped | |
| `communityName`, `subredditName`, `parsedCommunityName` | `r/name`, `name`, `name` | subreddit, junk filter, the report |
| `createdAt` | ISO 8601 | age in days |
| `score`, `upVotes` | same number | stored |
| `commentsCount`, `numberOfComments` | same number | relevance bonus, comment-target selection |
| `postUrl`, `url` | absolute permalink | the comments call's `startUrls` |
| `parsedId`, `id` | `abc123`, `t3_abc123` | post id |
| `authorName`, `nsfw`, `searchTerm` | | not read |

### Comment

| Field | Value | Read downstream by |
|---|---|---|
| `dataType` | `"comment"` | `Fetch Reddit Comments`; every other row is ignored |
| `body` | comment text | the comment, 800 chars stored |
| `score`, `upVotes` | same number | sort order |
| `subredditName`, `communityName` | `name`, `r/name` | attribution in the report |
| `commentCreatedAt`, `createdAt` | ISO 8601, same value | age in days |
| `id`, `parsedId`, `url`, `postId`, `parentId`, `authorName`, `depth` | | not read |

## Response headers

| Header | Meaning |
|---|---|
| `X-Scrape-Seconds` | wall clock for the call |
| `X-Pages-Fetched` | browser pages through the proxy, which is what the call cost in bandwidth |
| `X-Terms-Failed` | search: terms that errored while others succeeded |
| `X-Threads-Missing` | threads: deleted or private |
| `X-Threads-Failed` | threads: vendor failures while others succeeded |
| `X-Truncated` | `true` when `SCRAPE_BUDGET_S` stopped the call with work left |
| `X-Mobile-Posts`, `X-Mobile-Threads` | bodies and threads the app route served instead of the browser |
| `X-Term-Counts` | search: `returned/unique` per term in request order, e.g. `10/10,10/7,10/4`. `returned` is what the term produced under its own `maxPostsCount`; `unique` is what survived the cross-term merge. A raw count under 3 × `maxPostsCount` with every term at `10/…` is dedup, not a cap; a `0/0` is a term that failed |
| `X-Empty-Bodies` | search: posts returned with an empty `body`. Checked 19 Sep 2026 against Reddit directly for 70 such posts: all were image, link, video, gallery or title-only posts, i.e. posts with no text to return |

## Errors

```json
{ "error": { "type": "ScrapeBlocked", "status": 503, "message": "reddit refused the request (HTTP 403), title='Blocked'", "description": "..." } }
```

| Status | Type | When |
|---|---|---|
| `400` | `ValueError` | neither `searchTerms` nor `startUrls`, or nothing usable in them |
| `401` | | missing or wrong bearer token |
| `503` | `ScrapeBlocked` | Reddit refused every attempt |
| `503` | `ScrapeFailed` | the browser or network failed on every term or thread |
| `500` | | anything unexpected |

Stage 4 reads a `503` as a vendor failure (`reddit_request_failed`), which does not burn one of the brand's retries. The messages deliberately never contain `404`, `not found` or `invalid url`, because the workflow's brand-error pattern would.

## `GET /health`

Unauthenticated, for uptime checks.

```json
{
  "status": "ok", "version": "0.1.0", "auth": true, "proxy": true, "max_concurrency": 3,
  "mobile": { "enabled": true, "mints": 2, "calls": 145, "bytes": 1237581, "rate_limited": 0, "unauthorized": 0, "blocked": 0, "errors": 0 },
  "requests": 48, "search": 26, "threads": 22, "ok": 45, "empty": 0, "partial": 3,
  "blocked": 0, "failed": 0, "bad_request": 0, "truncated": 0, "in_flight": 0
}
```

Counters reset on restart. `partial` is a `200` with a failure header set. `truncated` rising means the budget is biting. `mobile.blocked` rising, or `mobile.calls` flat while `requests` climbs, means the app route is down and every call is quietly back on the browser at ~20x the bandwidth; the route fails soft by design and this is the only signal.

## Command line

Same code, no server:

```bash
uv run reddit-reviews search "Gymshark" "Gymshark reviews" --max 10 --pretty
uv run reddit-reviews thread https://www.reddit.com/r/Gymshark/comments/1vf53rw/ --max-comments 20 --pretty
```
