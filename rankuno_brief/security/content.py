"""Content screen (every story, before selection) and output gate (the finished email).

Screening decides, for each stored item, whether it may appear:
    allowed    nothing found
    held       a "hold" term (sensitive topic); left out until an editor approves it
    approved   held, but an editor approved it
    blocked    a "block" term or an unsafe link; never published

It also tidies what is shown (no emoji, invisible characters, "!!!" or ALL-CAPS headlines), since
those read as spam to mail filters, and drops images that are not safe to embed.
"""

from __future__ import annotations

import html
import ipaddress
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from urllib.parse import unquote, urlsplit

from .. import text
from .findings import Finding, error
from .matching import TermMatcher
from .policy import BLOCK, HOLD, ContentPolicy, FilterCategory

# Link problems are decided in code rather than by word lists.
UNSAFE_LINK = FilterCategory("unsafe_link", BLOCK, ())
BLOCKED_SITE = FilterCategory("blocked_site", BLOCK, ())
LINK_SHORTENER = FilterCategory("link_shortener", BLOCK, ())
LOOKALIKE_DOMAIN = FilterCategory("lookalike_domain", HOLD, ())

_CONTROL = re.compile("[\x00-\x08\x0b\x0c\x0e-\x1f\x7f-\x9f]")
_INVISIBLE = re.compile("[­͏؜᠎​-‏‪-‮⁠-⁯﻿]")
_EMOJI = re.compile(
    "[\U0001f000-\U0001faff☀-➿⬀-⯿️⃣\U000e0020-\U000e007f]"
    "|‍"  # zero-width joiner inside emoji sequences
)
_REPEATED_MARKS = re.compile(r"([!?])[!?]+")
_CLICK_HERE = re.compile(r"(?:please\s+)?click here\b[^.!?]*[.!?]?", re.IGNORECASE)
_WORD = re.compile(r"[^\W_][\w'’+.&-]*")
_SMALL_WORDS = frozenset("a an and as at but by for from in into is of on or the to vs via with".split())
_DEFAULT_CASING = (
    "AI API B2B B2C CEO CMO CPA CPC CPM CRM CTR CTV D2C DOJ EU FAQ FTC GDPR GEO AEO HTML iOS IPO KPI LLM "
    "LLMs PPC PR ROAS ROI SaaS SEO SERP SERPs SMB TV UK URL UX UI"
).split()

_URL_ATTRIBUTE = re.compile(r"""\b(href|src|background|action)\s*=\s*(["'])(.*?)\2""", re.IGNORECASE | re.DOTALL)
_ACTIVE_CONTENT = (
    (re.compile(r"<script\b", re.IGNORECASE), "a script"),
    (re.compile(r"<i?frame\b", re.IGNORECASE), "an embedded frame"),
    (re.compile(r"<(?:object|embed|applet)\b", re.IGNORECASE), "an embedded object"),
    (re.compile(r"<(?:form|input|button|textarea|select)\b", re.IGNORECASE), "a form"),
    (re.compile(r"<meta\b[^>]*http-equiv\s*=\s*[\"']?refresh", re.IGNORECASE), "an automatic redirect"),
    (re.compile(r"<base\b", re.IGNORECASE), "a base-address override"),
    (re.compile(r"<link\b", re.IGNORECASE), "an external resource link"),
    (re.compile(r"<[a-z][^>]*\son[a-z]+\s*=", re.IGNORECASE), "an event-handler attribute"),
)
# Only looked for inside <style> blocks and style="" attributes, so headlines about CSS are fine.
_STYLE_BLOCK = re.compile(r"<style\b[^>]*>(.*?)</style>|\bstyle\s*=\s*([\"'])(.*?)\2", re.IGNORECASE | re.DOTALL)
_ACTIVE_STYLE = re.compile(r"expression\s*\(|@import|behavior\s*:|(?:javascript|vbscript)\s*:", re.IGNORECASE)


@dataclass(frozen=True)
class Flag:
    category: str
    action: str  # block | hold
    where: str  # headline | summary | link | discussion link | image | text
    term: str

    def describe(self) -> str:
        return f'{self.category} in {self.where}: "{self.term}"'


@dataclass
class Verdict:
    item_id: int
    title: str
    source_id: str
    flags: list[Flag] = field(default_factory=list)
    approved: bool = False

    @property
    def decision(self) -> str:
        if any(flag.action == BLOCK for flag in self.flags):
            return "blocked"
        if self.flags:
            return "approved" if self.approved else "held"
        return "allowed"

    @property
    def reasons(self) -> str:
        return "; ".join(flag.describe() for flag in self.flags)


@dataclass
class Screening:
    allowed: list[dict]  # cleaned rows, ready for compose.build_content
    verdicts: list[Verdict]  # every item that was flagged, whatever the decision

    def with_decision(self, decision: str) -> list[Verdict]:
        return [verdict for verdict in self.verdicts if verdict.decision == decision]


class ContentGate:
    def __init__(self, policy: ContentPolicy, known_words: Iterable[str] = ()) -> None:
        self.policy = policy
        self._matcher = TermMatcher(policy.categories, policy.allow_phrases)
        self._block_matcher = TermMatcher(
            [category for category in policy.categories if category.action == BLOCK], policy.allow_phrases
        )
        self._casing = {word.lower(): word for word in _DEFAULT_CASING}
        for phrase in known_words:
            for word in phrase.split():
                if any(char.isupper() for char in word[1:]):  # SEO, ChatGPT, GA4, YouTube
                    self._casing[word.lower()] = word

    # Checks -------------------------------------------------------------------------------------

    def check_text(self, value: str, where: str = "text") -> list[Flag]:
        return [Flag(hit.category.id, hit.category.action, where, hit.term) for hit in self._matcher.find(value)]

    def check_link(self, url: str | None, where: str = "link") -> list[Flag]:
        """Unsafe scheme or host, blocked site, link shortener, look-alike domain, or listed words in the address."""
        if not url:
            return []
        parts = urlsplit(url.strip())
        scheme = parts.scheme.lower()
        if scheme not in ("http", "https"):
            return [_flag(UNSAFE_LINK, where, f"{scheme or 'missing'} address scheme")]
        try:
            host = (parts.hostname or "").rstrip(".")
        except ValueError:
            host = ""
        if not host:
            return [_flag(UNSAFE_LINK, where, "address without a host")]

        flags = []
        if parts.username or parts.password:
            flags.append(_flag(UNSAFE_LINK, where, "address hides its real host behind '@'"))
        if _is_ip_address(host):
            flags.append(_flag(UNSAFE_LINK, where, f"raw IP address {host}"))
        if _on_domain_list(host, self.policy.blocked_domains):
            flags.append(_flag(BLOCKED_SITE, where, host))
        if _on_domain_list(host, self.policy.url_shorteners):
            flags.append(_flag(LINK_SHORTENER, where, host))
        if any(label.startswith("xn--") for label in host.split(".")):
            flags.append(_flag(LOOKALIKE_DOMAIN, where, host))

        # Words in the address itself, e.g. example.com/2026/09/some-offensive-slug
        host_words = " ".join(host.split(".")[:-1])
        path_words = re.sub(r"[/\-_.+=&?%~:]+", " ", unquote(f"{parts.path} {parts.query}"))
        flags.extend(self.check_text(f"{host_words} {path_words}", where))
        return flags

    def safe_image(self, url: str | None, *, community: bool) -> str | None:
        """The image URL if it is safe to show in an email, otherwise None (the story keeps its text)."""
        if not url or (community and not self.policy.community_images):
            return None
        if not url.startswith("https://") or not text.usable_image_url(url):
            return None  # plain http images trigger mixed-content and spam warnings
        return None if self.check_link(url, "image") else url

    # Stories ------------------------------------------------------------------------------------

    def screen_items(self, rows: Iterable[Mapping], sources: Mapping, approved_ids: Iterable[int] = ()) -> Screening:
        approved = set(approved_ids)
        allowed: list[dict] = []
        verdicts: list[Verdict] = []
        for row in rows:
            source = sources.get(row["source_id"])
            title = self.clean_title(row["title"])
            if not title:
                continue
            excerpt = self.clean_excerpt(row["excerpt"] or "")
            verdict = Verdict(item_id=row["id"], title=title, source_id=row["source_id"], approved=row["id"] in approved)
            verdict.flags.extend(self.check_text(title, "headline"))
            verdict.flags.extend(self.check_text(excerpt, "summary"))
            verdict.flags.extend(self.check_link(row["url"], "link"))
            verdict.flags.extend(self.check_link(row["discovered_via"], "discussion link"))

            if verdict.flags:
                verdicts.append(verdict)
            if verdict.decision in ("allowed", "approved"):
                community = bool(source is not None and source.community)
                allowed.append(
                    {
                        **dict(row),
                        "title": title,
                        "excerpt": excerpt,
                        "image_url": self.safe_image(row["image_url"], community=community),
                    }
                )
        return Screening(allowed=allowed, verdicts=verdicts)

    def clean_title(self, title: str) -> str:
        value = _tidy(title)
        letters = [char for char in value if char.isalpha()]
        if len(letters) >= 12 and sum(char.isupper() for char in letters) / len(letters) >= 0.8:
            value = self._recase(value)
        return value

    def clean_excerpt(self, excerpt: str) -> str:
        return _tidy(_CLICK_HERE.sub("", _tidy(excerpt)))

    def _recase(self, shouted: str) -> str:
        """'GOOGLE CONFIRMS SEO CORE UPDATE!' -> 'Google Confirms SEO Core Update!'"""
        position = 0

        def recase(match: re.Match[str]) -> str:
            nonlocal position
            word = match.group()
            position += 1
            if word.lower() in self._casing:
                return self._casing[word.lower()]
            if any(char.isdigit() for char in word):
                return word  # GA4, Q3, 2026
            if position > 1 and word.lower() in _SMALL_WORDS:
                return word.lower()
            return word[:1].upper() + word[1:].lower()

        return _WORD.sub(recase, shouted)

    # The finished email -------------------------------------------------------------------------

    def scan_email(self, subject: str, html_body: str, text_body: str) -> list[Finding]:
        """Output gate: blocked terms anywhere in the email, active content, and unsafe links or images."""
        findings: list[Finding] = []
        for label, content in (
            ("subject", subject),
            ("email", text.html_to_text(html_body)),
            ("plain-text version", text_body),
        ):
            for hit in self._block_matcher.find(content):
                findings.append(error("content", f'Blocked term ({hit.category.id}) "{hit.term}" in the {label}'))

        for pattern, what in _ACTIVE_CONTENT:
            if pattern.search(html_body):
                findings.append(error("content", f"The email contains {what}, which is never allowed"))
        for block, _, attribute in _STYLE_BLOCK.findall(html_body):
            if _ACTIVE_STYLE.search(block or attribute):
                findings.append(error("content", "The email contains script-like styling, which is never allowed"))
                break

        for attribute, _, raw_url in _URL_ATTRIBUTE.findall(html_body):
            url = html.unescape(raw_url).strip()
            scheme = urlsplit(url).scheme.lower()
            if scheme in ("cid", "mailto"):
                continue
            for flag in self.check_link(url, attribute.lower()):
                if flag.action == BLOCK:
                    findings.append(error("content", f"Unsafe {attribute.lower()} ({flag.category}: {flag.term}): {url[:100]}"))
        return list(dict.fromkeys(findings))


def gate_for(cfg) -> ContentGate:
    """The content gate for a loaded Config."""
    return ContentGate(
        cfg.security.content,
        known_words=[keyword for section in cfg.taxonomy.sections for keyword in section.keywords],
    )


def _flag(category: FilterCategory, where: str, term: str) -> Flag:
    return Flag(category.id, category.action, where, term)


def _tidy(value: str) -> str:
    value = _CONTROL.sub(" ", value or "")
    value = _INVISIBLE.sub("", value)
    value = _EMOJI.sub("", value)
    value = _REPEATED_MARKS.sub(r"\1", value)
    return re.sub(r"\s+", " ", value).strip()


def _is_ip_address(host: str) -> bool:
    try:
        ipaddress.ip_address(host.strip("[]"))
    except ValueError:
        return False
    return True


def _on_domain_list(host: str, domains: Iterable[str]) -> bool:
    host = host.lower()
    return any(host == domain or host.endswith("." + domain) for domain in domains)
