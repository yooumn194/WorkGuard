"""End-to-end pipeline test: the full MVP date-change chain over the LangGraph
workflow with the sqlite checkpointer — upload -> detect -> approve -> execute
-> post verify -> rollback. Mirrors the demo but asserts every stage."""
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
DEMO = REPO_ROOT / "demo" / "workspace_alpha"

PRESET = [{"canonical_name": "Alpha V2.0", "aliases": ["Alpha", "V2.0"]}]


@pytest.fixture()
def workspace():
    from backend.db import Base, SessionLocal, engine, init_db
    from backend.services.ingest import create_workspace

    init_db()
    Base.metadata.drop_all(engine)
    init_db()
    with SessionLocal() as session:
        ws = create_workspace(session, "pipeline-test", preset_entities=PRESET)
        yield ws.id


def _upload_and_run(workspace_id, filename):
    from backend.db import SessionLocal
    from backend.services.ingest import start_change_detection, upload_artifact

    with SessionLocal() as session:
        artifact = upload_artifact(
            session, workspace_id, filename, (DEMO / filename).read_bytes()
        )
        artifact_id = artifact.id
    return start_change_detection(workspace_id, artifact_id)


def _upload_bytes_and_run(workspace_id, filename, content):
    from backend.db import SessionLocal
    from backend.services.ingest import start_change_detection, upload_artifact

    with SessionLocal() as session:
        artifact = upload_artifact(session, workspace_id, filename, content)
        artifact_id = artifact.id
    return start_change_detection(workspace_id, artifact_id)


def test_full_date_change_chain(workspace):
    from sqlalchemy import select

    from backend.db import SessionLocal
    from backend.models import ChangeEvent
    from backend.services import changes as change_service

    # 1. baseline uploads: no change events
    for name in ["PRD.docx", "release_plan.xlsx", "test_plan.docx",
                 "launch_plan.md", "weekly_0829.md"]:
        result = _upload_and_run(workspace, name)
        assert result["summary"]["change_events"] == []

    # 2. trigger upload: change detected, run paused at approval
    result = _upload_and_run(workspace, "weekly_0905.md")
    assert result["run_state"]["waiting_approval"] is True
    change_id = result["summary"]["change_events"][0]["change_event_id"]

    with SessionLocal() as session:
        event = session.get(ChangeEvent, change_id)
        card = change_service.serialize_change(session, event)
    assert card["old_value"] == "2026-09-20"
    assert card["new_value"] == "2026-09-27"
    assert card["source"]["artifact"] == "weekly_0905.md"

    verdicts = {c["artifact"]: c["verdict"] for c in card["conflicts"]}
    assert verdicts["PRD.docx"] == "conflict"
    assert verdicts["release_plan.xlsx"] == "conflict"
    assert verdicts["launch_plan.md"] == "conflict"
    assert verdicts["weekly_0829.md"] == "no_conflict"  # historical record
    assert len(card["impacts"]) >= 1                     # template dependency
    methods = {a["artifact"]: a["method"] for a in card["plan"]["actions"]
               if a["action_type"] in ("update_artifact", "suggest_patch")}
    assert methods["launch_plan.md"] == "direct_write"
    assert methods["PRD.docx"] == "suggestion"           # office -> patch only
    assert methods["release_plan.xlsx"] == "suggestion"

    # Rebuilding the graph creates a fresh SQLite saver connection, simulating
    # a single-instance service restart. The pending interrupt must survive.
    from backend.graph import workflow

    workflow._compiled = None
    assert workflow.get_thread_state(result["thread_id"])["waiting_approval"] is True

    # 3. approve -> execute -> post verify
    outcome = change_service.approve_change(change_id, {"all": "approve"})
    assert outcome["change"]["status"] == "executed"
    verify_by_artifact = {r["artifact"]: r for r in outcome["summary"]["post_verification"]}
    assert verify_by_artifact["launch_plan.md"]["success"] is True
    assert verify_by_artifact["PRD.docx"]["result"] == "patch_generated"
    assert verify_by_artifact["release_plan.xlsx"]["result"] == "patch_generated"

    from backend.models import Artifact, AuditLog

    with SessionLocal() as session:
        launch = session.scalars(
            select(Artifact).where(Artifact.name == "launch_plan.md")
        ).first()
        text = Path(launch.source_path).read_text()
        approvals = session.scalars(
            select(AuditLog).where(
                AuditLog.workspace_id == workspace,
                AuditLog.change_event_id == change_id,
                AuditLog.tool == "human_approval",
            )
        ).all()
    assert "9 月 27 日" in text
    assert len(approvals) == 1
    assert approvals[0].actor == "user"

    # 4. rollback restores file + fact truth pointer
    rolled = change_service.rollback_change(change_id)
    assert rolled["change"]["status"] == "rolled_back"
    with SessionLocal() as session:
        launch = session.scalars(
            select(Artifact).where(Artifact.name == "launch_plan.md")
        ).first()
        text = Path(launch.source_path).read_text()
        assert "9 月 20 日" in text
        fact = session.scalars(
            select(ChangeEvent).where(ChangeEvent.id == change_id)
        ).first()
        assert fact.status == "rolled_back"


def test_unverified_facts_never_trigger_changes(workspace):
    """User decision: low-confidence facts are excluded from conflict detection."""
    from sqlalchemy import select

    from backend.db import SessionLocal
    from backend.models import ChangeEvent, Fact
    from backend.services.ingest import start_change_detection, upload_artifact

    _upload_and_run(workspace, "PRD.docx")
    md = "# 0905 周会\n\n上线时间可能要延到 9 月 27 日，暂定。\n"
    with SessionLocal() as session:
        artifact = upload_artifact(session, workspace, "weekly_hedged.md", md.encode())
        artifact_id = artifact.id
    result = start_change_detection(workspace, artifact_id)
    assert result["summary"]["change_events"] == []
    with SessionLocal() as session:
        events = session.scalars(select(ChangeEvent)).all()
        assert events == []
        unverified = session.scalars(
            select(Fact).where(Fact.status == "unverified")
        ).all()
        assert any(f.value == "2026-09-27" for f in unverified)


def test_chat_answers_with_current_truth(workspace):
    from backend.db import SessionLocal
    from backend.services import changes as change_service
    from backend.services import chat as chat_service

    for name in ["PRD.docx", "weekly_0829.md", "weekly_0905.md"]:
        _upload_and_run(workspace, name)
    with SessionLocal() as session:
        change = change_service.list_changes(session, workspace)[-1]
        change_id = change.id
    outcome = change_service.approve_change(change_id, {"all": "approve"})
    assert outcome["change"]["status"] == "executed"

    with SessionLocal() as session:
        answer = chat_service.answer(session, workspace, "Alpha V2.0 现在什么时候上线？")
    assert "2026-09-27" in answer["answer"]
    assert answer["citations"][0]["artifact"] == "weekly_0905.md"


def test_post_verify_failure_marks_run_failed_and_remains_rollbackable(workspace, monkeypatch):
    """A successful tool call is not success until the written file re-verifies."""
    from sqlalchemy import select

    from backend.db import SessionLocal
    from backend.models import AgentRun, ChangeAction, ChangeEvent, Fact
    from backend.services import changes as change_service
    from backend.tools import document_tools

    for name in ["PRD.docx", "launch_plan.md", "weekly_0905.md"]:
        result = _upload_and_run(workspace, name)
    change_id = result["summary"]["change_events"][0]["change_event_id"]

    original_verify = document_tools.post_verify

    def fail_markdown_verify(artifact, old_iso, new_iso, locations):
        if artifact.name == "launch_plan.md":
            return {"success": False, "expected": new_iso, "actual": "not found",
                    "old_residuals": ["line_5"], "locations": locations}
        return original_verify(artifact, old_iso, new_iso, locations)

    monkeypatch.setattr(document_tools, "post_verify", fail_markdown_verify)
    outcome = change_service.approve_change(change_id, {"all": "approve"})

    assert outcome["change"]["status"] == "verification_failed"
    assert outcome["summary"]["errors"][0]["stage"] == "post_verify"
    with SessionLocal() as session:
        event = session.get(ChangeEvent, change_id)
        run = session.scalars(select(AgentRun).where(AgentRun.thread_id == event.thread_id)).first()
        action = session.scalars(
            select(ChangeAction).where(ChangeAction.change_plan_id == event.plan.id,
                                       ChangeAction.method == "direct_write")
        ).first()
        assert run.status == "failed"
        assert action.status == "verification_failed"
        synced = session.scalars(
            select(Fact).where(
                Fact.artifact_id == action.artifact_id,
                Fact.extracted_by == "tool",
                Fact.value == "2026-09-27",
            )
        ).all()
        assert synced == []

    rolled = change_service.rollback_change(change_id)
    assert rolled["change"]["status"] == "rolled_back"


def test_consecutive_date_changes_use_the_latest_written_fact(workspace):
    """9/20 -> 9/27 -> 10/4 must update the same target twice without stale retrieval."""
    from sqlalchemy import select

    from backend.db import SessionLocal
    from backend.models import Artifact, ArtifactVersion, Fact
    from backend.services import changes as change_service

    _upload_and_run(workspace, "launch_plan.md")
    first = _upload_bytes_and_run(
        workspace,
        "weekly_first.md",
        "# 周会\n\nAlpha V2.0 发布时间由 9月20日调整至 9月27日。\n".encode(),
    )
    first_id = first["summary"]["change_events"][0]["change_event_id"]
    approved = change_service.approve_change(first_id, {"all": "approve"})
    assert approved["change"]["status"] == "executed"

    second = _upload_bytes_and_run(
        workspace,
        "weekly_second.md",
        "# 周会\n\nAlpha V2.0 发布时间由 9月27日调整至 10月4日。\n".encode(),
    )
    second_id = second["summary"]["change_events"][0]["change_event_id"]
    with SessionLocal() as session:
        from backend.models import ChangeEvent
        event = session.get(ChangeEvent, second_id)
        card = change_service.serialize_change(session, event)
    assert any(c["artifact"] == "launch_plan.md" and c["verdict"] == "conflict"
               for c in card["conflicts"])
    approved = change_service.approve_change(second_id, {"all": "approve"})
    assert approved["change"]["status"] == "executed"

    with SessionLocal() as session:
        launch = session.scalars(select(Artifact).where(Artifact.name == "launch_plan.md")).first()
        current_version = session.scalars(
            select(ArtifactVersion).where(
                ArtifactVersion.artifact_id == launch.id,
                ArtifactVersion.version == launch.current_version,
            )
        ).first()
        synced = session.scalars(
            select(Fact).where(
                Fact.artifact_id == launch.id,
                Fact.artifact_version_id == current_version.id,
                Fact.value == "2026-10-04",
                Fact.extracted_by == "tool",
            )
        ).all()
        text = Path(launch.source_path).read_text()
    assert "10 月 4 日" in text
    assert synced


def test_stale_rollback_cannot_overwrite_a_later_date_change(workspace):
    from sqlalchemy import select

    from backend.db import SessionLocal
    from backend.models import Artifact
    from backend.services import changes as change_service

    _upload_and_run(workspace, "launch_plan.md")
    first = _upload_bytes_and_run(
        workspace, "weekly_first.md",
        "Alpha V2.0 发布时间由 9月20日调整至 9月27日。\n".encode(),
    )
    first_id = first["summary"]["change_events"][0]["change_event_id"]
    assert change_service.approve_change(first_id, {"all": "approve"})["change"]["status"] == "executed"

    second = _upload_bytes_and_run(
        workspace, "weekly_second.md",
        "Alpha V2.0 发布时间由 9月27日调整至 10月4日。\n".encode(),
    )
    second_id = second["summary"]["change_events"][0]["change_event_id"]
    assert change_service.approve_change(second_id, {"all": "approve"})["change"]["status"] == "executed"

    refused = change_service.rollback_change(first_id)
    assert refused["change"]["status"] == "rollback_failed"
    assert "stale rollback refused" in refused["rollbacks"][0]["reason"]
    with SessionLocal() as session:
        launch = session.scalars(select(Artifact).where(Artifact.name == "launch_plan.md")).first()
        text = Path(launch.source_path).read_text()
    assert "10 月 4 日" in text


def test_approval_refuses_target_changed_after_plan_was_generated(workspace):
    from sqlalchemy import select

    from backend.db import SessionLocal
    from backend.models import Artifact
    from backend.services import changes as change_service

    _upload_and_run(workspace, "launch_plan.md")
    result = _upload_bytes_and_run(
        workspace, "weekly_decision.md",
        "Alpha V2.0 发布时间由 9月20日调整至 9月27日。\n".encode(),
    )
    change_id = result["summary"]["change_events"][0]["change_event_id"]
    with SessionLocal() as session:
        launch = session.scalars(select(Artifact).where(Artifact.name == "launch_plan.md")).first()
        path = Path(launch.source_path)
        user_edit = path.read_text() + "\n用户在审批前补充的备注。\n"
        path.write_text(user_edit)

    outcome = change_service.approve_change(change_id, {"all": "approve"})
    assert outcome["change"]["status"] == "verification_failed"
    assert outcome["summary"]["errors"][0]["stage"] == "execute"
    assert "stale change plan" in outcome["summary"]["errors"][0]["reason"]
    assert Path(launch.source_path).read_text() == user_edit


def test_later_uploaded_stale_document_does_not_reverse_current_truth(workspace):
    from sqlalchemy import select

    from backend.db import SessionLocal
    from backend.models import Entity, Fact

    _upload_bytes_and_run(
        workspace, "decision_record.md",
        "Alpha V2.0 当前上线日期：2026-09-27。\n".encode(),
    )
    stale = _upload_bytes_and_run(
        workspace, "copied_old_prd.md",
        "Alpha V2.0 上线日期：2026-09-20。\n".encode(),
    )
    assert stale["summary"]["change_events"] == []
    with SessionLocal() as session:
        entity = session.scalars(
            select(Entity).where(Entity.canonical_name == "Alpha V2.0")
        ).first()
        current = session.scalars(
            select(Fact).where(
                Fact.entity_id == entity.id,
                Fact.predicate == "release_date",
                Fact.is_current.is_(True),
            )
        ).all()
    assert len(current) == 1
    assert current[0].value == "2026-09-27"


def test_multiple_conflict_locations_in_one_file_are_one_atomic_action(workspace):
    from backend.services import changes as change_service

    _upload_bytes_and_run(
        workspace, "multi_plan.md",
        ("# Alpha V2.0\n"
         "对外上线日期：2026-09-20。\n"
         "交付清单中的上线日期：2026-09-20。\n").encode(),
    )
    detected = _upload_bytes_and_run(
        workspace, "weekly_decision.md",
        "Alpha V2.0 上线日期由 2026-09-20 调整至 2026-09-27。\n".encode(),
    )
    change_id = detected["summary"]["change_events"][0]["change_event_id"]
    direct_actions = [
        action for action in detected["summary"]["plan"]["plans"][0]["actions"]
        if action["method"] == "direct_write"
    ]
    assert len(direct_actions) == 1
    assert set(direct_actions[0]["locations"]) == {"line_2", "line_3"}

    approved = change_service.approve_change(change_id, {"all": "approve"})
    assert approved["change"]["status"] == "executed"
    action = next(a for a in approved["change"]["plan"]["actions"]
                  if a["method"] == "direct_write")
    from pathlib import Path

    from sqlalchemy import select

    from backend.db import SessionLocal
    from backend.models import Artifact
    with SessionLocal() as session:
        artifact = session.scalars(
            select(Artifact).where(Artifact.name == "multi_plan.md")
        ).first()
        text = Path(artifact.source_path).read_text()
    assert text.count("2026-09-27") == 2
    assert action["status"] == "executed"
