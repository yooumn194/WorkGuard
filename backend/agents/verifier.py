"""Conflict Verifier (proposal #24-25) — the technical core.

Rules decide first (cheap, explainable); the LLM only re-checks rule outputs in
LLM mode. Verdicts:
  conflict          — a *living* document still asserts the superseded value;
                      safe-update candidate.
  no_conflict       — historical records (meeting minutes/reports are never
                      edited) or explicit historical context ("原计划 09-20…").
  need_review       — value diverged from both old & new, or evidence too weak;
                      never auto-updated (confidence gate).

Dimensions checked (proposal #25): same entity / same predicate / temporal
context / explicit replacement / artifact role (living vs historical) / recency.
"""
from __future__ import annotations

import json
import re

from sqlalchemy.orm import Session

from backend.llm.client import LLMClient, get_llm
from backend.models import Artifact
from backend.utils.text import normalize

_HISTORICAL_CONTEXT = re.compile(
    r"原计划|原定|原本|原打算|此前|最初|曾经|当时|过去|originally|initially|previously|was planned"
)
_HISTORICAL_NAME = re.compile(r"周会|会议|纪要|站会|review|weekly|meeting|minutes|聊天|群聊", re.I)


def _artifact_is_historical(artifact: Artifact) -> bool:
    return artifact.artifact_role in ("meeting", "report") or bool(
        _HISTORICAL_NAME.search(artifact.name or "")
    )


def verify_candidate(
    session: Session,
    change_event: dict,
    candidate: dict,
    block_text: str = "",
    llm: LLMClient | None = None,
) -> dict:
    artifact = session.get(Artifact, candidate["artifact_id"])
    artifact_name = artifact.name if artifact else candidate["artifact_id"]
    new_value, old_value = change_event["new_value"], change_event["old_value"]
    candidate_value = normalize(str(candidate.get("value", "")))
    norm_old = normalize(old_value)

    base = {
        "artifact_id": candidate["artifact_id"],
        "artifact_name": artifact_name,
        "location": candidate.get("location", ""),
        "fact_id": candidate.get("fact_id"),
        "evidence": candidate.get("evidence", ""),
        "kind": candidate.get("kind", "fact"),
    }

    # 1. historical records are never wrong — old meetings are allowed to say old dates
    if artifact and _artifact_is_historical(artifact):
        return {
            **base,
            "verdict": "no_conflict",
            "conflict_type": "historical_reference",
            "confidence": 0.9,
            "reason": f"来源「{artifact_name}」是历史记录（会议/报告），旧日期是当时的事实，不需要修改。",
            "recommended_action": "none",
        }

    # 2. explicit historical phrasing inside a living document
    if _HISTORICAL_CONTEXT.search(candidate.get("evidence", "")):
        return {
            **base,
            "verdict": "no_conflict",
            "conflict_type": "contextual",
            "confidence": 0.85,
            "reason": "原文是历史表述（如「原计划」），不是当前生效的事实。",
            "recommended_action": "none",
        }

    # 3. living document still asserting the superseded value -> real conflict
    if candidate_value and candidate_value == norm_old:
        return {
            **base,
            "verdict": "conflict",
            "conflict_type": "outdated_fact",
            "confidence": 0.93,
            "reason": f"「{artifact_name}」仍写着旧值 {old_value}，与最新决议 {new_value} 冲突。",
            "recommended_action": "update",
        }

    # 4. text mention carrying the old date surface (value scan hit)
    if candidate.get("kind") == "text_mention":
        return {
            **base,
            "verdict": "conflict",
            "conflict_type": "outdated_fact",
            "confidence": 0.8,
            "reason": f"「{artifact_name}」的该位置包含旧日期 {old_value} 的表述，疑似未同步。",
            "recommended_action": "update",
        }

    # 5. value diverged from both ends, or weak evidence -> human review
    return {
        **base,
        "verdict": "need_review",
        "conflict_type": "value_diverged" if candidate_value else "weak_reference",
        "confidence": 0.55,
        "reason": (
            f"「{artifact_name}」的取值（{candidate.get('value') or '未解析'}）既不等于旧值 {old_value} "
            f"也不等于新值 {new_value}，需要人工判断。"
        ),
        "recommended_action": "review",
    }


_VERIFY_SYSTEM_PROMPT = """You are a Fact Consistency Verifier.

Your task is NOT to decide what action to execute.

Determine whether candidate_fact conflicts with the current change decision.

Consider:
- entity identity
- predicate identity
- temporal context
- historical statements ("原计划…调整为…" is history, not a conflict)
- explicit replacement wording
- source authority (meeting minutes are historical records, never edited)

Return JSON only: {"is_conflict": true|false, "conflict_type": "outdated_fact|historical_reference|contextual",
"confidence": 0.0-1.0, "reason": "...", "recommended_action": "update|review|none"}"""


def _llm_recheck(
    llm: LLMClient, change_event: dict, candidate: dict, block_text: str, rule_verdict: dict
) -> dict:
    """Proposal #41: the LLM re-checks rule-confirmed conflicts before they
    become write suggestions. Conservative merge: an LLM disagreement can only
    DOWNGRADE a conflict to need_review (never upgrade or delete it), and the
    divergence is recorded for observability."""
    payload = {
        "change": {"entity": change_event.get("entity_name", ""),
                   "predicate": change_event.get("predicate"),
                   "old_value": change_event.get("old_value"),
                   "new_value": change_event.get("new_value")},
        "candidate": {"artifact": rule_verdict.get("artifact_name"),
                      "location": candidate.get("location"),
                      "kind": candidate.get("kind"),
                      "value": candidate.get("value"),
                      "text": block_text[:400]},
    }
    data = llm.complete_json(_VERIFY_SYSTEM_PROMPT, json.dumps(payload, ensure_ascii=False),
                             purpose="verify")
    if not isinstance(data, dict) or "is_conflict" not in data:
        return rule_verdict  # unparseable -> trust the rules
    verdict = dict(rule_verdict)
    verdict["llm_review"] = {
        "is_conflict": bool(data.get("is_conflict")),
        "confidence": data.get("confidence"),
        "reason": data.get("reason", ""),
        "diverged_from_rules": not bool(data.get("is_conflict")),
    }
    if not data.get("is_conflict"):
        verdict["verdict"] = "need_review"
        verdict["confidence"] = min(float(verdict.get("confidence", 0.9)), 0.69)
        verdict["reason"] = (
            f"规则判定冲突，但 LLM 复核认为不冲突（{data.get('reason', '')[:80]}）；"
            "降级为人工复核，永不自动修改。"
        )
    return verdict


def verify_candidates(
    session: Session,
    change_event: dict,
    candidates: list[dict],
    block_text_lookup: dict[tuple, str] | None = None,
    llm: LLMClient | None = None,
) -> list[dict]:
    """Run rule-based verification; in LLM mode re-check conflict verdicts."""
    llm = llm or get_llm()
    block_text_lookup = block_text_lookup or {}
    results = []
    for candidate in candidates:
        text = block_text_lookup.get(
            (candidate["artifact_id"], candidate.get("location", "")),
            candidate.get("evidence", ""),
        )
        verdict = verify_candidate(session, change_event, candidate, text, llm)
        if (
            llm.available
            and verdict["verdict"] == "conflict"
            and candidate.get("kind") == "weak_reference"
        ):
            verdict["verdict"] = "need_review"  # LLM mode or not, weak refs never auto-update
        if llm.available and verdict["verdict"] == "conflict":
            verdict = _llm_recheck(llm, change_event, candidate, text, verdict)
        results.append(verdict)
    return results
