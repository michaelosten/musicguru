"""Shared title/artist normalization and matching.

Recognizers (Shazam especially) return censored titles like "F**k Me Eyes" or
"Sh*t", while your library has the real word -- so naive normalization made them
never match. Two defenses:

* decensor(): rewrites common masked profanity to its plain form, so both sides
  normalize to the same thing (and searches use the real word).
* titles_match(): when a recognized title still contains "*", it's treated as a
  wildcard against the candidate, catching anything the dictionary misses.
"""
import re
import unicodedata

# Masked profanity -> canonical. Every pattern requires at least one "*", so
# un-masked text is never rewritten. Longer forms (…ing) come first.
_CENSOR = [
    (re.compile(r"f[\*u]*\*+[\*k]*(in[g']?)", re.I), r"fuck\1"),
    (re.compile(r"f[\*u]*\*+k", re.I), "fuck"),
    (re.compile(r"mothaf[\*u]*\*+k\w*", re.I), "motherfucker"),
    (re.compile(r"sh[\*i]*\*+t", re.I), "shit"),
    (re.compile(r"b[\*i]*\*+t?ch", re.I), "bitch"),
    (re.compile(r"d[\*i]*\*+ck", re.I), "dick"),
    (re.compile(r"p[\*u]*\*+s+y", re.I), "pussy"),
    (re.compile(r"c[\*u]*\*+nt", re.I), "cunt"),
    (re.compile(r"a[s\*]*\*+hole", re.I), "asshole"),
    (re.compile(r"\ba[\*s]*\*+(?=\b|es\b)", re.I), "ass"),
    (re.compile(r"d[\*a]*\*+mn", re.I), "damn"),
    (re.compile(r"h[\*e]*\*+ll\b", re.I), "hell"),
]


def decensor(s: str) -> str:
    for pat, repl in _CENSOR:
        s = pat.sub(repl, s)
    return s


def _fold(s: str) -> str:
    s = unicodedata.normalize("NFKD", s or "")
    s = "".join(c for c in s if not unicodedata.combining(c))
    return re.sub(r"\(.*?\)|\[.*?\]", "", s)   # drop "(Remastered)" etc.


def norm(s: str) -> str:
    """Aggressive comparison key: de-censored, accent-stripped, alnum-only."""
    return re.sub(r"[^0-9a-z]+", "", decensor(_fold(s)).lower())


def query_title(s: str) -> str:
    """A search-friendly title: de-censored, parentheticals removed."""
    return re.sub(r"\(.*?\)|\[.*?\]", "", decensor(s or "")).strip()


def _wildcard_core(s: str) -> str:
    """alnum + '*' only, lowercased -- for building a wildcard regex."""
    return re.sub(r"[^0-9a-z*]+", "", _fold(s).lower())


def titles_match(recognized: str, candidate: str) -> bool:
    """Whether a recognized title (possibly masked) matches a library candidate."""
    a, b = norm(recognized), norm(candidate)
    if a and b and (a == b or a in b or b in a):
        return True
    if "*" in (recognized or ""):
        core = _wildcard_core(recognized)
        if core:
            rx = "".join("." if c == "*" else re.escape(c) for c in core)
            try:
                if re.fullmatch(rx, b) or re.search(rx, b):
                    return True
            except re.error:
                pass
    return False
