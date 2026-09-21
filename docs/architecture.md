# Architecture

What the service replaces, why it is built the way it is, and the measurements each decision rests on.

## What it replaces

The research stage of the SmartLead pipeline, n8n workflow **02 · Learn About The Brand** (`LlXYr9cMoypAFYA4`), made two calls per brand to the Apify actor `harshmaur~reddit-scraper`:

| Node | Purpose | Fires |
|---|---|---|
| `Apify: Reddit Search` | 3 search terms, 10 posts each, no comments | always |
| `Apify: Reddit Comments` | up to 8 thread links, 20 comments each, 160 total | only when the search produced a strong thread |

Both went to `POST https://api.apify.com/v2/acts/harshmaur~reddit-scraper/run-sync-get-dataset-items` with `RESIDENTIAL` Apify proxies. This service answers both bodies at `POST http://reddit-reviews:8001/reddit`, unchanged. The exact bodies and the field contract are in [api.md](api.md).

What the workflow does with the rows decides most of the design here:

- **`Sort Reddit Results`** drops posts from rep, dupe and coupon subreddits and posts whose title reads as a listing, then requires a brand token in the title or body. Survivors are scored on title hits, review intent, age and comment count; score 3 keeps, score 4 with a title hit is *strong*. Up to 8 strong posts with comments become the comment targets.
- **`Fetch Reddit Comments`** drops `[removed]`, `[deleted]`, comments under 6 words and comments older than 5 years, flags the ones that name the brand and keeps up to 200.
- The **outcome** it sets is the field that judges a scraper: `request_failed` (the search call errored), `no_results` (zero raw posts), `all_irrelevant` (posts, none survived), `thin` (no strong post, or fewer than 8 kept posts plus comments), `ok`. Empty or thin sends the brand to a Tavily web-search fallback.

Three consequences. A post with an empty `body` cannot pass the brand gate on its body, so bodies must be full text, not the search snippet. An error must arrive as an object with `error` and no `dataType`, because that is what the failure detection looks for. And a call that returns zero posts because it ran out of time is indistinguishable downstream from a brand nobody discusses, which is why the time budget below never skips the first page.

## Search: a browser through a residential proxy

Reddit blocks by IP. Measured 17 Sep 2026 from the VPS: `curl`, Scrapling's Chromium and Camoufox's Firefox all get the same 403 block page for the same URL (`You've been blocked by network security`; 190,240 and 190,292 bytes), while a residential exit gets `200` the same second. Three clients from "no fingerprint" to "full stealth Firefox" treated identically means the block lands before fingerprinting, so a better browser would not help and none was tried.

Reddit's public JSON endpoints (`/search.json`, `/comments/<id>.json`) answer the same 403 to plain HTTP clients and to the stealth browser alike. The server-rendered web pages answer through a residential exit, and they carry the data in element attributes (`search-telemetry-tracker`, `shreddit-post`, `shreddit-comment`). The service reads those with Scrapling's stealth Chromium through the proxy, following the search cursor for up to `MAX_SEARCH_PAGES` pages per term (7 or 14 posts a page). A term pages on until it holds `maxPostsCount` posts that no earlier term in the same request returned: page 1 of every term is fetched in parallel, then each term waits for the terms before it to be final before deciding whether it needs page 2. That is what makes three overlapping terms reach ~30 distinct posts instead of ~22 (measured 21 Sep 2026; `docs/api.md` has the numbers).

## Bodies and comments: Reddit's own app API

Search results carry a snippet, not the body, and comment trees are a page each. Fetching those through the browser was 26 search-side pages plus 8 thread pages per brand at roughly 1 MB of proxied bandwidth each.

Reddit's Android app reads through an anonymous OAuth token (`POST /auth/v2/oauth/access-token/loid`, client id `ohXpoqrZYub1kg`) that is gated on client identity, not on the search index. `mobile.py` mints such a token under a fabricated device (User-Agent, two UUIDs, a qos figure, the returned `loid` and `session` headers) and reads by id: `/api/info?id=t3_...` returns 100 posts with full `selftext` in one call, `/comments/<id>` returns a thread. No ranking is involved, so the result is the same as the page would show.

Measured on one brand, same terms, minutes apart:

| | Proxied bytes | Wall clock | Browser pages |
|---|---|---|---|
| Browser only (`MOBILE_ENABLED=false`) | 92.1 MB | 185 s | 26 search + 8 thread |
| Hybrid (default) | 4.8 MB | 37 s | 5 search + 0 thread |

One device serves one call end to end, paced at `MOBILE_SPACING_S` plus jitter, under Reddit's 100 requests per 10 minutes per token. The body fill is all-or-nothing: if the route cannot answer, the browser does every body, because a partial fallback would quietly put ~25 proxied pages back on the bill. The thread fill falls back per thread, because a thread costs one page either way.

**This route is proxied too.** On 18 Sep 2026, about a day after minting ~25 tokens from the VPS, Reddit blocked its IP from `oauth.reddit.com` outright. Measured that afternoon: a direct mint returns the block page, a mint through a residential exit returns a token, and that token is still refused when used directly. So every call on this route leaves through the proxy, and each device holds one sticky exit for its whole life. The bandwidth case survives: ~270 KB of API calls per brand instead of ~29 browser pages.

## Why search does not use the app API

The app's search (`oauth.reddit.com/search`) is a different index, not a cheaper route to the same posts. A full-mobile search route was built behind a flag and run over the same 103 brands as the hybrid, hours apart:

| | Web search | App search |
|---|---|---|
| Posts | 2,209 | 2,678 |
| Shared posts | 158 (7.2% overlap, median 4.8% per brand) | |
| Posts naming the brand | 54.1% | 53.5% |
| Comments naming the brand | 1,388 | 606 |
| Brands with nothing usable | 6 | 15 |
| Proxy cost, 103 brands | $0.54 | $0.00 |

The app index finds threads that merely mention a brand; the web index finds threads about it, which is where the quotable comments are. The route was removed on 19 Sep 2026. `MOBILE_ENABLED=false` still falls everything back to the browser.

## The proxy

`proxy.py` is shared by both routes so that one pool decides which exit every in-flight fetch uses.

- **Sticky ports, never the rotating gateway.** A rotating gateway changes exit IP per request, which hands Reddit a new address for every cursor page of one search. The rotating ports (Decodo 7000, DataImpulse 823) are refused at startup with a message naming the fix. Both vendors' sticky ranges cover the pool's `10001–10099`.
- **One exit per in-flight fetch.** Each browser fetch and each mobile device takes a port from the pool and returns it when done. Two browsers leaving from one exit at the same time is a fingerprint, and independent random draws collide, so the pool is a lock, not a random choice.
- **A retry is a different exit.** Reddit refuses some residential exits outright and waiting does not change that, so `fetch_page` retries once and the retry draws a new port.
- **DNS inside the tunnel** (`dns_over_https=True`), so the container's resolver never sees `reddit.com`.
- **The credential is never logged.** Errors and logs carry the port number only. At startup the service probes `api.ipify.org` through the proxy and logs the egress IP, because a wrong credential and a blocked IP both look like a 403.
- **Vendor.** DataImpulse, pay-as-you-go at $1/GB with no subscription floor, measured 2.3x cheaper per brand than Decodo for this workload. The `__cr.us` username suffix pins US exits. Two forms of `SCRAPER_PROXY` are accepted: a URL, or `host:port:user:pass` split at most three times so a password containing `:` survives. The dict form is handed to Scrapling because its URL parser does no percent-decoding.

## The time budget

Stage 4's nodes time out at 290 s (search) and 250 s (comments). Nothing cancels a handler when the caller gives up, so a call that overruns keeps a browser busy for nobody. `SCRAPE_BUDGET_S` (200) bounds the whole call: before starting a page, a body or a thread the service checks that a full fetch's worst case (`attempts × FETCH_TIMEOUT_MS + retry delay`) still fits, and otherwise returns what it has with `X-Truncated: true`. The first search page is never skipped, for the reason given above. `docker-compose.yml`'s `stop_grace_period` (210 s) is this budget plus Chromium teardown, so a redeploy does not kill in-flight scrapes.

## Memory and concurrency

Scrapling launches a full Chromium per fetch and tears it down, so memory is spiky: peak is `MAX_CONCURRENCY` browsers (~254 MiB each) plus the Python process. Measured on the image: idle 43 MiB, peak 859 MiB at `MAX_CONCURRENCY=3`, 358 PIDs. The compose file sets `mem_limit: 2g` and `pids_limit: 1024` from those numbers. Concurrency is a latency budget as much as a memory knob: at 1, the same 6-post search took 239 s instead of 101 s, against the 290 s node timeout.

## Measured against Apify

The only brands with a real Apify result still stored are 26 bundles in the pipeline's data table. Run through the n8n test workflow with Stage 4's own Code nodes on 19 Sep 2026, one brand at a time:

| | Hybrid | Apify |
|---|---|---|
| Brands `ok` | 21 | 21 |
| Posts found | 570 | 699 |
| Posts kept after Stage 4's filter | 165 | 247 |
| Threads read | 119 | 119 |
| Comments kept | 1,210 | 1,339 |
| Request failures | 0 | |
| Wall clock | 31 s per brand | |
| Proxy bandwidth | 5.8 MB per brand, ~$0.15 for the run | |

The kept-posts gap is mostly the test harness: it builds brand tokens from the sheet name and domain, while the real Parse Keywords adds Claude's `brand_keywords` (the spaced spellings Reddit uses, "Lacrosse Unlimited", "Six Zero"). Replaying Stage 4's Sort node with one such token per brand recovered 42 of the 82 missing posts across six brands. Search terms are a reconstruction too, because the pipeline does not store them. The set is golf, pickleball and fitness, which Reddit covers unusually well; over the 103-brand skincare and wellness sheet the hybrid landed 58 `ok`, 38 `all_irrelevant`, 7 `thin`, again with zero failures, at 4.7 MB per brand.

## Layout

| Module | Role |
|---|---|
| `scraper.py` | URLs, fetch through the browser, page parsing, the search and thread orchestration and their budgets |
| `mobile.py` | the app API transport: device identity, token minting, `/api/info`, `/comments`, pacing |
| `proxy.py` | credential parsing, the rotating-gateway guard, the sticky-port pool |
| `mapping.py` | output rows in the Apify actor's field names |
| `api.py` | FastAPI surface, counters, the startup egress probe |
| `config.py` | environment |

`tests/fixtures/` are real Reddit pages with styles, scripts and SVGs stripped; refresh them if Reddit changes its markup. `tests/n8n/` holds verbatim copies of `Sort Reddit Results` and `Fetch Reddit Comments` and a runner that executes them with `$input` and `$()` stubbed, so the workflow's own JavaScript runs in the test suite against this service's output. `tests/n8n/stage4_reddit_chain.py` replays the whole chain against a running server.

## Configuration (environment variables)

`.env.example` documents every variable beside the measurement its default is based on.

| Variable | Default | Meaning |
|---|---|---|
| `API_TOKEN` | *(empty)* | Bearer token callers must send. Empty disables auth and logs a warning; local testing only. |
| `SCRAPER_PROXY` | *(empty)* | Residential proxy, as a URL or `host:port:user:pass`. A sticky port; the rotating gateways (7000, 823) are refused. |
| `MAX_CONCURRENCY` | `3` | Browser fetches in flight across all requests. Sized against `mem_limit`. |
| `FETCH_TIMEOUT_MS` | `45000` | Per-page browser timeout. |
| `BLOCK_RESOURCES` | `true` | Block images, fonts, CSS and media in the browser. |
| `MAX_SEARCH_PAGES` | `3` | Cursor pages followed per search term, including the pages a later term spends getting past the posts an earlier term already returned. |
| `FULL_BODIES` | `true` | Fill each found post's full body. Callers can override per call with `fullBodies`. |
| `MAX_BODY_FETCHES` | `30` | Bodies filled per search call. |
| `SCRAPE_BUDGET_S` | `200` | Wall-clock budget for one call. Keep under 250 and under `stop_grace_period`. |
| `MAX_THREADS` | `10` | Thread links read per call, whatever the caller sends. |
| `RETRY_DELAY_S` | `2` | Pause before the one retry of a refused page. A refusal is a non-200, a page carrying a block marker, or since 21 Sep 2026 a 200 that is not a Reddit document at all (no `redditstatic.com`, no `<shreddit-` element): a proxy's `Gateway` page came back that way and had been read as "no posts". |
| `MOBILE_ENABLED` | `true` | Kill switch for the app route. Off means the browser does bodies and threads at ~20x the bandwidth. |
| `MOBILE_DEVICES` | `3` | Devices in flight at once; caps concurrent calls on the route. |
| `MOBILE_MIN_BUDGET` | `10` | Re-mint when a token's 100-per-10-minutes budget falls this low. |
| `MOBILE_SPACING_S` | `2` | Minimum gap between two calls on one device. |
| `MOBILE_SPACING_JITTER_S` | `1` | Random extra gap. |
| `MOBILE_TIMEOUT_S` | `25` | Per-request timeout on the route; the calls answer in ~0.2 s. |
| `HOST` | `0.0.0.0` | Bind address. |
| `PORT` | `8001` | Bind port; `trustpilot-reviews` has 8000. |
