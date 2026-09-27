"""Deterministic (offline) fact extraction for DATE predicates.

This is the MVP fallback extractor: it implements exactly one narrow skill —
finding *date* facts near *date-predicate* keywords — and does so with plain
regex + keyword windows. LLM mode uses the same output schema, so the rest of
the pipeline is extraction-method agnostic.

Robustness features (each driven by a measured error bucket, see
eval/reports/error_analysis_heuristic.md):
- whitespace-tolerant keyword matching ("发 布 时 间" still matches);
- single-substitution fuzzy matching for CJK keywords >= 3 chars
  ("发部时间" -> "发布时间", "回规测试" -> "回归测试");
- cross-predicate containment dedup ("灰渡发布" must not also yield a
  release_date via its inner "发布");
- English month-name dates ("Sep 27, 2026");
- explicit next-year markers ("次年/明年") shift inferred years;
- conditional clauses ("若…则顺延") and hedges are scoped to their own
  fragment so they do not downgrade unrelated facts.

Scope guard (user decision): the MVP strictly covers the date-change chain.
Owner / status / version predicates are listed here for schema completeness but
marked out-of-scope and never emitted.
"""
from __future__ import annotations

import re
from datetime import date

from backend.utils.dates import find_dates
from backend.utils.text import has_hedge

# predicate -> keyword patterns (case-insensitive; see _keyword_pattern)
PREDICATE_KEYWORDS: dict[str, list[str]] = {
    "release_date": [
        "release date", "上线日期", "上线时间", "发布时间", "发布日期",
        "正式上线", "正式发布", "上线", "发布", "go live", "go-live", "release", "launch",
    ],
    "regression_deadline": [
        "回归测试", "回归完成", "测试完成", "回归", "regression deadline",
        "regression complete", "test complete",
    ],
    "gray_release_date": ["灰度", "灰度发布", "gray release", "canary"],
    "announcement_date": ["公告", "官宣", "对外宣布", "announcement"],
}

# release keywords that name the release date unambiguously (vs generic 发布/release)
_EXPLICIT_RELEASE = {"release date", "上线日期", "上线时间", "发布时间", "发布日期",
                     "正式上线", "正式发布", "go live", "go-live", "launch"}

# change-connector verbs; fuzzy variants are generated for len >= 2
_CONNECTOR_VERBS = ["调整", "变更", "推迟", "延后", "延期", "延", "改", "挪", "提前"]
_DELAY_VERBS = {"推迟", "延后", "延期", "延"}

_UNCERTAIN_MARKERS = ("暂定", "待定", "待确认", "尚未确认", "争取", "还是")
_CONDITIONAL_MARKERS = ("若", "如果", "假如", "如若", "一旦", "届时")
_NEGATED_ASSERTION = re.compile(
    r"取消.{0,40}(?:上线|发布).{0,12}(?:安排|计划)|"
    r"(?:不会|不再|并非|不是).{0,30}(?:上线|发布)"
)
_AMBIGUOUS_MDY = re.compile(r"(?<!\d)\d{1,2}/\d{1,2}/20\d{2}(?!\d)")
# Predicates intentionally out of MVP scope.
OUT_OF_SCOPE = ("owner", "status", "version", "budget", "decision")

_WS = r"[\s\u3000]*"
_PATTERN_CACHE: dict[str, re.Pattern] = {}
_CONNECTOR_RE: re.Pattern | None = None


def _keyword_pattern(keyword: str) -> re.Pattern:
    """Whitespace-tolerant pattern; CJK/long keywords also get one
    single-character-substitution variant each ("发部时间" ≈ "发布时间")."""
    cached = _PATTERN_CACHE.get(keyword)
    if cached is not None:
        return cached
    variants = [keyword]
    if len(keyword) >= 3:
        for i, ch in enumerate(keyword):
            if ch.isspace():
                continue
            variants.append(keyword[:i] + "\x00" + keyword[i + 1:])
    alternatives = []
    for variant in variants:
        pieces = []
        for ch in variant:
            if ch == "\x00":
                pieces.append(".")           # the substitution wildcard
            elif ch.isspace():
                pieces.append(_WS)
            else:
                pieces.append(re.escape(ch))
        alternatives.append(_WS.join(pieces))
    pattern = re.compile("|".join(alternatives), re.IGNORECASE)
    _PATTERN_CACHE[keyword] = pattern
    return pattern


def _close_to(word: str, vocab: set[str]) -> bool:
    """Exact or single-substitution membership (for fuzzy-matched verbs)."""
    if word in vocab:
        return True
    if len(word) >= 2:
        return any(
            len(v) == len(word) and sum(a != b for a, b in zip(v, word)) == 1
            for v in vocab
        )
    return False


def _connector_pattern() -> re.Pattern:
    global _CONNECTOR_RE
    if _CONNECTOR_RE is None:
        alternatives = []
        for verb in _CONNECTOR_VERBS:
            for variant in [verb] + (
                [verb[:i] + "\x00" + verb[i + 1:] for i in range(len(verb))]
                if len(verb) >= 2 else []
            ):
                pieces = []
                for ch in variant:
                    if ch == "\x00":
                        pieces.append(".")
                    else:
                        pieces.append(re.escape(ch))
                alternatives.append(_WS.join(pieces))
        _CONNECTOR_RE = re.compile(
            "(" + "|".join(alternatives) + r")[\s\u3000]*(至|到|为)"
        )
    return _CONNECTOR_RE


def _find_keyword_matches(text: str) -> list[dict]:
    """All predicate-keyword matches, with cross-predicate containment dedup:
    a match fully inside a LONGER match of a different predicate is dropped
    ("灰渡发布[灰度发布]从…" keeps gray, drops the inner generic 发布)."""
    matches: list[dict] = []
    for predicate, keywords in PREDICATE_KEYWORDS.items():
        for keyword in keywords:
            found = _keyword_pattern(keyword).search(text)
            if found:
                if keyword == "发布" and text[found.end():found.end() + 1] == "会":
                    continue  # 发布会日期 is an event date, not the product release date
                matches.append({
                    "predicate": predicate,
                    "keyword": keyword,
                    "start": found.start(),
                    "end": found.end(),
                })
    kept = []
    for match in matches:
        shadowed = any(
            other["predicate"] != match["predicate"]
            and other["start"] < match["end"]
            and match["start"] < other["end"]
            and (
                (other["end"] - other["start"]) > (match["end"] - match["start"])
                or (
                    (other["end"] - other["start"]) == (match["end"] - match["start"])
                    and other["start"] < match["start"]
                )
            )
            for other in matches
        )
        if not shadowed:
            kept.append(match)
    return kept


def match_predicate(text: str) -> tuple[str, str] | None:
    """Return (predicate, keyword) for the first date-predicate keyword in text."""
    matches = _find_keyword_matches(text)
    if not matches:
        return None
    best = min(matches, key=lambda m: (m["start"], -(m["end"] - m["start"])))
    return best["predicate"], best["keyword"]


def keyword_position(text: str, keyword: str) -> int:
    found = _keyword_pattern(keyword).search(text)
    return found.start() if found else -1


def extract_date_facts(
    text: str,
    default_year: int | None = None,
    hedge_scope: str | None = None,
    year_scope: str | None = None,
) -> list[dict]:
    """Extract ALL date facts from a block: every predicate keyword present gets
    the date nearest to it. Change sentences ("由 A 调整至 B") take the date
    AFTER the change connector as the new value (change_from = date before it).

    hedge_scope: the text whose hedging/conditional markers apply (usually the
    fragment containing the matched date), so a conditional clause does not
    downgrade an unrelated fact in the same block.

    Returns a list of {"predicate","value","surface_form","change_from",
                       "uncertain","confidence"} deduped by (predicate, value).
    """
    if _AMBIGUOUS_MDY.search(text):
        return []  # 09/07/2026 is locale-ambiguous; refuse to guess
    dates = find_dates(text, default_year=default_year)
    if not dates:
        return []
    scope = hedge_scope if hedge_scope is not None else text
    connector = _connector_pattern().search(text)
    if connector is None and _NEGATED_ASSERTION.search(text):
        return []
    conn_pos = connector.start() if connector else None
    range_like = False
    if connector is None and len(dates) >= 2:
        for left, right in zip(dates, dates[1:]):
            between = text[left["end"]:right["start"]]
            if re.search(r"(?:至|到|~|～|—|–)", between):
                range_like = True
                break

    matches = _find_keyword_matches(text)
    out: dict[tuple, dict] = {}
    for match in matches:
        predicate, keyword = match["predicate"], match["keyword"]
        if predicate == "release_date":
            # Generic words such as “发布/release” are substrings of specific
            # predicates (“灰度发布”). If another predicate is present and the
            # release mention is generic, do not manufacture an extra fact.
            specific_other = any(
                m["predicate"] != "release_date" for m in matches
            )
            explicit_release = keyword in _EXPLICIT_RELEASE
            if specific_other and not explicit_release:
                continue
        kw_pos = match["start"]
        change_from = None
        if conn_pos is not None and kw_pos < conn_pos:
            # new value = first date AFTER the connector verb ("调整至 X");
            # old value = last date BEFORE the verb
            assert connector is not None
            after_conn = [d for d in dates if d["start"] >= connector.end()]
            before_conn = [d for d in dates if d["end"] <= conn_pos]
            if after_conn:
                change_from = before_conn[-1]["iso"] if before_conn else None
                chosen = dict(after_conn[0])
                if (
                    change_from
                    and chosen["iso"] < change_from
                    and _close_to(connector.group(1), _DELAY_VERBS)
                    and not re.search(r"20\d{2}", chosen["surface"])
                ):
                    # “12 月 28 日延期至 1 月 4 日” crosses the year boundary.
                    old_year = int(change_from[:4])
                    chosen["iso"] = f"{old_year + 1:04d}{chosen['iso'][4:]}"
            else:
                chosen = _nearest_to_keyword(dates, kw_pos)
        else:
            chosen = _nearest_to_keyword(dates, kw_pos)

        # explicit next-year markers shift year-inferred dates ("次年开年")
        if chosen["iso"][:4] == str(default_year or date.today().year) and not re.search(
            r"20\d{2}", chosen["surface"]
        ) and re.search(r"次年|明年|来年|下一年|跨年", year_scope or text):
            chosen = dict(chosen)
            chosen["iso"] = f"{int(chosen['iso'][:4]) + 1:04d}{chosen['iso'][4:]}"

        key = (predicate, chosen["iso"])
        if key in out:
            continue
        uncertain = evaluate_uncertainty(scope) or range_like
        out[key] = {
            "predicate": predicate,
            "value": chosen["iso"],
            "surface_form": chosen["surface"],
            "change_from": change_from,
            "uncertain": uncertain,
            "confidence": 0.50 if uncertain else (0.92 if connector else 0.85),
        }
    return list(out.values())


def evaluate_uncertainty(scope: str) -> bool:
    """Hedge / tentative / conditional markers within the given scope
    (usually the fragment containing the fact) force the unverified lane."""
    return (
        has_hedge(scope)
        or any(marker in scope for marker in _UNCERTAIN_MARKERS)
        or any(marker in scope for marker in _CONDITIONAL_MARKERS)
    )


def is_negated_assertion(text: str) -> bool:
    """True when a date appears only inside an explicitly cancelled assertion."""
    return _connector_pattern().search(text) is None and bool(_NEGATED_ASSERTION.search(text))


def extract_date_fact(text: str, default_year: int | None = None) -> dict | None:
    facts = extract_date_facts(text, default_year=default_year)
    return facts[0] if facts else None


def _nearest_to_keyword(dates: list[dict], kw_pos: int) -> dict:
    """Nearest date by absolute distance to the keyword; ties prefer the date
    AFTER the keyword (prefix patterns like "Release Date: X" are more common)."""
    return min(dates, key=lambda d: (abs(d["start"] - kw_pos), 0 if d["start"] >= kw_pos else 1))


def default_year() -> int:
    return date.today().year
