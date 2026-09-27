"""WorkGuard data model (proposal #19, trimmed to the date-change MVP).

MVP simplifications (documented in README):
- artifact_chunk/embeddings live as parsed blocks inside artifact_version.raw_content;
  pgvector + separate chunk table is the Phase-3 upgrade path.
- fact_relation is materialised as workspace-level dependency rules
  (predicate -> predicate templates + user-manually-added rules), which is the
  "template/preset dependency" approach chosen for the MVP instead of LLM-inferred
  implicit dependencies.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from backend.db import Base


def uid(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


def utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


# ---------------------------------------------------------------- core entities
class Workspace(Base):
    __tablename__ = "workspace"

    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    name: Mapped[str] = mapped_column(String(200))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class Artifact(Base):
    __tablename__ = "artifact"

    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    workspace_id: Mapped[str] = mapped_column(ForeignKey("workspace.id"), index=True)
    name: Mapped[str] = mapped_column(String(300))
    type: Mapped[str] = mapped_column(String(20))  # markdown | txt | docx | xlsx
    source_path: Mapped[str] = mapped_column(Text)
    current_version: Mapped[int] = mapped_column(Integer, default=1)
    artifact_role: Mapped[str] = mapped_column(String(30), default="document")
    # document | meeting | report  (meetings/reports are historical records:
    # the Verifier never proposes edits to them)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)

    versions: Mapped[list["ArtifactVersion"]] = relationship(back_populates="artifact")


class ArtifactVersion(Base):
    __tablename__ = "artifact_version"

    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    artifact_id: Mapped[str] = mapped_column(ForeignKey("artifact.id"), index=True)
    version: Mapped[int] = mapped_column(Integer)
    content_hash: Mapped[str] = mapped_column(String(64))
    raw_content: Mapped[str] = mapped_column(Text)  # file text, or base64 for binary formats
    parsed_content: Mapped[dict] = mapped_column(JSON)  # structured blocks with locations
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    artifact: Mapped[Artifact] = relationship(back_populates="versions")


class Entity(Base):
    __tablename__ = "entity"

    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    workspace_id: Mapped[str] = mapped_column(ForeignKey("workspace.id"), index=True)
    entity_type: Mapped[str] = mapped_column(String(30), default="project_version")
    canonical_name: Mapped[str] = mapped_column(String(200))
    aliases: Mapped[list] = mapped_column(JSON, default=list)
    status: Mapped[str] = mapped_column(String(30), default="active")
    # active | pending_disambiguation  (cold-start: user resolves once, alias is learned)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class Fact(Base):
    __tablename__ = "fact"
    __allow_unmapped__ = True

    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    workspace_id: Mapped[str] = mapped_column(String(40), index=True)
    entity_id: Mapped[str | None] = mapped_column(String(40), index=True, nullable=True)
    predicate: Mapped[str] = mapped_column(String(50), index=True)
    value: Mapped[str] = mapped_column(String(200))  # ISO date for the MVP
    value_type: Mapped[str] = mapped_column(String(20), default="date")
    status: Mapped[str] = mapped_column(String(20), default="verified")
    # verified | unverified (low confidence / unresolved entity: NEVER used for conflict detection)
    is_current: Mapped[bool] = mapped_column(Boolean, default=True)
    artifact_id: Mapped[str] = mapped_column(ForeignKey("artifact.id"), index=True)
    artifact_version_id: Mapped[str] = mapped_column(String(40))
    source_location: Mapped[str] = mapped_column(Text, default="")
    evidence: Mapped[str] = mapped_column(Text, default="")
    confidence: Mapped[float] = mapped_column(Float, default=0.0)
    extracted_by: Mapped[str] = mapped_column(String(20), default="heuristic")  # heuristic | llm | tool
    effective_time: Mapped[str] = mapped_column(String(20), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    _previous_current_id: str | None
    _truth_transition_reason: str


class DependencyRule(Base):
    """Preset / manually-added dependency between predicates (proposal #17, MVP form).

    relation "before": facts with predicate_a are expected to fall before facts
    with predicate_b. origin: preset | manual (user-associated in the UI).
    """

    __tablename__ = "dependency_rule"

    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    workspace_id: Mapped[str] = mapped_column(String(40), index=True)
    predicate_a: Mapped[str] = mapped_column(String(50))
    predicate_b: Mapped[str] = mapped_column(String(50))
    relation: Mapped[str] = mapped_column(String(20), default="before")
    origin: Mapped[str] = mapped_column(String(20), default="preset")  # preset | manual
    note: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


# ------------------------------------------------------------- change lifecycle
class ChangeEvent(Base):
    __tablename__ = "change_event"

    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    workspace_id: Mapped[str] = mapped_column(String(40), index=True)
    thread_id: Mapped[str] = mapped_column(String(80), index=True)  # LangGraph thread
    entity_id: Mapped[str] = mapped_column(String(40))
    entity_name: Mapped[str] = mapped_column(String(200), default="")
    predicate: Mapped[str] = mapped_column(String(50))
    old_value: Mapped[str] = mapped_column(String(200))
    new_value: Mapped[str] = mapped_column(String(200))
    old_fact_id: Mapped[str | None] = mapped_column(String(40), nullable=True)
    new_fact_id: Mapped[str | None] = mapped_column(String(40), nullable=True)
    source_artifact_id: Mapped[str] = mapped_column(String(40))
    confidence: Mapped[float] = mapped_column(Float, default=0.0)
    status: Mapped[str] = mapped_column(String(20), default="detected")
    # detected | verified | pending_approval | approved | executed |
    # verification_failed | partially_executed | rejected | rolled_back | rollback_failed
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)

    conflicts: Mapped[list["Conflict"]] = relationship(back_populates="change_event")
    impacts: Mapped[list["Impact"]] = relationship(back_populates="change_event")
    plan: Mapped["ChangePlan | None"] = relationship(back_populates="change_event")


class Conflict(Base):
    __tablename__ = "conflict"

    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    change_event_id: Mapped[str] = mapped_column(ForeignKey("change_event.id"), index=True)
    fact_id: Mapped[str | None] = mapped_column(String(40), nullable=True)
    artifact_id: Mapped[str] = mapped_column(String(40))
    location: Mapped[str] = mapped_column(Text, default="")
    conflict_type: Mapped[str] = mapped_column(String(30), default="outdated_fact")
    # outdated_fact | historical_reference | contextual | value_diverged
    verdict: Mapped[str] = mapped_column(String(20), default="conflict")
    # conflict | no_conflict | need_review
    confidence: Mapped[float] = mapped_column(Float, default=0.0)
    reason: Mapped[str] = mapped_column(Text, default="")
    evidence: Mapped[str] = mapped_column(Text, default="")

    change_event: Mapped[ChangeEvent] = relationship(back_populates="conflicts")


class Impact(Base):
    __tablename__ = "impact"

    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    change_event_id: Mapped[str] = mapped_column(ForeignKey("change_event.id"), index=True)
    fact_id: Mapped[str | None] = mapped_column(String(40), nullable=True)
    artifact_id: Mapped[str] = mapped_column(String(40))
    relation: Mapped[str] = mapped_column(String(200), default="")
    impact_type: Mapped[str] = mapped_column(String(30), default="reconfirm")
    # reconfirm | order_violation
    confidence: Mapped[float] = mapped_column(Float, default=0.0)
    reason: Mapped[str] = mapped_column(Text, default="")
    auto_update_allowed: Mapped[bool] = mapped_column(Boolean, default=False)
    status: Mapped[str] = mapped_column(String(20), default="open")  # open | resolved | dismissed

    change_event: Mapped[ChangeEvent] = relationship(back_populates="impacts")


class ChangePlan(Base):
    __tablename__ = "change_plan"

    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    change_event_id: Mapped[str] = mapped_column(ForeignKey("change_event.id"), index=True)
    status: Mapped[str] = mapped_column(String(20), default="pending")
    # pending | approved | partially_approved | rejected | executed | verification_failed
    approved_by: Mapped[str] = mapped_column(String(100), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    change_event: Mapped[ChangeEvent] = relationship(back_populates="plan")
    actions: Mapped[list["ChangeAction"]] = relationship(back_populates="plan")


class ChangeAction(Base):
    __tablename__ = "change_action"

    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    change_plan_id: Mapped[str] = mapped_column(ForeignKey("change_plan.id"), index=True)
    artifact_id: Mapped[str] = mapped_column(String(40))
    action_type: Mapped[str] = mapped_column(String(30))
    # update_artifact | suggest_patch | human_review
    method: Mapped[str] = mapped_column(String(30), default="direct_write")
    # direct_write (md/txt) | controlled_write (docx/xlsx, opt-in) | suggestion
    old_value: Mapped[str] = mapped_column(String(200), default="")
    new_value: Mapped[str] = mapped_column(String(200), default="")
    locations: Mapped[list] = mapped_column(JSON, default=list)
    risk: Mapped[str] = mapped_column(String(10), default="low")  # low | medium | high
    status: Mapped[str] = mapped_column(String(20), default="pending")
    # pending | executed | failed | verification_failed | skipped | rolled_back
    patch_path: Mapped[str] = mapped_column(Text, default="")
    snapshot_version_id: Mapped[str] = mapped_column(String(40), default="")
    tool_result: Mapped[dict] = mapped_column(JSON, default=dict)

    plan: Mapped[ChangePlan] = relationship(back_populates="actions")


class AuditLog(Base):
    __tablename__ = "audit_log"

    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    workspace_id: Mapped[str] = mapped_column(String(40), index=True)
    change_action_id: Mapped[str] = mapped_column(String(40), default="")
    change_event_id: Mapped[str] = mapped_column(String(40), default="")
    actor: Mapped[str] = mapped_column(String(40), default="agent")
    tool: Mapped[str] = mapped_column(String(60))
    input: Mapped[dict] = mapped_column(JSON, default=dict)
    output: Mapped[dict] = mapped_column(JSON, default=dict)
    status: Mapped[str] = mapped_column(String(20), default="success")
    executed_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class AgentRun(Base):
    """Observability record for one LangGraph execution (thread)."""

    __tablename__ = "agent_run"

    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    thread_id: Mapped[str] = mapped_column(String(80), index=True)
    workspace_id: Mapped[str] = mapped_column(String(40), index=True)
    artifact_id: Mapped[str] = mapped_column(String(40), default="")
    graph: Mapped[str] = mapped_column(String(40), default="change_detection")
    status: Mapped[str] = mapped_column(String(20), default="running")
    # running | waiting_approval | completed | rejected | failed
    errors: Mapped[list] = mapped_column(JSON, default=list)
    started_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class BackgroundJob(Base):
    """Durable work item claimed by an API-local or standalone worker."""

    __tablename__ = "background_job"
    __table_args__ = (UniqueConstraint("idempotency_key", name="uq_background_job_idempotency"),)

    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    workspace_id: Mapped[str] = mapped_column(String(40), index=True, default="")
    kind: Mapped[str] = mapped_column(String(50), index=True)
    payload: Mapped[dict] = mapped_column(JSON, default=dict)
    idempotency_key: Mapped[str | None] = mapped_column(String(200), nullable=True)
    status: Mapped[str] = mapped_column(String(20), index=True, default="queued")
    # queued | running | completed | failed
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    max_attempts: Mapped[int] = mapped_column(Integer, default=3)
    available_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    worker_id: Mapped[str] = mapped_column(String(100), default="")
    result: Mapped[dict] = mapped_column(JSON, default=dict)
    error: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    started_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)


class LLMUsageRecord(Base):
    """One durable provider attempt; aggregate metrics survive process restarts."""

    __tablename__ = "llm_usage_record"

    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    purpose: Mapped[str] = mapped_column(String(80), index=True)
    model: Mapped[str] = mapped_column(String(120), index=True)
    prompt_tokens: Mapped[int] = mapped_column(Integer, default=0)
    completion_tokens: Mapped[int] = mapped_column(Integer, default=0)
    latency_ms: Mapped[float] = mapped_column(Float, default=0.0)
    ok: Mapped[bool] = mapped_column(Boolean, default=True, index=True)
    error: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)


# ------------------------------------------------------- Feishu integration
class FeishuBinding(Base):
    """Explicit folder-to-workspace routing for Feishu sync and events."""

    __tablename__ = "feishu_binding"
    __table_args__ = (UniqueConstraint("folder_token", name="uq_feishu_folder_token"),)

    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    workspace_id: Mapped[str] = mapped_column(ForeignKey("workspace.id"), index=True)
    folder_token: Mapped[str] = mapped_column(String(200), index=True)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    last_synced_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class FeishuRemoteFile(Base):
    """Remote identity learned during folder sync, used for exact event routing."""

    __tablename__ = "feishu_remote_file"
    __table_args__ = (UniqueConstraint("remote_token", name="uq_feishu_remote_token"),)

    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    binding_id: Mapped[str] = mapped_column(ForeignKey("feishu_binding.id"), index=True)
    remote_token: Mapped[str] = mapped_column(String(200), index=True)
    remote_type: Mapped[str] = mapped_column(String(30))
    remote_name: Mapped[str] = mapped_column(String(300))
    artifact_id: Mapped[str] = mapped_column(String(40), default="")
    content_hash: Mapped[str] = mapped_column(String(64), default="")
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)


class FeishuEvent(Base):
    """Persistent event receipt gives webhook delivery database-backed idempotency."""

    __tablename__ = "feishu_event"
    __table_args__ = (UniqueConstraint("event_id", name="uq_feishu_event_id"),)

    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    event_id: Mapped[str] = mapped_column(String(200), index=True)
    event_type: Mapped[str] = mapped_column(String(200), default="")
    file_token: Mapped[str] = mapped_column(String(200), default="")
    binding_id: Mapped[str] = mapped_column(String(40), default="")
    status: Mapped[str] = mapped_column(String(30), default="queued")
    # queued | completed | failed | ignored
    payload: Mapped[dict] = mapped_column(JSON, default=dict)
    result: Mapped[dict] = mapped_column(JSON, default=dict)
    error: Mapped[str] = mapped_column(Text, default="")
    received_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    processed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
