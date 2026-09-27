"""Initial WorkGuard schema baseline.

Revision ID: 20260908_0001
Revises:
"""
from alembic import op
import sqlalchemy as sa

revision = "20260908_0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "agent_run",
        sa.Column("id", sa.String(length=40), nullable=False),
        sa.Column("thread_id", sa.String(length=80), nullable=False),
        sa.Column("workspace_id", sa.String(length=40), nullable=False),
        sa.Column("artifact_id", sa.String(length=40), nullable=False),
        sa.Column("graph", sa.String(length=40), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("errors", sa.JSON(), nullable=False),
        sa.Column("started_at", sa.DateTime(), nullable=False),
        sa.Column("finished_at", sa.DateTime(), nullable=True),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_agent_run_thread_id"), "agent_run", ["thread_id"])
    op.create_index(op.f("ix_agent_run_workspace_id"), "agent_run", ["workspace_id"])
    op.create_table(
        "audit_log",
        sa.Column("id", sa.String(length=40), nullable=False),
        sa.Column("workspace_id", sa.String(length=40), nullable=False),
        sa.Column("change_action_id", sa.String(length=40), nullable=False),
        sa.Column("change_event_id", sa.String(length=40), nullable=False),
        sa.Column("actor", sa.String(length=40), nullable=False),
        sa.Column("tool", sa.String(length=60), nullable=False),
        sa.Column("input", sa.JSON(), nullable=False),
        sa.Column("output", sa.JSON(), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("executed_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_audit_log_workspace_id"), "audit_log", ["workspace_id"])
    op.create_table(
        "change_event",
        sa.Column("id", sa.String(length=40), nullable=False),
        sa.Column("workspace_id", sa.String(length=40), nullable=False),
        sa.Column("thread_id", sa.String(length=80), nullable=False),
        sa.Column("entity_id", sa.String(length=40), nullable=False),
        sa.Column("entity_name", sa.String(length=200), nullable=False),
        sa.Column("predicate", sa.String(length=50), nullable=False),
        sa.Column("old_value", sa.String(length=200), nullable=False),
        sa.Column("new_value", sa.String(length=200), nullable=False),
        sa.Column("old_fact_id", sa.String(length=40), nullable=True),
        sa.Column("new_fact_id", sa.String(length=40), nullable=True),
        sa.Column("source_artifact_id", sa.String(length=40), nullable=False),
        sa.Column("confidence", sa.Float(), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_change_event_thread_id"), "change_event", ["thread_id"])
    op.create_index(op.f("ix_change_event_workspace_id"), "change_event", ["workspace_id"])
    op.create_table(
        "dependency_rule",
        sa.Column("id", sa.String(length=40), nullable=False),
        sa.Column("workspace_id", sa.String(length=40), nullable=False),
        sa.Column("predicate_a", sa.String(length=50), nullable=False),
        sa.Column("predicate_b", sa.String(length=50), nullable=False),
        sa.Column("relation", sa.String(length=20), nullable=False),
        sa.Column("origin", sa.String(length=20), nullable=False),
        sa.Column("note", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_dependency_rule_workspace_id"), "dependency_rule", ["workspace_id"])
    op.create_table(
        "feishu_event",
        sa.Column("id", sa.String(length=40), nullable=False),
        sa.Column("event_id", sa.String(length=200), nullable=False),
        sa.Column("event_type", sa.String(length=200), nullable=False),
        sa.Column("file_token", sa.String(length=200), nullable=False),
        sa.Column("binding_id", sa.String(length=40), nullable=False),
        sa.Column("status", sa.String(length=30), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("result", sa.JSON(), nullable=False),
        sa.Column("error", sa.Text(), nullable=False),
        sa.Column("received_at", sa.DateTime(), nullable=False),
        sa.Column("processed_at", sa.DateTime(), nullable=True),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("event_id", name="uq_feishu_event_id"),
    )
    op.create_index(op.f("ix_feishu_event_event_id"), "feishu_event", ["event_id"])
    op.create_table(
        "workspace",
        sa.Column("id", sa.String(length=40), nullable=False),
        sa.Column("name", sa.String(length=200), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_table(
        "artifact",
        sa.Column("id", sa.String(length=40), nullable=False),
        sa.Column("workspace_id", sa.String(length=40), nullable=False),
        sa.Column("name", sa.String(length=300), nullable=False),
        sa.Column("type", sa.String(length=20), nullable=False),
        sa.Column("source_path", sa.Text(), nullable=False),
        sa.Column("current_version", sa.Integer(), nullable=False),
        sa.Column("artifact_role", sa.String(length=30), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["workspace_id"], ["workspace.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_artifact_workspace_id"), "artifact", ["workspace_id"])
    op.create_table(
        "change_plan",
        sa.Column("id", sa.String(length=40), nullable=False),
        sa.Column("change_event_id", sa.String(length=40), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("approved_by", sa.String(length=100), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["change_event_id"], ["change_event.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_change_plan_change_event_id"), "change_plan", ["change_event_id"])
    op.create_table(
        "conflict",
        sa.Column("id", sa.String(length=40), nullable=False),
        sa.Column("change_event_id", sa.String(length=40), nullable=False),
        sa.Column("fact_id", sa.String(length=40), nullable=True),
        sa.Column("artifact_id", sa.String(length=40), nullable=False),
        sa.Column("location", sa.Text(), nullable=False),
        sa.Column("conflict_type", sa.String(length=30), nullable=False),
        sa.Column("verdict", sa.String(length=20), nullable=False),
        sa.Column("confidence", sa.Float(), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("evidence", sa.Text(), nullable=False),
        sa.ForeignKeyConstraint(["change_event_id"], ["change_event.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_conflict_change_event_id"), "conflict", ["change_event_id"])
    op.create_table(
        "entity",
        sa.Column("id", sa.String(length=40), nullable=False),
        sa.Column("workspace_id", sa.String(length=40), nullable=False),
        sa.Column("entity_type", sa.String(length=30), nullable=False),
        sa.Column("canonical_name", sa.String(length=200), nullable=False),
        sa.Column("aliases", sa.JSON(), nullable=False),
        sa.Column("status", sa.String(length=30), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["workspace_id"], ["workspace.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_entity_workspace_id"), "entity", ["workspace_id"])
    op.create_table(
        "feishu_binding",
        sa.Column("id", sa.String(length=40), nullable=False),
        sa.Column("workspace_id", sa.String(length=40), nullable=False),
        sa.Column("folder_token", sa.String(length=200), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("last_synced_at", sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(["workspace_id"], ["workspace.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("folder_token", name="uq_feishu_folder_token"),
    )
    op.create_index(op.f("ix_feishu_binding_folder_token"), "feishu_binding", ["folder_token"])
    op.create_index(op.f("ix_feishu_binding_workspace_id"), "feishu_binding", ["workspace_id"])
    op.create_table(
        "impact",
        sa.Column("id", sa.String(length=40), nullable=False),
        sa.Column("change_event_id", sa.String(length=40), nullable=False),
        sa.Column("fact_id", sa.String(length=40), nullable=True),
        sa.Column("artifact_id", sa.String(length=40), nullable=False),
        sa.Column("relation", sa.String(length=200), nullable=False),
        sa.Column("impact_type", sa.String(length=30), nullable=False),
        sa.Column("confidence", sa.Float(), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("auto_update_allowed", sa.Boolean(), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.ForeignKeyConstraint(["change_event_id"], ["change_event.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_impact_change_event_id"), "impact", ["change_event_id"])
    op.create_table(
        "artifact_version",
        sa.Column("id", sa.String(length=40), nullable=False),
        sa.Column("artifact_id", sa.String(length=40), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("content_hash", sa.String(length=64), nullable=False),
        sa.Column("raw_content", sa.Text(), nullable=False),
        sa.Column("parsed_content", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["artifact_id"], ["artifact.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_artifact_version_artifact_id"), "artifact_version", ["artifact_id"])
    op.create_table(
        "change_action",
        sa.Column("id", sa.String(length=40), nullable=False),
        sa.Column("change_plan_id", sa.String(length=40), nullable=False),
        sa.Column("artifact_id", sa.String(length=40), nullable=False),
        sa.Column("action_type", sa.String(length=30), nullable=False),
        sa.Column("method", sa.String(length=30), nullable=False),
        sa.Column("old_value", sa.String(length=200), nullable=False),
        sa.Column("new_value", sa.String(length=200), nullable=False),
        sa.Column("locations", sa.JSON(), nullable=False),
        sa.Column("risk", sa.String(length=10), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("patch_path", sa.Text(), nullable=False),
        sa.Column("snapshot_version_id", sa.String(length=40), nullable=False),
        sa.Column("tool_result", sa.JSON(), nullable=False),
        sa.ForeignKeyConstraint(["change_plan_id"], ["change_plan.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_change_action_change_plan_id"), "change_action", ["change_plan_id"])
    op.create_table(
        "fact",
        sa.Column("id", sa.String(length=40), nullable=False),
        sa.Column("workspace_id", sa.String(length=40), nullable=False),
        sa.Column("entity_id", sa.String(length=40), nullable=True),
        sa.Column("predicate", sa.String(length=50), nullable=False),
        sa.Column("value", sa.String(length=200), nullable=False),
        sa.Column("value_type", sa.String(length=20), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("is_current", sa.Boolean(), nullable=False),
        sa.Column("artifact_id", sa.String(length=40), nullable=False),
        sa.Column("artifact_version_id", sa.String(length=40), nullable=False),
        sa.Column("source_location", sa.Text(), nullable=False),
        sa.Column("evidence", sa.Text(), nullable=False),
        sa.Column("confidence", sa.Float(), nullable=False),
        sa.Column("extracted_by", sa.String(length=20), nullable=False),
        sa.Column("effective_time", sa.String(length=20), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["artifact_id"], ["artifact.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_fact_artifact_id"), "fact", ["artifact_id"])
    op.create_index(op.f("ix_fact_entity_id"), "fact", ["entity_id"])
    op.create_index(op.f("ix_fact_predicate"), "fact", ["predicate"])
    op.create_index(op.f("ix_fact_workspace_id"), "fact", ["workspace_id"])
    op.create_table(
        "feishu_remote_file",
        sa.Column("id", sa.String(length=40), nullable=False),
        sa.Column("binding_id", sa.String(length=40), nullable=False),
        sa.Column("remote_token", sa.String(length=200), nullable=False),
        sa.Column("remote_type", sa.String(length=30), nullable=False),
        sa.Column("remote_name", sa.String(length=300), nullable=False),
        sa.Column("artifact_id", sa.String(length=40), nullable=False),
        sa.Column("content_hash", sa.String(length=64), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["binding_id"], ["feishu_binding.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("remote_token", name="uq_feishu_remote_token"),
    )
    op.create_index(op.f("ix_feishu_remote_file_binding_id"), "feishu_remote_file", ["binding_id"])
    op.create_index(op.f("ix_feishu_remote_file_remote_token"), "feishu_remote_file", ["remote_token"])


def downgrade() -> None:
    op.drop_index(op.f("ix_feishu_remote_file_remote_token"), table_name="feishu_remote_file")
    op.drop_index(op.f("ix_feishu_remote_file_binding_id"), table_name="feishu_remote_file")
    op.drop_table("feishu_remote_file")
    op.drop_index(op.f("ix_fact_workspace_id"), table_name="fact")
    op.drop_index(op.f("ix_fact_predicate"), table_name="fact")
    op.drop_index(op.f("ix_fact_entity_id"), table_name="fact")
    op.drop_index(op.f("ix_fact_artifact_id"), table_name="fact")
    op.drop_table("fact")
    op.drop_index(op.f("ix_change_action_change_plan_id"), table_name="change_action")
    op.drop_table("change_action")
    op.drop_index(op.f("ix_artifact_version_artifact_id"), table_name="artifact_version")
    op.drop_table("artifact_version")
    op.drop_index(op.f("ix_impact_change_event_id"), table_name="impact")
    op.drop_table("impact")
    op.drop_index(op.f("ix_feishu_binding_workspace_id"), table_name="feishu_binding")
    op.drop_index(op.f("ix_feishu_binding_folder_token"), table_name="feishu_binding")
    op.drop_table("feishu_binding")
    op.drop_index(op.f("ix_entity_workspace_id"), table_name="entity")
    op.drop_table("entity")
    op.drop_index(op.f("ix_conflict_change_event_id"), table_name="conflict")
    op.drop_table("conflict")
    op.drop_index(op.f("ix_change_plan_change_event_id"), table_name="change_plan")
    op.drop_table("change_plan")
    op.drop_index(op.f("ix_artifact_workspace_id"), table_name="artifact")
    op.drop_table("artifact")
    op.drop_table("workspace")
    op.drop_index(op.f("ix_feishu_event_event_id"), table_name="feishu_event")
    op.drop_table("feishu_event")
    op.drop_index(op.f("ix_dependency_rule_workspace_id"), table_name="dependency_rule")
    op.drop_table("dependency_rule")
    op.drop_index(op.f("ix_change_event_workspace_id"), table_name="change_event")
    op.drop_index(op.f("ix_change_event_thread_id"), table_name="change_event")
    op.drop_table("change_event")
    op.drop_index(op.f("ix_audit_log_workspace_id"), table_name="audit_log")
    op.drop_table("audit_log")
    op.drop_index(op.f("ix_agent_run_workspace_id"), table_name="agent_run")
    op.drop_index(op.f("ix_agent_run_thread_id"), table_name="agent_run")
    op.drop_table("agent_run")
