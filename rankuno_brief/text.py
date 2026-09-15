"""Pure text helpers: URL canonicalisation, HTML to text, excerpts and headline similarity."""

from __future__ import annotations

import hashlib
import html
import re
from html.parser import HTMLParser
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

_TRACKING_PARAMS = frozenset(
    {"fbclid", "gclid", "dclid", "msclkid", "mc_cid", "mc_eid", "igshid", "_hsenc", "_hsmi", "mkt_tok", "ref_src"}
)


def canonical_url(url: str) -> str:
    """Lower-case the scheme and host, drop tracking parameters and the fragment."""
    parts = urlsplit(url.strip())
    host = (parts.hostname or "").lower()
    netloc = host if parts.port in (None, 80, 443) else f"{host}:{parts.port}"
    query = [(key, value) for key, value in parse_qsl(parts.query, keep_blank_values=True) if not _is_tracking(key)]
    return urlunsplit(((parts.scheme or "https").lower(), netloc, parts.path or "/", urlencode(query), ""))


def url_key(url: str) -> str:
    """Stable identity for an article: ignores scheme, www., trailing slash and parameter order."""
    parts = urlsplit(canonical_url(url))
    host = parts.netloc.removeprefix("www.")
    path = parts.path.rstrip("/") or "/"
    query = urlencode(sorted(parse_qsl(parts.query, keep_blank_values=True)))
    return hashlib.sha256(f"{host}{path}?{query}".encode()).hexdigest()


def _is_tracking(key: str) -> bool:
    lowered = key.lower()
    return lowered.startswith("utm_") or lowered in _TRACKING_PARAMS


class _TextExtractor(HTMLParser):
    _SKIP = frozenset({"script", "style", "noscript"})
    _BLOCK = frozenset({"p", "br", "div", "li", "ul", "ol", "h1", "h2", "h3", "h4", "h5", "h6", "blockquote", "tr"})

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self._skip_depth = 0

    def handle_starttag(self, tag: str, attrs) -> None:
        if tag in self._SKIP:
            self._skip_depth += 1
        elif tag in self._BLOCK:
            self.parts.append(" ")

    def handle_endtag(self, tag: str) -> None:
        if tag in self._SKIP and self._skip_depth:
            self._skip_depth -= 1
        elif tag in self._BLOCK:
            self.parts.append(" ")

    def handle_data(self, data: str) -> None:
        if not self._skip_depth:
            self.parts.append(data)


def html_to_text(markup: str) -> str:
    if not markup:
        return ""
    parser = _TextExtractor()
    parser.feed(markup)
    parser.close()
    return fix_mojibake(re.sub(r"\s+", " ", html.unescape("".join(parser.parts))).strip())


# UTF-8 text that a publisher's CMS decoded as Windows-1252, e.g. "â€™" for "’" or "ðŸ¥œ" for an emoji.
_MOJIBAKE = re.compile(
    "(?:\u00c3|\u00c2|\u00e2\u20ac|\u00f0)"  # a mis-decoded UTF-8 lead byte: A-tilde, A-circumflex, a-circumflex + euro, eth
    "[\u0080-\u00bf\u0152\u0153\u0160\u0161\u0178\u017d\u017e\u0192\u02c6\u02dc"
    "\u2013\u2014\u2018-\u201e\u2020-\u2022\u2026\u2030\u2039\u203a\u20ac\u2122]+"  # followed by Windows-1252 continuation glyphs
)


def fix_mojibake(value: str) -> str:
    """Repair double-encoded text when the bytes survived; otherwise drop the garbled fragments."""
    if not _MOJIBAKE.search(value):
        return value
    try:
        return value.encode("cp1252").decode("utf-8")
    except UnicodeError:
        return re.sub(r"\s{2,}", " ", _MOJIBAKE.sub("", value)).strip()


_TITLE_BYLINE = re.compile(r"\s+via\s+@\w+(?:\s*,\s*@\w+)*\s*$", re.IGNORECASE)


def clean_title(title: str) -> str:
    """Headline without trailing social bylines such as 'via @sejournal, @MattGSouthern'."""
    return _TITLE_BYLINE.sub("", title).strip()


_BOILERPLATE = (
    re.compile(r"The post .+? appeared first on .+?$", re.IGNORECASE),
    re.compile(r"submitted by\s+/u/\S+.*$", re.IGNORECASE),
    re.compile(r"\[(?:link|comments)\]", re.IGNORECASE),
    re.compile(r"\b(?:Continue reading|Read more|Read the full (?:article|story))\b.*$", re.IGNORECASE),
    re.compile(r"\[(?:…|\.\.\.)\]"),
)


def make_excerpt(text: str, max_words: int = 60) -> str:
    """Remove feed boilerplate and shorten to about `max_words`, preferring a sentence boundary."""
    for pattern in _BOILERPLATE:
        text = pattern.sub("", text)
    text = re.sub(r"\s+", " ", text).strip()
    words = text.split()
    if len(words) <= max_words:
        return text
    clipped = " ".join(words[:max_words])
    sentence_end = max(clipped.rfind(". "), clipped.rfind("? "), clipped.rfind("! "))
    if sentence_end >= len(clipped) // 2:
        return clipped[: sentence_end + 1]
    return clipped.rstrip(",;:-–— ") + "…"


_IMG_SRC = re.compile(r"<img\b[^>]*?\bsrc\s*=\s*[\"']([^\"']+)[\"']", re.IGNORECASE)
_REDDIT_LINK = re.compile(r"<a\s+href=\"([^\"]+)\"\s*>\s*\[link\]\s*</a>", re.IGNORECASE)
_REDDIT_HOSTS = ("reddit.com", "redd.it", "redditmedia.com", "redditstatic.com")


def usable_image_url(url: str | None) -> bool:
    """Email-safe image URL: http(s) and not SVG, which Outlook and Gmail do not render."""
    if not url or not url.startswith(("http://", "https://")):
        return False
    path = urlsplit(url).path.lower()
    return not path.endswith(".svg")


def first_image(markup: str) -> str | None:
    for match in _IMG_SRC.finditer(markup or ""):
        candidate = html.unescape(match.group(1))
        if usable_image_url(candidate):
            return candidate
    return None


def reddit_external_link(markup: str) -> str | None:
    """The article a Reddit link post points to, or None for text posts and Reddit-hosted media."""
    match = _REDDIT_LINK.search(markup or "")
    if not match:
        return None
    link = html.unescape(match.group(1))
    host = host_of(link)
    if not host or host_matches(host, _REDDIT_HOSTS):
        return None
    return link


_SUBREDDIT_PATH = re.compile(r"^/r/([A-Za-z0-9_]+)/")


def reddit_community_name(url: str | None) -> str | None:
    """'Reddit r/SEO' for a Reddit thread URL, otherwise None."""
    parts = urlsplit(url or "")
    host = (parts.hostname or "").lower()
    if host != "reddit.com" and not host.endswith(".reddit.com"):
        return None
    match = _SUBREDDIT_PATH.match(parts.path)
    return f"Reddit r/{match.group(1)}" if match else None


def host_of(url: str | None) -> str:
    return (urlsplit(url or "").hostname or "").lower()


def host_matches(host: str, domains) -> bool:
    """True when `host` is one of `domains` or a subdomain of one."""
    return any(host == domain or host.endswith("." + domain) for domain in domains)


def site_name(url: str) -> str:
    """Display name for a site we know nothing else about, e.g. 'seroundtable.com'."""
    return host_of(url).removeprefix("www.")


_DISCUSSION_HOSTS = ("reddit.com", "news.ycombinator.com", "quora.com")


def is_discussion_url(url: str) -> bool:
    return host_matches(host_of(url), _DISCUSSION_HOSTS)


def unwrap_google_redirect(url: str) -> str:
    """https://www.google.com/url?...&url=<article> (used by Google Alerts) -> <article>."""
    parts = urlsplit(url)
    if host_matches((parts.hostname or "").lower(), ("google.com",)) and parts.path == "/url":
        params = dict(parse_qsl(parts.query))
        target = params.get("url") or params.get("q")
        if target and target.startswith(("http://", "https://")):
            return target
    return url


_ANCHOR = re.compile(r"<a\b[^>]*?\bhref\s*=\s*[\"']([^\"']+)[\"'][^>]*>(.*?)</a>", re.IGNORECASE | re.DOTALL)


def first_external_link(markup: str, own_domain: str) -> str | None:
    """First link in the markup that points away from the aggregator's own site."""
    for match in _ANCHOR.finditer(markup or ""):
        link = html.unescape(match.group(1))
        host = host_of(link)
        if link.startswith(("http://", "https://")) and host and not host_matches(host, (own_domain,)):
            return link
    return None


_TECHMEME_PUBLISHER = re.compile(r"<a\b[^>]*>([^<]+)</a>\s*:\s*<br", re.IGNORECASE)


def techmeme_publisher(markup: str) -> str | None:
    """Techmeme items read 'Author / <a>Publisher</a>:<br>Headline'; returns the publisher."""
    match = _TECHMEME_PUBLISHER.search(markup or "")
    return html.unescape(match.group(1)).strip() if match else None


def strip_publisher_suffix(title: str, publisher: str | None) -> str:
    """Google News titles end with ' - Publisher'."""
    if publisher:
        for separator in (" - ", " | ", " – "):
            suffix = f"{separator}{publisher}"
            if title.endswith(suffix):
                return title[: -len(suffix)].strip()
    return title


def publisher_matches(publisher: str | None, url: str, entries) -> bool:
    """Entries are publisher names (case-insensitive) or domains (anything containing a dot)."""
    name = (publisher or "").casefold()
    host = host_of(url)
    for entry in entries:
        value = entry.casefold()
        if value == name or ("." in value and host_matches(host, (value,))):
            return True
    return False


_STOPWORDS = frozenset(
    "a an the and or of to in on for with by at from is are was were be as its it this that "
    "new how what why your you will can now after over into about more than".split()
)


def title_tokens(title: str) -> frozenset[str]:
    words = re.findall(r"[a-z0-9]+(?:[.'+-][a-z0-9]+)*", title.lower())
    return frozenset(word for word in words if len(word) > 1 and word not in _STOPWORDS)


def same_story(a: frozenset[str], b: frozenset[str], threshold: float = 0.5, min_shared: int = 3) -> bool:
    """Headlines share enough distinctive words to be the same story from different publishers."""
    shared = len(a & b)
    if shared < min_shared:
        return False
    return shared / len(a | b) >= threshold
