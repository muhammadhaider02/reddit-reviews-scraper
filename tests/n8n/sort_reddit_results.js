// Sort Reddit Results. Step 1 of 2: score and filter the posts from the Reddit
// search, and pick the few strong threads worth fetching comments from.
const b = $('Parse Keywords').first().json;
const items = $input.all().map(i => i.json);
const tokens = (b.brand_match_tokens || []).map(t => String(t || '').toLowerCase()).filter(Boolean);

const first = items[0] || {};
const requestFailed = items.length === 1 && (!!first.error || !!first.message) && !first.dataType && !first.title;

const norm = (s) => String(s || '').toLowerCase().replace(/[^a-z0-9]+/g, ' ');
const tight = (s) => String(s || '').toLowerCase().replace(/[^a-z0-9]/g, '');

const brandHits = (txt) => {
  const t = norm(txt);
  const tt = tight(txt);
  let n = 0;
  for (const tok of tokens) {
    const nt = norm(tok).trim();
    const ntt = tight(tok);
    if (!nt) continue;
    if (nt.length >= 3) n += (t.split(nt).length - 1);
    else if (ntt && ntt.length >= 3) n += (tt.split(ntt).length - 1);
  }
  return n;
};

// Communities that are not customers. Counterfeit and dupe marketplaces name
// brands constantly and tell us nothing about the real product; snark and
// puzzle subs are not buyers either.
const BAD_SUB = /(fashionreps|repladies|reps?buy|designerreps|dhgate|aliexpress|taobao|pandabuy|superbuy|cnfans|hagobuy|kakobuy|weidian|dupe|superfake|wholesal|snark|fauxmoi|blogsnark|saintmeghanmarkle|influencersnark|celebritynumber|deals?$|freebies|coupon|dropship|flipping|testimonial)/i;

// Titles that give away replica or resale intent even in a normal community.
const BAD_TITLE = /(\brep\b|reps\b|replica|dupe|fake|batch|haul from|w2c\b|qc\b|\[wts\]|\[wtt\]|\[wtb\]|\[iso\]|selling|for sale|price drop|bundle deal)/i;

const ageDays = (iso) => {
  const d = Date.parse(iso);
  if (!d) return null;
  return Math.round((Date.now() - d) / 86400000);
};

const rawPosts = requestFailed ? [] : items.filter(r => r && typeof r === 'object' && (String(r.dataType || '').toLowerCase() === 'post' || r.title !== undefined));

const STRONG = 4;
const MIN_KEEP = 3;

const scored = [];
let postsRejected = 0;
const rejectReasons = { no_brand_mention: 0, off_brand: 0, bad_community: 0, too_old: 0, listing: 0 };

for (const p of rawPosts) {
  const title = String(p.title || '').replace(/\s+/g, ' ').trim();
  const body = String(p.body || '').replace(/\s+/g, ' ').trim();
  if (!title) continue;

  const sub = String(p.communityName || p.subredditName || '').replace(/^\/?r\//, '');
  const age = ageDays(p.createdAt);
  const numComments = Number(p.commentsCount || 0);

  if (BAD_SUB.test(sub)) { postsRejected++; rejectReasons.bad_community++; continue; }
  if (BAD_TITLE.test(title)) { postsRejected++; rejectReasons.listing++; continue; }

  const titleHits = brandHits(title);
  const bodyHits = brandHits(body);

  // HARD GATE. Bonuses for buying-intent words, recency and busy threads must
  // never carry a post that does not mention the brand at all. Without this an
  // unrelated movie review thread can score 4 purely from "Review" + recent +
  // thousands of comments.
  if (titleHits === 0 && bodyHits === 0) {
    postsRejected++; rejectReasons.no_brand_mention++; continue;
  }

  let score = (titleHits * 4) + Math.min(bodyHits, 4);

  if (/(review|worth|quality|sizing|fit\b|disappoint|refund|return|shipping|customer service|vs\.?\s|compare|anyone tried|thoughts on|experience|legit|overpriced|justify)/i.test(title)) score += 2;

  // A busy thread is a live discussion whenever it started, so it is never
  // aged down. Only quiet old threads lose points.
  if (age !== null && numComments < 25) {
    if (age > 1825) score -= 3;
    else if (age > 1095) score -= 2;
    else if (age > 730) score -= 1;
  }
  if (age !== null && age <= 365) score += 1;
  if (numComments >= 25) score += 1;

  if (score < MIN_KEEP) {
    postsRejected++;
    if (age !== null && age > 1825) rejectReasons.too_old++; else rejectReasons.off_brand++;
    continue;
  }

  scored.push({
    id: String(p.parsedId || p.id || ''),
    relevance: score,
    strong: score >= STRONG && titleHits > 0,
    title: title.slice(0, 300),
    text: body.slice(0, 800),
    score_upvotes: Number(p.score || p.upVotes || 0),
    num_comments: numComments,
    subreddit: sub,
    age_days: age,
    url: String(p.postUrl || p.url || '')
  });
}

scored.sort((x, y) => (y.relevance - x.relevance) || (y.num_comments - x.num_comments));
const keptPosts = scored.slice(0, 20);

const commentTargets = keptPosts
  .filter(p => p.strong && p.url && p.num_comments > 0)
  .slice(0, 8)
  .map(p => p.url);

return [{ json: {
  ...b,
  reddit_request_failed: requestFailed,
  reddit_error: requestFailed ? String(first.error || first.message || 'unknown').slice(0, 200) : '',
  reddit_posts: keptPosts,
  reddit_post_count: keptPosts.length,
  reddit_strong_post_count: keptPosts.filter(p => p.strong).length,
  reddit_raw_post_count: rawPosts.length,
  reddit_rejected_posts: postsRejected,
  reddit_reject_reasons: rejectReasons,
  reddit_comment_targets: commentTargets
} }];
