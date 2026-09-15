from dataclasses import replace

from rankuno_brief.scoring import Scorer


def test_ai_search_story_routes_to_ai_search(cfg):
    scorer = Scorer(cfg.taxonomy)
    source = cfg.source_map["search-engine-journal"]
    result = scorer.score("What Wikipedia Reveals About AI Overviews And Web Traffic", "", source, age_days=1)
    assert result.section_id == "ai_search"
    assert "AI Overviews" in result.matched_keywords


def test_longer_keyword_is_not_double_counted(cfg):
    scorer = Scorer(cfg.taxonomy)
    source = cfg.source_map["search-engine-land"]
    result = scorer.score("Google Analytics launches customizable Dashboards", "", source, age_days=0)
    assert result.section_id == "martech_analytics"
    assert result.matched_keywords == ("Google Analytics",)


def test_hyphen_prefix_matches_but_hyphen_suffix_does_not(cfg):
    scorer = Scorer(cfg.taxonomy)
    source = cfg.source_map["search-engine-land"]
    assert scorer.score("Query length shift post-AI Mode", "", source, 0).section_id == "ai_search"
    geo_targeting = scorer.score("Better geo-targeting for campaigns", "", source, 0)
    assert "GEO" not in geo_targeting.matched_keywords


def test_broad_source_without_keywords_is_dropped(cfg):
    scorer = Scorer(cfg.taxonomy)
    assert scorer.score("Our office dog has a birthday", "", cfg.source_map["the-verge-ai"], 0) is None


def test_excluded_title_keyword_drops_story(cfg):
    scorer = Scorer(cfg.taxonomy)
    assert scorer.score("Webinar: mastering AI Overviews", "", cfg.source_map["search-engine-land"], 0) is None


def test_community_story_stays_in_community_section(cfg):
    scorer = Scorer(cfg.taxonomy)
    result = scorer.score("ChatGPT loves us, Gemini has no idea we exist", "", cfg.source_map["reddit"], 0)
    assert result.section_id == "community"


def test_evergreen_and_older_stories_score_lower(cfg):
    scorer = Scorer(cfg.taxonomy)
    source = cfg.source_map["search-engine-land"]
    news = scorer.score("Google confirms AI Mode expansion", "", source, 0).score
    how_to = scorer.score("How to optimize for AI Mode", "", source, 0).score
    older = scorer.score("Google confirms AI Mode expansion", "", source, 4).score
    assert how_to < news
    assert older < news


def test_source_weight_scales_score(cfg):
    scorer = Scorer(cfg.taxonomy)
    source = cfg.source_map["search-engine-land"]
    heavier = replace(source, weight=source.weight * 2)
    title = "Google Search Console adds report"
    assert scorer.score(title, "", heavier, 0).score > scorer.score(title, "", source, 0).score
