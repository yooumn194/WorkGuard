"""Chat entry (proposal #15): RAG over the Fact Store with citations.

MVP is template-based (deterministic, offline-friendly): find entity +
predicate from the question, answer with the CURRENT truth, its source, and
how many living documents still disagree. "I don't know" when evidence is
insufficient (product principle #5).
"""
from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from backend.models import Artifact, ArtifactVersion, Entity, Fact
from backend.tools.fact_store import source_authority
from backend.utils.text import normalize

_QUESTION_PREDICATES = [
    ("release_date", ["什么时候上线", "何时上线", "什么时候发布", "上线时间", "发布时间",
                      "release date", "when.*release", "when.*launch"]),
    ("regression_deadline", ["回归", "测试.*完成", "regression"]),
    ("gray_release_date", ["灰度", "gray"]),
    ("announcement_date", ["公告", "宣布", "announcement"]),
]


def _detect_predicate(question: str) -> str | None:
    lowered = question.lower()
    for predicate, patterns in _QUESTION_PREDICATES:
        for pattern in patterns:
            if pattern.endswith(".*") or ".*" in pattern:
                import re

                if re.search(pattern, lowered):
                    return predicate
            elif pattern in lowered:
                return predicate
    return None


def _detect_entity(session: Session, workspace_id: str, question: str) -> Entity | None:
    norm_q = normalize(question).lower()
    entities = session.scalars(
        select(Entity).where(Entity.workspace_id == workspace_id, Entity.status == "active")
    ).all()
    for entity in entities:
        names = [entity.canonical_name] + list(entity.aliases or [])
        for name in names:
            if name and normalize(name).lower() in norm_q:
                return entity
    # fall back to the single project entity of a small workspace
    return entities[0] if len(entities) == 1 else None


def answer(session: Session, workspace_id: str, question: str) -> dict:
    predicate = _detect_predicate(question)
    entity = _detect_entity(session, workspace_id, question)
    if predicate is None or entity is None:
        return {
            "answer": "我无法从当前 Fact Store 中确定这个问题的答案。"
                      "请确认问题包含明确的实体（如项目名）与属性（如上线时间）。",
            "confidence": 0.0,
            "citations": [],
        }

    fact = session.scalars(
        select(Fact)
        .where(
            Fact.workspace_id == workspace_id,
            Fact.entity_id == entity.id,
            Fact.predicate == predicate,
            Fact.is_current.is_(True),
            Fact.status == "verified",
        )
        .order_by(Fact.created_at.desc())
    ).first()
    if fact is None:
        return {
            "answer": f"当前 Fact Store 中没有「{entity.canonical_name} / {predicate}」的可靠事实。",
            "confidence": 0.0,
            "citations": [],
        }

    # how many living documents still carry the old value?
    stale = session.scalars(
        select(Fact).where(
            Fact.workspace_id == workspace_id,
            Fact.entity_id == entity.id,
            Fact.predicate == predicate,
            Fact.status == "verified",
            Fact.value != fact.value,
        )
    ).all()
    stale_names, seen = [], set()
    for f in stale:
        artifact = session.get(Artifact, f.artifact_id)
        if artifact is None or artifact.artifact_role != "document":
            continue
        current_version = session.scalars(
            select(ArtifactVersion).where(
                ArtifactVersion.artifact_id == artifact.id,
                ArtifactVersion.version == artifact.current_version,
            )
        ).first()
        if current_version is None or f.artifact_version_id != current_version.id:
            continue
        if artifact.name not in seen:
            seen.add(artifact.name)
            stale_names.append(artifact.name)

    source_artifact = session.get(Artifact, fact.artifact_id)
    citations = [
        {
            "artifact": source_artifact.name if source_artifact else fact.artifact_id,
            "location": fact.source_location,
            "evidence": fact.evidence,
            "confidence": fact.confidence,
            "source_authority": source_authority(source_artifact),
        }
    ]
    if stale_names:
        answer_text = (
            f"{entity.canonical_name} 的 {predicate} 当前为 {fact.value}。\n"
            f"主要依据：{citations[0]['artifact']}（“{citations[0]['evidence']}”）。\n"
            f"注意：仍有 {len(stale_names)} 个文档包含旧值：{'、'.join(stale_names)}。"
        )
    else:
        answer_text = (
            f"{entity.canonical_name} 的 {predicate} 当前为 {fact.value}。\n"
            f"主要依据：{citations[0]['artifact']}（“{citations[0]['evidence']}”）。"
        )
    return {"answer": answer_text, "confidence": fact.confidence, "citations": citations}
