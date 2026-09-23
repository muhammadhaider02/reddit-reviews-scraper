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

`maxPostsCount` is filled per term *after* the earlier terms' posts are set aside. Stage 4's three terms per brand ("Brand", "Brand product", "Brand reviews") overlap heavily, and until 21 Sep 2026 each term took its 10 on its own and the merge dropped the overlap afterwards, so a brand landed on 20-25 distinct posts with every term reporting 10 (GRIP6 24, Vessel Golf 21, eskiin 21, against Apify's 28-30 from the same queries). Now term 2 pages on, within `MAX_SEARCH_PAGES` and in the same `searchSort` order, until it has 10 posts term 1 did not return, and term 3 does the same against terms 1 and 2. Page 1 of every term is still fetched in parallel; only the decision to page on waits for the earlier terms. A brand with no overlap is unchanged: one page per term.

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
| `X-Term-Counts` | search: `raw/unique` per term in request order, e.g. `14/10,28/10,21/8`. `raw` is every post the term's pages produced; `unique` is what the term contributed once the earlier terms' posts were set aside, which is its `maxPostsCount` wherever Reddit had that many. A `unique` under the quota with `raw` well above it is a term that paged to `MAX_SEARCH_PAGES` and still found mostly overlap; a `0/0` is a term that failed |
| `X-Empty-Bodies` | search: posts returned with an empty `body`. Checked 19 Sep 2026 against Reddit directly for 70 such posts: all were image, link, video, gallery or title-only posts, i.e. posts with no text to return |

## Errors

```json
{ "error": { "type": "ScrapeBlocked", "status": 503, "message": "reddit refused the request (HTTP 403), title='Blocked'", "description": "..." } }
```

| Status | Type | When |
|---|---|---|
| `400` | `ValueError` | neither `searchTerms` nor `startUrls`, or nothing usable in them |
| `401` | | missing or wrong bearer token |
| `503` | `ScrapeBlocked` | Reddit refused every attempt, or something in front of it (a proxy or CDN interstitial served as HTTP 200) answered every attempt |
| `503` | `ScrapeFailed` | the browser or network failed on every term or thread |
| `500` | | anything unexpected |

Stage 4 reads a `503` as a vendor failure (`reddit_request_failed`), which does not burn one of the brand's retries. The messages deliberately never contain `404`, `not found` or `invalid url`, because the workflow's brand-error pattern would.

## `POST /reddit/mentions` (the Tavily fallback)

Workflow 02's `Reddit Fallback Via Web Search` Code node ran one Tavily web search restricted to reddit.com whenever the primary outcome was not `ok` (about 1 brand in 3), and read `results[].title`, `results[].url` and `results[].raw_content || content`. Since 23 Sep 2026 that call goes here. The value Tavily added was finding threads where the brand is named **in a comment**, which the post search misses; this route asks Reddit's own comment search (`/svc/shreddit/search/?type=comments`) through the same browser and proxy as `/reddit`, one page per term in parallel, a second page only while the merged threads are still under `maxResults` and it fits the budget, then fills the post bodies with one app-route call. Measured from the VPS: 2-5 s a page, and for Howdysnax the exact three threads Tavily had found.

**Unauthenticated by default.** A Code node cannot read n8n credentials, and the container has no published ports, so only the docker network reaches it. `MENTIONS_REQUIRE_TOKEN=true` puts the bearer check on this route too (for the day the call moves into an HTTP Request node). `/reddit` is always locked.

Request, what the node has on the item:

| Field | Aliases | Default | Notes |
|---|---|---|---|
| `searchTerms` | `search_terms`, `reddit_search_terms` | | list; at most 3 are searched |
| `query` | `reddit_query`, `q` | | the same terms joined with ` \| `, split server-side when `searchTerms` is empty |
| `maxResults` | `max_results` | `15` | threads, 1-25 |
| `includeNSFW` | `include_nsfw` | `false` | |
| `productKeywords`, `primaryProduct`, `domain` | `product_keywords`, `primary_product`, `domain_clean` | | Parse Keywords' vocabulary; for a multi-word brand name a thread is kept only when the brand's first word is written capitalised mid-sentence, a product word appears, or the domain does. Without them every thread is returned. |

Response, Tavily's envelope:

```json
{ "query": "Howdysnax | Howdysnax reviews", "response_time": 8.4,
  "results": [ { "title": "Does anyone else wish there were savory protein bars?",
                 "url": "https://www.reddit.com/r/Protein/comments/1tyuc7l/",
                 "content": "Search for Howdy (authentic South African droëwors) You can find it on Amazon or howdysnax.com ...",
                 "raw_content": "<matching comments, best first>\n\n<post body>",
                 "subreddit": "Protein", "post_id": "t3_1tyuc7l", "created_at": "2026-06-08T15:52:31.312Z", "score": 1,
                 "post_score": 51, "post_comments": 26, "body_filled": true,
                 "matched_comments": [ { "id": "t1_oqgy3w4", "url": "...", "text": "...", "score": 1, "created_at": "...", "term": "Howdysnax" } ] } ] }
```

The node reads `title`, `url` and `raw_content || content` and slices the text to 900 characters, so `raw_content` leads with the matching comments and puts the post body after them. Everything from `subreddit` on is **not read by the node**: it is there for logs, for the acceptance comparison, and for a future where rescued threads carry real metadata, which changes research output and needs the owner's sign-off (tavily.md). Unlike Tavily's `raw_content`, there is no page chrome in the text, which is what produced Tavily's false brand matches.

Each term is searched with the brand name as an exact phrase (`"Kind Water Systems" water filter`: term 1 is the brand, so its text is quoted inside terms 2 and 3 and the rest is left loose for ranking). Unquoted, Reddit ranks comments that merely contain the words: measured 23 Sep 2026, 0 of 10 results carried "Kind Water Systems" unquoted and 10 of 10 quoted. A multi-word brand name is also an ordinary phrase: "Safe Hero" is what an Overwatch player calls a low-risk pick, and the node's gate cannot tell, because the phrase is in the text. Measured 23 Sep 2026 on the 14-brand baseline, that rescued 15 gaming comments for Safe Hero, 15 for The Hero Company and 12 for Exodus Strong, all noise. So for a multi-word name a thread is kept only with corroboration: the brand's first significant word written capitalised mid-sentence ("Hercules supplements", "the Longevity store"), a product word from `productKeywords` / `primaryProduct` ("softener", "gummies", "crossbody"), or the domain. Dropped threads are counted in `X-Generic-Dropped`. A one-word name needs none of this. Separately, every hit must name the brand as a word of its own: Reddit's comment search also matches "arq8" inside a Google Maps id, a YouTube id, a Reddit share link and a Steam key (the first production run after the Tavily cutover, 23 Sep 2026, kept 5 such threads of 8 for Arq8, and the node's gate cannot tell because the letters are in the text). A hit whose only occurrence has a letter, digit, `_` or `-` right before it or a letter, digit or `_` right after it is dropped before threads are built and counted in `X-Opaque-Dropped`; "arq8's", "@arq8", "www.howdysnax.com" and "kindwatersystems.com" still count. Threads come in term order, then Reddit's relevance order; the first term to find a thread owns it and every term's matching comments are kept on it. An empty `results` is a `200` and means "searched, found nothing"; a `503` or `500` carries the error envelope below and no `results` key, which the node counts as an error. Headers: `X-Scrape-Seconds`, `X-Pages-Fetched`, `X-Terms-Failed`, `X-Truncated`, `X-Mobile-Posts` (bodies filled), and `X-Term-Counts` as `hits/threads` per term (comment hits the term's pages produced, threads it was first to find).

Budget: the node gives the call 45 s. `MENTIONS_BUDGET_S` (30) is the wall clock, `MENTIONS_FETCH_TIMEOUT_MS` (10000) the per-page browser timeout, `MENTIONS_MAX_PAGES` (2) the cursor pages per term. Page 1 of every term is never skipped; a browser that cannot be had before the deadline fails the term fast (`503` if every term fails) rather than answering after the node gave up.

## `GET /health`

Unauthenticated, for uptime checks.

```json
{
  "status": "ok", "version": "0.1.0", "auth": true, "proxy": true, "max_concurrency": 3, "mentions_auth": false,
  "mobile": { "enabled": true, "mints": 2, "calls": 145, "bytes": 1237581, "rate_limited": 0, "unauthorized": 0, "blocked": 0, "errors": 0 },
  "requests": 57, "search": 26, "threads": 22, "mentions": 9, "ok": 54, "empty": 0, "partial": 3,
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
