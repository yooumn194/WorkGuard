"""Add durable background jobs and LLM usage accounting.

Revision ID: 20260909_0002
Revises: 20260908_0001
"""
from alembic import op
import sqlalchemy as sa

revision = "20260909_0002"
down_revision = "20260908_0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "background_job",
        sa.Column("id", sa.String(length=40), nullable=False),
        sa.Column("workspace_id", sa.String(length=40), nullable=False),
        sa.Column("kind", sa.String(length=50), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("idempotency_key", sa.String(length=200), nullable=True),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("max_attempts", sa.Integer(), nullable=False),
        sa.Column("available_at", sa.DateTime(), nullable=False),
        sa.Column("lease_expires_at", sa.DateTime(), nullable=True),
        sa.Column("worker_id", sa.String(length=100), nullable=False),
        sa.Column("result", sa.JSON(), nullable=False),
        sa.Column("error", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("started_at", sa.DateTime(), nullable=True),
        sa.Column("finished_at", sa.DateTime(), nullable=True),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("idempotency_key", name="uq_background_job_idempotency"),
    )
    op.create_index(op.f("ix_background_job_available_at"), "background_job", ["available_at"])
    op.create_index(op.f("ix_background_job_kind"), "background_job", ["kind"])
    op.create_index(op.f("ix_background_job_status"), "background_job", ["status"])
    op.create_index(op.f("ix_background_job_workspace_id"), "background_job", ["workspace_id"])

    op.create_table(
        "llm_usage_record",
        sa.Column("id", sa.String(length=40), nullable=False),
        sa.Column("purpose", sa.String(length=80), nullable=False),
        sa.Column("model", sa.String(length=120), nullable=False),
        sa.Column("prompt_tokens", sa.Integer(), nullable=False),
        sa.Column("completion_tokens", sa.Integer(), nullable=False),
        sa.Column("latency_ms", sa.Float(), nullable=False),
        sa.Column("ok", sa.Boolean(), nullable=False),
        sa.Column("error", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_llm_usage_record_created_at"), "llm_usage_record", ["created_at"])
    op.create_index(op.f("ix_llm_usage_record_model"), "llm_usage_record", ["model"])
    op.create_index(op.f("ix_llm_usage_record_ok"), "llm_usage_record", ["ok"])
    op.create_index(op.f("ix_llm_usage_record_purpose"), "llm_usage_record", ["purpose"])


def downgrade() -> None:
    op.drop_index(op.f("ix_llm_usage_record_purpose"), table_name="llm_usage_record")
    op.drop_index(op.f("ix_llm_usage_record_ok"), table_name="llm_usage_record")
    op.drop_index(op.f("ix_llm_usage_record_model"), table_name="llm_usage_record")
    op.drop_index(op.f("ix_llm_usage_record_created_at"), table_name="llm_usage_record")
    op.drop_table("llm_usage_record")
    op.drop_index(op.f("ix_background_job_workspace_id"), table_name="background_job")
    op.drop_index(op.f("ix_background_job_status"), table_name="background_job")
    op.drop_index(op.f("ix_background_job_kind"), table_name="background_job")
    op.drop_index(op.f("ix_background_job_available_at"), table_name="background_job")
    op.drop_table("background_job")
