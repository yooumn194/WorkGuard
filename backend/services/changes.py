"""Change lifecycle service: serialize / approve / reject / rollback.

Approval resumes the paused LangGraph thread (Command(resume=...)) — the
Human-in-the-loop demo point. Rollback restores artifact versions, flips the
fact-store current-truth pointer back, and audits everything.
"""
from __future__ import annotations

from pathlib import Path
from typing import TypeVar

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from backend.db import SessionLocal
from backend.graph import workflow
from backend.models import (
    Artifact,
    ArtifactVersion,
    ChangeAction,
    ChangeEvent,
    ChangePlan,
    Conflict,
    Fact,
    Impact,
)
from backend.tools import document_tools, fact_store
from backend.tools.audit import log_action

T = TypeVar("T")


def _required(value: T | None, kind: str, identifier: str | None) -> T:
    """Turn broken database invariants into an explicit, actionable failure."""
    if value is None:
        raise RuntimeError(f"{kind} not found: {identifier}")
    return value


def list_changes(session: Session, workspace_id: str) -> list[ChangeEvent]:
    return list(
        session.scalars(
            select(ChangeEvent)
            .where(ChangeEvent.workspace_id == workspace_id)
            .order_by(ChangeEvent.created_at.desc())
        ).all()
    )


def get_change(session: Session, change_event_id: str) -> ChangeEvent | None:
    return session.get(ChangeEvent, change_event_id)


def serialize_change(session: Session, event: ChangeEvent) -> dict:
    source_artifact = session.get(Artifact, event.source_artifact_id)
    plan = session.scalars(
        select(ChangePlan).where(ChangePlan.change_event_id == event.id)
    ).first()

    conflicts = []
    for c in session.scalars(select(Conflict).where(Conflict.change_event_id == event.id)).all():
        artifact = session.get(Artifact, c.artifact_id)
        conflicts.append(
            {
                "id": c.id,
                "artifact": artifact.name if artifact else c.artifact_id,
                "location": c.location,
                "verdict": c.verdict,
                "conflict_type": c.conflict_type,
                "confidence": c.confidence,
                "reason": c.reason,
                "evidence": c.evidence,
            }
        )
    impacts = []
    for i in session.scalars(select(Impact).where(Impact.change_event_id == event.id)).all():
        artifact = session.get(Artifact, i.artifact_id)
        impacts.append(
            {
                "id": i.id,
                "artifact": artifact.name if artifact else i.artifact_id,
                "relation": i.relation,
                "impact_type": i.impact_type,
                "confidence": i.confidence,
                "reason": i.reason,
                "auto_update_allowed": i.auto_update_allowed,
                "status": i.status,
            }
        )
    actions = []
    if plan:
        for action in plan.actions:
            artifact = session.get(Artifact, action.artifact_id)
            before, after = _action_diff_texts(session, artifact, action)
            actions.append(
                {
                    "action_id": action.id,
                    "artifact": artifact.name if artifact else action.artifact_id,
                    "artifact_type": artifact.type if artifact else "",
                    "action_type": action.action_type,
                    "method": action.method,
                    "old_value": action.old_value,
                    "new_value": action.new_value,
                    "locations": action.locations,
                    "risk": action.risk,
                    "status": action.status,
                    "patch_path": action.patch_path,
                    "before_text": before,
                    "after_text": after,
                }
            )

    new_fact = session.get(Fact, event.new_fact_id) if event.new_fact_id else None
    batch_events = session.scalars(
        select(ChangeEvent)
        .where(ChangeEvent.thread_id == event.thread_id)
        .order_by(ChangeEvent.created_at.asc(), ChangeEvent.id.asc())
    ).all()
    return {
        "change_id": event.id,
        "workspace_id": event.workspace_id,
        "status": event.status,
        "thread_id": event.thread_id,
        "entity": event.entity_name,
        "predicate": event.predicate,
        "old_value": event.old_value,
        "new_value": event.new_value,
        "confidence": event.confidence,
        "approval_scope": {
            "type": "run",
            "change_ids": [item.id for item in batch_events],
            "change_count": len(batch_events),
        },
        "source": {
            "artifact": source_artifact.name if source_artifact else "",
            "authority": fact_store.source_authority(source_artifact),
            "location": new_fact.source_location if new_fact else "",
            "evidence": new_fact.evidence if new_fact else "",
        },
        "conflicts": conflicts,
        "impacts": impacts,
        "plan": {
            "plan_id": plan.id if plan else None,
            "status": plan.status if plan else None,
            "actions": actions,
        },
    }


def _action_diff_texts(session: Session, artifact, action) -> tuple[str, str]:
    """Before/after preview for the Diff view, computed from the artifact's
    current parsed blocks. Works pre-execution (old value present) and
    post-execution (new value present; before is reconstructed)."""
    if artifact is None or not action.locations:
        return "", ""
    from sqlalchemy import select as _select

    version = session.scalars(
        _select(ArtifactVersion)
        .where(ArtifactVersion.artifact_id == artifact.id)
        .order_by(ArtifactVersion.version.desc())
    ).first()
    if version is None:
        return "", ""
    blocks = {
        block["location"]: block["text"]
        for block in (version.parsed_content or {}).get("blocks", [])
    }
    text = next(
        (blocks[loc] for loc in action.locations if loc in blocks),
        next(iter(blocks.values()), ""),
    )
    hit_old = document_tools._find_surface(text, action.old_value)
    if hit_old:
        after = text.replace(hit_old, _render_value(hit_old, action.new_value))
        return text, after
    hit_new = document_tools._find_surface(text, action.new_value)
    if hit_new:
        before = text.replace(hit_new, _render_value(hit_new, action.old_value))
        return before, text
    return text, text


def _render_value(surface: str, value: str) -> str:
    from backend.utils.dates import render_like

    return render_like(surface, value)


def _log(decisions: dict, change_event_ids: list[str], decision: str) -> None:
    with SessionLocal() as session:
        for change_event_id in change_event_ids:
            event = session.get(ChangeEvent, change_event_id)
            if event is None:
                continue
            log_action(
                session, event.workspace_id, tool="human_approval",
                action_input={
                    "change_event_id": change_event_id,
                    "approval_scope": change_event_ids,
                    "decisions": decisions,
                },
                action_output={"decision": decision}, actor="user",
                change_event_id=change_event_id,
            )
        session.commit()


def _thread_batch(session: Session, event: ChangeEvent) -> list[ChangeEvent]:
    return list(session.scalars(
        select(ChangeEvent).where(ChangeEvent.thread_id == event.thread_id)
    ).all())


def _claim_pending_batch(
    session: Session,
    requested_change_id: str,
    change_event_ids: list[str],
    status: str,
) -> None:
    """Atomically let one approve/reject request own the entire graph run."""
    claimed = session.execute(
        update(ChangeEvent)
        .where(
            ChangeEvent.id.in_(change_event_ids),
            ChangeEvent.status == "pending_approval",
        )
        .values(status=status)
    )
    if getattr(claimed, "rowcount", None) != len(change_event_ids):
        session.rollback()
        current = session.get(ChangeEvent, requested_change_id)
        if current is None:
            raise ValueError(f"change not found: {requested_change_id}")
        raise ValueError(
            f"approval batch for {requested_change_id} is no longer fully pending "
            f"(status={current.status})"
        )


def approve_change(change_event_id: str, decisions: dict | None = None) -> dict:
    """Resume the graph thread. decisions: {"all": "approve"} (default) or
    {"<action_id>": "approve"|"reject", ...}."""
    decisions = decisions or {"all": "approve"}
    with SessionLocal() as session:
        event = session.get(ChangeEvent, change_event_id)
        if event is None:
            raise ValueError(f"change not found: {change_event_id}")
        if event.status != "pending_approval":
            raise ValueError(
                f"change {change_event_id} is not pending approval (status={event.status})"
            )
        thread_id = event.thread_id
        batch = _thread_batch(session, event)
        if any(item.status != "pending_approval" for item in batch):
            raise ValueError(
                f"approval batch for {change_event_id} is no longer fully pending"
            )
        batch_ids = [item.id for item in batch]
        plans = session.scalars(
            select(ChangePlan).where(ChangePlan.change_event_id.in_(batch_ids))
        ).all()
        action_ids = {action.id for plan in plans for action in plan.actions}
        unknown = set(decisions) - action_ids - {"all"}
        invalid = {key: value for key, value in decisions.items()
                   if value not in ("approve", "reject")}
        if unknown:
            raise ValueError(f"unknown action ids: {sorted(unknown)}")
        if invalid:
            raise ValueError(f"invalid approval decisions: {invalid}")
        _claim_pending_batch(session, change_event_id, batch_ids, "approved")
        for plan in plans:
            plan.status = "approved"
            plan.approved_by = "user"
        session.commit()

    _log(decisions, batch_ids, "approved")
    summary = workflow.resume_run(thread_id, {"decision": "approved", "decisions": decisions})
    with SessionLocal() as session:
        event = _required(
            session.get(ChangeEvent, change_event_id), "change event", change_event_id
        )
        return {"change": serialize_change(session, event), "summary": summary}


def reject_change(change_event_id: str) -> dict:
    with SessionLocal() as session:
        event = session.get(ChangeEvent, change_event_id)
        if event is None:
            raise ValueError(f"change not found: {change_event_id}")
        if event.status != "pending_approval":
            raise ValueError(f"change {change_event_id} is not pending approval")
        thread_id = event.thread_id
        batch = _thread_batch(session, event)
        if any(item.status != "pending_approval" for item in batch):
            raise ValueError(
                f"approval batch for {change_event_id} is no longer fully pending"
            )
        batch_ids = [item.id for item in batch]
        _claim_pending_batch(session, change_event_id, batch_ids, "rejected")
        plans = session.scalars(
            select(ChangePlan).where(ChangePlan.change_event_id.in_(batch_ids))
        ).all()
        for plan in plans:
            plan.status = "rejected"
        session.commit()
    _log({}, batch_ids, "rejected")
    summary = workflow.resume_run(thread_id, {"decision": "rejected"})
    with SessionLocal() as session:
        event = _required(
            session.get(ChangeEvent, change_event_id), "change event", change_event_id
        )
        return {"change": serialize_change(session, event), "summary": summary}


def _rollback_change_batch(change_event_id: str) -> dict:
    """Atomically roll back every change produced by one multi-change run."""
    allowed = {
        "executed", "partially_executed", "verification_failed", "rollback_failed"
    }
    results: list[dict] = []
    with SessionLocal() as session:
        requested = session.get(ChangeEvent, change_event_id)
        if requested is None:
            raise ValueError(f"change not found: {change_event_id}")
        events = list(session.scalars(
            select(ChangeEvent).where(ChangeEvent.thread_id == requested.thread_id)
        ).all())
        if events and all(event.status == "rolled_back" for event in events):
            return {
                "change": serialize_change(session, requested),
                "rollbacks": [],
                "already_rolled_back": True,
            }
        invalid = [event for event in events if event.status not in allowed]
        if invalid:
            states = {event.id: event.status for event in invalid}
            raise ValueError(
                f"all changes in a rollback batch must be rollbackable (invalid={states})"
            )

        event_by_id = {event.id: event for event in events}
        plans = list(session.scalars(
            select(ChangePlan).where(ChangePlan.change_event_id.in_(event_by_id))
        ).all())
        event_by_plan = {plan.id: event_by_id[plan.change_event_id] for plan in plans}
        actions = [action for plan in plans for action in plan.actions]
        direct = [
            action for action in actions
            if action.method in ("direct_write", "controlled_write")
            and action.status in ("executed", "verification_failed")
        ]
        suggestions = [
            action for action in actions
            if action.method == "suggestion"
            and action.status in ("executed", "verification_failed")
        ]

        by_artifact: dict[str, list[ChangeAction]] = {}
        for action in direct:
            by_artifact.setdefault(action.artifact_id, []).append(action)

        errors: list[dict] = []
        previous_by_artifact: dict[str, ArtifactVersion] = {}
        for artifact_id, artifact_actions in by_artifact.items():
            artifact = session.get(Artifact, artifact_id)
            versions = [session.get(ArtifactVersion, action.snapshot_version_id)
                        for action in artifact_actions]
            missing = [action.id for action, version in zip(artifact_actions, versions)
                       if version is None]
            if missing:
                errors.extend({"action_id": action_id, "reason": "snapshot version missing"}
                              for action_id in missing)
                continue
            if artifact is None or not Path(artifact.source_path).is_file():
                errors.append({"action_id": artifact_actions[0].id,
                               "reason": "current source file missing"})
                continue
            valid_versions = [version for version in versions if version is not None]
            earliest = min(version.version for version in valid_versions)
            latest = max(version.version for version in valid_versions)
            if artifact.current_version != latest:
                errors.append({
                    "action_id": artifact_actions[-1].id,
                    "reason": "artifact has a newer version; stale rollback refused",
                    "action_version": latest,
                    "current_version": artifact.current_version,
                })
                continue
            previous = session.scalars(
                select(ArtifactVersion)
                .where(
                    ArtifactVersion.artifact_id == artifact_id,
                    ArtifactVersion.version < earliest,
                )
                .order_by(ArtifactVersion.version.desc())
            ).first()
            if previous is None:
                errors.append({"action_id": artifact_actions[0].id,
                               "reason": "previous version missing"})
            else:
                previous_by_artifact[artifact_id] = previous

        for action in suggestions:
            artifact = session.get(Artifact, action.artifact_id)
            patch = Path(action.patch_path) if action.patch_path else None
            source = Path(artifact.source_path) if artifact else None
            owned = bool(
                patch and source and patch.parent == source.parent
                and (
                    (patch.name.startswith(f"{source.name}.workguard-")
                     and patch.name.endswith(".patch.md"))
                    or patch.name == f"{source.name}.workguard-patch.md"
                )
            )
            if patch and not owned:
                errors.append({"action_id": action.id,
                               "reason": "unowned suggestion patch path refused"})

        if errors:
            for event in events:
                event.status = "rollback_failed"
            for error in errors:
                action_row = _required(
                    session.get(ChangeAction, error["action_id"]),
                    "change action",
                    error["action_id"],
                )
                error_event = event_by_plan.get(action_row.change_plan_id)
                log_action(
                    session, requested.workspace_id, tool="rollback_preflight",
                    action_input={"approval_scope": list(event_by_id)},
                    action_output=error, status="failed",
                    change_action_id=error["action_id"],
                    change_event_id=error_event.id if error_event else requested.id,
                )
            session.commit()
            return {"change": serialize_change(session, requested), "rollbacks": errors,
                    "already_rolled_back": False}

        backups: dict[Path, bytes] = {}
        for artifact_id in by_artifact:
            artifact = _required(session.get(Artifact, artifact_id), "artifact", artifact_id)
            path = Path(artifact.source_path)
            backups[path] = path.read_bytes()
        for action in suggestions:
            patch = Path(action.patch_path) if action.patch_path else None
            if patch and patch.exists():
                backups[patch] = patch.read_bytes()

        try:
            for artifact_id, artifact_actions in by_artifact.items():
                artifact = _required(session.get(Artifact, artifact_id), "artifact", artifact_id)
                previous = previous_by_artifact[artifact_id]
                outcome = document_tools.restore_version(session, artifact, previous)
                if not outcome["success"]:
                    raise RuntimeError(outcome["error"])
                restored_version = _required(
                    session.get(ArtifactVersion, outcome["result"]["version_id"]),
                    "artifact version",
                    outcome["result"]["version_id"],
                )
                for action in artifact_actions:
                    event = event_by_plan[action.change_plan_id]
                    action.status = "rolled_back"
                    fact_store.sync_written_facts(
                        session=session, artifact=artifact, version=restored_version,
                        entity_id=event.entity_id, predicate=event.predicate,
                        old_value=action.new_value, new_value=action.old_value,
                        locations=action.locations or [],
                    )
                    log_action(
                        session, artifact.workspace_id, tool="rollback_change",
                        action_input={"artifact": artifact.name,
                                      "restore_to_version": previous.version,
                                      "approval_scope": list(event_by_id)},
                        action_output=outcome, status="success",
                        change_action_id=action.id, change_event_id=event.id,
                    )
                    results.append({
                        "action_id": action.id, "status": action.status,
                        "restored_to_version": previous.version,
                        "current_version": outcome["result"]["current_version"],
                    })

            for action in suggestions:
                event = event_by_plan[action.change_plan_id]
                patch = Path(action.patch_path) if action.patch_path else None
                removed = False
                if patch and patch.exists():
                    patch.unlink()
                    removed = True
                action.status = "rolled_back"
                log_action(
                    session, event.workspace_id, tool="rollback_suggestion",
                    action_input={"patch_path": action.patch_path,
                                  "approval_scope": list(event_by_id)},
                    action_output={"removed": removed, "owned_patch": True},
                    status="success", change_action_id=action.id,
                    change_event_id=event.id,
                )
                results.append({"action_id": action.id, "status": action.status,
                                "patch_removed": removed})
                action.patch_path = ""

            for event in events:
                if event.old_fact_id:
                    fact_store.flip_current(
                        session, event.workspace_id, event.entity_id,
                        event.predicate, event.old_fact_id,
                    )
                event.status = "rolled_back"
            session.commit()
            return {"change": serialize_change(session, requested),
                    "rollbacks": results, "already_rolled_back": False}
        except Exception as exc:
            session.rollback()
            compensation_errors = []
            for path, content in backups.items():
                try:
                    path.write_bytes(content)
                except OSError as restore_exc:
                    compensation_errors.append(f"{path.name}: {restore_exc}")
            events = list(session.scalars(
                select(ChangeEvent).where(ChangeEvent.thread_id == requested.thread_id)
            ).all())
            for event in events:
                event.status = "rollback_failed"
            failure = {
                "status": "failed", "reason": str(exc),
                "compensated": not compensation_errors,
                "compensation_errors": compensation_errors,
            }
            log_action(
                session, requested.workspace_id, tool="rollback_compensation",
                action_input={"approval_scope": [event.id for event in events]},
                action_output=failure, status="failed",
                change_event_id=change_event_id,
            )
            session.commit()
            requested = _required(
                session.get(ChangeEvent, change_event_id), "change event", change_event_id
            )
            return {"change": serialize_change(session, requested),
                    "rollbacks": [failure], "already_rolled_back": False}


def rollback_change(change_event_id: str) -> dict:
    """Atomically undo all executed actions of a change.

    Exact current bytes are backed up before any restore. If one file operation
    or the final database commit fails, every touched file/patch is compensated
    and the event remains retryable as ``rollback_failed``.
    """
    with SessionLocal() as session:
        event = session.get(ChangeEvent, change_event_id)
        if event is None:
            raise ValueError(f"change not found: {change_event_id}")
        batch_size = len(session.scalars(
            select(ChangeEvent).where(ChangeEvent.thread_id == event.thread_id)
        ).all())
    if batch_size > 1:
        return _rollback_change_batch(change_event_id)

    results: list[dict] = []
    with SessionLocal() as session:
        event = session.get(ChangeEvent, change_event_id)
        if event is None:
            raise ValueError(f"change not found: {change_event_id}")
        if event.status == "rolled_back":
            return {
                "change": serialize_change(session, event),
                "rollbacks": [],
                "already_rolled_back": True,
            }
        if event.status not in (
            "executed", "partially_executed", "verification_failed", "rollback_failed"
        ):
            raise ValueError(
                "only executed, partially-executed or verification-failed changes "
                f"can be rolled back (status={event.status})"
            )

        plan = session.scalars(
            select(ChangePlan).where(ChangePlan.change_event_id == event.id)
        ).first()

        # Optimistic rollback guard: never overwrite a later approved change.
        # Validate every direct target before touching any file so a stale
        # rollback request cannot leave a multi-file change half reverted.
        preflight_errors: list[dict] = []
        previous_by_action: dict[str, ArtifactVersion] = {}
        for action in plan.actions if plan else []:
            if (
                action.method not in ("direct_write", "controlled_write")
                or action.status not in ("executed", "verification_failed")
            ):
                continue
            artifact = session.get(Artifact, action.artifact_id)
            action_version = session.get(ArtifactVersion, action.snapshot_version_id)
            if artifact is None:
                preflight_errors.append({"action_id": action.id, "reason": "artifact missing"})
            elif action_version is None:
                preflight_errors.append({"action_id": action.id, "reason": "snapshot version missing"})
            elif not Path(artifact.source_path).is_file():
                preflight_errors.append({"action_id": action.id, "reason": "current source file missing"})
            elif artifact.current_version != action_version.version:
                preflight_errors.append({
                    "action_id": action.id,
                    "reason": "artifact has a newer version; stale rollback refused",
                    "action_version": action_version.version,
                    "current_version": artifact.current_version,
                })
            else:
                previous = session.scalars(
                    select(ArtifactVersion)
                    .where(
                        ArtifactVersion.artifact_id == artifact.id,
                        ArtifactVersion.version < action_version.version,
                    )
                    .order_by(ArtifactVersion.version.desc())
                ).first()
                if previous is None:
                    preflight_errors.append({"action_id": action.id, "reason": "previous version missing"})
                else:
                    previous_by_action[action.id] = previous

        # Patch deletion is limited to files generated next to the source.
        for action in plan.actions if plan else []:
            if action.method != "suggestion" or action.status not in ("executed", "verification_failed"):
                continue
            artifact = session.get(Artifact, action.artifact_id)
            patch = Path(action.patch_path) if action.patch_path else None
            source = Path(artifact.source_path) if artifact else None
            owned = bool(
                patch and source and patch.parent == source.parent
                and (
                    (patch.name.startswith(f"{source.name}.workguard-")
                     and patch.name.endswith(".patch.md"))
                    or patch.name == f"{source.name}.workguard-patch.md"
                )
            )
            if patch and not owned:
                preflight_errors.append({"action_id": action.id,
                                         "reason": "unowned suggestion patch path refused"})
        if preflight_errors:
            event.status = "rollback_failed"
            for error in preflight_errors:
                log_action(
                    session, event.workspace_id, tool="rollback_preflight",
                    action_input={"change_event_id": event.id}, action_output=error,
                    status="failed", change_action_id=error["action_id"],
                    change_event_id=event.id,
                )
            session.commit()
            return {"change": serialize_change(session, event), "rollbacks": preflight_errors}

        backups: dict[Path, bytes] = {}
        for action in plan.actions if plan else []:
            if (action.method in ("direct_write", "controlled_write")
                    and action.status in ("executed", "verification_failed")):
                artifact = _required(
                    session.get(Artifact, action.artifact_id), "artifact", action.artifact_id
                )
                path = Path(artifact.source_path)
                backups[path] = path.read_bytes()
            elif action.method == "suggestion" and action.status in ("executed", "verification_failed"):
                patch = Path(action.patch_path) if action.patch_path else None
                if patch and patch.exists():
                    backups[patch] = patch.read_bytes()

        try:
            # Restore source documents first; patches are deleted only after all
            # document restores have succeeded.
            for action in plan.actions if plan else []:
                if (action.method not in ("direct_write", "controlled_write")
                        or action.status not in ("executed", "verification_failed")):
                    continue
                artifact = _required(
                    session.get(Artifact, action.artifact_id), "artifact", action.artifact_id
                )
                previous = previous_by_action[action.id]
                outcome = document_tools.restore_version(session, artifact, previous)
                if not outcome["success"]:
                    raise RuntimeError(outcome["error"])
                action.status = "rolled_back"
                restored_version = _required(
                    session.get(ArtifactVersion, outcome["result"]["version_id"]),
                    "artifact version",
                    outcome["result"]["version_id"],
                )
                fact_store.sync_written_facts(
                    session=session, artifact=artifact, version=restored_version,
                    entity_id=event.entity_id, predicate=event.predicate,
                    old_value=action.new_value, new_value=action.old_value,
                    locations=action.locations or [],
                )
                log_action(
                    session, artifact.workspace_id, tool="rollback_change",
                    action_input={"artifact": artifact.name,
                                  "restore_to_version": previous.version},
                    action_output=outcome, status="success",
                    change_action_id=action.id, change_event_id=event.id,
                )
                results.append({
                    "action_id": action.id, "status": action.status,
                    "restored_to_version": previous.version,
                    "restored_from_version": previous.version,
                    "current_version": outcome["result"]["current_version"],
                })

            for action in plan.actions if plan else []:
                if action.method != "suggestion" or action.status not in ("executed", "verification_failed"):
                    continue
                patch = Path(action.patch_path) if action.patch_path else None
                removed = False
                if patch and patch.exists():
                    patch.unlink()
                    removed = True
                action.status = "rolled_back"
                log_action(
                    session, event.workspace_id, tool="rollback_suggestion",
                    action_input={"patch_path": action.patch_path},
                    action_output={"removed": removed, "owned_patch": True},
                    status="success", change_action_id=action.id,
                    change_event_id=event.id,
                )
                results.append({"action_id": action.id, "status": action.status,
                                "patch_removed": removed})
                action.patch_path = ""

            if event.old_fact_id:
                fact_store.flip_current(
                    session, event.workspace_id, event.entity_id, event.predicate, event.old_fact_id
                )
            event.status = "rolled_back"
            session.commit()
            serialized = serialize_change(session, event)
            return {"change": serialized, "rollbacks": results, "already_rolled_back": False}
        except Exception as exc:
            session.rollback()
            compensation_errors = []
            for path, content in backups.items():
                try:
                    path.write_bytes(content)
                except OSError as restore_exc:
                    compensation_errors.append(f"{path.name}: {restore_exc}")

            event = _required(
                session.get(ChangeEvent, change_event_id), "change event", change_event_id
            )
            event.status = "rollback_failed"
            failure = {
                "status": "failed", "reason": str(exc),
                "compensated": not compensation_errors,
                "compensation_errors": compensation_errors,
            }
            log_action(
                session, event.workspace_id, tool="rollback_compensation",
                action_input={"change_event_id": event.id}, action_output=failure,
                status="failed", change_event_id=event.id,
            )
            session.commit()
            return {"change": serialize_change(session, event), "rollbacks": [failure],
                    "already_rolled_back": False}
