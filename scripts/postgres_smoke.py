"""PostgreSQL/Alembic/PostgresSaver lifecycle smoke test for CI.

Runs one deterministic date-change chain, rebuilds the graph connection at the
approval interrupt, resumes, verifies, and rolls back. Use only against an
ephemeral test database.
"""
from __future__ import annotations

from backend.config import settings
from backend.db import SessionLocal, init_db
from backend.graph import workflow
from backend.llm.usage import UsageTracker
from backend.models import BackgroundJob
from backend.services import changes, jobs
from backend.services.ingest import create_workspace, start_change_detection, upload_artifact


def main() -> None:
    if not settings.db_url.startswith(("postgresql://", "postgresql+psycopg://")):
        raise SystemExit("postgres_smoke.py requires a PostgreSQL WORKGUARD_DB_URL")

    init_db()
    with SessionLocal() as session:
        workspace = create_workspace(
            session,
            "postgres-ci-smoke",
            [{"canonical_name": "Alpha V2.0", "aliases": ["Alpha"]}],
        )
        baseline = upload_artifact(
            session,
            workspace.id,
            "plan.md",
            "Alpha V2.0 上线日期：2026-09-20。".encode(),
        )
    queued = jobs.enqueue_job(
        "artifact_change_detection",
        {"workspace_id": workspace.id, "artifact_id": baseline.id},
        workspace_id=workspace.id,
        idempotency_key=f"postgres-smoke:{baseline.id}",
    )
    assert jobs.run_one(worker_id="postgres-smoke-worker") is True
    with SessionLocal() as session:
        completed = session.get(BackgroundJob, queued.id)
        assert completed is not None and completed.status == "completed"

    usage = UsageTracker()
    usage.reset()
    usage.record("postgres_smoke", "synthetic-test-model", 7, 3, 1.0, True)
    assert UsageTracker().summary()["calls"] >= 1

    with SessionLocal() as session:
        decision = upload_artifact(
            session,
            workspace.id,
            "meeting.md",
            "Alpha V2.0 上线日期由 2026-09-20 调整至 2026-09-27。".encode(),
        )
    detected = start_change_detection(workspace.id, decision.id)
    assert detected["run_state"]["waiting_approval"] is True
    change_id = detected["summary"]["change_events"][0]["change_event_id"]

    # Closing and rebuilding the saver simulates a service process restart.
    workflow.close_checkpointer()
    assert workflow.get_thread_state(detected["thread_id"])["waiting_approval"] is True
    approved = changes.approve_change(change_id, {"all": "approve"})
    assert approved["change"]["status"] == "executed"
    rolled_back = changes.rollback_change(change_id)
    assert rolled_back["change"]["status"] == "rolled_back"
    workflow.close_checkpointer()
    print("postgres-smoke-ok")


if __name__ == "__main__":
    main()
