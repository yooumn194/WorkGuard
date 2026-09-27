"""P0 reliability: compensation, partial approval and rollback idempotency."""
from pathlib import Path

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from backend.db import Base, SessionLocal, engine, init_db
from backend.graph import workflow
from backend.models import Artifact, ArtifactVersion, ChangeAction, ChangeEvent, ChangePlan, Fact
from backend.services import changes as change_service
from backend.services.ingest import create_workspace, start_change_detection, upload_artifact
from backend.tools import document_tools


@pytest.fixture()
def workspace():
    Base.metadata.drop_all(engine)
    init_db()
    with SessionLocal() as session:
        ws = create_workspace(
            session, "p0-reliability",
            preset_entities=[{"canonical_name": "Alpha V2.0", "aliases": ["Alpha"]}],
        )
        return ws.id


def _upload(workspace_id: str, name: str, text: str):
    with SessionLocal() as session:
        artifact = upload_artifact(session, workspace_id, name, text.encode())
        artifact_id = artifact.id
    return start_change_detection(workspace_id, artifact_id)


def _change_for(workspace_id: str, targets: tuple[str, ...] = ("plan.md",)):
    for name in targets:
        _upload(workspace_id, name, "Alpha V2.0 上线日期：2026-09-20。\n")
    detected = _upload(
        workspace_id, "weekly_decision.md",
        "Alpha V2.0 上线日期由 2026-09-20 调整至 2026-09-27。\n",
    )
    return detected, detected["summary"]["change_events"][0]["change_event_id"]


def _artifact_texts(workspace_id: str) -> dict[str, str]:
    with SessionLocal() as session:
        artifacts = session.scalars(
            select(Artifact).where(Artifact.workspace_id == workspace_id)
        ).all()
        return {artifact.name: Path(artifact.source_path).read_text() for artifact in artifacts}


def test_partial_approval_executes_only_explicit_action_and_can_rollback(workspace):
    _, change_id = _change_for(workspace, ("plan_a.md", "plan_b.md"))
    with SessionLocal() as session:
        event = session.get(ChangeEvent, change_id)
        actions = [a for a in event.plan.actions if a.method == "direct_write"]
        by_name = {session.get(Artifact, a.artifact_id).name: a.id for a in actions}

    # No "all" default: omitted plan_b is safely treated as rejected.
    outcome = change_service.approve_change(change_id, {by_name["plan_a.md"]: "approve"})
    assert outcome["change"]["status"] == "partially_executed"
    actions = {a["artifact"]: a for a in outcome["change"]["plan"]["actions"]
             if a["method"] == "direct_write"}
    assert actions["plan_a.md"]["status"] == "executed"
    assert actions["plan_b.md"]["status"] == "skipped"
    texts = _artifact_texts(workspace)
    assert "2026-09-27" in texts["plan_a.md"]
    assert "2026-09-20" in texts["plan_b.md"]

    rolled = change_service.rollback_change(change_id)
    assert rolled["change"]["status"] == "rolled_back"
    texts = _artifact_texts(workspace)
    assert "2026-09-20" in texts["plan_a.md"]
    assert "2026-09-20" in texts["plan_b.md"]


def test_database_commit_failure_restores_file_and_discards_version(workspace, monkeypatch):
    detected, change_id = _change_for(workspace)
    thread_id = detected["thread_id"]
    with SessionLocal() as session:
        event = session.get(ChangeEvent, change_id)
        event.status = "approved"
        event.plan.status = "approved"
        session.commit()
        target = session.scalars(select(Artifact).where(Artifact.name == "plan.md")).first()
        original = Path(target.source_path).read_bytes()
        target_id = target.id

    original_commit = Session.commit
    armed = {"value": True}

    def fail_first_execute_commit(self):
        if armed["value"]:
            armed["value"] = False
            raise RuntimeError("injected database commit failure")
        return original_commit(self)

    monkeypatch.setattr(Session, "commit", fail_first_execute_commit)
    summary = workflow.resume_run(
        thread_id, {"decision": "approved", "decisions": {"all": "approve"}}
    )
    assert summary["errors"][0]["stage"] == "execute"
    with SessionLocal() as session:
        target = session.get(Artifact, target_id)
        versions = session.scalars(
            select(ArtifactVersion).where(ArtifactVersion.artifact_id == target_id)
        ).all()
        action = session.scalars(
            select(ChangeAction).where(ChangeAction.artifact_id == target_id)
        ).first()
        tool_facts = session.scalars(
            select(Fact).where(Fact.artifact_id == target_id, Fact.extracted_by == "tool")
        ).all()
    assert Path(target.source_path).read_bytes() == original
    assert target.current_version == 1 and len(versions) == 1
    assert action.status == "failed"
    assert action.tool_result["compensated"] is True
    assert tool_facts == []


def test_file_write_failure_is_compensated_and_audited(workspace, monkeypatch):
    _, change_id = _change_for(workspace)
    with SessionLocal() as session:
        target = session.scalars(select(Artifact).where(Artifact.name == "plan.md")).first()
        original = Path(target.source_path).read_bytes()
        target_id = target.id

    def corrupt_then_fail(artifact, edits):
        Path(artifact.source_path).write_text("partial/corrupt write")
        raise OSError("injected file save failure")

    monkeypatch.setattr(document_tools, "write_markdown", corrupt_then_fail)
    outcome = change_service.approve_change(change_id, {"all": "approve"})
    assert outcome["change"]["status"] == "verification_failed"
    with SessionLocal() as session:
        target = session.get(Artifact, target_id)
        action = session.scalars(
            select(ChangeAction).where(ChangeAction.artifact_id == target_id)
        ).first()
    assert Path(target.source_path).read_bytes() == original
    assert target.current_version == 1
    assert action.tool_result["compensated"] is True


def test_multifile_rollback_failure_compensates_every_file_and_is_retryable(
    workspace, monkeypatch
):
    _, change_id = _change_for(workspace, ("plan_a.md", "plan_b.md"))
    approved = change_service.approve_change(change_id, {"all": "approve"})
    assert approved["change"]["status"] == "executed"
    before_rollback = _artifact_texts(workspace)
    assert "2026-09-27" in before_rollback["plan_a.md"]
    assert "2026-09-27" in before_rollback["plan_b.md"]

    original_restore = document_tools.restore_version
    calls = {"count": 0}

    def fail_second_restore(session, artifact, version):
        calls["count"] += 1
        if calls["count"] == 2:
            return {"success": False, "result": None, "error": "injected second restore failure"}
        return original_restore(session, artifact, version)

    monkeypatch.setattr(document_tools, "restore_version", fail_second_restore)
    failed = change_service.rollback_change(change_id)
    assert failed["change"]["status"] == "rollback_failed"
    assert failed["rollbacks"][0]["compensated"] is True
    assert _artifact_texts(workspace) == before_rollback
    with SessionLocal() as session:
        event = session.get(ChangeEvent, change_id)
        direct = [a for a in event.plan.actions if a.method == "direct_write"]
        assert all(a.status == "executed" for a in direct)
        assert all(session.get(Artifact, a.artifact_id).current_version == 2 for a in direct)

    monkeypatch.setattr(document_tools, "restore_version", original_restore)
    retried = change_service.rollback_change(change_id)
    assert retried["change"]["status"] == "rolled_back"
    texts = _artifact_texts(workspace)
    assert "2026-09-20" in texts["plan_a.md"]
    assert "2026-09-20" in texts["plan_b.md"]
    repeated = change_service.rollback_change(change_id)
    assert repeated["already_rolled_back"] is True
    assert repeated["rollbacks"] == []


def test_missing_suggestion_patch_does_not_block_idempotent_rollback(workspace):
    demo_docx = Path(__file__).resolve().parent.parent / "demo" / "workspace_alpha" / "PRD.docx"
    with SessionLocal() as session:
        artifact = upload_artifact(session, workspace, "PRD.docx", demo_docx.read_bytes())
        artifact_id = artifact.id
    start_change_detection(workspace, artifact_id)
    detected = _upload(
        workspace, "weekly_decision.md",
        "Alpha V2.0 上线日期由 2026-09-20 调整至 2026-09-27。\n",
    )
    change_id = detected["summary"]["change_events"][0]["change_event_id"]
    approved = change_service.approve_change(change_id, {"all": "approve"})
    suggestion = next(a for a in approved["change"]["plan"]["actions"]
                      if a["method"] == "suggestion")
    patch = Path(suggestion["patch_path"])
    assert patch.exists()
    patch.unlink()  # simulate manual cleanup before rollback

    rolled = change_service.rollback_change(change_id)
    assert rolled["change"]["status"] == "rolled_back"
    assert rolled["rollbacks"][0]["patch_removed"] is False
    assert change_service.rollback_change(change_id)["already_rolled_back"] is True


def test_missing_snapshot_marks_rollback_failed_then_allows_retry(workspace):
    _, change_id = _change_for(workspace)
    change_service.approve_change(change_id, {"all": "approve"})
    with SessionLocal() as session:
        event = session.get(ChangeEvent, change_id)
        action = next(a for a in event.plan.actions if a.method == "direct_write")
        real_snapshot = action.snapshot_version_id
        action.snapshot_version_id = "ver_missing"
        session.commit()

    failed = change_service.rollback_change(change_id)
    assert failed["change"]["status"] == "rollback_failed"
    assert failed["rollbacks"][0]["reason"] == "snapshot version missing"
    with SessionLocal() as session:
        action = session.scalars(
            select(ChangeAction).where(ChangeAction.change_plan_id ==
                                       select(ChangePlan.id).where(
                                           ChangePlan.change_event_id == change_id
                                       ).scalar_subquery())
        ).first()
        action.snapshot_version_id = real_snapshot
        session.commit()
    assert change_service.rollback_change(change_id)["change"]["status"] == "rolled_back"


def test_missing_source_file_marks_rollback_failed_without_server_error(workspace):
    _, change_id = _change_for(workspace)
    change_service.approve_change(change_id, {"all": "approve"})
    with SessionLocal() as session:
        target = session.scalars(select(Artifact).where(Artifact.name == "plan.md")).first()
        path = Path(target.source_path)
        current_bytes = path.read_bytes()
    path.unlink()

    failed = change_service.rollback_change(change_id)
    assert failed["change"]["status"] == "rollback_failed"
    assert failed["rollbacks"][0]["reason"] == "current source file missing"

    path.write_bytes(current_bytes)
    retried = change_service.rollback_change(change_id)
    assert retried["change"]["status"] == "rolled_back"
