"""Durability tests for background work and LLM accounting."""
from datetime import timedelta

import pytest
from fastapi.testclient import TestClient

from backend.db import Base, SessionLocal, engine, init_db
from backend.llm.usage import UsageTracker
from backend.main import app
from backend.models import BackgroundJob, LLMUsageRecord, utcnow
from backend.services import jobs


@pytest.fixture(autouse=True)
def clean_database():
    Base.metadata.drop_all(engine)
    init_db()


def test_job_is_persisted_then_completed_by_worker(monkeypatch):
    seen = []

    def handle(payload):
        seen.append(payload["value"])
        return {"doubled": payload["value"] * 2}

    monkeypatch.setitem(jobs._HANDLERS, "test_job", handle)
    queued = jobs.enqueue_job("test_job", {"value": 4}, workspace_id="ws_1")
    with SessionLocal() as session:
        assert session.get(BackgroundJob, queued.id).status == "queued"

    assert jobs.run_one(worker_id="test-worker") is True
    with SessionLocal() as session:
        completed = session.get(BackgroundJob, queued.id)
        assert completed.status == "completed"
        assert completed.attempts == 1
        assert completed.result == {"doubled": 8}
        assert completed.worker_id == ""
    assert seen == [4]


def test_expired_lease_is_recovered_and_failures_reach_terminal_state(monkeypatch):
    monkeypatch.setitem(jobs._HANDLERS, "recoverable", lambda payload: {"ok": True})
    recovered = jobs.enqueue_job("recoverable", {}, workspace_id="ws_1")
    with SessionLocal() as session:
        row = session.get(BackgroundJob, recovered.id)
        row.status = "running"
        row.worker_id = "dead-worker"
        row.lease_expires_at = utcnow() - timedelta(seconds=1)
        session.commit()
    assert jobs.run_one(worker_id="replacement") is True
    with SessionLocal() as session:
        assert session.get(BackgroundJob, recovered.id).status == "completed"

    def fail(_payload):
        raise RuntimeError("transient provider failure")

    monkeypatch.setitem(jobs._HANDLERS, "always_fails", fail)
    failed = jobs.enqueue_job("always_fails", {}, max_attempts=2)
    assert jobs.run_one(worker_id="worker-a") is True
    with SessionLocal() as session:
        row = session.get(BackgroundJob, failed.id)
        assert row.status == "queued"
        row.available_at = utcnow() - timedelta(seconds=1)
        session.commit()
    assert jobs.run_one(worker_id="worker-b") is True
    with SessionLocal() as session:
        row = session.get(BackgroundJob, failed.id)
        assert row.status == "failed"
        assert row.attempts == 2
        assert "transient provider failure" in row.error


def test_stale_worker_cannot_overwrite_a_reclaimed_job(monkeypatch):
    queued = jobs.enqueue_job("ownership_test", {}, workspace_id="ws_1")

    def lose_ownership(_payload):
        with SessionLocal() as session:
            row = session.get(BackgroundJob, queued.id)
            row.worker_id = "replacement-worker"
            row.attempts += 1
            session.commit()
        return {"stale": "result"}

    monkeypatch.setitem(jobs._HANDLERS, "ownership_test", lose_ownership)
    assert jobs.run_one(worker_id="stale-worker") is True
    with SessionLocal() as session:
        row = session.get(BackgroundJob, queued.id)
        assert row.status == "running"
        assert row.worker_id == "replacement-worker"
        assert row.result == {}


def test_async_upload_returns_queryable_durable_job():
    with TestClient(app) as client:
        workspace = client.post("/api/workspaces", json={"name": "queued-upload"}).json()
        response = client.post(
            f"/api/workspaces/{workspace['workspace_id']}/artifacts",
            files={
                "file": (
                    "plan.md",
                    b"Alpha V2.0 release date: 2026-09-20",
                    "text/markdown",
                )
            },
        )
        assert response.status_code == 202
        job_id = response.json()["job_id"]
        assert client.get(f"/api/jobs/{job_id}").json()["status"] == "queued"

        assert jobs.run_one(worker_id="http-test-worker") is True
        completed = client.get(f"/api/jobs/{job_id}").json()
        assert completed["status"] == "completed"
        assert completed["result"]["thread_id"].startswith("thr_")


def test_llm_usage_survives_tracker_recreation_and_reset_preserves_history():
    tracker = UsageTracker()
    tracker.reset()
    tracker.record("extract", "gpt-4o-mini", 100, 20, 125.5, True)
    tracker.record("reflect", "gpt-4o-mini", 40, 10, 75.0, False, "timeout")
    window = tracker.summary()
    assert window["calls"] == 2
    assert window["errors"] == 1
    assert window["total_tokens"] == 170

    restarted = UsageTracker()
    persisted = restarted.summary()
    assert persisted["calls"] == 2
    assert persisted["total_tokens"] == 170
    assert all(record["created_at"] for record in persisted["records"])

    restarted.reset()
    assert restarted.summary()["calls"] == 0
    with SessionLocal() as session:
        assert len(session.query(LLMUsageRecord).all()) == 2
