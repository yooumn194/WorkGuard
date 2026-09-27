"""Small text helpers shared by parsers and agents."""
from __future__ import annotations

import re


def normalize(text: str) -> str:
    """Collapse whitespace for tolerant matching ("9 月 20 日" == "9月20日")."""
    return re.sub(r"\s+", "", text or "")


def contains_any(haystack: str, needles: list[str]) -> str | None:
    """Return the first needle present in haystack (whitespace-insensitive), else None."""
    norm = normalize(haystack)
    for needle in needles:
        if normalize(needle) and normalize(needle) in norm:
            return needle
    return None


def has_hedge(text: str) -> bool:
    from backend.utils.dates import HEDGES

    return any(h in text for h in HEDGES)


def token_set(text: str) -> set[str]:
    """Very light tokenisation: ascii words + CJK bigrams, for fuzzy entity match."""
    text = normalize(text).lower()
    tokens = set(re.findall(r"[a-z0-9]+", text))
    cjk = re.findall(r"[\u4e00-\u9fff]", text)
    tokens.update(f"{a}{b}" for a, b in zip(cjk, cjk[1:]))
    tokens.update(cjk)
    return tokens


def jaccard(a: set[str], b: set[str]) -> float:
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)
