"""Matching job titles and free text without the substring bugs.

Both ICP scorers used to test `keyword in text` on lower-cased strings. That is
wrong in a way that is easy to miss and expensive to trust:

    "cto"  in "director of marketing"   -> True   (dire-CTO-r)
    "coo"  in "recruiting coordinator"  -> True   (-COO-rdinator)
    "ai"   in "available for a chat"    -> True   (av-AI-lable)
    "ceo"  in "chief executive officer" -> False

So every Director scored as a C-level, every Coordinator as a COO, almost every
English bio matched the AI signal, and a title spelled out in full — which is
how a lot of people write it on LinkedIn — scored nothing at all.

The fix is not a `\\b` regex. `\\b` is defined in terms of word characters, and
these titles are not all written in a script that has word boundaries: a
Traditional Chinese title like 資訊主管 has none, and neither `\\b資訊主管\\b`
nor a token split does what you want inside 資深資訊主管. So matching is
script-aware:

  * a phrase containing CJK characters is matched as a **substring** of the
    normalized text — correct, because CJK is written without spaces;
  * anything else is matched as a **contiguous run of whole tokens**, which is
    exactly word-boundary semantics and needs no lookarounds at all.

Normalization lower-cases and turns every non-alphanumeric character into a
space, so "Co-Founder" and "co founder" are the same phrase and punctuation
never breaks a match. `str.isalnum()` is deliberately what decides: it keeps
accented Latin, CJK, Vietnamese and Cyrillic alike, so this works for every
market in the `markets` table rather than for English plus whatever we tested.
"""

import re
from typing import Iterable, List, Sequence, Set

__all__ = [
    "normalize",
    "tokens",
    "significant_tokens",
    "contains_phrase",
    "matches_any",
    "best_phrase_match",
]

_WHITESPACE = re.compile(r"\s+")

# Scripts written without spaces between words, where token matching is the
# wrong tool and substring matching is the right one.
_SCRIPTLESS_RANGES = (
    ("぀", "ヿ"),  # Hiragana + Katakana
    ("㐀", "䶿"),  # CJK Unified Ideographs Extension A
    ("一", "鿿"),  # CJK Unified Ideographs
    ("가", "힯"),  # Hangul syllables
    ("豈", "﫿"),  # CJK Compatibility Ideographs
)

# Connectives that carry no meaning in a job title, in the languages this
# product sells into. Dropped only when deciding PARTIAL credit — a phrase match
# always uses the words exactly as written.
_STOPWORDS = frozenset({
    "of", "the", "and", "for", "at", "in", "to", "on", "a", "an",
    "de", "del", "la", "el", "los", "las", "y", "en", "da", "do", "dos", "e",
    "und", "der", "die", "das", "von", "et", "des", "du",
    "va", "cua",
})


def _has_scriptless(text: str) -> bool:
    return any(lo <= ch <= hi for ch in text for lo, hi in _SCRIPTLESS_RANGES)


def normalize(text: object) -> str:
    """Lower-case, punctuation to spaces, whitespace collapsed.

    `isalnum()` rather than a Latin character class: it is True for CJK,
    accented Latin and Vietnamese tone marks, so a title in any of this
    product's markets survives normalization intact.
    """
    if not isinstance(text, str) or not text:
        return ""
    flattened = "".join(ch.lower() if ch.isalnum() else " " for ch in text)
    return _WHITESPACE.sub(" ", flattened).strip()


def tokens(text: object) -> List[str]:
    normalized = normalize(text)
    return normalized.split() if normalized else []


def significant_tokens(text: object) -> Set[str]:
    """Tokens worth comparing: at least 3 characters and not a connective.

    The length floor is what stops "IT Manager" and "HR Manager" from looking
    alike through a shared two-letter token, and CJK tokens are kept whatever
    their length since a two-character CJK token is a whole word.
    """
    out: Set[str] = set()
    for token in tokens(text):
        if token in _STOPWORDS:
            continue
        if len(token) >= 3 or _has_scriptless(token):
            out.add(token)
    return out


def contains_phrase(haystack_text: object, phrase: object) -> bool:
    """Whether `phrase` occurs in `haystack_text` as a whole phrase.

    Substring for CJK/Japanese/Korean phrases, contiguous-token match for
    everything else. Empty phrases never match — a combo with a blank keyword
    must not silently match every lead.
    """
    needle = normalize(phrase)
    hay = normalize(haystack_text)
    if not needle or not hay:
        return False

    if _has_scriptless(needle):
        return needle in hay

    needle_tokens = needle.split()
    hay_tokens = hay.split()
    span = len(needle_tokens)
    if span == 0 or span > len(hay_tokens):
        return False
    for start in range(len(hay_tokens) - span + 1):
        if hay_tokens[start:start + span] == needle_tokens:
            return True
    return False


def matches_any(haystack_text: object, phrases: Iterable[object]) -> bool:
    return any(contains_phrase(haystack_text, phrase) for phrase in phrases)


def best_phrase_match(haystack_text: object, phrases: Sequence[object]) -> float:
    """How well the text matches the closest of `phrases`, from 0.0 to 1.0.

    1.0  — one phrase occurs in full.
    0.5  — a multi-word phrase overlaps on at least two significant tokens and
           at least half of them. "Head of Supply Chain" vs a lead titled
           "Supply Chain Manager" is a real near-miss and should not score the
           same as a stranger.
    0.0  — neither.

    The two-token floor is deliberate and is what keeps this from repeating the
    old bug in a politer form: without it, "Marketing Director" and "Art
    Director" would share `director` and count as half a match, which is how
    every Director ended up looking like a target in the first place. A
    single-word phrase therefore earns credit only by matching outright, and a
    CJK phrase likewise — its substring rule already covers the near-miss case.
    """
    if not phrases:
        return 0.0

    hay_tokens = significant_tokens(haystack_text)
    best = 0.0

    for phrase in phrases:
        if contains_phrase(haystack_text, phrase):
            return 1.0
        if not hay_tokens:
            continue
        phrase_tokens = significant_tokens(phrase)
        if len(phrase_tokens) < 2:
            continue
        shared = phrase_tokens & hay_tokens
        if len(shared) >= 2 and len(shared) / len(phrase_tokens) >= 0.5:
            best = max(best, 0.5)

    return best
