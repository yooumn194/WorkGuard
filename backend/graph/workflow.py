"""Change-detection workflow (proposal #20 / #6).

    load_artifact -> extract_facts -> reflect_facts -> resolve_entities
      -> store_facts -> detect_changes
          |-- no change ------------------------------> finalize
          '-- change -> retrieve_candidates -> verify_conflicts
                -> analyze_impacts -> generate_plan -> request_approval
                      |-- rejected --> finalize
                      '-- approved --> execute_actions -> post_verify -> finalize

`request_approval` calls langgraph interrupt(): the run checkpoints and waits.
The approval API resumes the SAME thread with Command(resume=...), which is the
Human-in-the-loop demo point of this project.
"""
from __future__ import annotations

import logging
import sqlite3
import uuid
from pathlib import Path
from typing import TypeVar

from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.graph import END, START, StateGraph
from langgraph.types import Command, interrupt
from sqlalchemy import select

from backend.agents import extractor, planner, resolver, retriever, verifier
from backend.agents import impact as impact_agent
from backend.config import settings
from backend.db import SessionLocal
from backend.graph.state import AgentState
from backend.models import (
    AgentRun,
    Artifact,
    ArtifactVersion,
    ChangeAction,
    ChangeEvent,
    ChangePlan,
    Conflict,
    Entity,
    Fact,
    Impact,
    uid,
)
from backend.tools import document_tools, fact_store
from backend.tools.audit import log_action

logger = logging.getLogger(__name__)

T = TypeVar("T")


def _required(value: T | None, kind: str, identifier: str | None) -> T:
    """Turn broken database invariants into an explicit, actionable failure."""
    if value is None:
        raise RuntimeError(f"{kind} not found: {identifier}")
    return value

_checkpoint_context = None

def get_checkpointer():
    global _checkpoint_context
    if settings.db_url.startswith(("postgresql://", "postgresql+psycopg://")):
        from langgraph.checkpoint.postgres import PostgresSaver

        connection_url = settings.db_url.replace("postgresql+psycopg://", "postgresql://", 1)
        _checkpoint_context = PostgresSaver.from_conn_string(connection_url)
        saver = _checkpoint_context.__enter__()
        saver.setup()
        return saver
    path = settings.checkpoint_path
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), timeout=30, check_same_thread=False)
    return SqliteSaver(conn)


def close_checkpointer() -> None:
    """Release a PostgreSQL saver connection during application shutdown."""
    global _checkpoint_context, _compiled
    if _checkpoint_context is not None:
        _checkpoint_context.__exit__(None, None, None)
        _checkpoint_context = None
    _compiled = None


# ------------------------------------------------------------------ nodes
def load_artifact(state: AgentState) -> dict:
    with SessionLocal() as session:
        artifact = _required(
            session.get(Artifact, state["artifact_id"]), "artifact", state["artifact_id"]
        )
        version = _required(session.scalars(
            select(ArtifactVersion)
            .where(ArtifactVersion.artifact_id == artifact.id)
            .order_by(ArtifactVersion.version.desc())
        ).first(), "artifact version", artifact.id)
        # NB: keep state["event"] untouched — it carries thread_id for finalize
        return {"parsed": version.parsed_content}


def extract_facts(state: AgentState) -> dict:
    blocks = state["parsed"]["blocks"]
    facts = extractor.extract_facts(blocks)
    return {"extracted_facts": facts, "errors": []}


def reflect_facts(state: AgentState) -> dict:
    blocks = state["parsed"]["blocks"]
    kept, dropped = extractor.reflect_facts(blocks, state["extracted_facts"])
    return {"reviewed_facts": kept, "dropped_facts": dropped}


def resolve_entities(state: AgentState) -> dict:
    with SessionLocal() as session:
        facts, pending = resolver.resolve_entities(
            session, state["workspace_id"], state["reviewed_facts"]
        )
        session.commit()
    return {"reviewed_facts": facts, "pending_disambiguation": pending}


def store_facts(state: AgentState) -> dict:
    with SessionLocal() as session:
        artifact = _required(
            session.get(Artifact, state["artifact_id"]), "artifact", state["artifact_id"]
        )
        version = _required(session.scalars(
            select(ArtifactVersion)
            .where(ArtifactVersion.artifact_id == artifact.id)
            .order_by(ArtifactVersion.version.desc())
        ).first(), "artifact version", artifact.id)
        stored = fact_store.persist_facts(session, artifact, version, state["reviewed_facts"])
        transitions = {
            fact.id: fact._previous_current_id
            for fact in stored
            if getattr(fact, "_previous_current_id", None)
        }
        session.commit()
        return {"stored_fact_ids": [f.id for f in stored], "fact_transitions": transitions}


def detect_changes(state: AgentState) -> dict:
    """New verified current fact + previous current fact with another value -> ChangeEvent."""

    events: list[dict] = []
    with SessionLocal() as session:
        for fact_id in state["stored_fact_ids"]:
            fact = session.get(Fact, fact_id)
            if fact is None or fact.status != "verified" or not fact.is_current:
                continue
            artifact = _required(
                session.get(Artifact, fact.artifact_id), "artifact", fact.artifact_id
            )
            previous_id = (state.get("fact_transitions") or {}).get(fact.id)
            older = session.get(Fact, previous_id) if previous_id else None
            if older is None and state.get("event", {}).get("kind") == "entity.resolved":
                # Resolution promotes an existing stored row in a separate
                # service; retain the legacy lookup for that explicit flow.
                older = session.scalars(
                    select(Fact)
                    .where(
                        Fact.workspace_id == fact.workspace_id,
                        Fact.entity_id == fact.entity_id,
                        Fact.predicate == fact.predicate,
                        Fact.status == "verified",
                        Fact.id != fact.id,
                    )
                    .order_by(Fact.created_at.desc(), Fact.id.desc())
                ).first()
            if older is None or older.value == fact.value:
                continue  # first record or corroborating record: nothing changed
            entity = session.get(Entity, fact.entity_id)
            event_row = ChangeEvent(
                id=uid("chg"),
                workspace_id=fact.workspace_id,
                thread_id=state["event"]["thread_id"],
                entity_id=fact.entity_id,
                entity_name=entity.canonical_name if entity else "",
                predicate=fact.predicate,
                old_value=older.value,
                new_value=fact.value,
                old_fact_id=older.id,
                new_fact_id=fact.id,
                source_artifact_id=artifact.id,
                confidence=fact.confidence,
                status="detected",
            )
            session.add(event_row)
            session.flush()
            events.append(
                {
                    "change_event_id": event_row.id,
                    "entity_id": fact.entity_id,
                    "entity_name": event_row.entity_name,
                    "predicate": fact.predicate,
                    "old_value": older.value,
                    "new_value": fact.value,
                    "source_artifact_id": artifact.id,
                    "source_artifact_name": artifact.name,
                    "confidence": fact.confidence,
                    "evidence": fact.evidence,
                    "location": fact.source_location,
                }
            )
        session.commit()
    return {"change_events": events}


def retrieve_candidates(state: AgentState) -> dict:
    candidates: dict[str, list] = {}
    with SessionLocal() as session:
        for event in state["change_events"]:
            payload = dict(event)
            payload["workspace_id"] = state["workspace_id"]
            candidates[event["change_event_id"]] = retriever.retrieve_candidates(
                session, state["workspace_id"], payload
            )
    return {"candidates": candidates}


def verify_conflicts(state: AgentState) -> dict:
    conflicts: dict[str, list] = {}
    with SessionLocal() as session:
        for event in state["change_events"]:
            event_id = event["change_event_id"]
            block_lookup = {}
            for candidate in state["candidates"].get(event_id, []):
                artifact_id, location = candidate["artifact_id"], candidate.get("location", "")
                block_lookup[(artifact_id, location)] = _block_text(session, artifact_id, location)
            verified = verifier.verify_candidates(session, event, state["candidates"][event_id], block_lookup)
            # persist conflict rows
            for verdict in verified:
                session.add(
                    Conflict(
                        id=uid("cfl"),
                        change_event_id=event_id,
                        fact_id=verdict.get("fact_id"),
                        artifact_id=verdict["artifact_id"],
                        location=verdict.get("location", ""),
                        conflict_type=verdict.get("conflict_type", ""),
                        verdict=verdict["verdict"],
                        confidence=verdict.get("confidence", 0.0),
                        reason=verdict.get("reason", ""),
                        evidence=verdict.get("evidence", ""),
                    )
                )
            row = _required(session.get(ChangeEvent, event_id), "change event", event_id)
            has_real = any(v["verdict"] == "conflict" for v in verified)
            row.status = "verified" if has_real else "reviewing"
            session.flush()
            conflicts[event_id] = verified
        session.commit()
    return {"conflicts": conflicts}


def _block_text(session, artifact_id: str, location: str) -> str:
    version = session.scalars(
        select(ArtifactVersion)
        .where(ArtifactVersion.artifact_id == artifact_id)
        .order_by(ArtifactVersion.version.desc())
    ).first()
    if not version:
        return ""
    for block in (version.parsed_content or {}).get("blocks", []):
        if block["location"] == location:
            return block["text"]
    return ""


def analyze_impacts(state: AgentState) -> dict:
    impacts: dict[str, list] = {}
    with SessionLocal() as session:
        for event in state["change_events"]:
            event_id = event["change_event_id"]
            payload = dict(event)
            payload["workspace_id"] = state["workspace_id"]
            found = impact_agent.analyze_impacts(session, payload)
            for item in found:
                session.add(
                    Impact(
                        id=uid("imp"),
                        change_event_id=event_id,
                        fact_id=item.get("fact_id"),
                        artifact_id=item["artifact_id"],
                        relation=item.get("relation", ""),
                        impact_type=item.get("impact_type", "reconfirm"),
                        confidence=item.get("confidence", 0.5),
                        reason=item.get("reason", ""),
                        auto_update_allowed=False,
                    )
                )
            impacts[event_id] = found
        session.commit()
    return {"impacts": impacts}


def generate_plan(state: AgentState) -> dict:
    plans = []
    with SessionLocal() as session:
        for event in state["change_events"]:
            event_id = event["change_event_id"]
            row = _required(session.get(ChangeEvent, event_id), "change event", event_id)
            plan = planner.generate_plan(session, row, state["conflicts"].get(event_id, []),
                                         state["impacts"].get(event_id, []))
            row.status = "pending_approval"
            plans.append(
                {
                    "plan_id": plan.id,
                    "change_event_id": event_id,
                    "actions": [
                        {
                            "action_id": action.id,
                            "artifact_id": action.artifact_id,
                            "action_type": action.action_type,
                            "method": action.method,
                            "old_value": action.old_value,
                            "new_value": action.new_value,
                            "locations": action.locations,
                            "risk": action.risk,
                        }
                        for action in plan.actions
                    ],
                }
            )
        session.commit()
    return {"plan": {"plans": plans}}


def request_approval(state: AgentState) -> dict:
    """Human-in-the-loop checkpoint. The run pauses here (LangGraph interrupt)."""
    decision = interrupt({"plans": state["plan"]["plans"], "question": "approve change plan?"})
    return {"approval": decision}


def route_after_approval(state: AgentState) -> str:
    decision = (state.get("approval") or {}).get("decision", "rejected")
    return "execute_actions" if decision == "approved" else "finalize"


def execute_actions(state: AgentState) -> dict:
    results: list[dict] = []
    decisions = (state.get("approval") or {}).get("decisions", {})
    batch_plan_ids = [item["plan_id"] for item in state["plan"]["plans"]]

    def decision_for(action_id: str) -> str:
        if action_id in decisions:
            return decisions[action_id]
        # When callers submit per-action choices, omission means reject. This
        # prevents a partial-approval payload from silently approving the rest.
        return decisions.get("all", "reject")

    with SessionLocal() as session:
        for plan in state["plan"]["plans"]:
            plan_row = _required(
                session.get(ChangePlan, plan["plan_id"]), "change plan", plan["plan_id"]
            )
            for action in plan_row.actions:
                if action.action_type == "human_review":
                    action.status = "skipped"  # Agent never touches human decisions
                    continue
                if decision_for(action.id) == "reject":
                    action.status = "skipped"
                    results.append({"action_id": action.id, "status": "skipped"})
                    continue
                artifact = _required(
                    session.get(Artifact, action.artifact_id), "artifact", action.artifact_id
                )
                source_path = Path(artifact.source_path)
                original_bytes: bytes | None = None
                generated_patch: Path | None = None
                edits = [
                    {"location": loc, "old_iso": action.old_value, "new_iso": action.new_value}
                    for loc in (action.locations or [""])
                ]
                try:
                    original_bytes = source_path.read_bytes()
                    guard = ((action.tool_result or {}).get("plan_guard") or {})
                    if guard and (
                        guard.get("artifact_version") != artifact.current_version
                        or guard.get("file_sha256") != document_tools.file_sha256(artifact)
                    ):
                        raise RuntimeError(
                            "stale change plan: artifact changed after review; "
                            "re-run conflict detection before approving"
                        )
                    if action.method == "direct_write":
                        outcome = document_tools.write_markdown(artifact, edits)
                    elif action.method == "controlled_write":
                        outcome = (
                            document_tools.write_docx(artifact, edits)
                            if artifact.type == "docx"
                            else document_tools.write_xlsx(artifact, edits)
                        )
                        if not outcome["success"]:
                            fallback = document_tools.generate_patch(
                                artifact, edits,
                                _parsed_blocks(session, artifact.id),
                                reason="controlled write refused; degraded to suggestion",
                                patch_id=action.id,
                            )
                            action.method = "suggestion"
                            outcome = fallback
                    else:  # suggestion
                        outcome = document_tools.generate_patch(
                            artifact, edits, _parsed_blocks(session, artifact.id),
                            reason="office format: suggestion-only mode (file untouched)",
                            patch_id=action.id,
                        )
                    if action.method == "suggestion" and outcome["success"]:
                        generated_patch = Path(outcome["result"]["patch_path"])

                    if outcome["success"] and action.method in ("direct_write", "controlled_write"):
                        version = document_tools.commit_version(session, artifact)
                        action.snapshot_version_id = version.id
                        # Every plan in this run was reviewed against the same
                        # pre-execution file state. When an earlier approved
                        # action in this exact batch advances the file version,
                        # refresh later guards to that known internal state;
                        # external edits are still rejected before the first
                        # write and between independently approved runs.
                        next_guard = {
                            "artifact_version": artifact.current_version,
                            "file_sha256": document_tools.file_sha256(artifact),
                        }
                        downstream = session.scalars(
                            select(ChangeAction).where(
                                ChangeAction.change_plan_id.in_(batch_plan_ids),
                                ChangeAction.artifact_id == artifact.id,
                                ChangeAction.id != action.id,
                                ChangeAction.status == "pending",
                            )
                        ).all()
                        for pending_action in downstream:
                            pending_result = dict(pending_action.tool_result or {})
                            pending_result["plan_guard"] = next_guard
                            pending_action.tool_result = pending_result
                    if action.method == "suggestion" and outcome["success"]:
                        action.patch_path = outcome["result"]["patch_path"]

                    action.status = "executed" if outcome["success"] else "failed"
                    action.tool_result = outcome
                    log_action(
                        session, artifact.workspace_id,
                        tool=f"update_{artifact.type}",
                        action_input={"artifact": artifact.name, "edits": edits,
                                      "method": action.method},
                        action_output=outcome,
                        status="success" if outcome["success"] else "failed",
                        change_action_id=action.id,
                        change_event_id=plan["change_event_id"],
                    )
                    # Commit every filesystem action with its matching version,
                    # status and audit record. A later failure cannot roll the
                    # database back past an already-finished file operation.
                    session.commit()
                    results.append({"action_id": action.id, "status": action.status,
                                    "detail": outcome})
                except Exception as exc:  # tool failures must never crash the run
                    session.rollback()
                    compensation_errors = []
                    if original_bytes is not None:
                        try:
                            source_path.write_bytes(original_bytes)
                        except OSError as restore_exc:
                            compensation_errors.append(f"source restore failed: {restore_exc}")
                    if generated_patch and generated_patch.exists():
                        try:
                            generated_patch.unlink()
                        except OSError as remove_exc:
                            compensation_errors.append(f"patch cleanup failed: {remove_exc}")

                    # Rollback expires ORM state, so reload before recording the
                    # compensated failure in a fresh transaction.
                    action = _required(
                        session.get(ChangeAction, action.id), "change action", action.id
                    )
                    artifact = _required(
                        session.get(Artifact, action.artifact_id), "artifact", action.artifact_id
                    )
                    detail = {
                        "success": False,
                        "error": str(exc),
                        "compensated": not compensation_errors,
                        "compensation_errors": compensation_errors,
                    }
                    action.status = "failed"
                    action.tool_result = detail
                    log_action(
                        session, artifact.workspace_id,
                        tool=f"update_{artifact.type}",
                        action_input={"artifact": artifact.name, "edits": edits,
                                      "method": action.method},
                        action_output=detail,
                        status="failed",
                        change_action_id=action.id,
                        change_event_id=plan["change_event_id"],
                    )
                    results.append({"action_id": action.id, "status": "failed",
                                    "detail": detail})
                    session.commit()

            plan_row = _required(
                session.get(ChangePlan, plan["plan_id"]), "change plan", plan["plan_id"]
            )
            event_row = _required(
                session.get(ChangeEvent, plan["change_event_id"]),
                "change event",
                plan["change_event_id"],
            )
            managed = [a for a in plan_row.actions if a.action_type != "human_review"]
            failed = any(a.status == "failed" for a in managed)
            rejected = any(decision_for(a.id) == "reject" for a in managed)
            executed = any(a.status == "executed" for a in managed)
            if failed:
                plan_row.status = "partially_approved"
                event_row.status = "reviewing"
            elif rejected:
                plan_row.status = "partially_approved"
                event_row.status = "partially_executed" if executed else "reviewing"
            else:
                plan_row.status = "executed"
                event_row.status = "executed"
            session.commit()
    return {"execution_results": results}


def _parsed_blocks(session, artifact_id: str) -> list[dict]:
    version = session.scalars(
        select(ArtifactVersion)
        .where(ArtifactVersion.artifact_id == artifact_id)
        .order_by(ArtifactVersion.version.desc())
    ).first()
    return (version.parsed_content or {}).get("blocks", []) if version else []


def post_verify(state: AgentState) -> dict:
    reports: list[dict] = []
    verification_errors: list[dict] = []
    with SessionLocal() as session:
        for plan in state["plan"]["plans"]:
            plan_row = _required(
                session.get(ChangePlan, plan["plan_id"]), "change plan", plan["plan_id"]
            )
            errors_before_plan = len(verification_errors)
            for action in plan_row.actions:
                if action.status == "failed":
                    verification_errors.append({
                        "stage": "execute", "action_id": action.id,
                        "artifact_id": action.artifact_id,
                        "reason": (action.tool_result or {}).get("error", "tool execution failed"),
                    })
                    continue
                if action.status != "executed":
                    continue
                artifact = _required(
                    session.get(Artifact, action.artifact_id), "artifact", action.artifact_id
                )
                if action.method == "suggestion":
                    patch = Path(action.patch_path) if action.patch_path else None
                    patch_text = patch.read_text(encoding="utf-8") if patch and patch.exists() else ""
                    old_present = document_tools.contains_date_value(patch_text, action.old_value)
                    new_present = document_tools.contains_date_value(patch_text, action.new_value)
                    safe_to_apply = bool(
                        ((action.tool_result or {}).get("result") or {}).get("safe_to_apply")
                    )
                    ok = bool(patch_text) and old_present and new_present and safe_to_apply
                    report = {
                        "action_id": action.id,
                        "artifact": artifact.name,
                        "method": "suggestion",
                        "success": ok,
                        "result": "patch_generated" if ok else "patch_invalid",
                        "safe_to_apply": safe_to_apply,
                    }
                    reports.append(report)
                    log_action(
                        session, artifact.workspace_id, tool="post_verify",
                        action_input={"artifact": artifact.name, "expected": action.new_value,
                                      "method": "suggestion"},
                        action_output=report,
                        status="success" if ok else "failed",
                        change_action_id=action.id,
                        change_event_id=plan["change_event_id"],
                    )
                    if not ok:
                        action.status = "verification_failed"
                        verification_errors.append({
                            "stage": "post_verify", "action_id": action.id,
                            "artifact": artifact.name, "reason": "suggestion patch is missing or invalid",
                        })
                    continue
                report = document_tools.post_verify(
                    artifact, action.old_value, action.new_value, action.locations or []
                )
                if report["success"]:
                    version = _required(
                        session.get(ArtifactVersion, action.snapshot_version_id),
                        "artifact version",
                        action.snapshot_version_id,
                    )
                    event_row = _required(
                        session.get(ChangeEvent, plan["change_event_id"]),
                        "change event",
                        plan["change_event_id"],
                    )
                    synced = fact_store.sync_written_facts(
                        session=session,
                        artifact=artifact,
                        version=version,
                        entity_id=event_row.entity_id,
                        predicate=event_row.predicate,
                        old_value=action.old_value,
                        new_value=action.new_value,
                        locations=action.locations or [],
                    )
                    report["synced_fact_ids"] = [fact.id for fact in synced]
                    tool_result = dict(action.tool_result or {})
                    result = dict(tool_result.get("result") or {})
                    result["synced_fact_ids"] = report["synced_fact_ids"]
                    tool_result["result"] = result
                    action.tool_result = tool_result
                reports.append({"action_id": action.id, "artifact": artifact.name,
                                "method": action.method, **report})
                log_action(
                    session, artifact.workspace_id, tool="post_verify",
                    action_input={"artifact": artifact.name, "expected": action.new_value},
                    action_output=report,
                    status="success" if report["success"] else "failed",
                    change_action_id=action.id,
                    change_event_id=plan["change_event_id"],
                )
                if not report["success"]:
                    action.status = "verification_failed"
                    verification_errors.append({
                        "stage": "post_verify", "action_id": action.id,
                        "artifact": artifact.name, "reason": "written value did not pass re-read verification",
                    })

            if len(verification_errors) > errors_before_plan:
                plan_row.status = "verification_failed"
                event_row = _required(
                    session.get(ChangeEvent, plan["change_event_id"]),
                    "change event",
                    plan["change_event_id"],
                )
                event_row.status = "verification_failed"
        session.commit()
    return {
        "post_verification": reports,
        "errors": list(state.get("errors", [])) + verification_errors,
    }


def finalize(state: AgentState) -> dict:
    import datetime

    with SessionLocal() as session:
        run = session.scalars(
            select(AgentRun).where(AgentRun.thread_id == state["event"]["thread_id"])
        ).first()
        if run:
            if state.get("errors"):
                run.status = "failed"
            elif (state.get("approval") or {}).get("decision") == "rejected":
                run.status = "rejected"
            else:
                run.status = "completed"
            run.errors = state.get("errors", [])
            run.finished_at = datetime.datetime.now()
        session.commit()
    return {}


# ------------------------------------------------------------------ wiring
def has_change_events(state: AgentState) -> str:
    return "retrieve_candidates" if state.get("change_events") else "finalize"


def route_from_start(state: AgentState) -> str:
    return "detect_changes" if state.get("event", {}).get("kind") == "entity.resolved" else "load_artifact"


def build_graph():
    graph = StateGraph(AgentState)
    graph.add_node("load_artifact", load_artifact)
    graph.add_node("extract_facts", extract_facts)
    graph.add_node("reflect_facts", reflect_facts)
    graph.add_node("resolve_entities", resolve_entities)
    graph.add_node("store_facts", store_facts)
    graph.add_node("detect_changes", detect_changes)
    graph.add_node("retrieve_candidates", retrieve_candidates)
    graph.add_node("verify_conflicts", verify_conflicts)
    graph.add_node("analyze_impacts", analyze_impacts)
    graph.add_node("generate_plan", generate_plan)
    graph.add_node("request_approval", request_approval)
    graph.add_node("execute_actions", execute_actions)
    graph.add_node("post_verify", post_verify)
    graph.add_node("finalize", finalize)

    graph.add_conditional_edges(
        START,
        route_from_start,
        {"load_artifact": "load_artifact", "detect_changes": "detect_changes"},
    )
    graph.add_edge("load_artifact", "extract_facts")
    graph.add_edge("extract_facts", "reflect_facts")
    graph.add_edge("reflect_facts", "resolve_entities")
    graph.add_edge("resolve_entities", "store_facts")
    graph.add_edge("store_facts", "detect_changes")
    graph.add_conditional_edges("detect_changes", has_change_events,
                                {"retrieve_candidates": "retrieve_candidates",
                                 "finalize": "finalize"})
    graph.add_edge("retrieve_candidates", "verify_conflicts")
    graph.add_edge("verify_conflicts", "analyze_impacts")
    graph.add_edge("analyze_impacts", "generate_plan")
    graph.add_edge("generate_plan", "request_approval")
    graph.add_conditional_edges("request_approval", route_after_approval,
                                {"execute_actions": "execute_actions",
                                 "finalize": "finalize"})
    graph.add_edge("execute_actions", "post_verify")
    graph.add_edge("post_verify", "finalize")
    graph.add_edge("finalize", END)
    return graph.compile(checkpointer=get_checkpointer())


_compiled = None


def get_graph():
    global _compiled
    if _compiled is None:
        _compiled = build_graph()
    return _compiled


def start_run(workspace_id: str, artifact_id: str, thread_id: str) -> dict:
    """First invocation; returns state snapshot after any pause."""
    graph = get_graph()
    config = {"configurable": {"thread_id": thread_id}}
    state: AgentState = {
        "workspace_id": workspace_id,
        "artifact_id": artifact_id,
        "event": {"artifact_id": artifact_id, "thread_id": thread_id, "kind": "artifact.ingested"},
        "parsed": {}, "extracted_facts": [], "reviewed_facts": [], "dropped_facts": [],
        "pending_disambiguation": [], "stored_fact_ids": [], "fact_transitions": {},
        "change_events": [],
        "candidates": {}, "conflicts": {}, "impacts": {}, "plan": {},
        "approval": {}, "execution_results": [], "post_verification": [], "errors": [],
    }
    result = graph.invoke(state, config)
    return _summarize(result)


def start_resolved_fact_detection(
    workspace_id: str, artifact_id: str, fact_ids: list[str]
) -> dict:
    """Resume the normal change lifecycle after a human resolves an entity.

    The confirmed Fact rows are reused directly, avoiding a second extraction
    and duplicate facts. Any newly visible conflict still pauses at the same
    approval node as an uploaded document.
    """
    thread_id = f"thr_{uuid.uuid4().hex[:12]}"
    with SessionLocal() as session:
        session.add(AgentRun(
            id=uid("run"), thread_id=thread_id, workspace_id=workspace_id,
            artifact_id=artifact_id,
        ))
        session.commit()

    state: AgentState = {
        "workspace_id": workspace_id,
        "artifact_id": artifact_id,
        "event": {"artifact_id": artifact_id, "thread_id": thread_id, "kind": "entity.resolved"},
        "parsed": {}, "extracted_facts": [], "reviewed_facts": [], "dropped_facts": [],
        "pending_disambiguation": [], "stored_fact_ids": fact_ids, "fact_transitions": {},
        "change_events": [],
        "candidates": {}, "conflicts": {}, "impacts": {}, "plan": {},
        "approval": {}, "execution_results": [], "post_verification": [], "errors": [],
    }
    graph = get_graph()
    config = {"configurable": {"thread_id": thread_id}}
    try:
        result = graph.invoke(state, config)
        snapshot = get_thread_state(thread_id)
    except Exception as exc:
        # Import locally to avoid an ingest/workflow module cycle at import time.
        from backend.services.ingest import _failed_run_result

        return _failed_run_result(thread_id, exc)
    with SessionLocal() as session:
        run = session.scalars(select(AgentRun).where(AgentRun.thread_id == thread_id)).first()
        if run and run.status == "running":
            run.status = "waiting_approval" if snapshot["waiting_approval"] else "completed"
            if not snapshot["waiting_approval"]:
                from backend.models import utcnow

                run.finished_at = utcnow()
        session.commit()
    return {"thread_id": thread_id, "run_state": snapshot, "summary": _summarize(result)}


def resume_run(thread_id: str, resume_payload: dict) -> dict:
    graph = get_graph()
    config = {"configurable": {"thread_id": thread_id}}
    result = graph.invoke(Command(resume=resume_payload), config)
    return _summarize(result)


def get_thread_state(thread_id: str) -> dict:
    graph = get_graph()
    config = {"configurable": {"thread_id": thread_id}}
    snapshot = graph.get_state(config)
    return {
        "thread_id": thread_id,
        "next": list(snapshot.next or []),
        "waiting_approval": any(n == "request_approval" for n in (snapshot.next or [])),
        "interrupt": (snapshot.tasks[0].interrupts[0].value if snapshot.tasks
                      and snapshot.tasks[0].interrupts else None),
    }


def _summarize(result: dict) -> dict:
    return {
        "change_events": result.get("change_events", []),
        "plan": result.get("plan", {}),
        "approval": result.get("approval", {}),
        "execution_results": result.get("execution_results", []),
        "post_verification": result.get("post_verification", []),
        "pending_disambiguation": result.get("pending_disambiguation", []),
        "dropped_facts": result.get("dropped_facts", []),
        "errors": result.get("errors", []),
    }
