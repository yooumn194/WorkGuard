"""P2 resilience: multi-change batches, run failures and current-version chat."""
from pathlib import Path

import pytest
from sqlalchemy import select

from backend.db import Base, SessionLocal, engine, init_db
from backend.models import AgentRun, Artifact, AuditLog, ChangeEvent, ChangePlan
from backend.services import changes as change_service
from backend.services import chat as chat_service
from backend.services.ingest import create_workspace, start_change_detection, upload_artifact
from backend.tools import document_tools


@pytest.fixture()
def workspace():
    Base.metadata.drop_all(engine)
    init_db()
    with SessionLocal() as session:
        ws = create_workspace(
            session,
            "p2-resilience",
            preset_entities=[
                {"canonical_name": "Alpha V2.0", "aliases": ["Alpha"]},
                {"canonical_name": "Beta V1.0", "aliases": ["Beta"]},
            ],
        )
        return ws.id


def _upload(workspace_id: str, name: str, text: str):
    with SessionLocal() as session:
        artifact = upload_artifact(session, workspace_id, name, text.encode("utf-8"))
        artifact_id = artifact.id
    return start_change_detection(workspace_id, artifact_id)


def _multi_change(workspace_id: str):
    _upload(
        workspace_id,
        "combined_plan.md",
        "Alpha V2.0 上线日期：2026-09-20。\n"
        "Beta V1.0 上线日期：2026-10-01。\n",
    )
    return _upload(
        workspace_id,
        "weekly_meeting.md",
        "Alpha V2.0 上线日期由 2026-09-20 调整至 2026-09-27。\n"
        "Beta V1.0 上线日期由 2026-10-01 调整至 2026-10-08。\n",
    )


def test_multi_change_run_is_approved_atomically_and_writes_same_file_in_sequence(workspace):
    detected = _multi_change(workspace)
    events = detected["summary"]["change_events"]
    assert len(events) == 2
    change_ids = {event["change_event_id"] for event in events}

    approved = change_service.approve_change(events[0]["change_event_id"], {"all": "approve"})
    assert set(approved["change"]["approval_scope"]["change_ids"]) == change_ids
    assert approved["change"]["approval_scope"]["change_count"] == 2

    with SessionLocal() as session:
        rows = session.scalars(
            select(ChangeEvent).where(ChangeEvent.id.in_(change_ids))
        ).all()
        assert {row.status for row in rows} == {"executed"}
        artifact = session.scalars(
            select(Artifact).where(Artifact.name == "combined_plan.md")
        ).one()
        audits = session.scalars(
            select(AuditLog).where(
                AuditLog.change_event_id.in_(change_ids),
                AuditLog.tool == "human_approval",
            )
        ).all()
        text = Path(artifact.source_path).read_text()
    assert "Alpha V2.0 上线日期：2026-09-27" in text
    assert "Beta V1.0 上线日期：2026-10-08" in text
    assert len(audits) == 2
    assert all(set(row.input["approval_scope"]) == change_ids for row in audits)

    rolled = change_service.rollback_change(events[1]["change_event_id"])
    assert rolled["change"]["status"] == "rolled_back"
    with SessionLocal() as session:
        rows = session.scalars(
            select(ChangeEvent).where(ChangeEvent.id.in_(change_ids))
        ).all()
        artifact = session.scalars(
            select(Artifact).where(Artifact.name == "combined_plan.md")
        ).one()
        restored = Path(artifact.source_path).read_text()
    assert {row.status for row in rows} == {"rolled_back"}
    assert "Alpha V2.0 上线日期：2026-09-20" in restored
    assert "Beta V1.0 上线日期：2026-10-01" in restored
    assert change_service.rollback_change(events[0]["change_event_id"])[
        "already_rolled_back"
    ] is True


def test_rejecting_one_change_rejects_the_whole_paused_run(workspace):
    detected = _multi_change(workspace)
    ids = [event["change_event_id"] for event in detected["summary"]["change_events"]]
    rejected = change_service.reject_change(ids[1])
    assert rejected["change"]["status"] == "rejected"
    with SessionLocal() as session:
        events = session.scalars(select(ChangeEvent).where(ChangeEvent.id.in_(ids))).all()
        plans = session.scalars(
            select(ChangePlan).where(ChangePlan.change_event_id.in_(ids))
        ).all()
        assert {event.status for event in events} == {"rejected"}
        assert {plan.status for plan in plans} == {"rejected"}


def test_multi_change_rollback_failure_compensates_all_files_and_retries(
    workspace, monkeypatch
):
    _upload(workspace, "alpha_plan.md", "Alpha V2.0 上线日期：2026-09-20。\n")
    _upload(workspace, "beta_plan.md", "Beta V1.0 上线日期：2026-10-01。\n")
    detected = _upload(
        workspace, "weekly_meeting.md",
        "Alpha V2.0 上线日期由 2026-09-20 调整至 2026-09-27。\n"
        "Beta V1.0 上线日期由 2026-10-01 调整至 2026-10-08。\n",
    )
    ids = [event["change_event_id"] for event in detected["summary"]["change_events"]]
    change_service.approve_change(ids[0], {"all": "approve"})
    with SessionLocal() as session:
        artifacts = session.scalars(
            select(Artifact).where(Artifact.name.in_(["alpha_plan.md", "beta_plan.md"]))
        ).all()
        before = {artifact.source_path: Path(artifact.source_path).read_bytes()
                  for artifact in artifacts}

    original_restore = document_tools.restore_version
    calls = {"count": 0}

    def fail_second(session, artifact, version):
        calls["count"] += 1
        if calls["count"] == 2:
            return {"success": False, "result": None, "error": "injected batch failure"}
        return original_restore(session, artifact, version)

    monkeypatch.setattr(document_tools, "restore_version", fail_second)
    failed = change_service.rollback_change(ids[1])
    assert failed["change"]["status"] == "rollback_failed"
    assert failed["rollbacks"][0]["compensated"] is True
    assert all(Path(path).read_bytes() == content for path, content in before.items())
    with SessionLocal() as session:
        events = session.scalars(select(ChangeEvent).where(ChangeEvent.id.in_(ids))).all()
        assert {event.status for event in events} == {"rollback_failed"}

    monkeypatch.setattr(document_tools, "restore_version", original_restore)
    retried = change_service.rollback_change(ids[0])
    assert retried["change"]["status"] == "rolled_back"
    with SessionLocal() as session:
        events = session.scalars(select(ChangeEvent).where(ChangeEvent.id.in_(ids))).all()
        assert {event.status for event in events} == {"rolled_back"}


def test_workflow_exception_marks_run_failed_instead_of_leaving_running(workspace, monkeypatch):
    from starlette.requests import Request

    from backend.api.routes import get_run
    from backend.graph import workflow

    with SessionLocal() as session:
        artifact = upload_artifact(
            session, workspace, "plan.md",
            "Alpha V2.0 上线日期：2026-09-20。".encode("utf-8"),
        )
        artifact_id = artifact.id

    monkeypatch.setattr(workflow, "start_run", lambda *args: (_ for _ in ()).throw(
        RuntimeError("injected workflow failure")
    ))
    result = start_change_detection(workspace, artifact_id)
    assert result["run_state"]["status"] == "failed"
    assert result["summary"]["errors"][0]["stage"] == "workflow"
    with SessionLocal() as session:
        run = session.scalars(select(AgentRun).where(AgentRun.thread_id == result["thread_id"])).one()
        assert run.status == "failed"
        assert run.finished_at is not None
        assert run.errors[0]["reason"] == "injected workflow failure"
    api_state = get_run(
        result["thread_id"],
        Request({"type": "http", "method": "GET", "path": "/", "headers": []}),
    )
    assert api_state["status"] == "failed"
    assert api_state["errors"][0]["stage"] == "workflow"


def test_chat_does_not_report_stale_facts_from_old_artifact_versions(workspace):
    _upload(workspace, "launch_plan.md", "Alpha V2.0 上线日期：2026-09-20。\n")
    detected = _upload(
        workspace, "weekly_meeting.md",
        "Alpha V2.0 上线日期由 2026-09-20 调整至 2026-09-27。\n",
    )
    change_id = detected["summary"]["change_events"][0]["change_event_id"]
    change_service.approve_change(change_id, {"all": "approve"})
    with SessionLocal() as session:
        answer = chat_service.answer(session, workspace, "Alpha V2.0 什么时候上线？")
    assert "2026-09-27" in answer["answer"]
    assert "仍有" not in answer["answer"]
    assert answer["citations"][0]["source_authority"] == 90
