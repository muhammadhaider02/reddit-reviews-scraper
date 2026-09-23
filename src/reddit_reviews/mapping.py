"""Shape scraped posts and comments the way Stage 4 already reads the Apify actor's dataset items.

`Sort Reddit Results` reads, per post:
  dataType ('post'), title, body, communityName|subredditName, createdAt, commentsCount, score|upVotes,
  parsedId|id, postUrl|url
`Fetch Reddit Comments` reads, per comment:
  dataType ('comment'), body|text, commentCreatedAt|createdAt, score|upVotes, subredditName|communityName

`Reddit Fallback Via Web Search` reads, per comment-search result (`mention_item`):
  title, url, raw_content|content
and slices the text to 900 characters, so `raw_content` leads with the matching comments.

Keep those names stable. Extra fields are harmless.
"""

from .mentions import MentionThread
from .scraper import BASE, Comment, Post


def _url(permalink: str) -> str:
    if not permalink:
        return ""
    return permalink if permalink.startswith("http") else BASE + permalink


def post_item(p: Post) -> dict:
    return {
        "dataType": "post",
        "id": p.id,
        "parsedId": p.id.removeprefix("t3_"),
        "url": _url(p.permalink),
        "postUrl": _url(p.permalink),
        "title": p.title,
        "body": p.body,
        "bodyIsSnippet": p.body_is_snippet,
        "communityName": "r/" + p.subreddit if p.subreddit else "",
        "parsedCommunityName": p.subreddit,
        "subredditName": p.subreddit,
        "authorName": p.author,
        "createdAt": p.created_at,
        "score": p.score,
        "upVotes": p.score,
        "commentsCount": p.num_comments,
        "numberOfComments": p.num_comments,
        "nsfw": p.nsfw,
        "searchTerm": p.search_term,
    }


def comment_item(c: Comment) -> dict:
    return {
        "dataType": "comment",
        "id": c.id,
        "parsedId": c.id.removeprefix("t1_"),
        "url": _url(c.permalink),
        "postId": c.post_id,
        "parentId": c.parent_id,
        "body": c.body,
        "communityName": "r/" + c.subreddit if c.subreddit else "",
        "subredditName": c.subreddit,
        "authorName": c.author,
        "createdAt": c.created_at,
        "commentCreatedAt": c.created_at,
        "score": c.score,
        "upVotes": c.score,
        "depth": c.depth,
    }


def mention_item(t: MentionThread) -> dict:
    """One thread in the shape Tavily's `results[]` had. The node reads `title`, `url` and
    `raw_content || content`; everything else is for logs, the acceptance comparison, and a future
    where rescued threads carry real metadata (which needs the owner's sign-off, see tavily.md)."""
    comments = [c.text for c in t.comments if c.text]
    raw = "\n\n".join(comments)
    if t.body:
        raw = raw + "\n\n" + t.body if raw else t.body
    lead = comments[0] if comments else t.body
    return {
        "title": t.title,
        "url": t.url,
        "content": lead[:300],
        "raw_content": raw,
        "subreddit": t.subreddit,
        "post_id": t.post_id,
        "created_at": t.created_at,
        "score": t.score,
        "post_score": t.post_score,
        "post_comments": t.post_comments,
        "body_filled": t.body_filled,
        "matched_comments": [
            {"id": c.comment_id, "url": _url(c.permalink), "text": c.text, "score": c.score, "created_at": c.created_at, "term": c.term}
            for c in t.comments
        ],
    }
