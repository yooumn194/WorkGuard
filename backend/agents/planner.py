"""Change Planner + Risk Engine (proposal #27-28).

The Planner never executes; it only proposes actions. Write-method selection
implements the user's format-safety decision:

  markdown / txt  -> direct_write     (risk low)   — plain text, safe to edit
  docx / xlsx     -> suggestion patch (risk low)   — "修改建议片段 + 定位" ONLY,
                      unless WORKGUARD_OFFICE_WRITE=1 opts into controlled write
                      (single-run / non-formula-cell edits, risk medium)
  impacts         -> human_review     (risk medium)— Agent never auto-edits

Confidence gate: conflicts below WORKGUARD_CONFLICT_REVIEW_THRESHOLD are never
planned as updates; they stay in Need Review.
"""
from __future__ import annotations

from sqlalchemy.orm import Session

from backend.config import settings
from backend.models import Artifact, ChangeAction, ChangePlan, uid
from backend.tools.document_tools import file_sha256


def decide_write_method(artifact: Artifact) -> tuple[str, str]:
    """Return (action_type, method) for an artifact needing an update."""
    if artifact.type in ("markdown", "txt"):
        return "update_artifact", "direct_write"
    if settings.office_write:
        return "update_artifact", "controlled_write"
    return "suggest_patch", "suggestion"


def risk_of(action_type: str, method: str) -> str:
    if action_type == "human_review":
        return "medium"
    if method == "direct_write":
        return "low"
    if method == "controlled_write":
        return "medium"
    return "low"


def generate_plan(
    session: Session,
    change_event_row,
    conflicts: list[dict],
    impacts: list[dict],
) -> ChangePlan:
    plan = ChangePlan(
        id=uid("plan"),
        change_event_id=change_event_row.id,
        status="pending",
    )
    session.add(plan)

    threshold = settings.conflict_review_threshold
    update_locations: dict[str, list[str]] = {}
    for conflict in conflicts:
        if conflict["verdict"] == "conflict" and conflict["confidence"] >= threshold:
            locations = update_locations.setdefault(conflict["artifact_id"], [])
            location = conflict.get("location", "")
            if location not in locations:
                locations.append(location)
        elif conflict["verdict"] == "need_review":
            plan.actions.append(
                ChangeAction(
                    id=uid("act"),
                    artifact_id=conflict["artifact_id"],
                    action_type="human_review",
                    method="none",
                    old_value=change_event_row.old_value,
                    new_value=change_event_row.new_value,
                    locations=[conflict.get("location", "")],
                    risk="medium",
                    status="pending",
                )
            )

    # One action per artifact makes all confirmed replacements atomic. It also
    # ensures the optimistic version guard is evaluated once rather than making
    # a second location look stale after the first location creates a version.
    for artifact_id, locations in update_locations.items():
        artifact = session.get(Artifact, artifact_id)
        if artifact is None:
            continue
        action_type, method = decide_write_method(artifact)
        plan.actions.append(
            ChangeAction(
                id=uid("act"),
                artifact_id=artifact.id,
                action_type=action_type,
                method=method,
                old_value=change_event_row.old_value,
                new_value=change_event_row.new_value,
                locations=locations,
                risk=risk_of(action_type, method),
                status="pending",
                tool_result={
                    "plan_guard": {
                        "artifact_version": artifact.current_version,
                        "file_sha256": file_sha256(artifact),
                    }
                },
            )
        )

    for impact in impacts:
        plan.actions.append(
            ChangeAction(
                id=uid("act"),
                artifact_id=impact["artifact_id"],
                action_type="human_review",
                method="none",
                old_value=change_event_row.old_value,
                new_value=change_event_row.new_value,
                locations=[impact.get("location", "")],
                risk="medium",
                status="pending",
            )
        )

    session.add(plan)
    session.flush()
    return plan
