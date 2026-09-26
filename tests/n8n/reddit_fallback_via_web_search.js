// ===================================================================
// REDDIT FALLBACK VIA WEB SEARCH - a second route when Reddit search finds nothing
// ===================================================================
// When the primary Reddit search comes back with nothing usable (reddit_outcome
// is not 'ok'), this node tries a second route before the brand is marked as
// having no Reddit data: it calls the service's POST /reddit/mentions, which
// searches Reddit comments for the brand, and appends the results that name the
// brand as extra posts.
//
// It sits inline in the main chain (one path in, one path out) and makes its own
// HTTP call from inside the Code node, rather than adding a separate branch.
//
// *** IT ONLY FIRES WHEN THE PRIMARY SOURCE FOUND NOTHING ***
// If the primary search returned usable posts this node is a pass-through and
// costs nothing, so it never competes with the primary source.
//
// *** WHAT IT MUST NEVER DO: INVENT METADATA ***
// The primary route returns subreddit, post age, comment count and vote score.
// The search results carry a title and an extract, and NONE of that metadata.
// Filling those fields with plausible values would put fabricated evidence into
// the research output. Every fallback post therefore carries its provenance IN
// THE TEXT the model reads, and its metadata fields are left empty rather than
// guessed.
//
// *** THE BRAND-MENTION GATE STILL APPLIES ***
// `Sort Reddit Results` hard-rejects any post that never names the brand, because
// an unrelated thread can score well purely on engagement.
// A web search has no such filter of its own and drifts more, not less, so the
// same gate is applied here to the extracts. A result that does not name the
// brand is dropped, exactly as it would be on the primary route.
const b = $input.first().json;

// ---- REQUEST COUNTERS ------------------------------------------------------
// This node makes its own HTTP call, so it counts its own requests and errors.
// The tavily_calls / tavily_errors key names are kept because downstream nodes
// read them by name; they count calls to POST /reddit/mentions.
let tavilyCalls = 0;
let tavilyErrors = 0;

// Only rescue a brand the primary source genuinely failed on. 'ok' means the
// primary search found usable discussion and there is nothing to rescue.
const primaryOk = b.reddit_outcome === 'ok';
if (primaryOk) {
  return [{ json: { ...b, reddit_fallback_used: false, reddit_fallback_reason: 'primary reddit source returned usable data', tavily_calls: 0, tavily_errors: 0 } }];
}

const query = String(b.reddit_query || '').trim();
if (!query) {
  return [{ json: { ...b, reddit_fallback_used: false, reddit_fallback_reason: 'no reddit_query available to search with', tavily_calls: 0, tavily_errors: 0 } }];
}

// Same brand-match tokens the primary sorter uses, so the two routes apply an
// identical relevance standard.
const tokens = (b.brand_match_tokens || []).map(t => String(t || '').toLowerCase()).filter(Boolean);
const norm = (s) => String(s || '').toLowerCase().replace(/[^a-z0-9]+/g, ' ');
const tight = (s) => String(s || '').toLowerCase().replace(/[^a-z0-9]/g, '');
const namesBrand = (txt) => {
  const t = norm(txt);
  const tt = tight(txt);
  for (const tok of tokens) {
    const nt = norm(tok).trim();
    const ntt = tight(tok);
    if (!nt) continue;
    if (nt.length >= 3 && t.includes(nt)) return true;
    if (ntt && ntt.length >= 3 && tt.includes(ntt)) return true;
  }
  return false;
};

let res = null;
let err = '';
try {
  tavilyCalls++;
  res = await this.helpers.httpRequest({
    method: 'POST',
    // The service's comment search (reddit-reviews, POST /reddit/mentions). It answers in the
    // Tavily search response shape: { results: [{ title, url, content, raw_content }] }.
    // Unauthenticated by design: a Code node cannot read n8n credentials, so the service is
    // reached on a private network, and MENTIONS_REQUIRE_TOKEN on the service is the switch if
    // that ever changes.
    url: 'http://reddit-reviews:8001/reddit/mentions',
    timeout: 45000,
    json: true,
    headers: { 'Content-Type': 'application/json' },
    body: {
      searchTerms: Array.isArray(b.reddit_search_terms) ? b.reddit_search_terms : [],
      query: query,
      maxResults: 15,
      // Corroboration for a multi-word brand name: without these, a brand named with ordinary
      // words rescues comments that use those words in passing, and the gate cannot tell (the
      // phrase IS in the text).
      productKeywords: Array.isArray(b.product_keywords) ? b.product_keywords : [],
      primaryProduct: String(b.primary_product || ''),
      domain: String(b.domain_clean || '')
    }
  });
} catch (e) {
  err = String((e && e.message) || e).slice(0, 180);
}

// A search that returned nothing is a SUCCESSFUL call with no results, not a
// failed request. Only a throw counts as an error.
tavilyErrors = err ? 1 : 0;

const results = (res && Array.isArray(res.results)) ? res.results : [];

const PROVENANCE = '[WEB SEARCH EXTRACT, not a scraped post. Subreddit, post age, comment count and vote score are NOT AVAILABLE for this item, so never cite or imply any of them.] ';

const rescued = [];
let droppedNoBrandMention = 0;

for (const r of results) {
  const title = String((r && r.title) || '').replace(/\s+/g, ' ').trim();
  const body = String((r && (r.raw_content || r.content)) || '').replace(/\s+/g, ' ').trim();
  if (!title && !body) continue;

  // The same hard gate as the primary route. Web search drifts off-brand more
  // readily than a scraper, so this matters more here, not less.
  if (!namesBrand(title + ' ' + body)) { droppedNoBrandMention++; continue; }

  rescued.push({
    id: '',
    relevance: 0,
    strong: false,
    title: (PROVENANCE + title).slice(0, 400),
    text: body.slice(0, 900),
    // Left empty on purpose. These are the fields the search does not return, and a
    // plausible-looking value here would be fabricated evidence.
    score_upvotes: 0,
    num_comments: 0,
    subreddit: 'web-search-extract',
    age_days: null,
    url: String((r && r.url) || '')
  });
}

const got = rescued.length > 0;

let reason;
if (err) reason = 'tavily request failed: ' + err;
else if (!results.length) reason = 'tavily returned no reddit results for this brand';
else if (!got) reason = 'tavily returned ' + results.length + ' results but none named the brand, so all were dropped by the brand-mention gate';
else reason = 'rescued ' + rescued.length + ' of ' + results.length + ' web search extracts';

if (!got) {
  return [{ json: {
    ...b,
    reddit_fallback_used: true,
    reddit_fallback_count: 0,
    reddit_fallback_dropped_no_brand_mention: droppedNoBrandMention,
    reddit_fallback_error: err,
    reddit_fallback_reason: reason,
    tavily_calls: tavilyCalls,
    tavily_errors: tavilyErrors
  } }];
}

// Fallback posts are APPENDED to whatever the primary route did find, never
// substituted for it. A thin-but-real scraped post is better evidence than a
// search extract, so it keeps its place at the front.
const existingPosts = Array.isArray(b.reddit_posts) ? b.reddit_posts : [];
const mergedPosts = existingPosts.concat(rescued).slice(0, 20);

return [{ json: {
  ...b,
  reddit_posts: mergedPosts,
  reddit_post_count: mergedPosts.length,
  // A DISTINCT OUTCOME, never plain 'ok'. Downstream and anyone reading an
  // execution must be able to tell a scraped brief from a rescued one, and the
  // report prompt is handed this string directly.
  reddit_outcome: 'ok_web_search_fallback',
  reddit_has_data: true,
  reddit_fallback_used: true,
  reddit_fallback_count: rescued.length,
  reddit_fallback_dropped_no_brand_mention: droppedNoBrandMention,
  reddit_fallback_error: '',
  reddit_fallback_reason: reason,
  tavily_calls: tavilyCalls,
  tavily_errors: tavilyErrors
} }];
