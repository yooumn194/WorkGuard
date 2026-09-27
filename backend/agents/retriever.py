"""Candidate Retriever (proposal #23).

MVP hybrid retrieval, tuned for the date-change chain:
1. Metadata filter  — same workspace + same entity + same predicate, verified
   facts only (unverified facts never become candidates).
2. Value scan       — surface variants of the old date across every artifact's
   current parsed blocks (catches mentions not captured as facts, e.g. PPT-style
   one-liners). This is exact lexical matching: highest precision first.
3. BM25-lite        — lexical scoring of predicate keywords over blocks, used to
   surface weak/indirect references for the Verifier to inspect.

pgvector semantic search is the Phase-3 upgrade; the interface below stays.
"""
from __future__ import annotations

import math
import re
from collections import Counter

from sqlalchemy import select
from sqlalchemy.orm import Session

from backend.llm import heuristics
from backend.models import Artifact, ArtifactVersion, Entity, Fact
from backend.utils.dates import find_dates
from backend.utils.text import normalize


def _tokenize(text: str) -> list[str]:
    norm = normalize(text).lower()
    ascii_tokens = re.findall(r"[a-z0-9]+", norm)
    cjk = re.findall(r"[\u4e00-\u9fff]", norm)
    return ascii_tokens + cjk


class BM25Lite:
    """Tiny BM25 over a list of documents (kept dependency-free for the MVP)."""

    def __init__(self, docs: list[str], k1: float = 1.5, b: float = 0.75) -> None:
        self.k1, self.b = k1, b
        self.docs_tokens = [_tokenize(d) for d in docs]
        self.N = len(docs) or 1
        self.avgdl = sum(len(t) for t in self.docs_tokens) / self.N
        self.df: Counter = Counter()
        for tokens in self.docs_tokens:
            self.df.update(set(tokens))

    def score(self, query: str, index: int) -> float:
        tokens = _tokenize(query)
        doc = self.docs_tokens[index]
        if not doc:
            return 0.0
        tf = Counter(doc)
        score = 0.0
        for term in set(tokens):
            if term not in self.df:
                continue
            idf = math.log(1 + (self.N - self.df[term] + 0.5) / (self.df[term] + 0.5))
            score += idf * (tf[term] * (self.k1 + 1)) / (
                tf[term] + self.k1 * (1 - self.b + self.b * len(doc) / (self.avgdl or 1))
            )
        return score


def retrieve_candidates(
    session: Session, workspace_id: str, change_event: dict
) -> list[dict]:
    """Return deduplicated candidate references that still carry the old value."""
    old_iso = change_event["old_value"]
    old_year = int(old_iso.split("-", 1)[0])
    keywords = heuristics.PREDICATE_KEYWORDS.get(change_event["predicate"], [])
    source_artifact = change_event["source_artifact_id"]
    entity_id = change_event["entity_id"]

    changed_entity = session.get(Entity, entity_id) if entity_id else None
    entity_names = []
    if changed_entity:
        entity_names = [normalize(changed_entity.canonical_name).lower()] + [
            normalize(a).lower() for a in (changed_entity.aliases or [])
        ]
    entity_names = [n for n in entity_names if n]

    candidates: dict[tuple, dict] = {}

    # 1. metadata filter over the fact store
    facts = session.scalars(
        select(Fact).where(
            Fact.workspace_id == workspace_id,
            Fact.entity_id == entity_id,
            Fact.predicate == change_event["predicate"],
            Fact.status == "verified",
            Fact.value != change_event["new_value"],
        )
    ).all()
    for fact in facts:
        if fact.artifact_id == source_artifact:
            continue
        fact_artifact = session.get(Artifact, fact.artifact_id)
        current_version = session.scalars(
            select(ArtifactVersion).where(
                ArtifactVersion.artifact_id == fact.artifact_id,
                ArtifactVersion.version == fact_artifact.current_version,
            )
        ).first() if fact_artifact else None
        if current_version is not None and fact.artifact_version_id != current_version.id:
            continue  # immutable history is not a candidate for the current file
        key = (fact.artifact_id, fact.source_location)
        candidates[key] = {
            "kind": "fact",
            "fact_id": fact.id,
            "artifact_id": fact.artifact_id,
            "location": fact.source_location,
            "value": fact.value,
            "evidence": fact.evidence,
            "confidence": fact.confidence,
        }

    # 2. value scan + 3. weak-reference scan over all current artifact blocks
    artifacts = session.scalars(select(Artifact).where(Artifact.workspace_id == workspace_id)).all()
    for artifact in artifacts:
        if artifact.id == source_artifact:
            continue
        version = session.scalars(
            select(ArtifactVersion)
            .where(ArtifactVersion.artifact_id == artifact.id)
            .order_by(ArtifactVersion.version.desc())
        ).first()
        if not version:
            continue
        blocks = (version.parsed_content or {}).get("blocks", [])
        if not blocks:
            continue
        artifact_facts = session.scalars(
            select(Fact).where(
                Fact.artifact_id == artifact.id,
                Fact.artifact_version_id == version.id,
            )
        ).all()
        # the artifact attributes content to a DIFFERENT entity -> its blocks
        # are not about the changed entity unless they name it explicitly
        artifact_other_entity = any(
            f.entity_id and f.entity_id != entity_id for f in artifact_facts
        )
        # dates this artifact already explains via its own verified facts
        known_values = {f.value for f in artifact_facts}
        bm25 = BM25Lite([b["text"] for b in blocks])
        keyword_query = " ".join(keywords)
        for i, block in enumerate(blocks):
            norm_text = normalize(block["text"])
            block_lower = norm_text.lower()
            mentions_entity = any(n in block_lower for n in entity_names)
            variant_hit = next(
                (date["surface"] for date in find_dates(block["text"], default_year=old_year)
                 if date["iso"] == old_iso),
                None,
            )
            keyword_hit = any(kw.lower() in block_lower for kw in keywords)
            # this block's old-date occurrence belongs to another predicate or
            # another entity -> not a candidate for THIS change
            explained_by_other = any(
                f.value == old_iso
                and (
                    f.predicate != change_event["predicate"]
                    or (f.entity_id and f.entity_id != entity_id)
                )
                for f in artifact_facts
                if f.source_location == block["location"]
            )
            if variant_hit and not explained_by_other and (
                mentions_entity or not artifact_other_entity
            ):
                key = (artifact.id, block["location"])
                if key not in candidates:
                    candidates[key] = {
                        "kind": "text_mention",
                        "fact_id": None,
                        "artifact_id": artifact.id,
                        "location": block["location"],
                        "value": old_iso,
                        "evidence": block["text"],
                        "confidence": 0.75,
                    }
            elif (
                keyword_hit
                and not variant_hit
                and bm25.score(keyword_query, i) > 1.0
                and _has_unexplained_date(block["text"], known_values)
                and (mentions_entity or not artifact_other_entity)
            ):
                weak_key = (artifact.id, block["location"], "weak")
                candidates.setdefault(
                    weak_key,
                    {
                        "kind": "weak_reference",
                        "fact_id": None,
                        "artifact_id": artifact.id,
                        "location": block["location"],
                        "value": "",
                        "evidence": block["text"],
                        "confidence": 0.4,
                    },
                )
    return list(candidates.values())


def _has_unexplained_date(text: str, known_values: set[str]) -> bool:
    """True when the block carries a date that no verified fact of this
    artifact already explains — bare headings never qualify."""
    from backend.utils.dates import find_dates

    dates_in_block = {d["iso"] for d in find_dates(text)}
    return bool(dates_in_block - known_values)
