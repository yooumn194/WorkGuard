"""Entity disambiguation service (cold-start, user decision #3).

When the resolver cannot confidently link a mention, the entity is parked as
pending_disambiguation and its facts stay unverified. The user resolves it ONCE;
the answer is written into the alias table so the resolver learns.
"""
from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from backend.agents.resolver import learn_alias
from backend.config import settings
from backend.llm.heuristics import extract_date_facts
from backend.models import Artifact, Entity, Fact
from backend.tools import fact_store


def _transition_candidate(fact: Fact) -> dict:
    """Recover decision metadata that is intentionally not stored as columns."""
    try:
        year = int(fact.value.split("-", 1)[0])
    except (TypeError, ValueError):
        year = None
    extracted = extract_date_facts(fact.evidence or "", default_year=year)
    match = next(
        (
            item for item in extracted
            if item.get("predicate") == fact.predicate and item.get("value") == fact.value
        ),
        {},
    )
    return {
        "predicate": fact.predicate,
        "value": fact.value,
        "evidence": fact.evidence or "",
        "change_from": match.get("change_from"),
    }


def pending_entities(session: Session, workspace_id: str) -> list[dict]:
    rows = session.scalars(
        select(Entity).where(
            Entity.workspace_id == workspace_id,
            Entity.status == "pending_disambiguation",
        )
    ).all()
    return [
        {
            "entity_id": e.id,
            "mention": e.canonical_name,
            "entity_type": e.entity_type,
            "known_entities": [
                {"entity_id": k.id, "canonical_name": k.canonical_name}
                for k in session.scalars(
                    select(Entity).where(
                        Entity.workspace_id == workspace_id, Entity.status == "active"
                    )
                ).all()
            ],
        }
        for e in rows
    ]


def resolve_entity(
    session: Session,
    workspace_id: str,
    entity_id: str,
    mode: str,
    target_entity_id: str | None = None,
) -> dict:
    """mode: "merge_to" (mention == target entity, alias learned) or
    "keep_as_new" (it really is a separate entity)."""
    entity = session.get(Entity, entity_id)
    if entity is None or entity.workspace_id != workspace_id:
        raise ValueError(f"entity not found: {entity_id}")
    if entity.status != "pending_disambiguation":
        raise ValueError("entity is not pending disambiguation")

    facts = list(session.scalars(select(Fact).where(Fact.entity_id == entity.id)).all())

    def activate(target: Entity) -> list[Fact]:
        """Promote user-confirmed facts into semantic memory in ingestion order.

        The pending lane deliberately stores facts as non-current. A human
        resolution must therefore both relink and recompute the current pointer;
        callers can then run the promoted facts through change detection without
        extracting and duplicating them again.
        """
        promoted: list[Fact] = []
        for fact in sorted(facts, key=lambda row: (row.created_at, row.id)):
            fact.entity_id = target.id
            fact.is_current = False
            if fact.confidence < settings.fact_unverified_threshold:
                fact.status = "unverified"
                session.flush()
                continue
            fact.status = "verified"
            current = fact_store.get_current_fact(
                session, workspace_id, target.id, fact.predicate
            )
            if current is None:
                fact.is_current = True
                promoted.append(fact)
            elif current.value == fact.value:
                # Corroboration is verified but does not create a second
                # current pointer or a spurious ChangeEvent.
                session.flush()
                continue
            else:
                artifact = session.get(Artifact, fact.artifact_id)
                if artifact is None:
                    fact.status = "unverified"
                    session.flush()
                    continue
                advance, reason = fact_store.assess_truth_transition(
                    session, artifact, current, _transition_candidate(fact)
                )
                if reason in ("stale_precondition", "lower_authority"):
                    fact.status = "unverified"
                    session.flush()
                    continue
                if not advance:
                    session.flush()
                    continue
                current.is_current = False
                fact.is_current = True
                promoted.append(fact)
            # SessionLocal disables autoflush; the next historical fact must
            # observe the current pointer established by this iteration.
            session.flush()
        session.flush()
        return promoted

    if mode == "merge_to":
        target = session.get(Entity, target_entity_id)
        if target is None or target.workspace_id != workspace_id:
            raise ValueError(f"target entity not found: {target_entity_id}")
        learn_alias(session, target, entity.canonical_name)
        promoted = activate(target)
        entity.status = "merged"
        return {
            "merged_into": target.canonical_name,
            "facts_relinked": len(facts),
            "facts_promoted": len(promoted),
            "_reprocess": [
                {"artifact_id": fact.artifact_id, "fact_ids": [fact.id]} for fact in promoted
            ],
        }

    if mode == "keep_as_new":
        entity.status = "active"
        promoted = activate(entity)
        return {
            "kept_as": entity.canonical_name,
            "facts_verified": len(facts),
            "facts_promoted": len(promoted),
            "_reprocess": [
                {"artifact_id": fact.artifact_id, "fact_ids": [fact.id]} for fact in promoted
            ],
        }

    raise ValueError(f"unknown mode: {mode}")
