"""Fact store tool: persistence + current-truth transitions (proposal #16, #33).

Semantic memory = fact store. When a newer fact lands for the same
(entity, predicate), the previous current fact flips to is_current=False
(episodic memory: the old row is kept, never deleted) and the caller creates a
ChangeEvent linking old_fact_id -> new_fact_id.
"""
from __future__ import annotations

import re

from sqlalchemy import select
from sqlalchemy.orm import Session

from backend.config import settings
from backend.models import Artifact, ArtifactVersion, Fact, uid
from backend.utils.dates import find_dates

_AUTHORITY_CUES = re.compile(
    r"由.{0,30}(?:调整|变更|推迟|延后|延期|改|挪|提前)(?:至|到|为)"
    r"|(?:调整|变更|推迟|延后|延期|改|挪|提前)(?:至|到|为)"
    r"|当前口径|最新口径|最终定为|确定为|决议|决定"
)

_DECISION_RECORD_SOURCE = re.compile(
    r"decision[_ -]?record|(?:^|[_ .-])adr(?:[_ .-]|$)|决策记录|决议记录|决定记录", re.I
)
_DECISION_SOURCE = re.compile(r"decision|决策|决议|决定", re.I)
_PROJECT_MANAGEMENT_SOURCE = re.compile(
    r"project.?management|tracking|tracker|roadmap|jira|项目管理|项目跟踪", re.I
)
_PRD_SOURCE = re.compile(r"(?:^|[^a-z])prd(?:[^a-z]|$)|需求", re.I)
_PLAN_SOURCE = re.compile(r"plan|schedule|排期|计划", re.I)
_CHAT_SOURCE = re.compile(r"chat|聊天|群聊|im[_ -]?", re.I)
_HISTORICAL_SOURCE = re.compile(r"historical|history|archive|archived|历史|归档", re.I)


def source_authority(artifact: Artifact | None) -> int:
    """Return the MVP source-authority tier from proposal section 18.

    The score is deliberately deterministic and visible through the API. It is
    a conservative filename/role policy, not a claim that every organisation
    uses the same hierarchy; a later phase can move these patterns to workspace
    configuration without changing the transition contract.
    """
    if artifact is None:
        return 0
    name = artifact.name or ""
    if _HISTORICAL_SOURCE.search(name):
        return 30
    if _DECISION_RECORD_SOURCE.search(name):
        return 100
    if artifact.artifact_role == "meeting":
        return 90
    if _DECISION_SOURCE.search(name):
        return 100
    if _PROJECT_MANAGEMENT_SOURCE.search(name):
        return 85
    if _PRD_SOURCE.search(name):
        return 80
    if artifact.type == "xlsx" or _PLAN_SOURCE.search(name):
        return 75
    if artifact.artifact_role == "report":
        return 60
    if _CHAT_SOURCE.search(name):
        return 40
    return 70


def _can_advance_current_truth(fact: dict) -> bool:
    """Only a decision-like assertion may replace an established truth.

    A later upload is not necessarily a later decision: a stale PRD copied
    into the workspace must not reverse an approved meeting date merely due to
    ingestion order. The first fact can bootstrap truth; subsequent changes
    need an explicit transition or authoritative wording.
    """
    return bool(fact.get("change_from") or _AUTHORITY_CUES.search(fact.get("evidence", "")))


def assess_truth_transition(
    session: Session,
    artifact: Artifact,
    current: Fact,
    candidate: dict,
) -> tuple[bool, str]:
    """Apply precondition, decision-signal and source-authority gates.

    Recency is represented by ingestion order only after the other gates pass:
    an explicit decision from an equal-or-higher tier may advance truth, while
    a stale precondition or lower-authority source is parked for review.
    """
    expected_old = candidate.get("change_from")
    if expected_old and expected_old != current.value:
        return False, "stale_precondition"
    if not _can_advance_current_truth(candidate):
        return False, "not_decision"
    current_artifact = session.get(Artifact, current.artifact_id)
    if source_authority(artifact) < source_authority(current_artifact):
        return False, "lower_authority"
    return True, "accepted"


def get_current_fact(session: Session, workspace_id: str, entity_id: str, predicate: str) -> Fact | None:
    return session.scalars(
        select(Fact)
        .where(
            Fact.workspace_id == workspace_id,
            Fact.entity_id == entity_id,
            Fact.predicate == predicate,
            Fact.is_current.is_(True),
            Fact.status == "verified",
        )
        .order_by(Fact.created_at.desc())
    ).first()


def persist_facts(
    session: Session, artifact: Artifact, version: ArtifactVersion, facts: list[dict]
) -> list[Fact]:
    """Insert extracted facts; maintain is_current for (entity, predicate)."""
    stored: list[Fact] = []
    for fact in facts:
        entity_id = fact.get("entity_id")
        status = fact.get("status", "verified")
        confidence = float(fact.get("confidence", 0.0))
        if confidence < settings.fact_unverified_threshold and status == "verified":
            status = "unverified"
        if not entity_id:
            status = "unverified"

        is_current = False
        previous_current_id = None
        if status == "verified" and entity_id:
            current = get_current_fact(session, artifact.workspace_id, entity_id, fact["predicate"])
            if current is None:
                is_current = True
            elif current.value != fact["value"]:
                advance, reason = assess_truth_transition(session, artifact, current, fact)
                if reason in ("stale_precondition", "lower_authority"):
                    # Keep rejected decision assertions for review, but never
                    # let them fork the current-truth pointer.
                    status = "unverified"
                elif advance:
                    current.is_current = False
                    is_current = True
                    previous_current_id = current.id
                fact["truth_transition_reason"] = reason
            # same value -> corroborating record, current pointer unchanged

        row = Fact(
            id=uid("fact"),
            workspace_id=artifact.workspace_id,
            entity_id=entity_id,
            predicate=fact["predicate"],
            value=fact["value"],
            value_type=fact.get("value_type", "date"),
            status=status,
            is_current=is_current,
            artifact_id=artifact.id,
            artifact_version_id=version.id,
            source_location=fact.get("location", ""),
            evidence=fact.get("evidence", ""),
            confidence=confidence,
            extracted_by=fact.get("extracted_by", "heuristic"),
            effective_time=fact.get("effective_time", ""),
        )
        session.add(row)
        # Transient metadata consumed by the workflow before the session ends;
        # it avoids guessing the previous truth from row creation order.
        row._previous_current_id = previous_current_id
        row._truth_transition_reason = fact.get("truth_transition_reason", "")
        stored.append(row)
        # autoflush is disabled globally; flush each row so another fact in the
        # same document observes the current pointer established above.
        session.flush()
    return stored


def flip_current(session: Session, workspace_id: str, entity_id: str, predicate: str, to_fact_id: str) -> None:
    """Used by rollback: make *to_fact_id* the current fact again."""
    rows = session.scalars(
        select(Fact).where(
            Fact.workspace_id == workspace_id,
            Fact.entity_id == entity_id,
            Fact.predicate == predicate,
        )
    ).all()
    for row in rows:
        row.is_current = row.id == to_fact_id
    session.flush()


def list_facts(session: Session, workspace_id: str, entity_id: str | None = None) -> list[Fact]:
    query = select(Fact).where(Fact.workspace_id == workspace_id)
    if entity_id:
        query = query.where(Fact.entity_id == entity_id)
    return list(session.scalars(query).all())


def sync_written_facts(
    session: Session,
    artifact: Artifact,
    version: ArtifactVersion,
    entity_id: str,
    predicate: str,
    old_value: str,
    new_value: str,
    locations: list[str],
) -> list[Fact]:
    """Mirror a verified tool write into the Fact Store.

    Target-document facts are corroborating references, not the authoritative
    current truth, so they remain ``is_current=False`` while carrying the new
    artifact version and fresh evidence. Stale version facts stay as history and
    are ignored by retrieval.
    """
    blocks = {
        block["location"]: block["text"]
        for block in (version.parsed_content or {}).get("blocks", [])
    }
    synced: list[Fact] = []
    for location in dict.fromkeys(locations or []):
        evidence = blocks.get(location, "")
        new_year = int(new_value.split("-", 1)[0])
        evidence_dates = find_dates(evidence, default_year=new_year)
        if not evidence or not any(date["iso"] == new_value for date in evidence_dates):
            continue
        prior = session.scalars(
            select(Fact)
            .where(
                Fact.artifact_id == artifact.id,
                Fact.entity_id == entity_id,
                Fact.predicate == predicate,
                Fact.value == old_value,
                Fact.source_location == location,
            )
            .order_by(Fact.created_at.desc())
        ).first()
        row = Fact(
            id=uid("fact"),
            workspace_id=artifact.workspace_id,
            entity_id=entity_id,
            predicate=predicate,
            value=new_value,
            value_type="date",
            status="verified",
            is_current=False,
            artifact_id=artifact.id,
            artifact_version_id=version.id,
            source_location=location,
            evidence=evidence,
            confidence=prior.confidence if prior else 1.0,
            extracted_by="tool",
            effective_time=prior.effective_time if prior else "",
        )
        session.add(row)
        synced.append(row)
    session.flush()
    return synced
