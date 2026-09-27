"""FastAPI routes (proposal #36, MVP subset)."""
from __future__ import annotations

from fastapi import APIRouter, File, HTTPException, Request, UploadFile
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from sqlalchemy import select

from backend.config import settings
from backend.db import SessionLocal
from backend.security import issue_workspace_token, redact_sensitive, require_workspace_access
from backend.services import changes as change_service
from backend.services import chat as chat_service
from backend.services import entities as entity_service
from backend.services import ingest as ingest_service
from backend.tools import fact_store

router = APIRouter(prefix="/api", tags=["workguard"])


async def _read_upload_limited(file: UploadFile) -> bytes:
    """Read multipart data incrementally and stop once the configured cap is crossed."""
    content = bytearray()
    chunk_size = 64 * 1024
    while chunk := await file.read(chunk_size):
        content.extend(chunk)
        if len(content) > settings.max_upload_bytes:
            raise HTTPException(
                status_code=413,
                detail=f"file too large (limit {settings.max_upload_bytes} bytes)",
            )
    return bytes(content)


# ------------------------------------------------------------------ workspaces
class WorkspaceCreate(BaseModel):
    name: str
    preset_entities: list[dict] = Field(default_factory=list)


@router.post("/workspaces")
def create_workspace(body: WorkspaceCreate):
    with SessionLocal() as session:
        workspace = ingest_service.create_workspace(session, body.name, body.preset_entities)
        result = {"workspace_id": workspace.id, "name": workspace.name}
        if settings.workspace_auth:
            result["workspace_key"] = issue_workspace_token(workspace.id)
        return result


# ------------------------------------------------------------------- artifacts
@router.post("/workspaces/{workspace_id}/artifacts")
async def upload_artifact(
    workspace_id: str,
    request: Request,
    file: UploadFile = File(...),
    sync: bool = False,
):
    require_workspace_access(request, workspace_id)
    content = await _read_upload_limited(file)
    try:
        with SessionLocal() as session:
            artifact = ingest_service.upload_artifact(
                session, workspace_id, file.filename or "", content
            )
            artifact_id = artifact.id
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    if sync:
        result = ingest_service.start_change_detection(workspace_id, artifact_id)
        return JSONResponse(status_code=201, content={"artifact_id": artifact_id, **result})

    from backend.services.jobs import enqueue_job

    job = enqueue_job(
        "artifact_change_detection",
        {"workspace_id": workspace_id, "artifact_id": artifact_id},
        workspace_id=workspace_id,
        idempotency_key=f"artifact_change_detection:{artifact_id}",
    )
    return JSONResponse(
        status_code=202,
        content={
            "artifact_id": artifact_id,
            "job_id": job.id,
            "detail": "change detection queued durably",
        },
    )


@router.get("/workspaces/{workspace_id}/artifacts")
def list_artifacts(workspace_id: str, request: Request):
    from backend.models import Artifact

    require_workspace_access(request, workspace_id)
    with SessionLocal() as session:
        artifacts = session.scalars(
            select(Artifact).where(Artifact.workspace_id == workspace_id)
        ).all()
        return [
            {
                "artifact_id": a.id,
                "name": a.name,
                "type": a.type,
                "role": a.artifact_role,
                "source_authority": fact_store.source_authority(a),
                "current_version": a.current_version,
            }
            for a in artifacts
        ]


@router.get("/artifacts/{artifact_id}")
def get_artifact(artifact_id: str, request: Request):
    from sqlalchemy import select

    from backend.models import Artifact, ArtifactVersion, Fact

    with SessionLocal() as session:
        artifact = session.get(Artifact, artifact_id)
        if artifact is None:
            raise HTTPException(status_code=404, detail="artifact not found")
        require_workspace_access(request, artifact.workspace_id)
        version = session.scalars(
            select(ArtifactVersion)
            .where(ArtifactVersion.artifact_id == artifact_id)
            .order_by(ArtifactVersion.version.desc())
        ).first()
        facts = session.scalars(select(Fact).where(Fact.artifact_id == artifact_id)).all()
        return {
            "artifact_id": artifact.id,
            "name": artifact.name,
            "type": artifact.type,
            "role": artifact.artifact_role,
            "current_version": artifact.current_version,
            "blocks": (version.parsed_content or {}).get("blocks", []) if version else [],
            "facts": [
                {
                    "fact_id": f.id,
                    "entity": f.entity_id,
                    "predicate": f.predicate,
                    "value": f.value,
                    "status": f.status,
                    "is_current": f.is_current,
                    "confidence": f.confidence,
                    "source_authority": fact_store.source_authority(artifact),
                    "location": f.source_location,
                    "evidence": f.evidence,
                }
                for f in facts
            ],
        }


# ----------------------------------------------------------------------- facts
@router.get("/workspaces/{workspace_id}/facts")
def list_facts(workspace_id: str, request: Request, current_only: bool = False):
    from backend.models import Artifact, Entity, Fact

    require_workspace_access(request, workspace_id)
    with SessionLocal() as session:
        query = select(Fact).where(Fact.workspace_id == workspace_id)
        if current_only:
            query = query.where(Fact.is_current.is_(True))
        facts = session.scalars(query).all()
        out = []
        for f in facts:
            entity = session.get(Entity, f.entity_id) if f.entity_id else None
            artifact = session.get(Artifact, f.artifact_id)
            out.append(
                {
                    "fact_id": f.id,
                    "artifact": artifact.name if artifact else f.artifact_id,
                    "entity": entity.canonical_name if entity else None,
                    "predicate": f.predicate,
                    "value": f.value,
                    "status": f.status,
                    "is_current": f.is_current,
                    "confidence": f.confidence,
                    "source_authority": fact_store.source_authority(artifact),
                    "extracted_by": f.extracted_by,
                    "location": f.source_location,
                    "evidence": f.evidence,
                    "review_reason": (
                        "实体尚未完成消歧" if entity and entity.status == "pending_disambiguation"
                        else f"置信度 {f.confidence:.2f} 低于阈值"
                        if f.status == "unverified" else ""
                    ),
                }
            )
        return out


# --------------------------------------------------------------------- changes
@router.get("/workspaces/{workspace_id}/changes")
def list_changes(workspace_id: str, request: Request):
    require_workspace_access(request, workspace_id)
    with SessionLocal() as session:
        events = change_service.list_changes(session, workspace_id)
        return [
            {
                "change_id": e.id,
                "status": e.status,
                "entity": e.entity_name,
                "predicate": e.predicate,
                "old_value": e.old_value,
                "new_value": e.new_value,
                "confidence": e.confidence,
            }
            for e in events
        ]


@router.get("/changes/{change_id}")
def get_change(change_id: str, request: Request):
    with SessionLocal() as session:
        event = change_service.get_change(session, change_id)
        if event is None:
            raise HTTPException(status_code=404, detail="change not found")
        require_workspace_access(request, event.workspace_id)
        return change_service.serialize_change(session, event)


class ApprovalBody(BaseModel):
    decisions: dict = Field(default_factory=lambda: {"all": "approve"})


@router.post("/changes/{change_id}/approve")
def approve_change(change_id: str, request: Request, body: ApprovalBody | None = None):
    from backend.models import ChangeEvent

    with SessionLocal() as session:
        event = session.get(ChangeEvent, change_id)
        if event is None:
            raise HTTPException(status_code=404, detail="change not found")
        require_workspace_access(request, event.workspace_id)
    try:
        return change_service.approve_change(change_id, (body.decisions if body else None))
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc))


@router.post("/changes/{change_id}/reject")
def reject_change(change_id: str, request: Request):
    from backend.models import ChangeEvent

    with SessionLocal() as session:
        event = session.get(ChangeEvent, change_id)
        if event is None:
            raise HTTPException(status_code=404, detail="change not found")
        require_workspace_access(request, event.workspace_id)
    try:
        return change_service.reject_change(change_id)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc))


@router.post("/changes/{change_id}/rollback")
def rollback_change(change_id: str, request: Request):
    from backend.models import ChangeEvent

    with SessionLocal() as session:
        event = session.get(ChangeEvent, change_id)
        if event is None:
            raise HTTPException(status_code=404, detail="change not found")
        require_workspace_access(request, event.workspace_id)
    try:
        return change_service.rollback_change(change_id)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc))


# ------------------------------------------------------- runs (observability)
@router.get("/runs/{thread_id}")
def get_run(thread_id: str, request: Request):
    from backend.graph import workflow
    from backend.models import AgentRun

    with SessionLocal() as session:
        run = session.scalars(
            select(AgentRun).where(AgentRun.thread_id == thread_id)
        ).first()
        if run is None:
            raise HTTPException(status_code=404, detail="run not found")
        require_workspace_access(request, run.workspace_id)
        state = workflow.get_thread_state(thread_id)
        return {
            "run_id": run.id,
            "thread_id": thread_id,
            "status": run.status,
            "waiting_approval": state["waiting_approval"],
            "next_nodes": state["next"],
            "interrupt": state["interrupt"],
            "errors": run.errors,
        }


@router.get("/workspaces/{workspace_id}/runs")
def list_runs(workspace_id: str, request: Request):
    from backend.models import AgentRun

    require_workspace_access(request, workspace_id)
    with SessionLocal() as session:
        rows = session.scalars(
            select(AgentRun).where(AgentRun.workspace_id == workspace_id)
            .order_by(AgentRun.started_at.desc())
        ).all()
        return [{
            "thread_id": row.thread_id, "artifact_id": row.artifact_id,
            "status": row.status, "errors": row.errors,
            "started_at": row.started_at.isoformat() if row.started_at else None,
            "finished_at": row.finished_at.isoformat() if row.finished_at else None,
        } for row in rows]


@router.post("/runs/{thread_id}/retry")
def retry_run(thread_id: str, request: Request):
    from backend.models import AgentRun

    with SessionLocal() as session:
        run = session.scalars(select(AgentRun).where(AgentRun.thread_id == thread_id)).first()
        if run is None:
            raise HTTPException(status_code=404, detail="run not found")
        require_workspace_access(request, run.workspace_id)
    try:
        return ingest_service.retry_failed_run(thread_id)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc))


# ------------------------------------------------- entities / disambiguation
@router.get("/workspaces/{workspace_id}/entities/pending")
def pending_entities(workspace_id: str, request: Request):
    require_workspace_access(request, workspace_id)
    with SessionLocal() as session:
        return entity_service.pending_entities(session, workspace_id)


class ResolveBody(BaseModel):
    mode: str  # "merge_to" | "keep_as_new"
    target_entity_id: str | None = None


@router.post("/entities/{entity_id}/resolve")
def resolve_entity(entity_id: str, body: ResolveBody, request: Request):
    reprocess = []
    with SessionLocal() as session:
        from backend.models import Entity

        entity = session.get(Entity, entity_id)
        if entity is None:
            raise HTTPException(status_code=404, detail="entity not found")
        require_workspace_access(request, entity.workspace_id)
        try:
            result = entity_service.resolve_entity(
                session, entity.workspace_id, entity_id, body.mode, body.target_entity_id
            )
            workspace_id = entity.workspace_id
            reprocess = result.pop("_reprocess", [])
            session.commit()
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc))
    from backend.graph import workflow

    grouped: dict[str, list[str]] = {}
    for item in reprocess:
        grouped.setdefault(item["artifact_id"], []).extend(item["fact_ids"])
    result["runs"] = [
        workflow.start_resolved_fact_detection(workspace_id, artifact_id, fact_ids)
        for artifact_id, fact_ids in grouped.items()
    ]
    return result


# ------------------------------------------------- feishu integration
class FeishuSyncBody(BaseModel):
    workspace_id: str
    folder_token: str = ""


class FeishuBindingBody(BaseModel):
    workspace_id: str
    folder_token: str


@router.get("/integrations/feishu/status")
def feishu_status():
    """Config report for the Feishu integration (never echoes secrets)."""
    from backend.integrations.feishu.service import integration_status

    return integration_status()


@router.post("/integrations/feishu/sync")
def feishu_sync(body: FeishuSyncBody, request: Request):
    """Pull docs/bitable tables from a Feishu folder into the workspace and run
    change detection on each. Requires FEISHU_APP_ID / FEISHU_APP_SECRET."""
    from backend.integrations.feishu.client import FeishuError
    from backend.integrations.feishu.service import sync_workspace
    from backend.models import Workspace

    require_workspace_access(request, body.workspace_id)
    with SessionLocal() as session:
        if session.get(Workspace, body.workspace_id) is None:
            raise HTTPException(status_code=404, detail="workspace not found")
        try:
            result = sync_workspace(session, body.workspace_id, body.folder_token)
        except FeishuError as exc:
            raise HTTPException(status_code=503, detail=str(exc))
        return result


@router.post("/integrations/feishu/bindings")
def create_feishu_binding(body: FeishuBindingBody, request: Request):
    from backend.integrations.feishu.service import create_binding, serialize_binding

    require_workspace_access(request, body.workspace_id)
    with SessionLocal() as session:
        try:
            return serialize_binding(create_binding(session, body.workspace_id, body.folder_token))
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc))


@router.get("/workspaces/{workspace_id}/integrations/feishu")
def list_feishu_bindings(workspace_id: str, request: Request):
    from backend.integrations.feishu.service import list_bindings

    require_workspace_access(request, workspace_id)
    with SessionLocal() as session:
        return list_bindings(session, workspace_id)


@router.post("/integrations/feishu/webhook")
async def feishu_webhook(request: dict):
    """Feishu event subscription endpoint: answers the URL-verification
    challenge and re-syncs on document-update events."""
    from backend.integrations.feishu.client import FeishuError
    from backend.integrations.feishu.service import accept_webhook_event

    try:
        with SessionLocal() as session:
            result = accept_webhook_event(request, session)
    except FeishuError as exc:
        message = str(exc)
        status = 403 if "invalid" in message else 400 if "encrypted" in message else 503
        raise HTTPException(status_code=status, detail=message)
    return result


@router.get("/integrations/feishu/events/{event_id}")
def get_feishu_event(event_id: str, request: Request):
    from backend.models import FeishuBinding, FeishuEvent

    with SessionLocal() as session:
        row = session.scalars(select(FeishuEvent).where(FeishuEvent.event_id == event_id)).first()
        if row is None:
            raise HTTPException(status_code=404, detail="Feishu event not found")
        binding = session.get(FeishuBinding, row.binding_id) if row.binding_id else None
        if settings.workspace_auth and binding is None:
            raise HTTPException(status_code=403, detail="workspace access denied")
        if binding is not None:
            require_workspace_access(request, binding.workspace_id)
        return {"event_id": row.event_id, "event_type": row.event_type,
                "file_token": row.file_token, "status": row.status,
                "result": row.result, "error": row.error}


# ---------------------------------------------------------- durable background jobs
@router.get("/jobs/{job_id}")
def get_background_job(job_id: str, request: Request):
    from backend.models import BackgroundJob
    from backend.services.jobs import serialize_job

    with SessionLocal() as session:
        job = session.get(BackgroundJob, job_id)
        if job is None:
            raise HTTPException(status_code=404, detail="background job not found")
        if job.workspace_id:
            require_workspace_access(request, job.workspace_id)
        elif settings.workspace_auth:
            raise HTTPException(status_code=403, detail="workspace access denied")
        return serialize_job(job)


# -------------------------------------------------------------- llm observability
@router.get("/llm/usage")
def llm_usage():
    """Persisted LLM call/token/cost accounting (heuristic mode reports zeros)."""
    from backend.llm.usage import get_usage

    return get_usage().summary()


# ------------------------------------------------------------------------ chat
class ChatBody(BaseModel):
    question: str


@router.post("/workspaces/{workspace_id}/chat")
def chat(workspace_id: str, body: ChatBody, request: Request):
    require_workspace_access(request, workspace_id)
    with SessionLocal() as session:
        return chat_service.answer(session, workspace_id, body.question)


# ----------------------------------------------------------------------- audit
@router.get("/workspaces/{workspace_id}/audit")
def audit_log(workspace_id: str, request: Request, include_payloads: bool = False):
    from backend.models import AuditLog

    require_workspace_access(request, workspace_id)
    with SessionLocal() as session:
        rows = session.scalars(
            select(AuditLog)
            .where(AuditLog.workspace_id == workspace_id)
            .order_by(AuditLog.executed_at.asc())
        ).all()
        return [
            {
                "id": r.id,
                "actor": r.actor,
                "tool": r.tool,
                "input": redact_sensitive(r.input) if include_payloads else {},
                "output": redact_sensitive(r.output) if include_payloads else {},
                "has_payload": bool(r.input or r.output),
                "status": r.status,
                "executed_at": r.executed_at.isoformat() if r.executed_at else None,
            }
            for r in rows
        ]
