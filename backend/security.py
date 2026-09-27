"""Small deployment and workspace authorization helpers.

This is intentionally not an identity provider. Shared deployments can put an
API-key perimeter around the service and additionally enable signed,
workspace-scoped capability tokens so one workspace token cannot read another.
"""
from __future__ import annotations

import hashlib
import hmac
from typing import Any

from fastapi import HTTPException, Request

from backend.config import settings


def _secret() -> bytes:
    secret = settings.workspace_token_secret
    if not secret:
        raise RuntimeError(
            "WORKGUARD_WORKSPACE_TOKEN_SECRET or WORKGUARD_API_KEY is required "
            "when WORKGUARD_WORKSPACE_AUTH=1"
        )
    return secret.encode("utf-8")


def issue_workspace_token(workspace_id: str) -> str:
    signature = hmac.new(_secret(), workspace_id.encode("utf-8"), hashlib.sha256).hexdigest()
    return f"{workspace_id}.{signature}"


def verify_workspace_token(workspace_id: str, token: str) -> bool:
    try:
        supplied_workspace, supplied_signature = token.rsplit(".", 1)
    except ValueError:
        return False
    if supplied_workspace != workspace_id:
        return False
    expected = hmac.new(_secret(), workspace_id.encode("utf-8"), hashlib.sha256).hexdigest()
    return hmac.compare_digest(supplied_signature, expected)


def require_workspace_access(request: Request, workspace_id: str) -> None:
    if not settings.workspace_auth:
        return
    token = request.headers.get("x-workspace-key", "").strip()
    if not token or not verify_workspace_token(workspace_id, token):
        raise HTTPException(status_code=403, detail="workspace access denied")


_SENSITIVE_PARTS = (
    "authorization", "api_key", "apikey", "password", "secret", "token",
    "webhook", "credential", "cookie",
)


def redact_sensitive(value: Any, *, key: str = "") -> Any:
    """Recursively redact credential-shaped values before persistence/output."""
    lowered = key.lower()
    if key and any(part in lowered for part in _SENSITIVE_PARTS):
        return "[REDACTED]"
    if isinstance(value, dict):
        return {str(k): redact_sensitive(v, key=str(k)) for k, v in value.items()}
    if isinstance(value, list):
        return [redact_sensitive(item) for item in value]
    if isinstance(value, tuple):
        return [redact_sensitive(item) for item in value]
    return value
