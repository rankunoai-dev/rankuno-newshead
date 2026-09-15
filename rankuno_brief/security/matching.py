"""Finds listed terms in text, including disguised spellings.

Text is compared in several normalised forms, so common tricks do not get through:

    accents, full-width, look-alikes  fück, ｆｕｃｋ, fuсk (Cyrillic с)  -> fuck
    invisible characters              fu<zero-width space>ck          -> fuck
    leetspeak                         sh1t, $hit, b!tch, fvck         -> shit, shit, bitch, fuck
    spaced letters                    f u c k, f.u.c.k                -> fuck
    stretched letters                 fuuuuck                         -> fuck
    masked letters                    f*ck, sh#t, f***ing             -> fuck, shit, fucking

Terms always match whole words, so "analytics" or "Scunthorpe" never trip a shorter term.
Single words are looked up in a set; only phrases and symbol terms ("100% free") use regular expressions.
"""

from __future__ import annotations

import re
import unicodedata
from collections import defaultdict
from collections.abc import Iterable
from dataclasses import dataclass

from .policy import BLOCK, FilterCategory

_INVISIBLE = re.compile(
    "[­͏؜ᅟᅠ឴឵᠋-᠏​-‏‪-‮"
    "⁠-⁯ㅤ︀-️﻿ﾠ\U000e0000-\U000e007f]"
)
_DASHES = "֊־᐀᠆‐‑‒–—―−⸗⸚⸺⸻⹀〜〰﹘﹣－"
_PUNCTUATION = str.maketrans({**{char: "'" for char in "‘’ʼ′`"}, **{char: "-" for char in _DASHES}})
# Cyrillic and Greek letters that look like Latin ones (lower case; text is case-folded first).
_LOOKALIKES = str.maketrans("авекмнорстухіјѕԁɡαβεικνορτυχ", "abekmhopctyxijsdgabeikvoptux")
_LEET = str.maketrans({"0": "o", "1": "i", "3": "e", "4": "a", "5": "s", "7": "t", "@": "a", "$": "s", "!": "i", "|": "i", "v": "u"})
_LEET_TOKEN = re.compile(r"[a-z0-9@$!|*#]*[0-9@$!|v][a-z0-9@$!|*#]*")
_EDGE_PUNCTUATION = "!|*#"
_SPACED_LETTERS = re.compile(r"(?<![a-z0-9])(?:[a-z0-9][ .\-_*]){2,}[a-z0-9](?![a-z0-9])")
_SPACED_SEPARATORS = re.compile(r"[ .\-_*]")
_STRETCHED = re.compile(r"([a-z])\1{2,}")
_WORD = re.compile(r"[a-z0-9]+")
_MASKED_TOKEN = re.compile(r"(?<![a-z0-9@$!|*#])[a-z0-9@$!|]+[*#]+[a-z0-9@$!|*#]*")
_MASK = "*#"
# Endings tried when a masked word is compared with a "word*" term: f***ing -> fucking.
_PREFIX_ENDINGS = ("", "s", "ed", "er", "ers", "ing", "in", "y", "ies", "head", "hole", "face", "wit")


def fold(text: str) -> str:
    """Case-folded text with accents, look-alike letters, invisible characters and odd dashes normalised."""
    text = unicodedata.normalize("NFKC", text or "")
    text = _INVISIBLE.sub("", text).translate(_PUNCTUATION)
    if not text.isascii():
        decomposed = unicodedata.normalize("NFKD", text)
        if decomposed != text:
            text = "".join(char for char in decomposed if not unicodedata.combining(char))
    return re.sub(r"\s+", " ", text.casefold().translate(_LOOKALIKES)).strip()


def _forms(folded: str) -> list[str]:
    def decode(match: re.Match[str]) -> str:
        token = match.group().strip(_EDGE_PUNCTUATION)
        return token.translate(_LEET) if any(char.isalpha() for char in token) else token

    leet = _LEET_TOKEN.sub(decode, folded)
    joined = _SPACED_LETTERS.sub(lambda match: _SPACED_SEPARATORS.sub("", match.group()), leet)
    forms = [folded, joined]
    if _STRETCHED.search(joined):
        forms += [_STRETCHED.sub(r"\1", joined), _STRETCHED.sub(r"\1\1", joined)]
    return list(dict.fromkeys(forms))


def compile_terms(terms: Iterable[str]) -> re.Pattern[str] | None:
    """One pattern for a list of terms, each matching as whole words."""
    bodies = []
    for term in sorted(terms, key=len, reverse=True):
        folded = fold(term)
        words = [word for word in re.split(r"[\s\-]+", folded.rstrip("*")) if word]
        if words:
            body = r"[\s\-]*".join(re.escape(word) for word in words)
            bodies.append(body + ("[a-z0-9]*" if folded.endswith("*") else ""))
    return re.compile(rf"(?<![a-z0-9])(?:{'|'.join(bodies)})(?![a-z0-9])") if bodies else None


@dataclass(frozen=True)
class TermHit:
    category: FilterCategory
    term: str  # the matched text, decoded (e.g. "shit" for "sh1t")


class TermMatcher:
    def __init__(self, categories: Iterable[FilterCategory], allow_phrases: Iterable[str] = ()) -> None:
        # Blocking categories first, so the most serious reason is reported first.
        self._ordered = sorted(categories, key=lambda category: category.action != BLOCK)
        self._words: dict[str, FilterCategory] = {}
        self._prefixes: list[tuple[str, FilterCategory]] = []
        phrases: dict[str, list[str]] = defaultdict(list)
        self._masked_words: dict[int, list[tuple[str, FilterCategory]]] = defaultdict(list)

        for category in self._ordered:
            for term in category.terms:
                folded = fold(term)
                stem = folded.rstrip("*")
                is_prefix = folded.endswith("*")
                if _WORD.fullmatch(stem):
                    if is_prefix:
                        self._prefixes.append((stem, category))
                    else:
                        self._words.setdefault(stem, category)
                else:
                    phrases[category.id].append(term)
                if stem.isalpha():
                    for ending in _PREFIX_ENDINGS if is_prefix else ("",):
                        self._masked_words[len(stem + ending)].append((stem + ending, category))

        self._prefix_stems = tuple(stem for stem, _ in self._prefixes)
        self._phrases = [
            (category, pattern) for category in self._ordered if (pattern := compile_terms(phrases[category.id]))
        ]
        self._any_phrase = compile_terms([term for terms in phrases.values() for term in terms])
        self._allow = compile_terms(allow_phrases)

    def find(self, text: str) -> list[TermHit]:
        """Every category the text hits, with the first matching term for each, most serious first."""
        folded = fold(text)
        if not folded:
            return []
        if self._allow is not None:
            folded = self._allow.sub(" ", folded)
        hits: dict[str, TermHit] = {}

        def record(category: FilterCategory, term: str) -> None:
            hits.setdefault(category.id, TermHit(category, term))

        for form in _forms(folded):
            for word in _WORD.findall(form):
                if word in self._words:
                    record(self._words[word], word)
                if self._prefix_stems and word.startswith(self._prefix_stems):
                    for stem, category in self._prefixes:
                        if word.startswith(stem):
                            record(category, word)
            if self._any_phrase is not None and self._any_phrase.search(form):
                for category, pattern in self._phrases:
                    if category.id not in hits and (match := pattern.search(form)):
                        record(category, match.group())
        if "*" in folded or "#" in folded:
            for hit in self._masked_hits(folded):
                hits.setdefault(hit.category.id, hit)

        order = {category.id: index for index, category in enumerate(self._ordered)}
        return sorted(hits.values(), key=lambda hit: order[hit.category.id])

    def _masked_hits(self, folded: str) -> Iterable[TermHit]:
        for match in _MASKED_TOKEN.finditer(folded):
            token = match.group().rstrip("!|")
            decoded = "".join(char if char in _MASK else char.translate(_LEET) for char in token)
            letters = [char for char in decoded if char not in _MASK]
            # A real masked word starts with a letter and is otherwise letters: f*ck, sh#t, f***ing.
            if len(decoded) < 3 or not decoded[0].isalpha() or not all(char.isalpha() for char in letters):
                continue
            shape = re.compile("".join("[a-z]" if char in _MASK else re.escape(char) for char in decoded))
            for word, category in self._masked_words.get(len(decoded), ()):
                if shape.fullmatch(word):
                    yield TermHit(category, match.group())
                    break
