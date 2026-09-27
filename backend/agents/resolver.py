"""Agent 2: Entity Resolver.

Cascade (proposal #22): Exact Match -> Alias Match -> Fuzzy/Embedding -> LLM.
Cold-start strategy (user decision): workspaces ship with a preset entity
dictionary; when the resolver is still unsure it does NOT guess — the entity is
created as pending_disambiguation, its facts are stored as unverified (never
used for conflict detection), and the user resolves it once via API/UI; the
answer is written back into the alias table (few-shot learning).
"""
from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from backend.config import settings
from backend.llm.client import LLMClient, get_llm
from backend.models import Entity
from backend.utils.text import jaccard, normalize, token_set


def _embedding(text: str) -> set[str]:
    """Offline hash embedding: CJK bigrams + ascii tokens -> bag vector."""
    return token_set(text)


def _similarity(a: str, b: str) -> float:
    ta, tb = _embedding(a), _embedding(b)
    if not ta or not tb:
        return 0.0
    return jaccard(ta, tb)


def _llm_resolve(mention: str, candidates: list[Entity], llm: LLMClient) -> str | None:
    system = (
        "You are an Entity Resolver. Decide whether the mention refers to one of "
        "the known entities. Return JSON {\"entity_id\": \"<id>\"} or "
        "{\"entity_id\": null}. Answer null when unsure."
    )
    listing = "\n".join(f"- {e.id}: {e.canonical_name} (aliases: {', '.join(e.aliases or [])})" for e in candidates)
    data = llm.complete_json(system, f"Mention: {mention}\nKnown entities:\n{listing}", purpose="resolve")
    if isinstance(data, dict) and data.get("entity_id") in {e.id for e in candidates}:
        return data["entity_id"]
    return None


def resolve_entities(
    session: Session, workspace_id: str, facts: list[dict], llm: LLMClient | None = None
) -> tuple[list[dict], list[dict]]:
    """Attach entity_id to each fact. Returns (resolved_facts, pending_disambiguations)."""
    llm = llm or get_llm()
    entities = list(
        session.scalars(select(Entity).where(Entity.workspace_id == workspace_id)).all()
    )
    pending: list[dict] = []
    entity_cache: dict[str, tuple[Entity | None, str, float]] = {}

    def _match(mention: str) -> tuple[Entity | None, str, float]:
        norm = normalize(mention).lower()
        # Pending entities are quarantine records, not known-good candidates.
        # Seeing the same unresolved mention again must reuse that pending row
        # below, but must never silently turn it into an exact match.
        candidates = [e for e in entities if e.status == "active"]
        # 1 exact canonical
        for e in candidates:
            if normalize(e.canonical_name).lower() == norm:
                return e, "exact", 1.0
        # 2 alias
        for e in candidates:
            for alias in e.aliases or []:
                if normalize(alias).lower() == norm:
                    return e, "alias", 0.95
        # 3 fuzzy (containment or jaccard above auto-link threshold)
        best, best_score = None, 0.0
        for e in candidates:
            names = [e.canonical_name] + list(e.aliases or [])
            score = max(_similarity(mention, n) for n in names)
            compact_hit = any(
                normalize(n).lower() in norm or norm in normalize(n).lower() for n in names
            )
            score = max(score, 0.85 if compact_hit else 0.0)
            if score > best_score:
                best, best_score = e, score
        if best is not None and best_score >= settings.entity_auto_link_threshold:
            return best, "fuzzy", best_score
        # 4 LLM
        if llm.available and best is not None and best_score >= 0.5:
            entity_id = _llm_resolve(mention, [best], llm)
            if entity_id:
                return best, "llm", max(best_score, 0.75)
        return None, "unresolved", best_score

    for fact in facts:
        mention = (fact.get("entity_mention") or "").strip()
        if not mention:
            fact["entity_id"] = None
            fact["status"] = "unverified"
            fact["review_note"] = "no entity mention in source block"
            continue
        if mention not in entity_cache:
            entity_cache[mention] = _match(mention)
        entity, method, score = entity_cache[mention]
        if entity is not None:
            fact["entity_id"] = entity.id
            fact["entity_name"] = entity.canonical_name
            fact["resolution_method"] = method
            fact["resolution_score"] = score
            if fact.get("confidence", 0) < settings.fact_unverified_threshold:
                fact["status"] = "unverified"
        else:
            # cold start: register a pending entity and flag for one-time user disambiguation
            existing = next(
                (
                    e
                    for e in entities
                    if e.status == "pending_disambiguation"
                    and normalize(e.canonical_name).lower() == normalize(mention).lower()
                ),
                None,
            )
            if existing is None:
                from backend.models import uid

                existing = Entity(
                    id=uid("ent"),
                    workspace_id=workspace_id,
                    entity_type="project_version",
                    canonical_name=mention,
                    aliases=[],
                    status="pending_disambiguation",
                )
                session.add(existing)
                session.flush()
                entities.append(existing)
            fact["entity_id"] = existing.id
            fact["entity_name"] = existing.canonical_name
            fact["status"] = "unverified"
            fact["review_note"] = "entity pending disambiguation"
            if all(p["mention"] != mention for p in pending):
                pending.append({"mention": mention, "entity_id": existing.id})
    session.flush()
    return facts, pending


def learn_alias(session: Session, entity: Entity, alias: str) -> None:
    """Persist a user-confirmed alias (few-shot learning for the resolver)."""
    alias = alias.strip()
    if alias and alias.lower() not in {a.lower() for a in (entity.aliases or [])}:
        entity.aliases = list(entity.aliases or []) + [alias]
    session.flush()
