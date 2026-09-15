from datetime import date, timedelta

from conftest import NOW, make_item

from rankuno_brief import compose, render


def build(cfg, rows):
    content = compose.build_content(rows, cfg, NOW)
    meta = render.IssueMeta(number=7, issue_date=date(2026, 9, 14), window_start=NOW - timedelta(days=3),
                            window_end=NOW, subject=render.make_subject(cfg, date(2026, 9, 14)))
    return render.render_issue(content, meta, cfg)


def test_issue_renders_stories_escaped_and_complete(cfg):
    rows = [
        make_item(1, "AI Overviews <script>alert(1)</script> expand", "search-engine-land",
                  excerpt="Google expanded AI Overviews & AI Mode.", image="https://example.com/a.jpg"),
        make_item(2, "Google Ads adds Performance Max reports", "ppc-land"),
        make_item(3, "Is GEO real? Our data", "reddit", via="https://www.reddit.com/r/SEO/comments/1/x/",
                  url="https://blog.example.org/geo-data"),
    ]
    html_body, text_body = build(cfg, rows)

    assert "<script>alert(1)</script>" not in html_body
    assert "&lt;script&gt;" in html_body
    assert "Google expanded AI Overviews &amp; AI Mode." in html_body
    assert "ISSUE NO" not in html_body.upper() and "Issue No" not in text_body
    assert "Monday, 14 September 2026" in html_body
    assert "Found via Reddit r/SEO" in html_body
    assert "BLOG.EXAMPLE.ORG" in html_body
    assert "https://example.com/a.jpg" in html_body
    assert 'src="cid:logo"' in html_body and 'src="cid:logo_white"' in html_body

    assert "THE RANKUNO BRIEF" in text_body
    assert "Found via Reddit r/SEO: https://www.reddit.com/r/SEO/comments/1/x/" in text_body
    assert "{{" not in html_body and "{{" not in text_body


def test_coverage_label_formats():
    assert render.coverage_label(date(2026, 9, 10), date(2026, 9, 14)) == "10 – 14 September 2026"
    assert render.coverage_label(date(2026, 9, 29), date(2026, 10, 2)) == "29 September – 2 October 2026"
    assert render.coverage_label(date(2026, 12, 30), date(2027, 1, 2)) == "30 December 2026 – 2 January 2027"
