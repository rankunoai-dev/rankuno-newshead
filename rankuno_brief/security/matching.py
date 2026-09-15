"""Finds listed terms in text, including disguised spellings.

Text is compared in several normalised forms, so common tricks do not get through:

    accents, full-width, look-alikes  fück, ｆｕｃｋ, fаck (Cyrillic а)  -> fuck
    invisible characters              fu<zero-width space>ck          -> fuck
    leetspeak                         sh1t, $hit, b!tch, fvck         -> shit, shit, bitch, fuck
    spaced letters                    f u c k, f.u.c.k                -> fuck
    stretched letters                 fuuuuck                         -> fuck
    masked letters                    f*ck, sh#t, f***ing             -> fuck, shit, fucking

Terms always match whole words, so "analytics" or "Scunthorpe" never trip a shorter term.
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
_APOSTROPHES = str.maketrans({"‘": "'", "’": "'", "ʼ": "'", "′": "'", "`": "'"})
# Cyrillic and Greek letters that look like Latin ones (lower case; text is case-folded first).
_LOOKALIKES = str.maketrans(
    "авекмнорстухіјѕԁɡαβεικνορτυχ",
    "abekmhopctyxijsdgabeikvoptux",
)
_LEET = str.maketrans({"0": "o", "1": "i", "3": "e", "4": "a", "5": "s", "7": "t", "@": "a", "$": "s", "!": "i", "|": "i", "v": "u"})
_LEET_TOKEN = re.compile(r"[a-z0-9@$!|*#]+")
_EDGE_PUNCTUATION = "!|*#"
_SPACED_LETTERS = re.compile(r"(?<![a-z0-9])(?:[a-z0-9][ .\-_*]){2,}[a-z0-9](?![a-z0-9])")
_SPACED_SEPARATORS = re.compile(r"[ .\-_*]")
_MASKED_TOKEN = re.compile(r"(?<![a-z0-9@$!|*#])[a-z0-9@$!|]+[*#]+[a-z0-9@$!|*#]*")
_MASK = "*#"
# Endings tried when a masked word is compared with a "word*" term: f***ing -> fucking.
_PREFIX_ENDINGS = ("", "s", "ed", "er", "ers", "ing", "in", "y", "ies", "head", "hole", "face", "wit")


def fold(text: str) -> str:
    """Case-folded text with accents, look-alike letters, invisible characters and odd dashes normalised."""
    text = unicodedata.normalize("NFKC", text or "")
    text = _INVISIBLE.sub("", text).translate(_APOSTROPHES)
    text = "".join(char for char in unicodedata.normalize("NFKD", text) if not unicodedata.combining(char))
    text = "".join("-" if unicodedata.category(char) == "Pd" else char for char in text)
    return re.sub(r"\s+", " ", text.casefold().translate(_LOOKALIKES)).strip()


def _decode_leet(folded: str) -> str:
    def decode(match: re.Match[str]) -> str:
        token = match.group().strip(_EDGE_PUNCTUATION)
        return token.translate(_LEET) if any(char.isalpha() for char in token) else token

    return _LEET_TOKEN.sub(decode, folded)


def _forms(folded: str) -> list[str]:
    leet = _decode_leet(folded)
    joined = _SPACED_LETTERS.sub(lambda match: _SPACED_SEPARATORS.sub("", match.group()), leet)
    forms = [folded, leet, joined, re.sub(r"([a-z])\1{2,}", r"\1", joined), re.sub(r"([a-z])\1{2,}", r"\1\1", joined)]
    return list(dict.fromkeys(forms))


def _term_source(term: str) -> str | None:
    folded = fold(term)
    prefix = folded.endswith("*")
    words = [word for word in re.split(r"[\s\-]+", folded.rstrip("*")) if word]
    if not words:
        return None
    body = r"[\s\-]*".join(re.escape(word) for word in words)
    return rf"(?<![a-z0-9]){body}{'[a-z0-9]*' if prefix else ''}(?![a-z0-9])"


def compile_terms(terms: Iterable[str]) -> re.Pattern[str] | None:
    sources = [source for term in sorted(terms, key=len, reverse=True) if (source := _term_source(term))]
    return re.compile("|".join(sources)) if sources else None


@dataclass(frozen=True)
class TermHit:
    category: FilterCategory
    term: str  # the matched text, decoded (e.g. "shit" for "sh1t")


class TermMatcher:
    def __init__(self, categories: Iterable[FilterCategory], allow_phrases: Iterable[str] = ()) -> None:
        # Blocking categories first, so the most serious reason is reported first.
        ordered = sorted(categories, key=lambda category: category.action != BLOCK)
        self._patterns = [(category, pattern) for category in ordered if (pattern := compile_terms(category.terms))]
        self._allow = compile_terms(allow_phrases)
        self._masked_words: dict[int, list[tuple[str, FilterCategory]]] = defaultdict(list)
        for category in ordered:
            for term in category.terms:
                folded = fold(term)
                stem = folded.rstrip("*")
                if not stem.isalpha():
                    continue
                for ending in _PREFIX_ENDINGS if folded.endswith("*") else ("",):
                    word = stem + ending
                    self._masked_words[len(word)].append((word, category))

    def find(self, text: str) -> list[TermHit]:
        """Every category the text hits, with the first matching term for each."""
        folded = fold(text)
        if not folded:
            return []
        if self._allow is not None:
            folded = self._allow.sub(" ", folded)
        forms = _forms(folded)
        hits: dict[str, TermHit] = {}
        for category, pattern in self._patterns:
            for form in forms:
                match = pattern.search(form)
                if match:
                    hits[category.id] = TermHit(category, match.group())
                    break
        for hit in self._masked_hits(folded):
            hits.setdefault(hit.category.id, hit)
        return list(hits.values())

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
