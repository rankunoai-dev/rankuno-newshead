from datetime import date, timedelta

import pytest
from conftest import NOW, make_item

from rankuno_brief import compose, render
from rankuno_brief.security.content import gate_for
from rankuno_brief.security.findings import has_errors


@pytest.fixture(scope="module")
def gate(cfg):
    return gate_for(cfg)


def categories(flags, action=None):
    return {flag.category for flag in flags if action is None or flag.action == action}


# Headlines --------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "headline",
    [
        "Google expands AI Overviews to 40 more countries",
        "Site reputation abuse: Google updates its spam policy",
        "Google kills Universal Analytics data for good",
        "Democratizing AI search for small businesses",
        "Dick's Sporting Goods expands its retail media network",
        "A chink in the armor of Performance Max",
        "Should you redirect the naked domain to www?",
        "Buy now, pay later ads grow on Google Shopping",
        "Why you won't rank without E-E-A-T",
        "Essex agency wins big at the Cannes Lions awards",
        "The trump card for local SEO",
        "Here are the gory details of the March core update",
        "JavaScript: SEO best practices for 2026",
        "Scunthorpe analytics team adopts GA4 and Looker Studio",
        "Price war: Amazon Ads cuts CPCs",
    ],
)
def test_ordinary_marketing_headlines_pass(gate, headline):
    assert gate.check_text(headline) == []


@pytest.mark.parametrize(
    "headline",
    [
        "This f*cking core update",
        "sh1t rankings after the update",
        "$hit rankings after the update",
        "F U C K this algorithm",
        "fuuuuuck Google",
        "fück Google Ads",
        "fu​ck Google Ads",  # zero-width space
        "ｆｕｃｋ Google",  # full-width letters
        "fuсk Google",  # Cyrillic "с"
        "What a b!tch of an update",
        "s**t happens in PPC",
    ],
)
def test_disguised_profanity_is_blocked(gate, headline):
    assert "profanity" in categories(gate.check_text(headline), "block")


@pytest.mark.parametrize(
    ("headline", "category"),
    [
        ("Trump signs executive order on AI", "politics_religion"),
        ("Brand ads ran next to hate speech on X", "hate_extremism"),
        ("Teen suicide lawsuit names TikTok", "self_harm"),
        ("Mass shooting coverage hits publisher ad revenue", "violence_crime"),
        ("Meta relaxes rules for gambling ads", "drugs_weapons_gambling"),
        ("Make money fast with this AI trick", "spam_risk"),
    ],
)
def test_sensitive_topics_are_held_not_blocked(gate, headline, category):
    flags = gate.check_text(headline)
    assert category in categories(flags, "hold")
    assert not categories(flags, "block")


# Stories ----------------------------------------------------------------------------------------


def test_blocked_story_is_never_selected_even_if_approved(gate, cfg):
    rows = [make_item(1, "Google Ads is sh1t now", "search-engine-land"), make_item(2, "Google Ads adds reports", "ppc-land")]
    screening = gate.screen_items(rows, cfg.source_map, approved_ids={1})
    assert [row["id"] for row in screening.allowed] == [2]
    assert screening.verdicts[0].decision == "blocked"


def test_held_story_is_left_out_until_approved(gate, cfg):
    rows = [make_item(1, "Election ad spend breaks records on Google Ads", "search-engine-land")]
    held = gate.screen_items(rows, cfg.source_map)
    assert held.allowed == [] and held.verdicts[0].decision == "held"

    approved = gate.screen_items(rows, cfg.source_map, approved_ids={1})
    assert [row["id"] for row in approved.allowed] == [1]
    assert approved.verdicts[0].decision == "approved"


def test_offensive_summary_blocks_the_story(gate, cfg):
    rows = [make_item(1, "Community thread on AI Overviews", "reddit", excerpt="honestly this is bullsh1t")]
    assert gate.screen_items(rows, cfg.source_map).allowed == []


@pytest.mark.parametrize(
    ("url", "category"),
    [
        ("https://bit.ly/3abcde", "link_shortener"),
        ("https://203.0.113.9/seo-news", "unsafe_link"),
        ("javascript:alert(1)", "unsafe_link"),
        ("https://searchengineland.com@evil.example/article", "unsafe_link"),
        ("https://www.pornhub.com/insights/ai", "blocked_site"),
        ("https://example.com/2026/09/sh1t-rankings", "profanity"),
    ],
)
def test_unsafe_links_block_the_story(gate, cfg, url, category):
    rows = [make_item(1, "Google Ads adds new reports", "search-engine-land", url=url)]
    screening = gate.screen_items(rows, cfg.source_map)
    assert screening.allowed == []
    assert category in categories(screening.verdicts[0].flags, "block")


def test_lookalike_domain_is_held(gate):
    assert "lookalike_domain" in categories(gate.check_link("https://xn--gogle-0nd.com/ads"), "hold")


def test_unsafe_images_are_dropped_but_the_story_stays(gate, cfg):
    rows = [
        make_item(1, "Google Ads adds new reports", "search-engine-land", image="http://example.com/a.jpg"),
        make_item(2, "Google Ads adds more reports", "search-engine-land", image="https://bit.ly/pic.jpg"),
        make_item(3, "AI Overviews thread", "reddit", image="https://preview.redd.it/x.jpg"),
        make_item(4, "Performance Max gets asset reporting", "ppc-land", image="https://ppc.land/a.jpg"),
    ]
    images = {row["id"]: row["image_url"] for row in gate.screen_items(rows, cfg.source_map).allowed}
    assert images == {1: None, 2: None, 3: None, 4: "https://ppc.land/a.jpg"}


def test_headlines_are_tidied_for_mail_filters(gate):
    assert gate.clean_title("GOOGLE CONFIRMS SEO CORE UPDATE FOR GA4!!!") == "Google Confirms SEO Core Update for GA4!"
    assert gate.clean_title("ChatGPT search is here \U0001f680\U0001f525") == "ChatGPT search is here"
    assert gate.clean_title("AI​ Mode expands") == "AI Mode expands"
    assert gate.clean_excerpt("Google launched a report. Click here to read the full story.") == "Google launched a report."


def test_final_screen_checks_resolved_links_publishers_and_mentions(gate, cfg):
    rows = [
        make_item(1, "Google expands AI Mode to 40 more countries", "google-news-ai-search",
                  url="https://news.google.com/rss/articles/a", publisher="Reuters"),
        make_item(2, "Google Ads adds new Performance Max reports", "search-engine-land"),
    ]
    content = compose.build_content(rows, cfg, NOW)
    by_id = {story.item_id: story for story in content.stories}
    by_id[1].url = "https://tinyurl.com/resolved"  # what link resolution returned
    by_id[2].also_reported_by = [("Fine Outlet", "https://example.com/a"), ("Sh1t Outlet", "https://example.com/b")]
    by_id[2].image_url = "http://example.com/fetched.jpg"  # found by enrichment

    verdicts = gate.screen_content(content, cfg.source_map)

    assert [story.item_id for story in content.stories] == [2]
    assert verdicts[0].item_id == 1 and verdicts[0].decision == "blocked"
    assert content.stories[0].also_reported_by == [("Fine Outlet", "https://example.com/a")]
    assert content.stories[0].image_url is None


# The finished email -----------------------------------------------------------------------------


def render_issue(cfg, rows):
    content = compose.build_content(rows, cfg, NOW)
    meta = render.IssueMeta(number=1, issue_date=date(2026, 9, 14), window_start=NOW - timedelta(days=3),
                            window_end=NOW, subject=render.make_subject(cfg, date(2026, 9, 14)))
    return meta.subject, *render.render_issue(content, meta, cfg)


def test_a_normal_issue_passes_the_output_gate(gate, cfg):
    subject, html_body, text_body = render_issue(cfg, [
        make_item(1, "AI Overviews expand to 40 more countries", "search-engine-land", image="https://example.com/a.jpg"),
        make_item(2, "Google Ads adds Performance Max reports", "ppc-land"),
    ])
    assert gate.scan_email(subject, html_body, text_body) == []


@pytest.mark.parametrize(
    "tampering",
    [
        '<script src="https://example.com/x.js"></script>',
        '<img src="https://example.com/a.jpg" onerror="steal()">',
        '<a href="https://bit.ly/abc">Read</a>',
        '<a href="javascript:alert(1)">Read</a>',
        "<p>What a fucking week</p>",
        '<form action="https://example.com"><input name="password"></form>',
    ],
)
def test_output_gate_rejects_unsafe_or_offensive_email(gate, cfg, tampering):
    subject, html_body, text_body = render_issue(cfg, [make_item(1, "AI Overviews expand", "search-engine-land")])
    findings = gate.scan_email(subject, html_body.replace("</body>", tampering + "</body>"), text_body)
    assert has_errors(findings)
