"""Entity Resolver tests: cascade + cold-start disambiguation + alias learning."""
import pytest
from sqlalchemy import select

from backend.agents.resolver import resolve_entities
from backend.db import SessionLocal, init_db
from backend.models import Entity, Fact
from backend.services.ingest import create_workspace


@pytest.fixture()
def workspace():
    init_db()
    with SessionLocal() as session:
        ws = create_workspace(
            session,
            "resolver-tests",
            preset_entities=[
                {"canonical_name": "Alpha V2.0", "aliases": ["Alpha", "V2.0"]},
                {"canonical_name": "Beta V1.0", "aliases": ["Beta"]},
            ],
        )
        yield ws.id


def _resolve(workspace_id, facts):
    with SessionLocal() as session:
        resolved, pending = resolve_entities(session, workspace_id, facts)
        session.commit()
    return resolved, pending


def test_exact_and_alias_match(workspace):
    facts = [
        {"entity_mention": "Alpha V2.0", "confidence": 0.9},
        {"entity_mention": "V2.0", "confidence": 0.9},
    ]
    resolved, pending = _resolve(workspace, facts)
    assert pending == []
    assert all(f["entity_id"] for f in resolved)
    assert resolved[0]["resolution_method"] == "exact"
    assert resolved[1]["resolution_method"] == "alias"


def test_unknown_mention_goes_pending_and_facts_unverified(workspace):
    facts = [{"entity_mention": "Gamma 工具", "confidence": 0.9}]
    resolved, pending = _resolve(workspace, facts)
    assert len(pending) == 1
    assert resolved[0]["status"] == "unverified"


def test_alias_learning_after_resolution(workspace):
    with SessionLocal() as session:
        entity = session.scalars(
            select(Entity).where(Entity.workspace_id == workspace)
        ).first()
        from backend.agents.resolver import learn_alias

        learn_alias(session, entity, "阿尔法二代")
        session.commit()
    facts = [{"entity_mention": "阿尔法二代", "confidence": 0.9}]
    resolved, _ = _resolve(workspace, facts)
    assert resolved[0]["resolution_method"] == "alias"


def test_api_resolution_promotes_fact_and_reenters_change_detection(workspace):
    from starlette.requests import Request

    from backend.api.routes import ResolveBody
    from backend.api.routes import resolve_entity as resolve_entity_route
    from backend.db import SessionLocal
    from backend.services.ingest import start_change_detection, upload_artifact

    with SessionLocal() as session:
        baseline = upload_artifact(
            session, workspace, "prd.md",
            "Alpha V2.0 上线日期：2026-09-20。".encode("utf-8"),
        )
    start_change_detection(workspace, baseline.id)

    with SessionLocal() as session:
        pending_doc = upload_artifact(
            session, workspace, "decision.md",
            "Gamma V1.0 上线日期由 2026-09-20 调整至 2026-09-27。".encode("utf-8"),
        )
    first_run = start_change_detection(workspace, pending_doc.id)
    assert first_run["summary"]["change_events"] == []

    with SessionLocal() as session:
        pending = session.scalars(
            select(Entity).where(Entity.workspace_id == workspace,
                                 Entity.status == "pending_disambiguation")
        ).first()
        target = session.scalars(
            select(Entity).where(Entity.workspace_id == workspace,
                                 Entity.canonical_name == "Alpha V2.0")
        ).first()

    result = resolve_entity_route(
        pending.id,
        ResolveBody(mode="merge_to", target_entity_id=target.id),
        Request({"type": "http", "method": "POST", "path": "/", "headers": []}),
    )
    assert result["facts_promoted"] == 1
    assert result["runs"][0]["run_state"]["waiting_approval"] is True
    assert result["runs"][0]["summary"]["change_events"][0]["new_value"] == "2026-09-27"

    with SessionLocal() as session:
        current = session.scalars(
            select(Fact).where(
                Fact.workspace_id == workspace,
                Fact.entity_id == target.id,
                Fact.predicate == "release_date",
                Fact.is_current.is_(True),
            )
        ).all()
        assert len(current) == 1
        assert current[0].status == "verified"
        assert current[0].value == "2026-09-27"
