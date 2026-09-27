"""Impact Analyzer (proposal #26) — MVP form.

Per user decision, automatic *implicit* dependency inference by LLM is
de-emphasised (high hallucination risk). The MVP only reports impacts backed by
explicit relations: preset dependency templates seeded per workspace, plus
rules the user added manually (few, auditable, explainable). Every impact is
advisory: auto_update_allowed=False, Agent never edits dependent dates.
"""
from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from backend.models import Artifact, DependencyRule, Fact, uid
from backend.utils.dates import days_between


def seed_preset_rules(session: Session, workspace_id: str) -> None:
    """Template dependencies for a release-style project (proposal #26 phase 1)."""
    presets = [
        ("regression_deadline", "release_date", "回归测试完成时间应早于上线时间"),
        ("gray_release_date", "release_date", "灰度发布时间应早于上线时间"),
        ("announcement_date", "release_date", "对外公告时间应早于或等于上线时间"),
    ]
    for predicate_a, predicate_b, note in presets:
        session.add(
            DependencyRule(
                id=uid("rule"),
                workspace_id=workspace_id,
                predicate_a=predicate_a,
                predicate_b=predicate_b,
                relation="before",
                origin="preset",
                note=note,
            )
        )
    session.flush()


def analyze_impacts(session: Session, change_event: dict) -> list[dict]:
    rules = list(
        session.scalars(
            select(DependencyRule).where(
                DependencyRule.workspace_id == change_event["workspace_id"],
                DependencyRule.predicate_b == change_event["predicate"],
            )
        ).all()
    )
    if not rules:
        return []

    impacts: list[dict] = []
    for rule in rules:
        facts = session.scalars(
            select(Fact).where(
                Fact.workspace_id == change_event["workspace_id"],
                Fact.entity_id == change_event["entity_id"],
                Fact.predicate == rule.predicate_a,
                Fact.status == "verified",
                Fact.is_current.is_(True),
            )
        ).all()
        for fact in facts:
            try:
                gap = days_between(fact.value, change_event["new_value"])
            except ValueError:
                continue
            violation = gap < 0
            artifact = session.get(Artifact, fact.artifact_id)
            impacts.append(
                {
                    "fact_id": fact.id,
                    "artifact_id": fact.artifact_id,
                    "artifact_name": artifact.name if artifact else fact.artifact_id,
                    "location": fact.source_location,
                    "relation": f"{rule.predicate_a} {rule.relation} {rule.predicate_b}"
                    + (f"（{rule.note}）" if rule.note else ""),
                    "impact_type": "order_violation" if violation else "reconfirm",
                    "confidence": 0.85 if violation else 0.7,
                    "reason": (
                        f"上线时间由 {change_event['old_value']} 变更为 {change_event['new_value']}。"
                        + (
                            f"但「{artifact.name if artifact else ''}」的 {rule.predicate_a} = {fact.value} "
                            f"晚于新上线时间，违反「{rule.relation}」约束，需要负责人重新确认。"
                            if violation
                            else f"「{artifact.name if artifact else ''}」的 {rule.predicate_a} = {fact.value} "
                            f"依赖上线时间，是否同步调整由负责人决定，Agent 不自动修改。"
                        )
                    ),
                    "auto_update_allowed": False,
                }
            )
    return impacts
