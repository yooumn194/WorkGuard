"""Safe Feishu ingestion, event routing, and approval notifications."""
from __future__ import annotations

import hashlib
import hmac
import logging
from urllib.parse import urlencode

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from backend.config import settings
from backend.db import SessionLocal
from backend.integrations.feishu.client import FeishuClient, FeishuError
from backend.models import FeishuBinding, FeishuEvent, FeishuRemoteFile, Workspace, uid, utcnow
from backend.services.ingest import (
    replace_artifact_content,
    start_change_detection,
    upload_artifact,
)
from backend.tools.audit import log_action

logger = logging.getLogger(__name__)
_DOC_TYPES = {"doc", "docx"}


def _sync_priority(item: dict) -> tuple[int, str]:
    """Put authoritative baselines before meeting decisions.

    Feishu does not guarantee a business-meaningful folder listing order.  A
    stable, conservative order makes the first import deterministic and keeps
    a meeting change from becoming the baseline merely because it was listed
    first.
    """
    name = str(item.get("name") or "").lower()
    if any(marker in name for marker in ("prd", "需求", "spec", "baseline", "基线")):
        rank = 0
    elif any(marker in name for marker in ("周会", "会议", "纪要", "决议", "weekly", "meeting")):
        rank = 2
    else:
        rank = 1
    return rank, name


def integration_status() -> dict:
    client = FeishuClient()
    return {
        "configured": client.configured,
        "webhook_configured": client.webhook_configured,
        "app_notification_configured": client.app_notification_configured,
        "approval_link_configured": bool(settings.workguard_public_base_url),
        "verification_configured": bool(settings.feishu_verification_token),
        "folder_token_preset": bool(settings.feishu_folder_token),
        "encrypted_events_supported": False,
        "app_id_masked": (settings.feishu_app_id[:4] + "****") if settings.feishu_app_id else "",
    }


def serialize_binding(binding: FeishuBinding) -> dict:
    return {
        "binding_id": binding.id, "workspace_id": binding.workspace_id,
        "folder_token": binding.folder_token, "enabled": binding.enabled,
        "last_synced_at": binding.last_synced_at.isoformat() if binding.last_synced_at else None,
    }


def create_binding(session: Session, workspace_id: str, folder_token: str) -> FeishuBinding:
    if session.get(Workspace, workspace_id) is None:
        raise ValueError(f"workspace not found: {workspace_id}")
    folder_token = folder_token.strip()
    if not folder_token:
        raise ValueError("folder_token is required")
    existing = session.scalars(
        select(FeishuBinding).where(FeishuBinding.folder_token == folder_token)
    ).first()
    if existing:
        if existing.workspace_id != workspace_id:
            raise ValueError("folder_token is already bound to another workspace")
        existing.enabled = True
        session.commit()
        return existing
    binding = FeishuBinding(
        id=uid("fsb"), workspace_id=workspace_id, folder_token=folder_token, enabled=True
    )
    session.add(binding)
    session.commit()
    return binding


def list_bindings(session: Session, workspace_id: str) -> list[dict]:
    return [serialize_binding(row) for row in session.scalars(
        select(FeishuBinding).where(FeishuBinding.workspace_id == workspace_id)
        .order_by(FeishuBinding.created_at.asc())
    ).all()]


def _first_table_id(client: FeishuClient, app_token: str) -> str:
    tables = client.list_bitable_tables(app_token)
    if not tables or not tables[0].get("table_id"):
        raise FeishuError("bitable has no table_id")
    return tables[0]["table_id"]


def _item_content(client: FeishuClient, item: dict) -> tuple[str, bytes]:
    name, remote_type, token = (
        item.get("name") or "untitled", item.get("type"), item.get("token")
    )
    if not token:
        raise FeishuError("remote file is missing token")
    if remote_type in _DOC_TYPES:
        filename = name if name.lower().endswith(".docx") else f"{name}.docx"
        return filename, client.export_doc_as_docx(token)
    if remote_type == "bitable":
        records = client.list_bitable_records(token, _first_table_id(client, token))
        return f"{name}.md", client.bitable_records_to_markdown(name, records).encode()
    raise FeishuError(f"unsupported type: {remote_type}")


def _sync_item(session: Session, binding: FeishuBinding, item: dict, client: FeishuClient) -> dict:
    token = item.get("token") or ""
    remote_type = item.get("type") or ""
    subscription_error = ""
    try:
        client.subscribe_file_events(token, remote_type)
    except FeishuError as exc:
        # Manual sync remains useful even when the tenant has not granted the
        # event-subscription scope, but the response must expose that webhook
        # auto-sync is not armed for this file.
        subscription_error = str(exc)
    filename, content = _item_content(client, item)
    digest = hashlib.sha256(content).hexdigest()
    link = session.scalars(
        select(FeishuRemoteFile).where(FeishuRemoteFile.remote_token == token)
    ).first()
    if link and link.binding_id != binding.id:
        raise FeishuError("remote file is already linked through another binding")
    if link and link.content_hash == digest:
        return {
            "status": "unchanged",
            "artifact_id": link.artifact_id,
            "name": filename,
            "event_subscription": {
                "subscribed": not subscription_error,
                "error": subscription_error,
            },
        }

    artifact = (
        replace_artifact_content(session, link.artifact_id, filename, content)
        if link and link.artifact_id
        else upload_artifact(session, binding.workspace_id, filename, content)
    )
    if link is None:
        link = FeishuRemoteFile(
            id=uid("fsf"), binding_id=binding.id, remote_token=token,
            remote_type=item.get("type") or "", remote_name=item.get("name") or filename,
        )
        session.add(link)
    link.artifact_id = artifact.id
    link.remote_type = item.get("type") or link.remote_type
    link.remote_name = item.get("name") or link.remote_name
    link.content_hash = digest
    session.commit()
    return {
        "status": "synced", "artifact_id": artifact.id, "name": filename,
        "event_subscription": {
            "subscribed": not subscription_error,
            "error": subscription_error,
        },
        "run": start_change_detection(binding.workspace_id, artifact.id),
    }


def sync_workspace(
    session: Session, workspace_id: str, folder_token: str = "",
    client: FeishuClient | None = None,
) -> dict:
    client = client or FeishuClient()
    if not client.configured:
        raise FeishuError("Feishu is not configured: set FEISHU_APP_ID / FEISHU_APP_SECRET first")
    folder = (folder_token or settings.feishu_folder_token).strip()
    if not folder:
        raise FeishuError("no folder_token given and FEISHU_FOLDER_TOKEN is empty")
    try:
        binding = create_binding(session, workspace_id, folder)
    except ValueError as exc:
        raise FeishuError(str(exc)) from exc

    synced, unchanged, skipped, runs, event_subscriptions = [], [], [], [], []
    for item in sorted(client.list_folder_files(folder), key=_sync_priority):
        if item.get("type") not in _DOC_TYPES | {"bitable"}:
            skipped.append({"name": item.get("name") or "untitled", "type": item.get("type"),
                            "reason": "unsupported type"})
            continue
        try:
            result = _sync_item(session, binding, item, client)
        except (FeishuError, ValueError) as exc:
            skipped.append({"name": item.get("name") or "untitled", "type": item.get("type"),
                            "reason": str(exc)})
            continue
        summary = {"artifact_id": result["artifact_id"], "name": result["name"]}
        event_subscriptions.append({
            **summary,
            **result["event_subscription"],
        })
        if result["status"] == "unchanged":
            unchanged.append(summary)
        else:
            synced.append(summary)
            runs.append(result["run"])
    binding.last_synced_at = utcnow()
    session.commit()
    return {"binding": serialize_binding(binding), "synced": synced, "unchanged": unchanged,
            "skipped": skipped, "runs": runs, "event_subscriptions": event_subscriptions}


def _approval_url(change_card: dict) -> str:
    if not settings.workguard_public_base_url:
        return ""
    query = urlencode({
        "workspace": change_card.get("workspace_id", ""),
        "change": change_card.get("change_id", ""),
    })
    return f"{settings.workguard_public_base_url}/app/?{query}"


def _audit_notification(change_card: dict, channel: str, sent: bool, error: str = "") -> None:
    workspace_id = str(change_card.get("workspace_id") or "")
    if not workspace_id:
        return
    try:
        with SessionLocal() as session:
            log_action(
                session,
                workspace_id,
                tool="feishu_approval_notification",
                action_input={
                    "change_event_id": change_card.get("change_id", ""),
                    "channel": channel,
                    "approval_link_included": bool(_approval_url(change_card)),
                },
                action_output={"sent": sent, "error": error},
                status="success" if sent else "failed",
                change_event_id=str(change_card.get("change_id") or ""),
            )
            session.commit()
    except Exception as exc:
        logger.warning("Feishu notification audit failed: %s", exc)


def notify_pending_approval(change_card: dict) -> bool:
    client = FeishuClient()
    if not client.webhook_configured and not client.app_notification_configured:
        return False
    channel = "app_bot" if client.app_notification_configured else "custom_webhook"
    try:
        source = change_card.get("source", {})
        conflicts = [c for c in change_card.get("conflicts", []) if c.get("verdict") == "conflict"]
        approval_url = _approval_url(change_card)
        text = (
            "[WorkGuard] 检测到需要人工批准的变更\n"
            f"变更: {change_card.get('entity')} / {change_card.get('predicate')}  "
            f"{change_card.get('old_value')} -> {change_card.get('new_value')}\n"
            f"来源: {source.get('artifact')}（“{source.get('evidence')}”）\n"
            f"明确冲突 {len(conflicts)} 项；潜在影响 {len(change_card.get('impacts', []))} 项\n"
            "请在 Change Center 审批：未批准前不会修改任何文件。"
        )
        if approval_url:
            text += f"\n审批链接: {approval_url}"
        if client.app_notification_configured:
            client.send_app_text(text)
        else:
            client.send_webhook_text(text)
        _audit_notification(change_card, channel, True)
        return True
    except (FeishuError, ValueError) as exc:
        logger.warning("Feishu notification failed: %s", exc)
        _audit_notification(change_card, channel, False, str(exc))
        return False


def _verify_payload(payload: dict) -> None:
    if payload.get("encrypt"):
        raise FeishuError("encrypted Feishu events are not supported; use an unencrypted subscription")
    expected = settings.feishu_verification_token
    if not expected:
        raise FeishuError("FEISHU_VERIFICATION_TOKEN is not configured")
    supplied = str((payload.get("header") or {}).get("token") or payload.get("token") or "")
    if not supplied or not hmac.compare_digest(supplied, expected):
        raise FeishuError("invalid Feishu verification token")


def _event_file_token(event: dict) -> str:
    objects = event.get("objects") or []
    first = objects[0] if objects and isinstance(objects[0], dict) else {}
    return str(event.get("file_token") or event.get("document_id") or event.get("objectId")
               or event.get("obj_token") or first.get("object_id") or "")


def accept_webhook_event(payload: dict, session: Session) -> dict:
    """Authenticate and persist one event without making Feishu network calls."""
    _verify_payload(payload)
    if "challenge" in payload:
        return {"challenge": payload["challenge"]}
    header = payload.get("header") or {}
    event_type = str(header.get("event_type") or payload.get("event_type") or "")
    event_id = str(header.get("event_id") or payload.get("uuid") or "")
    if not event_id:
        raise FeishuError("event_id is required")
    if session.scalars(select(FeishuEvent).where(FeishuEvent.event_id == event_id)).first():
        return {"handled": False, "duplicate": True, "event_id": event_id}

    raw_event = payload.get("event") or {}
    file_token = _event_file_token(raw_event) if isinstance(raw_event, dict) else ""
    link = session.scalars(
        select(FeishuRemoteFile).where(FeishuRemoteFile.remote_token == file_token)
    ).first() if file_token else None
    binding = session.get(FeishuBinding, link.binding_id) if link else None
    supported = any(word in event_type.lower() for word in ("file", "doc", "edit"))
    status = "queued" if supported and binding and binding.enabled else "ignored"
    row = FeishuEvent(
        id=uid("fse"), event_id=event_id, event_type=event_type, file_token=file_token,
        binding_id=binding.id if binding else "", status=status, payload=payload,
        result={} if status == "queued" else {"reason": "unsupported event or unbound file"},
        processed_at=None if status == "queued" else utcnow(),
    )
    session.add(row)
    try:
        job_id = None
        if status == "queued":
            from backend.services.jobs import enqueue_job

            assert binding is not None
            job = enqueue_job(
                "feishu_event",
                {"event_record_id": row.id},
                workspace_id=binding.workspace_id,
                idempotency_key=f"feishu_event:{row.id}",
                session=session,
            )
            job_id = job.id
        session.commit()
    except IntegrityError:
        session.rollback()
        return {"handled": False, "duplicate": True, "event_id": event_id}
    return {"handled": status == "queued", "duplicate": False, "event_id": event_id,
            "event_record_id": row.id if status == "queued" else None,
            "job_id": job_id,
            "reason": None if status == "queued" else "unsupported event or unbound file"}


def process_webhook_event(event_record_id: str, client: FeishuClient | None = None) -> None:
    """Network-heavy sync worker with a persisted terminal result."""
    client = client or FeishuClient()
    with SessionLocal() as session:
        row = session.get(FeishuEvent, event_record_id)
        if row is None or row.status not in {"queued", "failed"}:
            return
        if row.status == "failed":
            row.status, row.error, row.processed_at = "queued", "", None
        link = session.scalars(select(FeishuRemoteFile).where(
            FeishuRemoteFile.remote_token == row.file_token
        )).first()
        binding = session.get(FeishuBinding, row.binding_id) if row.binding_id else None
        if not link or not binding or not binding.enabled:
            row.status, row.error, row.processed_at = (
                "failed", "binding or remote file mapping no longer exists", utcnow()
            )
            session.commit()
            return
        item = {"token": link.remote_token, "name": link.remote_name, "type": link.remote_type}
        try:
            result = _sync_item(session, binding, item, client)
            row = session.get(FeishuEvent, event_record_id)
            assert row is not None
            row.status = "completed"
            run = result.get("run") or {}
            summary = run.get("summary") or {}
            row.result = {
                **{k: v for k, v in result.items() if k != "run"},
                "thread_id": run.get("thread_id", ""),
                "change_event_ids": [
                    item.get("change_event_id")
                    for item in summary.get("change_events", [])
                    if item.get("change_event_id")
                ],
                "notification_sent": run.get("notification_sent"),
            }
        except Exception as exc:
            session.rollback()
            row = session.get(FeishuEvent, event_record_id)
            assert row is not None
            row.status, row.error = "failed", str(exc)
        row.processed_at = utcnow()
        session.commit()


handle_webhook_event = accept_webhook_event
