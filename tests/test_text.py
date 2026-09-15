from rankuno_brief import text


def test_canonical_url_drops_tracking_parameters_and_fragment():
    url = "HTTPS://SearchEngineLand.com/story-123/?utm_source=rss&utm_medium=feed&page=2&fbclid=abc#comments"
    assert text.canonical_url(url) == "https://searchengineland.com/story-123/?page=2"


def test_url_key_ignores_scheme_www_trailing_slash_and_parameter_order():
    first = text.url_key("http://www.example.com/a/?b=2&a=1")
    second = text.url_key("https://example.com/a?a=1&b=2&utm_campaign=x")
    assert first == second
    assert first != text.url_key("https://example.com/b")


def test_html_to_text_strips_markup_scripts_and_entities():
    markup = "<p>Google&#8217;s <b>AI Mode</b></p><script>alert(1)</script><p>expands</p>"
    assert text.html_to_text(markup) == "Google’s AI Mode expands"


def test_fix_mojibake_repairs_or_removes_double_encoded_text():
    assert text.fix_mojibake("Googleâ€™s update") == "Google’s update"
    assert text.fix_mojibake('says "...nuts? ð¥"') == 'says "...nuts? "'
    assert text.fix_mojibake("César de la Fuente – plain text") == "César de la Fuente – plain text"


def test_make_excerpt_removes_feed_boilerplate():
    raw = "Google confirmed the update. The post Core update rolls out appeared first on Search Engine Land."
    assert text.make_excerpt(raw) == "Google confirmed the update."
    reddit = "Is AI traffic worth tracking? submitted by /u/someone [link] [comments]"
    assert text.make_excerpt(reddit) == "Is AI traffic worth tracking?"


def test_make_excerpt_cuts_at_sentence_boundary_when_possible():
    raw = ("One two three four five six. " * 6).strip()
    excerpt = text.make_excerpt(raw, max_words=20)
    assert excerpt.endswith(".")
    assert len(excerpt.split()) <= 20


def test_clean_title_removes_social_byline():
    assert text.clean_title("What Wikipedia Reveals via @sejournal, @MattGSouthern") == "What Wikipedia Reveals"
    assert text.clean_title("Email via Outlook") == "Email via Outlook"


def test_reddit_external_link_only_returns_off_site_articles():
    link_post = '<a href="https://searchengineland.com/x">[link]</a> <a href="https://www.reddit.com/r/SEO/c">[comments]</a>'
    assert text.reddit_external_link(link_post) == "https://searchengineland.com/x"
    self_post = '<a href="https://www.reddit.com/r/SEO/comments/1/x/">[link]</a>'
    assert text.reddit_external_link(self_post) is None
    image_post = '<a href="https://i.redd.it/abc.png">[link]</a>'
    assert text.reddit_external_link(image_post) is None


def test_reddit_community_name():
    assert text.reddit_community_name("https://www.reddit.com/r/PPC/comments/1/x/") == "Reddit r/PPC"
    assert text.reddit_community_name("https://searchengineland.com/r/PPC/") is None


def test_usable_image_url_rejects_svg_and_non_http():
    assert text.usable_image_url("https://example.com/a.jpg?w=600")
    assert not text.usable_image_url("https://example.com/logo.svg")
    assert not text.usable_image_url("data:image/png;base64,abc")
    assert not text.usable_image_url(None)


def test_same_story_matches_rewordings_but_not_topic_neighbours():
    a = text.title_tokens("Google Search Console Indexing report missing June data")
    b = text.title_tokens("Google Search Console Indexing Report Missing Old June Data")
    c = text.title_tokens("Google Search Console adds new Insights report")
    assert text.same_story(a, b)
    assert not text.same_story(a, c)
