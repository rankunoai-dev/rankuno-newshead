from dataclasses import replace

from conftest import NOW, make_item

from rankuno_brief import compose


def test_same_story_from_two_publishers_is_merged_and_boosted(cfg):
    rows = [
        make_item(1, "Google Search Console Indexing report missing June data", "search-engine-land"),
        make_item(2, "Google Search Console Indexing Report Missing Old June Data", "search-engine-roundtable"),
    ]
    content = compose.build_content(rows, cfg, NOW)
    assert content.story_count == 1
    story = content.stories[0]
    assert story.also_reported_by == [("Search Engine Roundtable", "https://example.com/2")]


def test_top_stories_come_from_different_sections(cfg):
    rows = [
        make_item(1, "Google confirms March core update rollout", "search-engine-land"),
        make_item(2, "Google Search Console adds Insights report", "search-engine-journal"),
        make_item(3, "AI Overviews expand to 40 more countries", "search-engine-roundtable"),
        make_item(4, "Performance Max gets new asset reporting", "ppc-land"),
    ]
    content = compose.build_content(rows, cfg, NOW)
    sections = [story.section_id for story in content.top_stories]
    assert len(sections) == len(set(sections))


def test_community_stories_never_become_top_stories(cfg):
    rows = [make_item(1, "AI Overviews destroyed our traffic, what now?", "reddit")]
    content = compose.build_content(rows, cfg, NOW)
    assert content.top_stories == []
    assert content.sections[0].id == "community"


def test_per_source_limit_is_applied(cfg):
    small_cap = replace(cfg, issue=replace(cfg.issue, max_per_source=2, top_stories=0))
    titles = ["Google Ads tests new bidding", "Performance Max adds reports", "Merchant Center feed rules",
              "Microsoft Advertising expands", "Meta Ads Advantage+ update"]
    rows = [make_item(i, title, "search-engine-land") for i, title in enumerate(titles, start=1)]
    content = compose.build_content(rows, small_cap, NOW)
    assert content.story_count == 2


def test_reddit_link_post_keeps_article_url_and_discussion_link(cfg):
    rows = [make_item(1, "Google Testing Thinner AI Overview Citations Panel", "reddit",
                      url="https://www.seroundtable.com/ai-overview-citations.html",
                      via="https://www.reddit.com/r/SEO/comments/abc/thread/")]
    story = compose.build_content(rows, cfg, NOW).stories[0]
    assert story.url == "https://www.seroundtable.com/ai-overview-citations.html"
    assert story.source_name == "seroundtable.com"
    assert (story.via_name, story.via_url) == ("Reddit r/SEO", "https://www.reddit.com/r/SEO/comments/abc/thread/")
    assert not story.is_discussion


def test_reddit_text_post_is_the_discussion_itself(cfg):
    rows = [make_item(1, "ChatGPT loves us, Gemini has no idea we exist", "reddit",
                      url="https://www.reddit.com/r/SEO/comments/xyz/chatgpt_loves_us/")]
    story = compose.build_content(rows, cfg, NOW).stories[0]
    assert (story.source_name, story.via_name, story.via_url) == ("Reddit r/SEO", None, None)
    assert story.is_discussion


def test_google_news_story_credits_publisher_and_google_news(cfg):
    rows = [make_item(1, "Google expands AI Mode to 40 more countries", "google-news-ai-search",
                      url="https://news.google.com/rss/articles/CBMiabc?oc=5", publisher="Reuters")]
    story = compose.build_content(rows, cfg, NOW).stories[0]
    assert (story.source_name, story.via_name, story.via_url) == ("Reuters", "Google News", None)


def test_hacker_news_link_story_links_the_thread(cfg):
    rows = [make_item(1, "OpenAI launches a new ChatGPT search mode", "hacker-news",
                      url="https://openai.com/index/new-search", via="https://news.ycombinator.com/item?id=1")]
    story = compose.build_content(rows, cfg, NOW).stories[0]
    assert (story.source_name, story.via_name) == ("openai.com", "Hacker News")
    assert story.via_url == "https://news.ycombinator.com/item?id=1"


def test_google_news_story_from_unknown_publisher_needs_wider_coverage(cfg):
    lone = [make_item(1, "New research compares GEO agencies for AI Overviews", "google-news-ai-search",
                      url="https://news.google.com/rss/articles/a", publisher="The National Tribune")]
    assert compose.build_content(lone, cfg, NOW).story_count == 0

    trusted = [make_item(1, "Google expands AI Overviews to 40 more countries", "google-news-ai-search",
                         url="https://news.google.com/rss/articles/b", publisher="Reuters")]
    assert compose.build_content(trusted, cfg, NOW).story_count == 1

    widely_covered = [
        make_item(1, "Google expands AI Overviews to 40 more countries", "google-news-ai-search",
                  url="https://news.google.com/rss/articles/c", publisher="Some Local Paper"),
        make_item(2, "Google expands AI Overviews to 40 more countries today", "google-news-seo",
                  url="https://news.google.com/rss/articles/d", publisher="Another Outlet"),
    ]
    assert compose.build_content(widely_covered, cfg, NOW).story_count == 1


def test_trusted_version_leads_a_merged_story(cfg):
    rows = [
        make_item(1, "Anthropic pitches new Claude tool for financial advisers", "google-news-ai-platforms",
                  url="https://news.google.com/rss/articles/a", publisher="AdvisorHub"),
        make_item(2, "Anthropic pitches new Claude tool for financial advisers", "google-news-ai-search",
                  url="https://news.google.com/rss/articles/b", publisher="Reuters"),
    ]
    story = compose.build_content(rows, cfg, NOW).stories[0]
    assert story.source_name == "Reuters"
    assert story.also_reported_by == [("AdvisorHub", "https://news.google.com/rss/articles/a")]


def test_merge_does_not_credit_a_publisher_to_itself(cfg):
    rows = [
        make_item(1, "Google Search Console Indexing report missing June data", "search-engine-land"),
        make_item(2, "Google Search Console Indexing report missing June data", "google-news-seo",
                  url="https://news.google.com/rss/articles/x", publisher="Search Engine Land"),
    ]
    content = compose.build_content(rows, cfg, NOW)
    assert content.story_count == 1
    assert content.stories[0].also_reported_by == []


def test_drop_duplicate_urls_after_links_are_resolved(cfg):
    rows = [
        make_item(1, "Google confirms September core update", "search-engine-land", url="https://example.com/core"),
        make_item(2, "Core update rolls out to all users worldwide today", "google-news-seo",
                  url="https://news.google.com/rss/articles/x", publisher="Search Engine Land"),
    ]
    content = compose.build_content(rows, cfg, NOW)
    assert content.story_count == 2
    next(story for story in content.stories if story.item_id == 2).url = "https://example.com/core/"
    assert compose.drop_duplicate_urls(content) == 1
    assert [story.item_id for story in content.stories] == [1]


def test_items_from_removed_sources_are_ignored(cfg):
    rows = [make_item(1, "AI Overviews update", "a-source-that-no-longer-exists")]
    assert compose.build_content(rows, cfg, NOW).story_count == 0
