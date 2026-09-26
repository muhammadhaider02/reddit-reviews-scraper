// Fetch Reddit Comments. Step 2 of 2 (processing only): filter the comments an
// upstream HTTP node fetched for the strong threads, and set reddit_outcome. The
// fetch itself is an HTTP node because a Code node cannot use a stored credential.
const b = $('Sort Reddit Results').first().json;
const items = $input.all().map(i => i.json);
const targets = b.reddit_comment_targets || [];

const tokens = (b.brand_match_tokens || []).map(t => String(t || '').toLowerCase()).filter(Boolean);
const norm = (s) => String(s || '').toLowerCase().replace(/[^a-z0-9]+/g, ' ');
const tight = (s) => String(s || '').toLowerCase().replace(/[^a-z0-9]/g, '');
const brandHits = (txt) => {
  const t = norm(txt); const tt = tight(txt);
  let n = 0;
  for (const tok of tokens) {
    const nt = norm(tok).trim(); const ntt = tight(tok);
    if (!nt) continue;
    if (nt.length >= 3) n += (t.split(nt).length - 1);
    else if (ntt && ntt.length >= 3) n += (tt.split(ntt).length - 1);
  }
  return n;
};

// The junk filter. A deleted comment or a five-word "same here" adds nothing and
// crowds out real customer language.
const isJunk = (txt) => {
  const t = String(txt || '').trim();
  if (!t) return true;
  if (/^\[(removed|deleted)\]$/i.test(t)) return true;
  if (t.split(/\s+/).length < 6) return true;
  return false;
};

// Comment records use a different date field from posts, so try every
// plausible name.
const ageDays = (r) => {
  const raw = r.commentCreatedAt || r.createdAt || r.created_at || r.createdAtISO || r.timestamp;
  const d = Date.parse(raw);
  if (!d) return null;
  return Math.round((Date.now() - d) / 86400000);
};

const firstItem = items[0] || {};
const commentFetchFailed = targets.length > 0 && items.length === 1 && (!!firstItem.error || !!firstItem.message) && !firstItem.dataType;

const comments = [];
let commentsRejected = 0;
let rawCommentCount = 0;

if (!commentFetchFailed) {
  for (const c of items) {
    if (!c || typeof c !== 'object') continue;
    if (String(c.dataType || '').toLowerCase() !== 'comment') continue;
    rawCommentCount++;

    const text = String(c.body || c.text || '').replace(/\s+/g, ' ').trim();
    if (isJunk(text)) { commentsRejected++; continue; }

    const age = ageDays(c);
    if (age !== null && age > 1825) { commentsRejected++; continue; }

    comments.push({
      text: text.slice(0, 800),
      score_upvotes: Number(c.score || c.upVotes || 0),
      subreddit: String(c.subredditName || c.communityName || '').replace(/^\/?r\//, ''),
      age_days: age,
      names_brand: brandHits(text) > 0
    });
  }
}

// Comments naming the brand outrank ones relevant only by context.
comments.sort((x, y) => (Number(y.names_brand) - Number(x.names_brand)) || (y.score_upvotes - x.score_upvotes));
// The source returns up to 20 comments per post across 8 threads (160), so the
// cap sits above that.
const keptComments = comments.slice(0, 200);

const posts = b.reddit_posts || [];
const strongCount = b.reddit_strong_post_count || 0;
const subreddits = [...new Set([...posts, ...keptComments].map(x => x.subreddit).filter(Boolean))].slice(0, 12);
const keptTotal = posts.length + keptComments.length;

// A failed comment fetch is NOT a failed brand - we still have the posts.
let outcome;
if (b.reddit_request_failed) outcome = 'request_failed';
else if ((b.reddit_raw_post_count || 0) === 0) outcome = 'no_results';
else if (posts.length === 0) outcome = 'all_irrelevant';
else if (strongCount === 0 || keptTotal < 8) outcome = 'thin';
else outcome = 'ok';

return [{ json: {
  ...b,
  reddit_outcome: outcome,
  reddit_has_data: keptTotal > 0,
  reddit_comments: keptComments,
  reddit_comment_count: keptComments.length,
  reddit_raw_comment_count: rawCommentCount,
  reddit_rejected_comments: commentsRejected,
  reddit_comment_fetch_failed: commentFetchFailed,
  reddit_comment_error: commentFetchFailed ? String(firstItem.error || firstItem.message || 'unknown').slice(0, 200) : '',
  reddit_threads_read: targets.length,
  reddit_subreddits: subreddits
} }];
