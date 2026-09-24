# Stage 4 node copies

n8n is read-only for this project, so the workflow's Reddit logic is exercised here instead.

| File | What it is |
|---|---|
| `sort_reddit_results.js` | verbatim `jsCode` of **Sort Reddit Results** |
| `fetch_reddit_comments.js` | verbatim `jsCode` of **Fetch Reddit Comments** |
| `reddit_fallback_via_web_search.js` | **Reddit Fallback Via Web Search** (workflow 02), verbatim since 23 Sep 2026: the in-house `POST /reddit/mentions` call replaced Tavily. The same code runs in `scraper-testing` twice: in the Reddit lane after `Fetch Reddit Comments` (mirrors Stage 4; `Collect Results` reports `fallback_*`), and as `Reddit Fallback (Tavily baseline)` in the `Start Fallback Test` lane, which seeds the 14 brands Tavily rescued in stored Stage 4 executions with their real Parse Keywords output and diffs the kept threads against Tavily's (`Collect Fallback Results`). First runs 24 Sep 2026: executions 3691 (14 brands, 14 calls, 0 errors, 54 kept vs Tavily 23, mean 12.8 s, max 34.6 s) and 3694 (26 brands, 6 fallbacks fired, 0 errors, 46 rescued, all on-brand) |
| `run_node.mjs` | runs one of those files with `$input` / `$()` stubbed |
| `stage4_reddit_chain.py` | replays Search → Sort → IF → Comments → Fetch → Fallback against a running server (`--force-fallback` runs the last leg even when the outcome is `ok`) |

Copied from workflow `u3698BQra0i9oK4b` (SmartLead | Stage 4 - Brand Research Bundle), version
`048984af-5eef-41d2-9afb-df710aef2498`, updated 2026-09-13T20:50:47Z. If the workflow changes those nodes,
copy the new code over these files.

```bash
uv run reddit-reviews serve                      # terminal 1
uv run python tests/n8n/stage4_reddit_chain.py \
  --brand Gymshark --domain gymshark.com \
  --terms "Gymshark" "Gymshark gym clothes" "Gymshark reviews" --out ./stage4_run
```

The script prints the numbers Stage 4 would record (`reddit_outcome`, posts kept, comments kept, call
durations against the 280 s / 240 s node timeouts) and keeps every intermediate JSON in `--out`.
