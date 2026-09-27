"""Agent 1: Change/Fact Extractor + Reflection self-check.

MVP scope: DATE facts only (release_date / regression_deadline / gray_release_date /
announcement_date). Owner / status / version are deliberately out of scope for
the first chain.

Two layers fight extraction hallucination (the "Achilles' heel"):
1. Reflection node: after extraction, every fact is checked back against the
   source blocks — the evidence quote must exist in the original text, dates
   must parse, hedges downgrade confidence. LLM mode adds an LLM self-review
   pass before the deterministic guards.
2. Confidence gate: facts below WORKGUARD_FACT_UNVERIFIED_THRESHOLD are stored
   as status=unverified and are EXCLUDED from conflict detection (user decision:
   low-confidence facts never trigger write suggestions).
"""
from __future__ import annotations

import logging
import math
import re

from backend.config import settings
from backend.llm import heuristics
from backend.llm.client import LLMClient, get_llm
from backend.utils.dates import find_dates
from backend.utils.text import has_hedge

logger = logging.getLogger(__name__)

EXTRACT_SYSTEM_PROMPT = """You are a Fact Extractor for a project-document consistency system.
Extract ONLY date-related business facts (predicate one of:
release_date, regression_deadline, gray_release_date, announcement_date).
Ignore owner/status/version facts in this MVP.
For every fact return: entity_mention (project/version name as written, or ""),
predicate, value (ISO yyyy-mm-dd, infer the year when missing),
surface_form (the exact date string as written), evidence (shortest original
quote that supports the fact), location (given block location), confidence (0-1).
Set confidence <= 0.5 when the sentence is hedged (可能/或许/大概/暂定) or vague.
Return JSON: {"facts": [...]}. No prose."""


def _safe_confidence(value, default: float = 0.5) -> float:
    """Coerce untrusted model output to a finite probability."""
    try:
        confidence = float(value)
    except (TypeError, ValueError):
        return default
    if not math.isfinite(confidence):
        return default
    return max(0.0, min(1.0, confidence))


def _derive_transition(evidence: str, predicate: str, value: str) -> dict:
    """Recover old-value and ambiguity metadata with deterministic parsing.

    This keeps the stale-precondition safety gate active in LLM mode: the
    model is responsible for extraction, but not trusted to define transition
    semantics on its own.
    """
    year = int(value[:4]) if len(value) == 10 and value[:4].isdigit() else None
    return next(
        (
            item for item in heuristics.extract_date_facts(evidence, default_year=year)
            if item.get("predicate") == predicate and item.get("value") == value
        ),
        {},
    )


def _document_year(blocks: list[dict]) -> int | None:
    """Most frequent explicit year in the document — safer than today's year
    for demo/eval content that only writes "9 月 20 日" without a year."""
    import re
    from collections import Counter

    years: Counter[int] = Counter()
    for block in blocks:
        years.update(int(y) for y in re.findall(r"\b(20\d{2})\b", block["text"]))
    return years.most_common(1)[0][0] if years else None


_ANAPHORA = re.compile(r"那个时间|该时间|此时间|这一时间|上述时间")


def _heuristic_extract(blocks: list[dict]) -> list[dict]:
    facts: list[dict] = []
    year = _document_year(blocks) or heuristics.default_year()
    doc_context = ""  # last entity mention seen anywhere in the document
    doc_predicate = ""  # last predicate mentioned; resolves "那个时间" anaphora
    for block in blocks:
        text, location = block["text"], block["location"]
        mention_here = _find_entity_mention(text)
        if mention_here:
            doc_context = mention_here
        entity = block.get("meta", {}).get("context_entity") or doc_context
        hits = _extract_block_facts(text, year)
        if not hits and doc_predicate and find_dates(text) and _ANAPHORA.search(text):
            # "那个时间大概在 9 月 28 日左右" — anaphoric date with no local
            # keyword: inherit the document's last-mentioned predicate, but
            # keep it in the unverified lane (confidence 0.45).
            chosen = min(find_dates(text), key=lambda d: d["start"])
            hits = [{
                "predicate": doc_predicate,
                "value": chosen["iso"],
                "surface_form": chosen["surface"],
                "change_from": None,
                "evidence": text.strip(),
                "uncertain": True,
                "confidence": 0.45,
            }]
        for hit in hits:
            facts.append(
                {
                    "entity_mention": _find_entity_mention(hit["evidence"]) or entity,
                    "predicate": hit["predicate"],
                    "value": hit["value"],
                    "value_type": "date",
                    "surface_form": hit["surface_form"],
                    "change_from": hit.get("change_from"),
                    "location": location,
                    "evidence": hit["evidence"],
                    "confidence": hit["confidence"],
                    "uncertain": hit["uncertain"],
                    "extracted_by": "heuristic",
                }
            )
        keyword_hit = heuristics.match_predicate(text)
        if keyword_hit:
            doc_predicate = keyword_hit[0]
        elif hits:
            doc_predicate = hits[-1]["predicate"]
    return facts


def _extract_block_facts(text: str, year: int) -> list[dict]:
    """Change decisions are cross-clause statements ("原计划 09-20 上线，最终
    调整为 09-27"), so they are read from the WHOLE block first; every other
    predicate is extracted per fragment, which keeps ``Alpha ... 9/20，Beta
    ... 10/1`` as two entity-scoped facts. Hedging/conditional markers are
    evaluated on the fact's OWN fragment, so "若回归 9 月 17 日未完成" does
    not downgrade the gray-release fact in the same sentence."""
    fragments = _split_sentences(text)
    whole_hits = heuristics.extract_date_facts(text, default_year=year)
    change_hits = {h["predicate"]: h for h in whole_hits if h.get("change_from")}
    out, seen = [], set()

    def emit(hit: dict, evidence: str) -> None:
        key = (hit["predicate"], hit["value"], evidence)
        if key in seen:
            return
        seen.add(key)
        out.append({**hit, "evidence": evidence.strip()})

    for predicate, hit in change_hits.items():
        surface = hit["surface_form"].replace(" ", "")
        fragment = next(
            (f for f in fragments if surface in f.replace(" ", "")),
            text,
        )
        # re-scope uncertainty to the change fragment itself
        hit = dict(hit)
        hit["uncertain"] = heuristics.evaluate_uncertainty(fragment)
        hit["confidence"] = 0.50 if hit["uncertain"] else 0.92
        emit(hit, fragment)

    for fragment in fragments or [text]:
        for hit in heuristics.extract_date_facts(fragment, default_year=year,
                                                 hedge_scope=fragment,
                                                 year_scope=text):
            if hit["predicate"] in change_hits:
                continue  # covered by the whole-block change reading (old value stays history)
            emit(hit, fragment)
    return out


def _split_sentences(text: str) -> list[str]:
    import re

    parts = re.split(r"(?<=[。；;，,])|(?=\|)|\n", text)
    return [p.strip(" |") for p in parts if p and p.strip(" |")]


def _find_entity_mention(text: str) -> str:
    """Lightweight project mention detector used to attach facts to entities."""
    import re

    patterns = [
        r"[A-Za-z][A-Za-z0-9 .]{0,24}(?:V\d(?:\.\d)?|Version \d(?:\.\d)?)",  # Alpha V2.0
        r"[A-Za-z][\w-]{1,20}(?:App|API|平台|系统|项目)",
        r"[\u4e00-\u9fff]{2,10}(?:项目|系统|平台)",
    ]
    for pattern in patterns:
        m = re.search(pattern, text)
        if m:
            return m.group(0).strip()
    return ""


def _llm_extract(blocks: list[dict], llm: LLMClient) -> list[dict]:
    from datetime import date

    corpus = "\n".join(f"[{b['location']}] {b['text']}" for b in blocks)
    user = f"Current year for date inference: {date.today().year}\n\nBlocks:\n{corpus}"
    data = llm.complete_json(EXTRACT_SYSTEM_PROMPT, user, purpose="extract")
    raw = (data or {}).get("facts", []) if isinstance(data, dict) else []
    facts = []
    for f in raw:
        if not isinstance(f, dict):
            continue
        predicate = f.get("predicate")
        if predicate not in heuristics.PREDICATE_KEYWORDS:
            continue
        value = f.get("value", "") if isinstance(f.get("value", ""), str) else ""
        evidence = f.get("evidence", "") if isinstance(f.get("evidence", ""), str) else ""
        derived = _derive_transition(evidence, predicate, value)
        confidence = _safe_confidence(f.get("confidence", 0.5))
        uncertain = confidence <= 0.5 or bool(derived.get("uncertain"))
        facts.append(
            {
                "entity_mention": f.get("entity_mention", "")
                if isinstance(f.get("entity_mention", ""), str) else "",
                "predicate": predicate,
                "value": value,
                "value_type": "date",
                "surface_form": f.get("surface_form", "")
                if isinstance(f.get("surface_form", ""), str) else "",
                "change_from": derived.get("change_from"),
                "location": f.get("location", "")
                if isinstance(f.get("location", ""), str) else "",
                "evidence": evidence,
                "confidence": confidence,
                "uncertain": uncertain,
                "extracted_by": "llm",
            }
        )
    return facts


# ---------------------------------------------------------------- reflection
def _deterministic_review(blocks: list[dict], facts: list[dict]) -> tuple[list[dict], list[dict]]:
    """Guard every fact against its source text. Returns (kept, dropped)."""
    block_text_by_location = {b["location"]: b["text"] for b in blocks}
    all_text = "\n".join(b["text"] for b in blocks)
    kept, dropped = [], []
    for fact in facts:
        location = fact.get("location", "")
        source = block_text_by_location.get(location, all_text)
        norm_source = source.replace(" ", "")
        reasons = []

        if location not in block_text_by_location:
            reasons.append("unknown source location")

        # 1. evidence must exist in the source (hallucination guard)
        evidence = (fact.get("evidence") or "").replace(" ", "")
        if not evidence or (evidence[:40] not in norm_source and evidence not in norm_source.replace(" ", "")):
            reasons.append("evidence not found in source text")

        # 2. value must be a real parseable date present in the block
        value = fact.get("value", "")
        change_from = fact.get("change_from") or ""
        inferred_year = int(value[:4]) if len(value) == 10 and value[:4].isdigit() else None
        dates = {d["iso"] for d in find_dates(source, default_year=inferred_year)}
        inferred_year_rollover = any(
            value[4:] == parsed[4:]
            and change_from
            and value[:4].isdigit()
            and change_from[:4].isdigit()
            and int(value[:4]) == int(change_from[:4]) + 1
            for parsed in dates
            if len(value) == 10 and len(change_from) == 10
        )
        if value not in dates and not inferred_year_rollover:
            reasons.append("value date not found in source block")

        # 3. hedges force the fact into the low-confidence lane
        if fact.get("uncertain") or has_hedge(source):
            fact["confidence"] = min(_safe_confidence(fact.get("confidence")), 0.5)
            fact["uncertain"] = True

        if heuristics.is_negated_assertion(source):
            reasons.append("date appears in a negated or cancelled assertion")

        if reasons:
            fact["review_note"] = "; ".join(reasons)
            dropped.append(fact)
        else:
            kept.append(fact)
    return kept, dropped


def _llm_reflection(blocks: list[dict], facts: list[dict], llm: LLMClient) -> list[dict]:
    """Ask the model to re-check each extracted fact against the original text."""
    if not facts:
        return facts
    corpus = "\n".join(f"[{b['location']}] {b['text']}" for b in blocks)
    payload = [
        {k: f.get(k) for k in ("predicate", "value", "surface_form", "location", "evidence")}
        for f in facts
    ]
    system = (
        "You are a Fact Extraction Reviewer. For each candidate fact, verify it "
        "against the source blocks: correct predicate/value if wrong, keep the "
        "original evidence quote, lower confidence to <=0.5 if hedged or ambiguous, "
        "drop facts not supported by the text (set supported=false). "
        'Return JSON {"facts":[{...candidate fields..., "supported": true|false}]}.'
    )
    data = llm.complete_json(system, f"Source:\n{corpus}\n\nCandidates:\n{payload}", purpose="reflect")
    if not isinstance(data, dict) or not isinstance(data.get("facts"), list):
        return facts
    reviewed = []
    by_index = {f"{f['predicate']}|{f['value']}|{f['location']}": f for f in facts}
    for item in data.get("facts", []):
        if not isinstance(item, dict) or not item.get("supported", True):
            continue
        key = f"{item.get('predicate')}|{item.get('value')}|{item.get('location')}"
        base = by_index.get(key)
        if base is None:
            # A reviewer may correct the value, but it may not introduce a
            # brand-new predicate/location. Preserve provenance fields from
            # the unique original candidate at that source location.
            same_source = [
                fact for fact in facts
                if fact.get("predicate") == item.get("predicate")
                and fact.get("location") == item.get("location")
            ]
            if len(same_source) != 1:
                continue
            base = same_source[0]
        updates = {k: v for k, v in item.items() if k in ("value", "confidence")}
        if "confidence" in updates:
            updates["confidence"] = _safe_confidence(updates["confidence"])
        base = {**base, **updates}
        derived = _derive_transition(
            base.get("evidence", ""), base.get("predicate", ""), base.get("value", "")
        )
        base["change_from"] = derived.get("change_from")
        base["uncertain"] = (
            _safe_confidence(base.get("confidence")) <= 0.5
            or bool(derived.get("uncertain"))
        )
        reviewed.append(base)
    return reviewed


# ------------------------------------------------------------------ entries
def _quarantine_fallback(facts: list[dict], reason: str) -> list[dict]:
    """Keep heuristic recovery useful without letting it trigger conflicts.

    A fallback means the model did not positively support the result.  The
    candidate remains visible for human review, but is deliberately kept
    below the verified-fact threshold and therefore cannot advance Current
    Truth or enter automatic conflict detection.
    """
    for fact in facts:
        fact["confidence"] = min(
            _safe_confidence(fact.get("confidence")),
            max(0.0, min(0.5, settings.fact_unverified_threshold - 0.01)),
        )
        fact["uncertain"] = True
        fact["status"] = "unverified"
        fact["extracted_by"] = "heuristic_fallback"
        fact["fallback_reason"] = reason
    return facts


def extract_facts(
    blocks: list[dict], llm: LLMClient | None = None, *, allow_fallback: bool = True,
    use_default_llm: bool = True,
) -> list[dict]:
    llm = llm or (get_llm() if use_default_llm else None)
    if llm is not None and llm.available:
        facts = _llm_extract(blocks, llm)
        if facts or not allow_fallback:
            return facts
        return _quarantine_fallback(
            _heuristic_extract(blocks), "llm_empty_or_invalid_extraction"
        )
    return _heuristic_extract(blocks)


def reflect_facts(
    blocks: list[dict], facts: list[dict], llm: LLMClient | None = None,
    *, allow_fallback: bool = True, use_default_llm: bool = True,
):
    """Self-correction node. Returns (verified_facts, dropped_with_reason)."""
    llm = llm or (get_llm() if use_default_llm else None)
    fallback_input = bool(facts) and all(
        fact.get("extracted_by") == "heuristic_fallback" for fact in facts
    )
    if llm is not None and llm.available and facts and not fallback_input:
        facts = _llm_reflection(blocks, facts, llm)
    kept, dropped = _deterministic_review(blocks, facts)
    if allow_fallback and llm is not None and llm.available and not kept:
        fallback_kept, fallback_dropped = _deterministic_review(
            blocks, _heuristic_extract(blocks)
        )
        return _quarantine_fallback(
            fallback_kept, "llm_empty_or_invalid_reflection"
        ), dropped + fallback_dropped
    return kept, dropped
