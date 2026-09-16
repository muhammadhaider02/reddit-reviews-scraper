"""Shape scraped posts and comments the way Stage 4 already reads the Apify actor's dataset items.

`Sort Reddit Results` reads, per post:
  dataType ('post'), title, body, communityName|subredditName, createdAt, commentsCount, score|upVotes,
  parsedId|id, postUrl|url
`Fetch Reddit Comments` reads, per comment:
  dataType ('comment'), body|text, commentCreatedAt|createdAt, score|upVotes, subredditName|communityName

Keep those names stable. Extra fields are harmless.
"""

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
