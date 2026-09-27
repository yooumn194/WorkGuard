"""Database-backed background jobs with leases and crash recovery.

The queue deliberately stays small: WorkGuard has two asynchronous job kinds,
and SQLAlchemy is already part of the service. PostgreSQL workers coordinate
with ``FOR UPDATE SKIP LOCKED``; SQLite remains suitable for one local worker.
"""
from __future__ import annotations

import logging
import socket
import threading
import uuid
from datetime import timedelta
from typing import Any, Callable

from sqlalchemy import and_, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from backend.config import settings
from backend.db import SessionLocal
from backend.models import BackgroundJob, uid, utcnow

logger = logging.getLogger(__name__)
JobHandler = Callable[[dict[str, Any]], dict[str, Any] | None]


def enqueue_job(
    kind: str,
    payload: dict[str, Any],
    *,
    workspace_id: str = "",
    idempotency_key: str | None = None,
    max_attempts: int | None = None,
    session: Session | None = None,
) -> BackgroundJob:
    """Persist a job before returning control to the caller.

    Passing a session lets an event receipt and its job share one transaction.
    The caller owns commit/rollback in that case.
    """
    owns_session = session is None
    db = session or SessionLocal()
    job = BackgroundJob(
        id=uid("job"),
        workspace_id=workspace_id,
        kind=kind,
        payload=payload,
        idempotency_key=idempotency_key,
        max_attempts=max_attempts or settings.task_max_attempts,
    )
    db.add(job)
    try:
        if owns_session:
            db.commit()
        else:
            db.flush()
        return job
    except IntegrityError:
        db.rollback()
        if not idempotency_key:
            raise
        existing = db.scalars(
            select(BackgroundJob).where(BackgroundJob.idempotency_key == idempotency_key)
        ).first()
        if existing is None:
            raise
        return existing
    finally:
        if owns_session:
            db.close()


def _claim(worker_id: str) -> BackgroundJob | None:
    now = utcnow()
    eligible = or_(
        and_(BackgroundJob.status == "queued", BackgroundJob.available_at <= now),
        and_(BackgroundJob.status == "running", BackgroundJob.lease_expires_at <= now),
    )
    with SessionLocal() as session:
        with session.begin():
            job = session.scalars(
                select(BackgroundJob)
                .where(eligible, BackgroundJob.attempts < BackgroundJob.max_attempts)
                .order_by(BackgroundJob.available_at.asc(), BackgroundJob.created_at.asc())
                .with_for_update(skip_locked=True)
                .limit(1)
            ).first()
            if job is None:
                return None
            job.status = "running"
            job.attempts += 1
            job.worker_id = worker_id
            job.started_at = job.started_at or now
            job.lease_expires_at = now + timedelta(seconds=settings.task_lease_seconds)
            job.error = ""
            session.flush()
            job_id = job.id
        return session.get(BackgroundJob, job_id)


def _renew_lease(job_id: str, worker_id: str) -> bool:
    """Extend a running job only while this worker still owns it."""
    with SessionLocal() as session:
        with session.begin():
            job = session.scalars(
                select(BackgroundJob)
                .where(
                    BackgroundJob.id == job_id,
                    BackgroundJob.status == "running",
                    BackgroundJob.worker_id == worker_id,
                )
                .with_for_update()
            ).first()
            if job is None:
                return False
            job.lease_expires_at = utcnow() + timedelta(seconds=settings.task_lease_seconds)
            return True


def _keep_lease(job_id: str, worker_id: str, stopped: threading.Event) -> None:
    interval = max(min(settings.task_lease_seconds / 3, 30.0), 0.1)
    while not stopped.wait(interval):
        try:
            if not _renew_lease(job_id, worker_id):
                return
        except Exception:
            logger.exception("could not renew lease for background job %s", job_id)


def _artifact_change_detection(payload: dict[str, Any]) -> dict[str, Any]:
    from backend.services.ingest import start_change_detection

    result = start_change_detection(str(payload["workspace_id"]), str(payload["artifact_id"]))
    return {
        "thread_id": result["thread_id"],
        "run_state": result["run_state"],
    }


def _feishu_event(payload: dict[str, Any]) -> dict[str, Any]:
    from backend.integrations.feishu.service import process_webhook_event
    from backend.models import FeishuEvent

    event_record_id = str(payload["event_record_id"])
    process_webhook_event(event_record_id)
    with SessionLocal() as session:
        event = session.get(FeishuEvent, event_record_id)
        if event is None:
            raise RuntimeError("Feishu event disappeared while processing")
        if event.status == "failed":
            raise RuntimeError(event.error or "Feishu event processing failed")
        return {"event_id": event.event_id, "status": event.status, "result": event.result}


_HANDLERS: dict[str, JobHandler] = {
    "artifact_change_detection": _artifact_change_detection,
    "feishu_event": _feishu_event,
}


def run_one(*, worker_id: str | None = None) -> bool:
    """Claim and execute one available job; return whether work was found."""
    worker_id = worker_id or f"{socket.gethostname()}:{uuid.uuid4().hex[:8]}"
    job = _claim(worker_id)
    if job is None:
        return False
    lease_stopped = threading.Event()
    lease_thread = threading.Thread(
        target=_keep_lease,
        args=(job.id, worker_id, lease_stopped),
        name=f"workguard-lease-{job.id}",
        daemon=True,
    )
    lease_thread.start()
    handler = _HANDLERS.get(job.kind)
    try:
        if handler is None:
            raise ValueError(f"unknown background job kind: {job.kind}")
        result = handler(dict(job.payload or {})) or {}
    except Exception as exc:
        lease_stopped.set()
        lease_thread.join(timeout=2.0)
        logger.exception("background job %s failed on attempt %s", job.id, job.attempts)
        with SessionLocal() as session:
            current = session.scalars(
                select(BackgroundJob)
                .where(
                    BackgroundJob.id == job.id,
                    BackgroundJob.status == "running",
                    BackgroundJob.worker_id == worker_id,
                )
                .with_for_update()
            ).first()
            if current is None:
                logger.warning("worker %s no longer owns failed job %s", worker_id, job.id)
                return True
            current.error = str(exc)[:2000]
            current.worker_id = ""
            current.lease_expires_at = None
            if current.attempts >= current.max_attempts:
                current.status = "failed"
                current.finished_at = utcnow()
            else:
                current.status = "queued"
                current.available_at = utcnow() + timedelta(seconds=min(2 ** current.attempts, 60))
            session.commit()
        return True

    lease_stopped.set()
    lease_thread.join(timeout=2.0)
    with SessionLocal() as session:
        current = session.scalars(
            select(BackgroundJob)
            .where(
                BackgroundJob.id == job.id,
                BackgroundJob.status == "running",
                BackgroundJob.worker_id == worker_id,
            )
            .with_for_update()
        ).first()
        if current is not None:
            current.status = "completed"
            current.result = result
            current.error = ""
            current.worker_id = ""
            current.lease_expires_at = None
            current.finished_at = utcnow()
            session.commit()
        else:
            logger.warning("worker %s no longer owns completed job %s", worker_id, job.id)
    return True


def serialize_job(job: BackgroundJob) -> dict[str, Any]:
    return {
        "job_id": job.id,
        "workspace_id": job.workspace_id,
        "kind": job.kind,
        "status": job.status,
        "attempts": job.attempts,
        "max_attempts": job.max_attempts,
        "result": job.result,
        "error": job.error,
        "created_at": job.created_at.isoformat() if job.created_at else None,
        "started_at": job.started_at.isoformat() if job.started_at else None,
        "finished_at": job.finished_at.isoformat() if job.finished_at else None,
    }


class JobWorker:
    def __init__(self) -> None:
        self.worker_id = f"{socket.gethostname()}:{uuid.uuid4().hex[:8]}"
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def run_forever(self) -> None:
        logger.info("durable task worker started: %s", self.worker_id)
        while not self._stop.is_set():
            try:
                found = run_one(worker_id=self.worker_id)
            except Exception:
                logger.exception("durable task worker polling failed")
                found = False
            if not found:
                self._stop.wait(max(settings.task_poll_seconds, 0.05))
        logger.info("durable task worker stopped: %s", self.worker_id)

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(
            target=self.run_forever,
            name="workguard-task-worker",
            daemon=True,
        )
        self._thread.start()

    def stop(self, timeout: float = 10.0) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=timeout)


_worker = JobWorker()


def start_worker() -> None:
    _worker.start()


def stop_worker() -> None:
    _worker.stop()


def run_worker_forever() -> None:
    worker = JobWorker()
    try:
        worker.run_forever()
    except KeyboardInterrupt:
        worker.stop()
