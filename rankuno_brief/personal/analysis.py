"""Key takeaways, suggested actions, and mentions of clients and competitors, from the text of the articles.

No AI is used. Takeaways are the article's own sentences that contain the most client-relevant terms. Suggested
actions follow fixed rules. Mentions are found by matching names in the article text, and an article "links" to a
client when it links to the client's website.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field

from .. import text
from . import articles, matching

SENTENCE_BREAK = re.compile(r"(?<=[.!?])\s+(?=[A-Z0-9\"'“‘])")
TIER_WEIGHTS = {"name": 3.0, "competitor": 3.0, "core": 2.0, "operational": 1.5, "broad": 0.5}
MIN_SENTENCE, MAX_SENTENCE = 45, 320
TAKEAWAYS = 3
SNIPPET_WIDTH = 110


@dataclass(frozen=True)
class Mention:
    kind: str  # linked | unlinked | unknown (no website on file to check) | competitor
    term: str  # the client name or alias, or the competitor
    publisher: str
    title: str
    url: str
    snippet: str


@dataclass
class ClientFindings:
    mentions: list[Mention] = field(default_factory=list)
    competitor_watch: list[Mention] = field(default_factory=list)


def _tiers(terms: matching.Terms) -> list[tuple[str, tuple[str, ...]]]:
    return [("name", terms.names), ("competitor", terms.competitors), ("core", terms.core),
            ("operational", terms.operational), ("broad", terms.broad)]


def sentences(body: str) -> list[str]:
    return [part.strip() for part in SENTENCE_BREAK.split(body or "") if MIN_SENTENCE <= len(part.strip()) <= MAX_SENTENCE]


def takeaways(body: str, terms: matching.Terms, count: int = TAKEAWAYS) -> tuple[str, ...]:
    """The sentences that carry the most client-relevant terms, in the order they appear in the article."""
    scored = []
    for index, sentence in enumerate(sentences(body)):
        score = sum(TIER_WEIGHTS[tier] * len(set(matching._hits(group, sentence))) for tier, group in _tiers(terms))
        if index < 2:
            score += 0.3  # the opening sentences usually say what the article is about
        scored.append((score, index, sentence))
    relevant = [entry for entry in scored if entry[0] > 0.3]
    if not relevant:
        relevant = scored[:2]  # nothing matched: fall back to the opening sentences
    top = sorted(relevant, key=lambda entry: -entry[0])[:count]
    return tuple(sentence for _, _, sentence in sorted(top, key=lambda entry: entry[1]))


def suggested_actions(title: str, body: str, terms: matching.Terms, *, client_name: str, content_type: str,
                      strategic: bool) -> tuple[str, ...]:
    """One or two plain actions for the account team, from fixed rules."""
    actions: list[str] = []
    if content_type == "sponsored":
        actions.append("Sponsored content: read it as the sponsor's message, not as independent reporting.")
    elif content_type == "vendor":
        actions.append("Vendor content: check its claims, and note which company is behind it.")
    competitors = matching._hits(terms.competitors, f"{title} {body}")
    if competitors:
        actions.append(f"Competitor activity ({', '.join(competitors[:2])}): compare it with {client_name}'s positioning and talk track.")
    if matching._hits(terms.names, body) and not matching._hits(terms.names, title):
        actions.append(f"{client_name} is named in the article but not in the headline: check how it is described.")
    core = matching._hits(terms.core, f"{title} {body}")
    operational = matching._hits(terms.operational, f"{title} {body}")
    if core and strategic:
        actions.append(f"Core topic ({core[0]}): consider a sales or content angle for {client_name}.")
    elif operational:
        actions.append(f"Operating trend ({operational[0]}): relevant to {client_name}'s solution areas.")
    if not actions:
        actions.append("Background reading for the account team.")
    return tuple(dict.fromkeys(actions))[:2]


def snippet_around(body: str, term: str) -> str:
    found = matching._pattern(term).search(body or "")
    if not found:
        return ""
    start = max(0, found.start() - SNIPPET_WIDTH)
    end = min(len(body), found.end() + SNIPPET_WIDTH)
    return ("…" if start else "") + body[start:end].strip() + ("…" if end < len(body) else "")


def _linked_to(links: Iterable[str], website_host: str) -> bool:
    return any(text.host_matches(text.host_of(link), (website_host,)) for link in links)


def find_mentions(item: Mapping, article: articles.ArticleText, terms: matching.Terms, website: str) -> list[Mention]:
    """The client's name in the article, and whether the article links to the client's website."""
    body = " ".join(part for part in (item["excerpt"] or "", article.body) if part)
    host = text.host_of(website) if website else ""
    found = []
    for term in dict.fromkeys(matching._hits(terms.names, body)):
        if host:
            kind = "linked" if _linked_to(article.links, host) else "unlinked"
        else:
            kind = "unknown"
        found.append(Mention(kind, term, item["publisher"] or text.site_name(item["url"]), item["title"], item["url"],
                             snippet_around(body, term)))
    return found


def find_competitors(item: Mapping, article: articles.ArticleText, terms: matching.Terms) -> list[Mention]:
    body = " ".join(part for part in (item["excerpt"] or "", article.body) if part)
    return [
        Mention("competitor", term, item["publisher"] or text.site_name(item["url"]), item["title"], item["url"],
                snippet_around(body, term))
        for term in dict.fromkeys(matching._hits(terms.competitors, f"{item['title']} {body}"))
    ]


def scan(clients: Mapping[int, tuple[Mapping, matching.Terms, str]], pool: list[dict],
         read: Mapping[int, articles.ArticleText]) -> dict[int, ClientFindings]:
    """Mentions and competitor watch for each client, from the articles already read.
    `clients` maps client id -> (client row, terms, website). `read` maps item id -> article text."""
    findings = {client_id: ClientFindings() for client_id in clients}
    for item in pool:
        article = read.get(item["id"])
        if article is None:
            continue
        for client_id, (_, terms, website) in clients.items():
            found = findings[client_id]
            found.mentions.extend(find_mentions(item, article, terms, website))
            found.competitor_watch.extend(find_competitors(item, article, terms))
    return findings


def candidates(pool: list[dict], clients: Mapping[int, tuple[Mapping, matching.Terms, str]]) -> list[dict]:
    """News worth reading for mentions: any headline or summary that touches a client's names, competitors or topics."""
    terms_all = [term for _, terms, _ in clients.values() for group in (terms.names, terms.competitors, terms.core,
                                                                          terms.operational, terms.broad) for term in group]
    return [item for item in pool if matching._hits(terms_all, f"{item['title']} {item['excerpt'] or ''}")]
