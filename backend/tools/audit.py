"""Audit tool (proposal #4 / #14): every agent action is recorded."""
from __future__ import annotations

from sqlalchemy.orm import Session

from backend.models import AuditLog, uid
from backend.security import redact_sensitive


def log_action(
    session: Session,
    workspace_id: str,
    tool: str,
    action_input: dict,
    action_output: dict,
    status: str = "success",
    change_action_id: str = "",
    change_event_id: str = "",
    actor: str = "agent",
) -> AuditLog:
    row = AuditLog(
        id=uid("aud"),
        workspace_id=workspace_id,
        change_action_id=change_action_id,
        change_event_id=change_event_id,
        actor=actor,
        tool=tool,
        input=redact_sensitive(action_input),
        output=redact_sensitive(action_output),
        status=status,
    )
    session.add(row)
    session.flush()
    return row
