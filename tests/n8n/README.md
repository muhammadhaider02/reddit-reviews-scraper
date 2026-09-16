# Stage 4 node copies

n8n is read-only for this project, so the workflow's Reddit logic is exercised here instead.

| File | What it is |
|---|---|
| `sort_reddit_results.js` | verbatim `jsCode` of **Sort Reddit Results** |
| `fetch_reddit_comments.js` | verbatim `jsCode` of **Fetch Reddit Comments** |
| `run_node.mjs` | runs one of those files with `$input` / `$()` stubbed |
| `stage4_reddit_chain.py` | replays Search → Sort → IF → Comments → Fetch against a running server |

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
