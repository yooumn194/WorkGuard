"""Ingestion service: workspace bootstrap + artifact upload + graph trigger."""
from __future__ import annotations

import base64
import hashlib
import io
import logging
import os
import re
import uuid
import zipfile
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from backend.agents.impact import seed_preset_rules
from backend.config import settings
from backend.db import SessionLocal
from backend.graph import workflow
from backend.models import AgentRun, Artifact, ArtifactVersion, Entity, Workspace, uid, utcnow
from backend.parsers import detect_kind, parse_file

logger = logging.getLogger(__name__)

_MEETING_NAME = re.compile(r"周会|会议|纪要|站会|weekly|meeting|minutes", re.I)
_REPORT_NAME = re.compile(r"周报|日报|汇报|报告|report|review", re.I)


def detect_role(filename: str) -> str:
    if _MEETING_NAME.search(filename):
        return "meeting"
    if _REPORT_NAME.search(filename):
        return "report"
    return "document"


def create_workspace(
    session: Session,
    name: str,
    preset_entities: list[dict] | None = None,
    seed_rules: bool = True,
) -> Workspace:
    """preset_entities: [{"canonical_name": "Alpha V2.0", "aliases": [...], "type": ...}]

    The preset dictionary is the cold-start answer for Entity Resolution
    (user decision #3): the workspace ships with known project names/aliases.
    """
    workspace = Workspace(id=uid("ws"), name=name)
    session.add(workspace)
    # Flush the parent first. SQLite does not enforce foreign keys by default,
    # but PostgreSQL correctly rejects an Entity inserted before its Workspace.
    session.flush()
    for preset in preset_entities or []:
        session.add(
            Entity(
                id=uid("ent"),
                workspace_id=workspace.id,
                entity_type=preset.get("type", "project_version"),
                canonical_name=preset["canonical_name"],
                aliases=preset.get("aliases", []),
                status="active",
            )
        )
    session.flush()
    if seed_rules:
        seed_preset_rules(session, workspace.id)
    session.commit()
    return workspace


def upload_artifact(
    session: Session, workspace_id: str, filename: str, content: bytes
) -> Artifact:
    if session.get(Workspace, workspace_id) is None:
        raise ValueError(f"workspace not found: {workspace_id}")
    if not filename or Path(filename).name != filename:
        raise ValueError("filename must be a non-empty basename")
    kind = detect_kind(filename)
    if kind is None:
        raise ValueError(f"Unsupported file type: {filename} (supported: md/txt/docx/xlsx)")
    if not content:
        raise ValueError("empty files are not supported")
    if len(content) > settings.max_upload_bytes:
        raise ValueError(
            f"file too large: {len(content)} bytes (limit {settings.max_upload_bytes})"
        )
    if kind in ("docx", "xlsx"):
        _validate_office_archive(content, kind)

    ws_dir = Path(settings.data_dir) / workspace_id
    ws_dir.mkdir(parents=True, exist_ok=True)
    dest = ws_dir / f"{uuid.uuid4().hex[:8]}__{filename}"
    dest.write_bytes(content)
    try:
        parsed = parse_file(dest, kind)
    except Exception as exc:
        # The just-created upload is owned by this operation and has not been
        # referenced by the database yet, so cleanup is safe and recoverable.
        dest.unlink(missing_ok=True)
        raise ValueError(f"invalid or corrupt {kind.upper()} file: {exc}") from exc
    if kind in ("docx", "xlsx"):
        raw_content = base64.b64encode(content).decode("ascii")
    else:
        raw_content = content.decode("utf-8", errors="replace")

    artifact = Artifact(
        id=uid("art"),
        workspace_id=workspace_id,
        name=filename,
        type=kind,
        source_path=str(dest),
        current_version=1,
        artifact_role=detect_role(filename),
    )
    session.add(artifact)
    session.flush()
    session.add(
        ArtifactVersion(
            id=uid("ver"),
            artifact_id=artifact.id,
            version=1,
            content_hash=hashlib.sha256(content).hexdigest()[:16],
            raw_content=raw_content,
            parsed_content=parsed,
        )
    )
    session.commit()
    return artifact


def replace_artifact_content(
    session: Session, artifact_id: str, filename: str, content: bytes
) -> Artifact:
    """Replace a connector-owned artifact while preserving its stable identity.

    Parsing happens against a temporary sibling first. The source is atomically
    replaced only after validation, and restored if the database commit fails.
    """
    artifact = session.get(Artifact, artifact_id)
    if artifact is None:
        raise ValueError(f"artifact not found: {artifact_id}")
    if not filename or Path(filename).name != filename:
        raise ValueError("filename must be a non-empty basename")
    kind = detect_kind(filename)
    if kind is None or kind != artifact.type:
        raise ValueError(f"remote file type changed from {artifact.type} to {kind or 'unsupported'}")
    if not content:
        raise ValueError("empty files are not supported")
    if len(content) > settings.max_upload_bytes:
        raise ValueError(f"file too large: {len(content)} bytes (limit {settings.max_upload_bytes})")
    if kind in ("docx", "xlsx"):
        _validate_office_archive(content, kind)

    source = Path(artifact.source_path)
    original = source.read_bytes()
    temporary = source.with_name(f".{source.name}.feishu-{uuid.uuid4().hex[:8]}.tmp")
    temporary.write_bytes(content)
    try:
        parsed = parse_file(temporary, kind)
        os.replace(temporary, source)
        raw_content = (
            base64.b64encode(content).decode("ascii")
            if kind in ("docx", "xlsx") else content.decode("utf-8", errors="replace")
        )
        artifact.current_version += 1
        artifact.name = filename
        artifact.artifact_role = detect_role(filename)
        session.add(ArtifactVersion(
            id=uid("ver"), artifact_id=artifact.id, version=artifact.current_version,
            content_hash=hashlib.sha256(content).hexdigest()[:16],
            raw_content=raw_content, parsed_content=parsed,
        ))
        session.commit()
    except Exception as exc:
        temporary.unlink(missing_ok=True)
        source.write_bytes(original)
        session.rollback()
        if isinstance(exc, ValueError):
            raise
        raise ValueError(f"failed to update connector artifact: {exc}") from exc
    return artifact


def _validate_office_archive(content: bytes, kind: str) -> None:
    """Reject corrupt, encrypted and zip-bomb-like Office packages pre-parse."""
    try:
        with zipfile.ZipFile(io.BytesIO(content)) as archive:
            infos = archive.infolist()
            if len(infos) > settings.max_office_archive_entries:
                raise ValueError("too many archive entries")
            if any(info.flag_bits & 0x1 for info in infos):
                raise ValueError("encrypted Office files are not supported")
            expanded = sum(info.file_size for info in infos)
            if expanded > settings.max_office_uncompressed_bytes:
                raise ValueError(
                    f"expanded Office file too large: {expanded} bytes "
                    f"(limit {settings.max_office_uncompressed_bytes})"
                )
            required = "word/document.xml" if kind == "docx" else "xl/workbook.xml"
            if required not in archive.namelist():
                raise ValueError(f"missing {required}")
    except zipfile.BadZipFile as exc:
        raise ValueError("not a valid Office ZIP package") from exc


def _failed_run_result(thread_id: str, exc: Exception) -> dict:
    """Persist a terminal run record and return the normal result envelope."""
    error = {"stage": "workflow", "reason": str(exc)}
    with SessionLocal() as session:
        run = session.scalars(
            select(AgentRun).where(AgentRun.thread_id == thread_id)
        ).first()
        if run:
            run.status = "failed"
            run.errors = [error]
            run.finished_at = utcnow()
        session.commit()
    return {
        "thread_id": thread_id,
        "run_state": {
            "thread_id": thread_id,
            "status": "failed",
            "waiting_approval": False,
            "next": [],
            "interrupt": None,
        },
        "summary": {
            "change_events": [], "plan": {}, "approval": {},
            "execution_results": [], "post_verification": [],
            "pending_disambiguation": [], "dropped_facts": [],
            "errors": [error],
        },
    }


def start_change_detection(workspace_id: str, artifact_id: str, synchronous: bool = True) -> dict:
    """Run the change-detection graph. Returns run summary + thread id.

    The run may pause at the approval interrupt (waiting_approval), which the
    caller surfaces to the user; the approval API then resumes the thread.
    """
    thread_id = f"thr_{uuid.uuid4().hex[:12]}"
    with SessionLocal() as session:
        run = AgentRun(
            id=uid("run"), thread_id=thread_id, workspace_id=workspace_id, artifact_id=artifact_id
        )
        session.add(run)
        session.commit()

    try:
        summary = workflow.start_run(workspace_id, artifact_id, thread_id)
        state = workflow.get_thread_state(thread_id)
    except Exception as exc:
        return _failed_run_result(thread_id, exc)
    with SessionLocal() as session:
        existing_run = session.scalars(
            select(AgentRun).where(AgentRun.thread_id == thread_id)
        ).first()
        if existing_run and existing_run.status == "running":
            existing_run.status = (
                "waiting_approval" if state["waiting_approval"] else "completed"
            )
            if not state["waiting_approval"]:
                existing_run.finished_at = utcnow()
        session.commit()
    notification_sent = None
    if state["waiting_approval"]:
        notification_sent = _notify_feishu_if_configured(thread_id)
    return {
        "thread_id": thread_id,
        "run_state": state,
        "summary": summary,
        "notification_sent": notification_sent,
    }


def retry_failed_run(thread_id: str) -> dict:
    with SessionLocal() as session:
        run = session.scalars(select(AgentRun).where(AgentRun.thread_id == thread_id)).first()
        if run is None:
            raise ValueError(f"run not found: {thread_id}")
        if run.status != "failed":
            raise ValueError(f"run is not failed (status={run.status})")
        workspace_id, artifact_id = run.workspace_id, run.artifact_id
    result = start_change_detection(workspace_id, artifact_id)
    return {"retried_from": thread_id, **result}


def _notify_feishu_if_configured(thread_id: str) -> bool:
    """Best-effort Feishu group notification for pending approvals.
    Never propagates errors into the ingest path."""
    try:
        from backend.integrations.feishu.service import notify_pending_approval
        from backend.models import ChangeEvent
        from backend.services.changes import serialize_change

        with SessionLocal() as session:
            event = session.scalars(
                select(ChangeEvent).where(
                    ChangeEvent.thread_id == thread_id,
                    ChangeEvent.status == "pending_approval",
                )
            ).first()
            if event is None:
                return False
            return notify_pending_approval(serialize_change(session, event))
    except Exception as exc:  # notification must never break ingestion
        logger.warning("Feishu approval notification skipped: %s", exc)
        return False


def copy_demo_file(src: Path) -> bytes:
    return Path(src).read_bytes()


def remove_artifact_file(artifact: Artifact) -> None:
    try:
        Path(artifact.source_path).unlink(missing_ok=True)
    except OSError:
        pass
