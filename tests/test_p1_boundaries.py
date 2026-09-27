"""P1 boundary coverage: source authority, disambiguation and approval races."""
import threading
from concurrent.futures import ThreadPoolExecutor

import pytest
from sqlalchemy import select

from backend.db import Base, SessionLocal, engine, init_db
from backend.models import Artifact, ChangeEvent, Entity, Fact
from backend.services import changes as change_service
from backend.services.ingest import create_workspace, start_change_detection, upload_artifact
from backend.tools.fact_store import source_authority


@pytest.fixture()
def workspace():
    Base.metadata.drop_all(engine)
    init_db()
    with SessionLocal() as session:
        ws = create_workspace(
            session,
            "p1-boundaries",
            preset_entities=[{"canonical_name": "Alpha V2.0", "aliases": ["Alpha"]}],
        )
        return ws.id


def _upload(workspace_id: str, name: str, text: str):
    with SessionLocal() as session:
        artifact = upload_artifact(session, workspace_id, name, text.encode("utf-8"))
        artifact_id = artifact.id
    return start_change_detection(workspace_id, artifact_id)


def _current(workspace_id: str) -> Fact:
    with SessionLocal() as session:
        return session.scalars(
            select(Fact).where(
                Fact.workspace_id == workspace_id,
                Fact.predicate == "release_date",
                Fact.is_current.is_(True),
                Fact.status == "verified",
            )
        ).one()


def test_source_authority_tiers_are_deterministic():
    def artifact(name: str, kind: str = "markdown", role: str = "document"):
        return Artifact(name=name, type=kind, artifact_role=role)

    assert source_authority(artifact("decision_record.md")) == 100
    assert source_authority(artifact("meeting.md", role="meeting")) == 90
    assert source_authority(artifact("weekly_decision.md", role="meeting")) == 90
    assert source_authority(artifact("jira_tracker.md")) == 85
    assert source_authority(artifact("PRD.md")) == 80
    assert source_authority(artifact("release.xlsx", kind="xlsx")) == 75
    assert source_authority(artifact("weekly_report.md", role="report")) == 60
    assert source_authority(artifact("team_chat.md")) == 40
    assert source_authority(artifact("historical_decision.md")) == 30


def test_lower_authority_decision_cannot_replace_current_truth(workspace):
    _upload(
        workspace, "decision_record.md",
        "Alpha V2.0 当前上线日期：2026-09-27。\n",
    )
    result = _upload(
        workspace, "team_chat.md",
        "Alpha V2.0 上线日期由 2026-09-27 调整至 2026-10-04。\n",
    )
    assert result["summary"]["change_events"] == []
    assert _current(workspace).value == "2026-09-27"
    with SessionLocal() as session:
        blocked = session.scalars(
            select(Fact).where(Fact.value == "2026-10-04")
        ).one()
        assert blocked.status == "unverified"
        assert blocked.is_current is False


def test_higher_authority_decision_replaces_lower_authority_truth(workspace):
    _upload(workspace, "team_chat.md", "Alpha V2.0 上线日期：2026-09-20。\n")
    result = _upload(
        workspace, "decision_record.md",
        "Alpha V2.0 上线日期由 2026-09-20 调整至 2026-09-27。\n",
    )
    assert result["summary"]["change_events"][0]["old_value"] == "2026-09-20"
    assert result["summary"]["change_events"][0]["new_value"] == "2026-09-27"
    assert _current(workspace).value == "2026-09-27"
    with SessionLocal() as session:
        event = session.get(
            ChangeEvent, result["summary"]["change_events"][0]["change_event_id"]
        )
        assert change_service.serialize_change(session, event)["source"]["authority"] == 100


def test_same_tier_stale_meeting_precondition_is_rejected(workspace):
    _upload(workspace, "PRD.md", "Alpha V2.0 上线日期：2026-09-20。\n")
    first = _upload(
        workspace, "meeting_a.md",
        "Alpha V2.0 上线日期由 2026-09-20 调整至 2026-09-27。\n",
    )
    assert len(first["summary"]["change_events"]) == 1
    stale = _upload(
        workspace, "meeting_b.md",
        "Alpha V2.0 上线日期由 2026-09-20 调整至 2026-10-04。\n",
    )
    assert stale["summary"]["change_events"] == []
    assert _current(workspace).value == "2026-09-27"


def test_authoritative_change_without_old_value_uses_current_baseline(workspace):
    _upload(workspace, "PRD.md", "Alpha V2.0 上线日期：2026-09-20。\n")
    result = _upload(
        workspace, "meeting.md",
        "Alpha V2.0 上线日期调整至 2026-09-27。\n",
    )
    event = result["summary"]["change_events"][0]
    assert (event["old_value"], event["new_value"]) == (
        "2026-09-20", "2026-09-27"
    )


def test_rejection_rejects_propagation_but_preserves_source_truth(workspace):
    _upload(workspace, "PRD.md", "Alpha V2.0 上线日期：2026-09-20。\n")
    result = _upload(
        workspace, "meeting.md",
        "Alpha V2.0 上线日期由 2026-09-20 调整至 2026-09-27。\n",
    )
    change_id = result["summary"]["change_events"][0]["change_event_id"]
    rejected = change_service.reject_change(change_id)
    assert rejected["change"]["status"] == "rejected"
    assert rejected["change"]["plan"]["status"] == "rejected"
    assert _current(workspace).value == "2026-09-27"


def test_disambiguation_replays_multiple_facts_with_authority_rules(workspace):
    from starlette.requests import Request

    from backend.api.routes import ResolveBody
    from backend.api.routes import resolve_entity as resolve_entity_route

    _upload(workspace, "PRD.md", "Alpha V2.0 上线日期：2026-09-20。\n")
    _upload(
        workspace, "jira_tracker.md",
        "Gamma V1.0 当前口径上线日期为 2026-09-25。\n",
    )
    _upload(
        workspace, "decision_record.md",
        "Gamma V1.0 上线日期由 2026-09-25 调整至 2026-09-27。\n",
    )
    with SessionLocal() as session:
        pending = session.scalars(
            select(Entity).where(
                Entity.workspace_id == workspace,
                Entity.status == "pending_disambiguation",
            )
        ).one()
        target = session.scalars(
            select(Entity).where(Entity.canonical_name == "Alpha V2.0")
        ).one()
        pending_id, target_id = pending.id, target.id

    result = resolve_entity_route(
        pending_id,
        ResolveBody(mode="merge_to", target_entity_id=target_id),
        Request({"type": "http", "method": "POST", "path": "/", "headers": []}),
    )
    assert result["facts_promoted"] == 2
    assert sum(len(run["summary"]["change_events"]) for run in result["runs"]) == 1
    assert _current(workspace).value == "2026-09-27"
    with SessionLocal() as session:
        currents = session.scalars(
            select(Fact).where(
                Fact.workspace_id == workspace,
                Fact.entity_id == target_id,
                Fact.predicate == "release_date",
                Fact.is_current.is_(True),
            )
        ).all()
        assert len(currents) == 1


def test_concurrent_approve_and_reject_have_exactly_one_winner(workspace, monkeypatch):
    from backend.graph import workflow

    with SessionLocal() as session:
        event = ChangeEvent(
            id="chg_concurrent",
            workspace_id=workspace,
            thread_id="thr_concurrent",
            entity_id="ent_concurrent",
            entity_name="Alpha V2.0",
            predicate="release_date",
            old_value="2026-09-20",
            new_value="2026-09-27",
            source_artifact_id="art_concurrent",
            confidence=0.92,
            status="pending_approval",
        )
        session.add(event)
        session.commit()

    barrier = threading.Barrier(2)
    original_claim = change_service._claim_pending_batch
    resumes: list[str] = []

    def synchronized_claim(session, requested_change_id, change_event_ids, status):
        barrier.wait(timeout=5)
        return original_claim(session, requested_change_id, change_event_ids, status)

    def fake_resume(thread_id, payload):
        resumes.append(payload["decision"])
        return {"decision": payload["decision"]}

    monkeypatch.setattr(change_service, "_claim_pending_batch", synchronized_claim)
    monkeypatch.setattr(workflow, "resume_run", fake_resume)

    def run(operation):
        try:
            return operation("chg_concurrent"), None
        except ValueError as exc:
            return None, str(exc)

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(run, [change_service.approve_change,
                                       change_service.reject_change]))

    assert sum(result is not None for result, _ in outcomes) == 1
    assert sum(error is not None for _, error in outcomes) == 1
    assert len(resumes) == 1
    with SessionLocal() as session:
        final = session.get(ChangeEvent, "chg_concurrent")
        assert final.status in ("approved", "rejected")
        assert resumes == [final.status]
