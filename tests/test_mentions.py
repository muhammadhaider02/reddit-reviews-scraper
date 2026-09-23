"""The comment-search fallback: Reddit's own comment search in the shape the Tavily node reads."""

import threading
import time

import pytest
from conftest import FakeReddit, fixture, set_frozen

from reddit_reviews import mentions
from reddit_reviews.config import settings
from reddit_reviews.mapping import mention_item
from reddit_reviews.mentions import parse_comment_search, phrase_query, search_mentions
from reddit_reviews.scraper import ScrapeBlocked, ScrapeFailed, build_comment_search_url

HOWDYSNAX = "search_comments_howdysnax.html"
GYMSHARK = "search_comments_gymshark_reviews.html"
GYMSHARK_2 = "search_comments_gymshark_reviews_page2.html"


def url_for(term: str, primary: str | None = None) -> str:
    return build_comment_search_url(phrase_query(term, primary or term))


def comment_site(extra: dict | None = None) -> FakeReddit:
    page1 = fixture(GYMSHARK)
    pages = {
        url_for("Howdysnax"): (200, fixture(HOWDYSNAX)),
        url_for("Gymshark reviews"): (200, page1),
        parse_comment_search(page1).next_url: (200, fixture(GYMSHARK_2)),
    }
    pages.update(extra or {})
    return FakeReddit(pages)


# --------------------------------------------------------------------------- parsing


def test_comment_search_url_is_literal():
    url = build_comment_search_url("Safe Hero car escape tool")
    assert "type=comments" in url and "q=Safe+Hero+car+escape+tool" in url
    assert "disableSpellCorrection=true" in url and "sort=relevance" in url and "t=all" in url


def test_phrase_query_quotes_the_brand_and_leaves_the_rest_loose():
    """Unquoted, a three-word brand returns comments that merely contain the words; measured 23 Sep
    2026, "Kind Water Systems" had 0 of 10 carrying the phrase unquoted and 10 of 10 quoted."""
    assert phrase_query("Kind Water Systems", "Kind Water Systems") == '"Kind Water Systems"'
    assert phrase_query("Kind Water Systems water filter", "Kind Water Systems") == '"Kind Water Systems" water filter'
    assert phrase_query("Kind Water Systems reviews", "Kind Water Systems") == '"Kind Water Systems" reviews'
    assert phrase_query("Howdysnax", "Howdysnax") == '"Howdysnax"'
    assert phrase_query("Gymshark reviews", "Howdysnax") == '"Gymshark reviews"', "not built on the brand: quoted whole"
    assert phrase_query('"Already quoted"', "x") == '"Already quoted"'
    assert 'q=%22Kind+Water+Systems%22+water+filter' in build_comment_search_url(phrase_query("Kind Water Systems water filter", "Kind Water Systems"))


def test_parse_comment_search_howdysnax():
    """The three threads Tavily found for this brand, each carrying the comment that names it."""
    page = parse_comment_search(fixture(HOWDYSNAX), "Howdysnax")
    assert len(page.hits) == 3
    assert {h.subreddit for h in page.hits} == {"Protein", "OaklandFood", "office"}
    assert len({h.post_id for h in page.hits}) == 3
    assert page.next_url is None
    for h in page.hits:
        assert h.comment_id.startswith("t1_") and h.post_id.startswith("t3_")
        assert h.title and h.text and h.term == "Howdysnax"
        assert h.created_at.endswith("Z")
        assert h.permalink.endswith("/" + h.comment_id.removeprefix("t1_") + "/")
        assert h.thread_link.startswith("/r/") and "/comments/" in h.thread_link
    first = page.hits[0]
    # The comment's own words only: no author prefix, no trailing vote count, no post title.
    assert first.text.startswith("Search for Howdy") and "howdysnax.com" in first.text
    assert "Thinderella28" not in first.text and not first.text.endswith("vote")
    # Comment-level time and votes, post-level counter row.
    assert first.created_at == "2026-06-08T15:52:31.312Z" and first.score == 1
    assert first.post_score == 51 and first.post_comments == 26


def test_nested_trackers_do_not_duplicate_hits():
    html = fixture(GYMSHARK)
    page = parse_comment_search(html)
    assert len(page.hits) == html.count('data-testid="search-sdui-comment-unit"') == 7
    assert len({h.comment_id for h in page.hits}) == 7


def test_parse_comment_search_with_cursor():
    page = parse_comment_search(fixture(GYMSHARK))
    assert page.next_url.startswith("https://www.reddit.com/svc/shreddit/search/?")
    assert "type=comments" in page.next_url and "cursor=" in page.next_url and "&amp;" not in page.next_url


# --------------------------------------------------------------------------- orchestration


def test_merge_keeps_term_order_and_dedupes_threads(monkeypatch):
    # Two fixtures, two brands: the merge mechanics are under test, not the brand-as-a-word check,
    # which would rightly drop every Gymshark hit for a request whose brand is Howdysnax.
    monkeypatch.setattr(mentions, "names_brand_as_a_word", lambda text, brand: True)
    site = comment_site()
    res = search_mentions(["Howdysnax", "Gymshark reviews"], max_results=25, fetcher=site)
    assert [t.subreddit for t in res.threads[:3]] == ["Protein", "OaklandFood", "office"]
    assert res.term_counts == {"Howdysnax": (3, 3), "Gymshark reviews": (17, 16)}, "page 2 repeats one thread"
    assert len(res.threads) == 19 and len({t.post_id for t in res.threads}) == 19
    assert res.pages_fetched == 3 and len(site.calls) == 3
    assert res.failed_terms == {} and res.truncated is False
    twice = [t for t in res.threads if len(t.comments) == 2]
    assert twice, "the thread that appears on both Gymshark pages keeps both comments"
    assert all(t.first_term == "Howdysnax" for t in res.threads[:3])


def test_page_two_only_while_under_max_results():
    site = comment_site()
    res = search_mentions(["Gymshark reviews"], max_results=5, fetcher=site)
    assert len(site.calls) == 1 and res.pages_fetched == 1
    assert len(res.threads) == 5, "capped at max_results"

    site = comment_site()
    res = search_mentions(["Gymshark reviews"], max_results=25, fetcher=site)
    assert len(site.calls) == 2 and res.pages_fetched == 2 and len(res.threads) == 16


def test_page_two_stops_at_mentions_max_pages():
    site = comment_site()
    before = settings.mentions_max_pages
    set_frozen(settings, "mentions_max_pages", 1)
    try:
        res = search_mentions(["Gymshark reviews"], max_results=25, fetcher=site)
    finally:
        set_frozen(settings, "mentions_max_pages", before)
    assert len(site.calls) == 1 and len(res.threads) == 7


def test_deadline_truncates_before_page_two():
    site = comment_site()
    res = search_mentions(["Gymshark reviews"], max_results=25, fetcher=site, budget_s=0.5)
    assert len(site.calls) == 1, "page 1 is never skipped, page 2 does not fit"
    assert len(res.threads) == 7 and res.truncated is True and res.failed_terms == {}


def test_partial_failure_returns_the_other_terms(monkeypatch):
    monkeypatch.setattr(mentions, "names_brand_as_a_word", lambda text, brand: True)  # two brands, see above
    site = comment_site({url_for("Howdysnax"): (403, fixture("blocked.html"))})
    res = search_mentions(["Howdysnax", "Gymshark reviews"], max_results=25, fetcher=site)
    assert list(res.failed_terms) == ["Howdysnax"]
    assert res.term_counts["Howdysnax"] == (0, 0) and len(res.threads) == 16


def test_all_terms_blocked_raises_vendor_error():
    site = FakeReddit(default=(403, fixture("blocked.html")))
    with pytest.raises(ScrapeBlocked) as e:
        search_mentions(["Howdysnax", "Gymshark reviews"], fetcher=site)
    msg = str(e.value).lower()
    assert "404" not in msg and "not found" not in msg and "invalid url" not in msg


def test_a_refused_page_two_keeps_page_one():
    page1 = fixture(GYMSHARK)
    site = comment_site({parse_comment_search(page1).next_url: (403, fixture("blocked.html"))})
    res = search_mentions(["Gymshark reviews"], max_results=25, fetcher=site)
    assert len(res.threads) == 7 and res.failed_terms == {} and res.pages_fetched == 1


def test_search_requires_a_term():
    with pytest.raises(ValueError):
        search_mentions([" ", ""], fetcher=comment_site())


def test_at_most_three_terms_are_searched():
    site = comment_site()
    search_mentions(["Howdysnax", "Gymshark reviews", "a", "b"], max_results=25, fetcher=site)
    assert any("q=%22a%22&" in u for u in site.calls) and not any("q=%22b%22&" in u for u in site.calls)


def test_bodies_filled_from_mobile_in_one_call(mobile_on, monkeypatch):
    calls = []

    def info(ids):
        calls.append(list(ids))
        return {i: {"name": i, "selftext": f"body of {i}", "title": "t"} for i in ids}

    monkeypatch.setattr(mentions.mobile, "info", info)
    res = search_mentions(["Howdysnax"], fetcher=comment_site())
    assert res.bodies_filled == 3 and len(calls) == 1 and sorted(calls[0]) == sorted(t.post_id for t in res.threads)
    item = mention_item(res.threads[0])
    assert item["raw_content"].startswith(res.threads[0].comments[0].text)
    assert item["raw_content"].endswith("body of " + res.threads[0].post_id)
    assert item["body_filled"] is True


def test_body_fill_is_skipped_without_time_and_never_raises(mobile_on, monkeypatch):
    def slow(ids):
        time.sleep(2)
        return {}

    monkeypatch.setattr(mentions.mobile, "info", slow)
    started = time.time()
    res = search_mentions(["Howdysnax"], fetcher=comment_site(), budget_s=0.3)
    assert res.bodies_filled == 0 and res.truncated is True and len(res.threads) == 3
    assert time.time() - started < 1.5, "the fill is bounded by the budget, not by the slow route"


def test_body_fill_error_is_swallowed(mobile_on, monkeypatch):
    def boom(ids):
        raise mentions.mobile.MobileError("nope")

    monkeypatch.setattr(mentions.mobile, "info", boom)
    res = search_mentions(["Howdysnax"], fetcher=comment_site())
    assert res.bodies_filled == 0 and len(res.threads) == 3 and res.truncated is False


def test_body_fill_skipped_when_mobile_off(monkeypatch):
    called = threading.Event()
    monkeypatch.setattr(mentions.mobile, "info", lambda ids: called.set() or {})
    res = search_mentions(["Howdysnax"], fetcher=comment_site())
    assert res.bodies_filled == 0 and not called.is_set()
    assert all(t.body == "" and t.body_filled is False for t in res.threads)


def test_mention_item_shape_and_order():
    res = search_mentions(["Gymshark reviews"], max_results=25, fetcher=comment_site())
    twice = next(t for t in res.threads if len(t.comments) == 2)
    twice.comments[0].score, twice.comments[1].score = 1, 9
    twice.body = "the post body"
    item = mention_item(twice)
    assert {"title", "url", "content", "raw_content"} <= set(item)
    assert item["url"].startswith("https://www.reddit.com/r/") and item["url"].endswith("/")
    assert len(item["content"]) <= 300
    assert item["raw_content"].startswith(twice.comments[0].text) and item["raw_content"].endswith("the post body")
    assert [c["id"] for c in item["matched_comments"]] == [c.comment_id for c in twice.comments]
    assert item["matched_comments"][0]["url"].startswith("https://www.reddit.com/r/")
    assert isinstance(item["score"], int) and item["subreddit"] and item["post_id"].startswith("t3_")


def test_comments_sort_best_first():
    res = search_mentions(["Gymshark reviews"], max_results=25, fetcher=comment_site())
    twice = next(t for t in res.threads if len(t.comments) == 2)
    twice.comments[0].score, twice.comments[1].score = 1, 9
    # re-run the ordering the way search_mentions does it
    twice.comments.sort(key=lambda c: c.created_at, reverse=True)
    twice.comments.sort(key=lambda c: -c.score)
    assert twice.comments[0].score == 9


def test_all_blocked_is_a_scrape_failed_for_non_vendor_errors():
    site = FakeReddit(default=(404, "<html><title>gone</title></html>"))
    with pytest.raises(ScrapeFailed):
        search_mentions(["Howdysnax"], fetcher=site)


# --------------------------------------------------------------------------- corroboration


from reddit_reviews.mentions import Corroboration  # noqa: E402


def test_the_brand_must_be_a_word_of_its_own():
    """Execution 3335, 23 Sep 2026: Reddit matched "arq8" inside ids and a key, 5 junk threads of 8."""
    from reddit_reviews.mentions import names_brand_as_a_word

    for junk in [
        "well reviewed one near downtown: https://maps.app.goo.gl/YHvb94R9M5EcbARq8?g_st=ic (link: maps.app.goo.gl/YHvb94R9M5EcbARq8)",
        "noodled the section for a bit. (link: youtu.be/L_pUxXhArq8) If you listen to Cowboy Song",
        "Please allow me to introduce myself: https://www.youtube.com/watch?v=1LyG0S_arq8 (link: www.youtube.com/watch)",
        "Here's s link https://www.reddit.com/r/Dewalt/s/Wz2AenARQ8 (link: www.reddit.com/r/Dewalt/s/Wz2AenARQ8)",
        "Free to whoever gets it first: ?V24H-N7F24-ARQ8? ? is the missing letter",
        "ARQ8XL is the model number",
    ]:
        assert not names_brand_as_a_word(junk, "Arq8"), junk
    for real in [
        "Am using the arq8 creatine gummies. Has anyone taken creatine",
        "I got served an ad on Instagram for a gummy creatine called Arq8 that apparently",
        "Arq8's powder dissolves better", "try @arq8 on insta", "ARQ8.", "u/arq8 posted it", "(arq8)",
    ]:
        assert names_brand_as_a_word(real, "Arq8"), real
    # r/office names Howdysnax only in an href, rendered as a link host
    assert names_brand_as_a_word("apparently this shit is really good! (link: www.howdysnax.com/products/box)", "Howdysnax")
    assert names_brand_as_a_word("Lyons Leather Co, the crossbody one", "Lyons Leather Co.")
    assert names_brand_as_a_word("ordered from kindwatersystems.com last week", "Kind Water Systems")
    assert names_brand_as_a_word("the Kind-Water-Systems softener", "Kind Water Systems")
    for spelled in ["blackmask.products", "blackmask products", "blackmaskproducts.com", "BlackMask Products"]:
        assert names_brand_as_a_word(f"I bought from {spelled} last week", "blackmask.products"), spelled
    assert not names_brand_as_a_word("be kind, water the plants, check the systems", "Kind Water Systems")
    assert names_brand_as_a_word("anything", ""), "no brand: nothing to check"


def test_brand_of_is_the_words_the_terms_share():
    from reddit_reviews.mentions import brand_of

    assert brand_of(["Arq8", "Arq8 creatine gummies", "Arq8 reviews"]) == "Arq8"
    assert brand_of(["Kind Water Systems", "Kind Water Systems water softener", "Kind Water Systems reviews"]) == "Kind Water Systems"
    assert brand_of(["Gymshark reviews"]) == "Gymshark"
    assert brand_of(["Howdysnax"]) == "Howdysnax"
    assert brand_of(["Safe Hero", "car escape tool"]) == "Safe Hero", "a term that shares nothing is ignored"
    assert brand_of(["The Longevity Store", "the longevity store NAD+"]) == "The Longevity Store"


def test_search_mentions_counts_opaque_hits(monkeypatch):
    real = search_mentions(["Howdysnax"], fetcher=comment_site())
    assert real.opaque_dropped == 0 and len(real.threads) == 3, "the r/office link-only mention survives"
    # A brand the fixture's comments never name as a word: every hit is opaque, no thread is built.
    monkeypatch.setattr(mentions, "names_brand_as_a_word", lambda text, brand: False)
    none = search_mentions(["Howdysnax"], fetcher=comment_site())
    assert none.opaque_dropped == 3 and none.threads == []


def test_one_word_brands_need_no_corroboration():
    assert Corroboration.build("Howdysnax", ["snack box"], "snack box", "howdysnax.com") is None
    assert Corroboration.build("Eskiin", [], "", "") is None
    assert Corroboration.build("blackmask.products", ["charcoal face mask"], "charcoal face mask", "blackmaskproducts.com") is None


def test_the_fully_capitalised_brand_counts_anywhere_in_the_sentence():
    w = Corroboration.build("Wild Woollys", ["wool clothing", "merino wool"], "wool clothing and knitwear", "wildwoollys.com")
    assert w.holds("Wild Woollys is not a local storefront but we are a local dyer"), "sentence-initial, but every word capitalised"
    assert w.holds("Just ordered from Wild Woollys in Victoria.")
    assert not w.holds("Seven day, wild woolly and wow with Glacier Bay!")
    assert not w.holds("Wild woolly and fun!")


def test_corroboration_vocabulary_excludes_brand_and_stop_words():
    c = Corroboration.build("Kind Water Systems", ["water softener system", "reverse osmosis", "thermogenic"], "whole home water filtration system", "kindwater.com")
    assert c.first_word == "Kind" and c.domain == "kindwater.com"
    assert {"water softener", "softener system", "reverse osmosis", "water filtration", "filtration system", "thermogenic"} <= c.product_words
    assert "water" not in c.product_words and "light" not in c.product_words, "single short words are too generic"
    assert c.phrase.pattern.startswith(r"\bKind")


def test_corroboration_keeps_real_mentions_and_drops_the_phrase_as_ordinary_words():
    c = Corroboration.build("Safe Hero", ["car escape tool", "window breaker", "seatbelt cutter"], "car escape hammer", "safehero.shop")
    for junk in ["You dont wanna be a boring safe hero main do you?", "I think Xal'atath is a very safe hero.", "Stay safe Heroes and kitty.",
                 "Safe hero pick for ranked, honestly.", "stay safe hero"]:
        assert not c.holds(junk), junk
    assert c.holds("Bought the Safe Hero for my car last month, the seatbelt cutter is sharp.")
    assert c.holds("I keep an escape tool in the door pocket, the safe hero one")
    assert c.holds("ordered from safehero.shop, arrived in 3 days")

    h = Corroboration.build("Hercules Supplements", ["fat burner", "pre workout"], "fat burner supplement", "herculessupplements.com.au")
    assert h.holds("I've used Hercules supplements for awhile and got good results"), "proper noun mid-sentence"
    assert not h.holds("Hercules? never heard of them"), "opens the sentence, so the capital proves nothing"
    assert "supplement" not in h.product_words, "a plural of a brand word is still a brand word"
    assert not h.holds("fed twice a day, turmash, hercules supplement and garlic")

    l = Corroboration.build("The Longevity Store", ["longevity supplement", "NAD+ supplement"], "longevity supplement powder", "thelongevitystore.com")
    assert l.first_word == "Longevity"
    assert l.holds("AG1, IM8, the Longevity store all have money-back guarantees")
    assert not l.holds('Have the "Main Mall" where all the longevity stores are')

    e = Corroboration.build("Exodus Strong", ["red light therapy device", "red light therapy panel"], "red light therapy device", "exodusstrong.com")
    for junk in ["Exodus, strong enough of a telekinetic to move mountains", "Ideologically closest to Exodus? Strongly disagree.",
                 "Chamber. Exodus. Strong Guy. Special mention: Darwin.", "I can't recommend Metro: Exodus strongly enough",
                 "in light of all this, the exodus strong arm tactics failed"]:
        assert not e.holds(junk), junk
    assert e.holds("my Exodus Strong panel arrived, red light therapy every morning now")
    assert e.holds("I use an exodus strong device for light therapy")


def test_search_mentions_drops_uncorroborated_threads_before_the_cap(monkeypatch):
    site = comment_site()
    plain = search_mentions(["Gymshark reviews"], max_results=25, fetcher=site)
    assert plain.generic_dropped == 0, "no vocabulary: everything through"
    # Gymshark comments talk about the brand as a proper noun or about gym clothes; a vocabulary
    # that matches nothing and a lowercase-only first word leave only the proper-noun signal.
    strict = search_mentions(["Gymshark reviews"], max_results=25, fetcher=comment_site(), product_keywords=["zzzz"], primary_product="qqqq", domain="nope.example")
    assert strict.generic_dropped >= 1 and len(strict.threads) + strict.generic_dropped == len(plain.threads)
