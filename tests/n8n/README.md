# n8n Code node copies

These are copies of the Reddit Code nodes from the n8n research workflow that consumes this service.
They run locally against the service's output to prove the two stay compatible, without touching the
workflow itself. `tests/test_n8n_nodes.py` drives them.

| File | What it is |
|---|---|
| `sort_reddit_results.js` | the `jsCode` of **Sort Reddit Results**: scores and filters the posts from `POST /reddit` and picks the threads worth reading comments from |
| `fetch_reddit_comments.js` | the `jsCode` of **Fetch Reddit Comments**: filters the comments from the second `POST /reddit` call and sets `reddit_outcome` |
| `reddit_fallback_via_web_search.js` | the `jsCode` of **Reddit Fallback Via Web Search**: when the outcome is not `ok`, calls `POST /reddit/mentions` itself and appends the brand-naming results as extra posts |
| `run_node.mjs` | runs one of those files with `$input` / `$()` / `this.helpers.httpRequest` stubbed |
| `research_reddit_chain.py` | replays Search → Sort → IF → Comments → Fetch → Fallback against a running server (`--force-fallback` runs the last leg even when the outcome is `ok`) |

The node logic is kept as the workflow runs it; only the comments have been edited. If the workflow
changes those nodes, copy the new code over these files.

```bash
uv run reddit-reviews serve                      # terminal 1
uv run python tests/n8n/research_reddit_chain.py \
  --brand Gymshark --domain gymshark.com \
  --terms "Gymshark" "Gymshark gym clothes" "Gymshark reviews" --out ./research_run
```

The script prints the numbers the workflow would record (`reddit_outcome`, posts kept, comments kept,
call durations against the 280 s / 240 s node timeouts) and keeps every intermediate JSON in `--out`.
