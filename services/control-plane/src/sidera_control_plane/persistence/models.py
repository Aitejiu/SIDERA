from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    PrimaryKeyConstraint,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PostgreSQLUUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


class TimestampMixin:
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class WorkspaceModel(TimestampMixin, Base):
    __tablename__ = "workspaces"
    __table_args__ = (CheckConstraint("version >= 1", name="ck_workspaces_version"),)

    id: Mapped[UUID] = mapped_column(PostgreSQLUUID(as_uuid=True), primary_key=True)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )


class PlanSnapshotModel(TimestampMixin, Base):
    __tablename__ = "task_plan_snapshots"
    __table_args__ = (
        ForeignKeyConstraint(
            ["root_task_id", "workspace_id"],
            ["tasks.id", "tasks.workspace_id"],
            ondelete="RESTRICT",
            name="fk_plan_snapshots_root_task_workspace",
        ),
        ForeignKeyConstraint(
            ["parent_snapshot_id"],
            ["task_plan_snapshots.id"],
            ondelete="RESTRICT",
            name="fk_plan_snapshots_parent",
        ),
        CheckConstraint("generation >= 1", name="ck_plan_snapshots_generation"),
        CheckConstraint("schema_version >= 1", name="ck_plan_snapshots_schema_version"),
        UniqueConstraint(
            "root_task_id", "generation", name="uq_plan_snapshots_root_generation"
        ),
        UniqueConstraint("id", "workspace_id", name="uq_plan_snapshots_id_workspace"),
        Index(
            "ix_plan_snapshots_workspace_root_generation",
            "workspace_id",
            "root_task_id",
            "generation",
        ),
    )

    id: Mapped[UUID] = mapped_column(PostgreSQLUUID(as_uuid=True), primary_key=True)
    schema_version: Mapped[int] = mapped_column(Integer, nullable=False)
    workspace_id: Mapped[UUID] = mapped_column(
        PostgreSQLUUID(as_uuid=True),
        ForeignKey("workspaces.id", ondelete="RESTRICT"),
        nullable=False,
    )
    root_task_id: Mapped[UUID] = mapped_column(
        PostgreSQLUUID(as_uuid=True), nullable=False
    )
    generation: Mapped[int] = mapped_column(Integer, nullable=False)
    parent_snapshot_id: Mapped[UUID | None] = mapped_column(
        PostgreSQLUUID(as_uuid=True)
    )
    source_type: Mapped[str] = mapped_column(String(100), nullable=False)
    source_ref: Mapped[str] = mapped_column(String(500), nullable=False)
    title: Mapped[str] = mapped_column(String(500), nullable=False)
    goal_summary: Mapped[str] = mapped_column(Text, nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    published_by: Mapped[str] = mapped_column(String(255), nullable=False)
    published_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )


class RootTaskPlanHeadModel(TimestampMixin, Base):
    __tablename__ = "root_task_plan_heads"
    __table_args__ = (
        CheckConstraint("active_generation >= 1", name="ck_plan_heads_generation"),
        CheckConstraint(
            "active_snapshot_hash ~ '^[0-9a-f]{64}$'",
            name="ck_plan_heads_snapshot_hash",
        ),
        CheckConstraint("version >= 1", name="ck_plan_heads_version"),
        ForeignKeyConstraint(
            ["workspace_id"],
            ["workspaces.id"],
            ondelete="RESTRICT",
            name="fk_plan_heads_workspace",
        ),
        ForeignKeyConstraint(
            ["root_task_id", "workspace_id"],
            ["tasks.id", "tasks.workspace_id"],
            ondelete="RESTRICT",
            name="fk_plan_heads_root_task_workspace",
        ),
        ForeignKeyConstraint(
            ["active_snapshot_id", "workspace_id"],
            ["task_plan_snapshots.id", "task_plan_snapshots.workspace_id"],
            ondelete="RESTRICT",
            name="fk_plan_heads_snapshot_workspace",
        ),
        PrimaryKeyConstraint(
            "workspace_id", "root_task_id", name="pk_root_task_plan_heads"
        ),
        Index(
            "ix_plan_heads_workspace_generation",
            "workspace_id",
            "active_generation",
        ),
    )

    workspace_id: Mapped[UUID] = mapped_column(
        PostgreSQLUUID(as_uuid=True), nullable=False
    )
    root_task_id: Mapped[UUID] = mapped_column(
        PostgreSQLUUID(as_uuid=True), nullable=False
    )
    active_snapshot_id: Mapped[UUID] = mapped_column(
        PostgreSQLUUID(as_uuid=True), nullable=False
    )
    active_generation: Mapped[int] = mapped_column(Integer, nullable=False)
    active_snapshot_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )


class PlanTransitionModel(TimestampMixin, Base):
    __tablename__ = "plan_transitions"
    __table_args__ = (
        CheckConstraint(
            "status IN ('converging', 'converged', 'degraded')",
            name="ck_plan_transitions_status",
        ),
        CheckConstraint(
            "from_generation >= 1 AND to_generation = from_generation + 1",
            name="ck_plan_transitions_generations",
        ),
        CheckConstraint(
            "from_snapshot_id <> to_snapshot_id",
            name="ck_plan_transitions_distinct_snapshots",
        ),
        CheckConstraint("version >= 1", name="ck_plan_transitions_version"),
        ForeignKeyConstraint(
            ["workspace_id"],
            ["workspaces.id"],
            ondelete="RESTRICT",
            name="fk_plan_transitions_workspace",
        ),
        ForeignKeyConstraint(
            ["root_task_id", "workspace_id"],
            ["tasks.id", "tasks.workspace_id"],
            ondelete="RESTRICT",
            name="fk_plan_transitions_root_task_workspace",
        ),
        ForeignKeyConstraint(
            ["planning_request_id", "workspace_id"],
            ["planning_requests.id", "planning_requests.workspace_id"],
            ondelete="RESTRICT",
            name="fk_plan_transitions_request_workspace",
        ),
        ForeignKeyConstraint(
            ["proposal_revision_id", "workspace_id"],
            [
                "planning_proposal_revisions.id",
                "planning_proposal_revisions.workspace_id",
            ],
            ondelete="RESTRICT",
            name="fk_plan_transitions_proposal_workspace",
        ),
        ForeignKeyConstraint(
            ["from_snapshot_id", "workspace_id"],
            ["task_plan_snapshots.id", "task_plan_snapshots.workspace_id"],
            ondelete="RESTRICT",
            name="fk_plan_transitions_from_snapshot_workspace",
        ),
        ForeignKeyConstraint(
            ["to_snapshot_id", "workspace_id"],
            ["task_plan_snapshots.id", "task_plan_snapshots.workspace_id"],
            ondelete="RESTRICT",
            name="fk_plan_transitions_to_snapshot_workspace",
        ),
        PrimaryKeyConstraint("id"),
        UniqueConstraint("id", "workspace_id", name="uq_plan_transitions_identity"),
        UniqueConstraint(
            "planning_request_id",
            "proposal_revision_id",
            name="uq_plan_transitions_request_proposal",
        ),
        Index(
            "sidera_plan_transitions_active",
            "workspace_id",
            "root_task_id",
            "started_at",
            postgresql_where=text("status = 'converging'"),
        ),
    )

    id: Mapped[UUID] = mapped_column(PostgreSQLUUID(as_uuid=True), primary_key=True)
    workspace_id: Mapped[UUID] = mapped_column(
        PostgreSQLUUID(as_uuid=True), nullable=False
    )
    root_task_id: Mapped[UUID] = mapped_column(
        PostgreSQLUUID(as_uuid=True), nullable=False
    )
    planning_request_id: Mapped[UUID] = mapped_column(
        PostgreSQLUUID(as_uuid=True), nullable=False
    )
    proposal_revision_id: Mapped[UUID] = mapped_column(
        PostgreSQLUUID(as_uuid=True), nullable=False
    )
    from_snapshot_id: Mapped[UUID] = mapped_column(
        PostgreSQLUUID(as_uuid=True), nullable=False
    )
    from_generation: Mapped[int] = mapped_column(Integer, nullable=False)
    to_snapshot_id: Mapped[UUID] = mapped_column(
        PostgreSQLUUID(as_uuid=True), nullable=False
    )
    to_generation: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    version: Mapped[int] = mapped_column(Integer, nullable=False)


class WorkerTransitionModel(TimestampMixin, Base):
    __tablename__ = "worker_transitions"
    __table_args__ = (
        CheckConstraint(
            "action IN ('preserve', 'move', 'patch', 'continue', 'create', 'retire')",
            name="ck_worker_transitions_action",
        ),
        CheckConstraint(
            "fallback_action IS NULL OR fallback_action IN "
            "('preserve', 'move', 'patch', 'continue', 'create', 'retire')",
            name="ck_worker_transitions_fallback_action",
        ),
        CheckConstraint(
            "status IN ('pending', 'waiting_safe_boundary', 'applying', "
            "'waiting_activation', 'converged', 'failed', 'blocked_unknown')",
            name="ck_worker_transitions_status",
        ),
        CheckConstraint(
            "writer_state IN ('active', 'fenced', 'closed', 'unknown')",
            name="ck_worker_transitions_writer_state",
        ),
        CheckConstraint(
            "target_context_version >= 1",
            name="ck_worker_transitions_target_context_version",
        ),
        CheckConstraint(
            "fencing_token >= 0", name="ck_worker_transitions_fencing_token"
        ),
        CheckConstraint(
            "attempt_count >= 0", name="ck_worker_transitions_attempt_count"
        ),
        CheckConstraint("version >= 1", name="ck_worker_transitions_version"),
        CheckConstraint(
            "lease_id IS NULL OR source_execution_id IS NOT NULL",
            name="ck_worker_transitions_lease_source",
        ),
        ForeignKeyConstraint(
            ["plan_transition_id", "workspace_id"],
            ["plan_transitions.id", "plan_transitions.workspace_id"],
            ondelete="RESTRICT",
            name="fk_worker_transitions_plan_transition_workspace",
        ),
        ForeignKeyConstraint(
            ["workspace_id"],
            ["workspaces.id"],
            ondelete="RESTRICT",
            name="fk_worker_transitions_workspace",
        ),
        ForeignKeyConstraint(
            ["root_task_id", "workspace_id"],
            ["tasks.id", "tasks.workspace_id"],
            ondelete="RESTRICT",
            name="fk_worker_transitions_root_task_workspace",
        ),
        ForeignKeyConstraint(
            ["worker_item_id", "workspace_id"],
            ["worker_items.id", "worker_items.workspace_id"],
            ondelete="RESTRICT",
            name="fk_worker_transitions_worker_item_workspace",
        ),
        ForeignKeyConstraint(
            ["from_subtask_id", "workspace_id"],
            ["tasks.id", "tasks.workspace_id"],
            ondelete="RESTRICT",
            name="fk_worker_transitions_from_subtask_workspace",
        ),
        ForeignKeyConstraint(
            ["to_subtask_id", "workspace_id"],
            ["tasks.id", "tasks.workspace_id"],
            ondelete="RESTRICT",
            name="fk_worker_transitions_to_subtask_workspace",
        ),
        ForeignKeyConstraint(
            ["source_execution_id", "workspace_id"],
            ["executions.id", "executions.workspace_id"],
            ondelete="RESTRICT",
            name="fk_worker_transitions_source_execution_workspace",
        ),
        ForeignKeyConstraint(
            ["source_session_id", "workspace_id"],
            ["execution_sessions.id", "execution_sessions.workspace_id"],
            ondelete="RESTRICT",
            name="fk_worker_transitions_source_session_workspace",
        ),
        ForeignKeyConstraint(
            ["target_execution_id", "workspace_id"],
            ["executions.id", "executions.workspace_id"],
            ondelete="RESTRICT",
            name="fk_worker_transitions_target_execution_workspace",
        ),
        ForeignKeyConstraint(
            ["target_session_id", "workspace_id"],
            ["execution_sessions.id", "execution_sessions.workspace_id"],
            ondelete="RESTRICT",
            name="fk_worker_transitions_target_session_workspace",
        ),
        ForeignKeyConstraint(
            ["lease_id", "source_execution_id", "workspace_id"],
            [
                "assignment_leases.id",
                "assignment_leases.execution_id",
                "assignment_leases.workspace_id",
            ],
            ondelete="RESTRICT",
            name="fk_worker_transitions_lease_source_execution_workspace",
        ),
        PrimaryKeyConstraint("id"),
        UniqueConstraint("id", "workspace_id", name="uq_worker_transitions_identity"),
        Index(
            "uq_worker_transitions_active_worker_item",
            "worker_item_id",
            unique=True,
            postgresql_where=text(
                "status IN ('pending', 'waiting_safe_boundary', 'applying', "
                "'waiting_activation', 'blocked_unknown')"
            ),
        ),
        Index(
            "ix_worker_transitions_claimable",
            "status",
            "next_attempt_at",
        ),
    )

    id: Mapped[UUID] = mapped_column(PostgreSQLUUID(as_uuid=True), primary_key=True)
    plan_transition_id: Mapped[UUID] = mapped_column(
        PostgreSQLUUID(as_uuid=True), nullable=False
    )
    workspace_id: Mapped[UUID] = mapped_column(
        PostgreSQLUUID(as_uuid=True), nullable=False
    )
    root_task_id: Mapped[UUID] = mapped_column(
        PostgreSQLUUID(as_uuid=True), nullable=False
    )
    worker_item_id: Mapped[UUID] = mapped_column(
        PostgreSQLUUID(as_uuid=True), nullable=False
    )
    from_subtask_id: Mapped[UUID | None] = mapped_column(PostgreSQLUUID(as_uuid=True))
    to_subtask_id: Mapped[UUID | None] = mapped_column(PostgreSQLUUID(as_uuid=True))
    action: Mapped[str] = mapped_column(String(32), nullable=False)
    fallback_action: Mapped[str | None] = mapped_column(String(32))
    target_context_version: Mapped[int] = mapped_column(Integer, nullable=False)
    source_execution_id: Mapped[UUID | None] = mapped_column(
        PostgreSQLUUID(as_uuid=True)
    )
    source_session_id: Mapped[UUID | None] = mapped_column(PostgreSQLUUID(as_uuid=True))
    target_execution_id: Mapped[UUID | None] = mapped_column(
        PostgreSQLUUID(as_uuid=True)
    )
    target_session_id: Mapped[UUID | None] = mapped_column(PostgreSQLUUID(as_uuid=True))
    writer_state: Mapped[str] = mapped_column(String(32), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    failure_code: Mapped[str | None] = mapped_column(String(100))
    lease_id: Mapped[UUID | None] = mapped_column(PostgreSQLUUID(as_uuid=True))
    fencing_token: Mapped[int] = mapped_column(BigInteger, nullable=False)
    attempt_count: Mapped[int] = mapped_column(Integer, nullable=False)
    next_attempt_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    version: Mapped[int] = mapped_column(Integer, nullable=False)


class PlanEntityLineageModel(TimestampMixin, Base):
    __tablename__ = "plan_entity_lineages"
    __table_args__ = (
        CheckConstraint(
            "length(btrim(target_type)) > 0",
            name="ck_plan_entity_lineages_target_type",
        ),
        CheckConstraint(
            "length(btrim(source_type)) > 0",
            name="ck_plan_entity_lineages_source_type",
        ),
        CheckConstraint(
            "length(btrim(reason)) > 0", name="ck_plan_entity_lineages_reason"
        ),
        ForeignKeyConstraint(
            ["plan_transition_id", "workspace_id"],
            ["plan_transitions.id", "plan_transitions.workspace_id"],
            ondelete="RESTRICT",
            name="fk_plan_entity_lineages_transition_workspace",
        ),
        ForeignKeyConstraint(
            ["workspace_id"],
            ["workspaces.id"],
            ondelete="RESTRICT",
            name="fk_plan_entity_lineages_workspace",
        ),
        PrimaryKeyConstraint("id"),
        UniqueConstraint(
            "plan_transition_id",
            "target_type",
            "target_id",
            "source_type",
            "source_id",
            name="uq_plan_entity_lineages_identity",
        ),
        Index(
            "ix_plan_entity_lineages_workspace_target",
            "workspace_id",
            "target_type",
            "target_id",
        ),
    )

    id: Mapped[UUID] = mapped_column(PostgreSQLUUID(as_uuid=True), primary_key=True)
    plan_transition_id: Mapped[UUID] = mapped_column(
        PostgreSQLUUID(as_uuid=True), nullable=False
    )
    workspace_id: Mapped[UUID] = mapped_column(
        PostgreSQLUUID(as_uuid=True), nullable=False
    )
    target_type: Mapped[str] = mapped_column(String(64), nullable=False)
    target_id: Mapped[UUID] = mapped_column(
        PostgreSQLUUID(as_uuid=True), nullable=False
    )
    source_type: Mapped[str] = mapped_column(String(64), nullable=False)
    source_id: Mapped[UUID] = mapped_column(
        PostgreSQLUUID(as_uuid=True), nullable=False
    )
    reason: Mapped[str] = mapped_column(String(500), nullable=False)


class ContextPatchModel(TimestampMixin, Base):
    __tablename__ = "context_patches"
    __table_args__ = (
        CheckConstraint(
            "base_generation >= 1 AND target_generation = base_generation + 1",
            name="ck_context_patches_generation",
        ),
        CheckConstraint(
            "target_context_version >= 1",
            name="ck_context_patches_target_context_version",
        ),
        CheckConstraint("scope = 'worker_item'", name="ck_context_patches_scope"),
        CheckConstraint(
            "base_package_hash ~ '^[0-9a-f]{64}$' AND "
            "base_context_hash ~ '^[0-9a-f]{64}$' AND "
            "patch_hash ~ '^[0-9a-f]{64}$'",
            name="ck_context_patches_hashes",
        ),
        CheckConstraint(
            "jsonb_typeof(changed_requirements) = 'array' AND "
            "jsonb_typeof(changed_acceptance_refs) = 'array' AND "
            "jsonb_typeof(changed_dependency_refs) = 'array' AND "
            "jsonb_typeof(decision_refs) = 'array' AND "
            "jsonb_typeof(artifact_refs) = 'array' AND "
            "jsonb_typeof(evidence_refs) = 'array' AND "
            "jsonb_typeof(intervention_refs) = 'array'",
            name="ck_context_patches_refs_arrays",
        ),
        CheckConstraint("expires_at > created_at", name="ck_context_patches_expiry"),
        ForeignKeyConstraint(
            ["workspace_id"],
            ["workspaces.id"],
            ondelete="RESTRICT",
            name="fk_context_patches_workspace",
        ),
        ForeignKeyConstraint(
            ["root_task_id", "workspace_id"],
            ["tasks.id", "tasks.workspace_id"],
            ondelete="RESTRICT",
            name="fk_context_patches_root_task_workspace",
        ),
        ForeignKeyConstraint(
            ["worker_item_id", "workspace_id"],
            ["worker_items.id", "worker_items.workspace_id"],
            ondelete="RESTRICT",
            name="fk_context_patches_worker_item_workspace",
        ),
        ForeignKeyConstraint(
            ["plan_transition_id", "workspace_id"],
            ["plan_transitions.id", "plan_transitions.workspace_id"],
            ondelete="RESTRICT",
            name="fk_context_patches_plan_transition_workspace",
        ),
        ForeignKeyConstraint(
            ["worker_transition_id", "workspace_id"],
            ["worker_transitions.id", "worker_transitions.workspace_id"],
            ondelete="RESTRICT",
            name="fk_context_patches_worker_transition_workspace",
        ),
        ForeignKeyConstraint(
            ["base_plan_snapshot_id", "workspace_id"],
            ["task_plan_snapshots.id", "task_plan_snapshots.workspace_id"],
            ondelete="RESTRICT",
            name="fk_context_patches_base_snapshot_workspace",
        ),
        ForeignKeyConstraint(
            ["target_plan_snapshot_id", "workspace_id"],
            ["task_plan_snapshots.id", "task_plan_snapshots.workspace_id"],
            ondelete="RESTRICT",
            name="fk_context_patches_target_snapshot_workspace",
        ),
        ForeignKeyConstraint(
            ["base_context_package_id", "workspace_id"],
            ["context_packages.id", "context_packages.workspace_id"],
            ondelete="RESTRICT",
            name="fk_context_patches_base_package_workspace",
        ),
        ForeignKeyConstraint(
            ["base_work_context_revision_id", "workspace_id"],
            [
                "work_context_revisions.id",
                "work_context_revisions.workspace_id",
            ],
            ondelete="RESTRICT",
            name="fk_context_patches_base_context_revision_workspace",
        ),
        PrimaryKeyConstraint("id"),
        UniqueConstraint("id", "workspace_id", name="uq_context_patches_identity"),
        UniqueConstraint("patch_hash", name="uq_context_patches_patch_hash"),
        UniqueConstraint(
            "worker_transition_id",
            "target_context_version",
            name="uq_context_patches_transition_context_version",
        ),
        Index(
            "ix_context_patches_workspace_worker_item",
            "workspace_id",
            "worker_item_id",
            "created_at",
        ),
    )

    id: Mapped[UUID] = mapped_column(PostgreSQLUUID(as_uuid=True), primary_key=True)
    workspace_id: Mapped[UUID] = mapped_column(
        PostgreSQLUUID(as_uuid=True), nullable=False
    )
    root_task_id: Mapped[UUID] = mapped_column(
        PostgreSQLUUID(as_uuid=True), nullable=False
    )
    worker_item_id: Mapped[UUID] = mapped_column(
        PostgreSQLUUID(as_uuid=True), nullable=False
    )
    plan_transition_id: Mapped[UUID] = mapped_column(
        PostgreSQLUUID(as_uuid=True), nullable=False
    )
    worker_transition_id: Mapped[UUID] = mapped_column(
        PostgreSQLUUID(as_uuid=True), nullable=False
    )
    base_plan_snapshot_id: Mapped[UUID] = mapped_column(
        PostgreSQLUUID(as_uuid=True), nullable=False
    )
    base_generation: Mapped[int] = mapped_column(Integer, nullable=False)
    target_plan_snapshot_id: Mapped[UUID] = mapped_column(
        PostgreSQLUUID(as_uuid=True), nullable=False
    )
    target_generation: Mapped[int] = mapped_column(Integer, nullable=False)
    base_context_package_id: Mapped[UUID] = mapped_column(
        PostgreSQLUUID(as_uuid=True), nullable=False
    )
    base_package_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    base_work_context_revision_id: Mapped[UUID] = mapped_column(
        PostgreSQLUUID(as_uuid=True), nullable=False
    )
    base_context_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    target_context_version: Mapped[int] = mapped_column(Integer, nullable=False)
    scope: Mapped[str] = mapped_column(String(32), nullable=False)
    changed_requirements: Mapped[list[str]] = mapped_column(JSONB, nullable=False)
    changed_acceptance_refs: Mapped[list[str]] = mapped_column(JSONB, nullable=False)
    changed_dependency_refs: Mapped[list[str]] = mapped_column(JSONB, nullable=False)
    decision_refs: Mapped[list[str]] = mapped_column(JSONB, nullable=False)
    artifact_refs: Mapped[list[str]] = mapped_column(JSONB, nullable=False)
    evidence_refs: Mapped[list[str]] = mapped_column(JSONB, nullable=False)
    intervention_refs: Mapped[list[str]] = mapped_column(JSONB, nullable=False)
    unfinished_work: Mapped[str] = mapped_column(Text, nullable=False)
    checkpoint_ref: Mapped[str] = mapped_column(String(1000), nullable=False)
    patch_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    proposer: Mapped[str] = mapped_column(String(255), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )


class ContextPatchAcknowledgementModel(TimestampMixin, Base):
    __tablename__ = "context_patch_acknowledgements"
    __table_args__ = (
        CheckConstraint(
            "patch_hash ~ '^[0-9a-f]{64}$'",
            name="ck_context_patch_acknowledgements_hash",
        ),
        CheckConstraint(
            "accepted_sequence >= 0",
            name="ck_context_patch_acknowledgements_sequence",
        ),
        ForeignKeyConstraint(
            ["patch_id", "workspace_id"],
            ["context_patches.id", "context_patches.workspace_id"],
            ondelete="RESTRICT",
            name="fk_context_patch_acknowledgements_patch_workspace",
        ),
        ForeignKeyConstraint(
            ["execution_id", "workspace_id"],
            ["executions.id", "executions.workspace_id"],
            ondelete="RESTRICT",
            name="fk_context_patch_acknowledgements_execution_workspace",
        ),
        ForeignKeyConstraint(
            ["session_id", "workspace_id"],
            ["execution_sessions.id", "execution_sessions.workspace_id"],
            ondelete="RESTRICT",
            name="fk_context_patch_acknowledgements_session_workspace",
        ),
        PrimaryKeyConstraint("patch_id"),
        Index(
            "ix_context_patch_acknowledgements_workspace_execution",
            "workspace_id",
            "execution_id",
        ),
    )

    patch_id: Mapped[UUID] = mapped_column(
        PostgreSQLUUID(as_uuid=True), primary_key=True
    )
    workspace_id: Mapped[UUID] = mapped_column(
        PostgreSQLUUID(as_uuid=True), nullable=False
    )
    patch_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    execution_id: Mapped[UUID] = mapped_column(
        PostgreSQLUUID(as_uuid=True), nullable=False
    )
    session_id: Mapped[UUID] = mapped_column(
        PostgreSQLUUID(as_uuid=True), nullable=False
    )
    operation_id: Mapped[UUID] = mapped_column(
        PostgreSQLUUID(as_uuid=True), nullable=False
    )
    accepted_sequence: Mapped[int] = mapped_column(BigInteger, nullable=False)
    adapter_ref: Mapped[str] = mapped_column(String(500), nullable=False)
    acknowledged_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )


class WorkspaceProjectionCursorModel(Base):
    __tablename__ = "workspace_projection_cursors"

    workspace_id: Mapped[UUID] = mapped_column(
        PostgreSQLUUID(as_uuid=True),
        ForeignKey("workspaces.id", ondelete="CASCADE"),
        primary_key=True,
    )
    last_cursor: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)


class ProjectionChangeModel(Base):
    __tablename__ = "projection_changes"
    __table_args__ = (
        ForeignKeyConstraint(
            ["workspace_id"],
            ["workspaces.id"],
            ondelete="CASCADE",
            name="fk_projection_changes_workspace",
        ),
        CheckConstraint("cursor >= 1", name="ck_projection_changes_cursor"),
        CheckConstraint(
            "schema_version >= 1", name="ck_projection_changes_schema_version"
        ),
        Index(
            "ix_projection_changes_workspace_occurred",
            "workspace_id",
            "occurred_at",
        ),
        Index(
            "ix_projection_changes_collaboration_target_cursor",
            "workspace_id",
            "projection_type",
            "resource_id",
            "cursor",
        ),
    )

    workspace_id: Mapped[UUID] = mapped_column(
        PostgreSQLUUID(as_uuid=True), primary_key=True
    )
    cursor: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    schema_version: Mapped[int] = mapped_column(Integer, nullable=False)
    projection_type: Mapped[str] = mapped_column(String(100), nullable=False)
    resource_id: Mapped[UUID | None] = mapped_column(PostgreSQLUUID(as_uuid=True))
    change_type: Mapped[str] = mapped_column(String(50), nullable=False)
    resource_version: Mapped[int | None] = mapped_column(BigInteger)
    occurred_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    correlation_id: Mapped[UUID] = mapped_column(
        PostgreSQLUUID(as_uuid=True), nullable=False
    )


class AttentionAcknowledgementModel(Base):
    __tablename__ = "attention_acknowledgements"
    __table_args__ = (
        ForeignKeyConstraint(
            ["root_task_id", "workspace_id"],
            ["tasks.id", "tasks.workspace_id"],
            ondelete="RESTRICT",
            name="fk_attention_ack_root_task_workspace",
        ),
        CheckConstraint(
            "source_resource_version >= 1",
            name="ck_attention_ack_source_resource_version",
        ),
        Index(
            "ix_attention_ack_root_acknowledged",
            "workspace_id",
            "root_task_id",
            "acknowledged_at",
        ),
    )

    workspace_id: Mapped[UUID] = mapped_column(
        PostgreSQLUUID(as_uuid=True),
        ForeignKey("workspaces.id", ondelete="CASCADE"),
        primary_key=True,
    )
    actor_type: Mapped[str] = mapped_column(String(50), primary_key=True)
    actor_id: Mapped[str] = mapped_column(String(255), primary_key=True)
    attention_key: Mapped[str] = mapped_column(String(500), primary_key=True)
    root_task_id: Mapped[UUID] = mapped_column(
        PostgreSQLUUID(as_uuid=True), nullable=False
    )
    source_correlation_id: Mapped[UUID] = mapped_column(
        PostgreSQLUUID(as_uuid=True), nullable=False
    )
    source_resource_version: Mapped[int] = mapped_column(BigInteger, nullable=False)
    acknowledged_command_id: Mapped[UUID] = mapped_column(
        PostgreSQLUUID(as_uuid=True), nullable=False
    )
    acknowledged_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )


class TaskModel(TimestampMixin, Base):
    __tablename__ = "tasks"
    __table_args__ = (
        CheckConstraint("priority >= 0 AND priority <= 100", name="ck_tasks_priority"),
        CheckConstraint("version >= 1", name="ck_tasks_version"),
        Index("ix_tasks_workspace_status", "workspace_id", "status"),
        UniqueConstraint("id", "workspace_id", name="uq_tasks_id_workspace"),
    )

    id: Mapped[UUID] = mapped_column(PostgreSQLUUID(as_uuid=True), primary_key=True)
    workspace_id: Mapped[UUID] = mapped_column(
        PostgreSQLUUID(as_uuid=True),
        ForeignKey("workspaces.id", ondelete="RESTRICT"),
        nullable=False,
    )
    title: Mapped[str] = mapped_column(String(500), nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False)
    acceptance_criteria: Mapped[list[str]] = mapped_column(JSONB, nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    priority: Mapped[int] = mapped_column(Integer, nullable=False)
    requested_by: Mapped[str] = mapped_column(String(255), nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )


class WorkerModel(TimestampMixin, Base):
    __tablename__ = "workers"
    __table_args__ = (
        CheckConstraint("capacity >= 1", name="ck_workers_capacity"),
        CheckConstraint(
            "current_load >= 0 AND current_load <= capacity",
            name="ck_workers_current_load",
        ),
        CheckConstraint(
            "cost_per_hour_microunits >= 0",
            name="ck_workers_cost_per_hour_microunits",
        ),
        CheckConstraint(
            "startup_latency_ms >= 0", name="ck_workers_startup_latency_ms"
        ),
        CheckConstraint(
            "desired_state IN ('active', 'draining')",
            name="ck_workers_desired_state",
        ),
        CheckConstraint("version >= 1", name="ck_workers_version"),
        UniqueConstraint("id", "workspace_id", name="uq_workers_id_workspace"),
        Index(
            "ix_workers_scheduler_candidates",
            "workspace_id",
            "status",
            "last_heartbeat_at",
        ),
    )

    id: Mapped[UUID] = mapped_column(PostgreSQLUUID(as_uuid=True), primary_key=True)
    workspace_id: Mapped[UUID] = mapped_column(
        PostgreSQLUUID(as_uuid=True),
        ForeignKey("workspaces.id", ondelete="RESTRICT"),
        nullable=False,
    )
    adapter_type: Mapped[str] = mapped_column(String(100), nullable=False)
    display_name: Mapped[str] = mapped_column(String(255), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    desired_state: Mapped[str] = mapped_column(
        String(32), nullable=False, server_default="active"
    )
    capabilities: Mapped[list[str]] = mapped_column(JSONB, nullable=False)
    capacity: Mapped[int] = mapped_column(Integer, nullable=False)
    current_load: Mapped[int] = mapped_column(Integer, nullable=False)
    cost_per_hour_microunits: Mapped[int] = mapped_column(BigInteger, nullable=False)
    startup_latency_ms: Mapped[int] = mapped_column(Integer, nullable=False)
    last_heartbeat_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )


class WorkerRuntimeConfigurationModel(TimestampMixin, Base):
    __tablename__ = "worker_runtime_configurations"
    __table_args__ = (
        CheckConstraint(
            "adapter_module_config_version >= 1",
            name="ck_worker_runtime_configurations_adapter_config_version",
        ),
        CheckConstraint(
            "workspace_module_config_version >= 1",
            name="ck_worker_runtime_configurations_workspace_config_version",
        ),
        CheckConstraint(
            "version >= 1", name="ck_worker_runtime_configurations_version"
        ),
        ForeignKeyConstraint(
            ["worker_id", "workspace_id"],
            ["workers.id", "workers.workspace_id"],
            ondelete="RESTRICT",
            name="fk_worker_runtime_configurations_worker_workspace",
        ),
        ForeignKeyConstraint(
            ["adapter_module_instance_id", "workspace_id"],
            ["module_instances.id", "module_instances.workspace_id"],
            ondelete="RESTRICT",
            name="fk_worker_runtime_configurations_adapter_module_workspace",
        ),
        ForeignKeyConstraint(
            ["workspace_module_instance_id", "workspace_id"],
            ["module_instances.id", "module_instances.workspace_id"],
            ondelete="RESTRICT",
            name="fk_worker_runtime_configurations_workspace_module_workspace",
        ),
        ForeignKeyConstraint(
            ["workspace_id"],
            ["workspaces.id"],
            ondelete="RESTRICT",
            name="fk_worker_runtime_configurations_workspace",
        ),
    )

    worker_id: Mapped[UUID] = mapped_column(
        PostgreSQLUUID(as_uuid=True), primary_key=True
    )
    workspace_id: Mapped[UUID] = mapped_column(
        PostgreSQLUUID(as_uuid=True), nullable=False
    )
    adapter_module_instance_id: Mapped[UUID] = mapped_column(
        PostgreSQLUUID(as_uuid=True), nullable=False
    )
    adapter_module_config_version: Mapped[int] = mapped_column(Integer, nullable=False)
    workspace_module_instance_id: Mapped[UUID] = mapped_column(
        PostgreSQLUUID(as_uuid=True), nullable=False
    )
    workspace_module_config_version: Mapped[int] = mapped_column(
        Integer, nullable=False
    )
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )


class WorkerItemModel(TimestampMixin, Base):
    __tablename__ = "worker_items"
    __table_args__ = (
        CheckConstraint(
            "status IN ('active', 'paused', 'retired')",
            name="ck_worker_items_status",
        ),
        CheckConstraint("context_version >= 1", name="ck_worker_items_context_version"),
        CheckConstraint(
            "acknowledged_context_version >= 0 AND "
            "acknowledged_context_version <= context_version",
            name="ck_worker_items_ack_context_version",
        ),
        CheckConstraint("version >= 1", name="ck_worker_items_version"),
        CheckConstraint(
            "jsonb_typeof(derived_from_worker_item_ids) = 'array'",
            name="ck_worker_items_derived_ids_array",
        ),
        ForeignKeyConstraint(
            ["root_task_id", "workspace_id"],
            ["tasks.id", "tasks.workspace_id"],
            ondelete="RESTRICT",
            name="fk_worker_items_root_task_workspace",
        ),
        UniqueConstraint("id", "workspace_id", name="uq_worker_items_id_workspace"),
        UniqueConstraint(
            "root_task_id", "stable_key", name="uq_worker_items_root_stable_key"
        ),
        Index(
            "ix_worker_items_workspace_root_status",
            "workspace_id",
            "root_task_id",
            "status",
        ),
    )

    id: Mapped[UUID] = mapped_column(PostgreSQLUUID(as_uuid=True), primary_key=True)
    workspace_id: Mapped[UUID] = mapped_column(
        PostgreSQLUUID(as_uuid=True),
        ForeignKey("workspaces.id", ondelete="RESTRICT"),
        nullable=False,
    )
    root_task_id: Mapped[UUID] = mapped_column(
        PostgreSQLUUID(as_uuid=True), nullable=False
    )
    stable_key: Mapped[str] = mapped_column(String(500), nullable=False)
    title: Mapped[str] = mapped_column(String(500), nullable=False)
    responsibility: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    context_version: Mapped[int] = mapped_column(Integer, nullable=False)
    acknowledged_context_version: Mapped[int] = mapped_column(Integer, nullable=False)
    derived_from_worker_item_ids: Mapped[list[str]] = mapped_column(
        JSONB, nullable=False, server_default=text("'[]'::jsonb")
    )
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )


class WorkerItemPlacementModel(TimestampMixin, Base):
    __tablename__ = "worker_item_placements"
    __table_args__ = (
        CheckConstraint(
            "plan_generation >= 1", name="ck_worker_item_placements_generation"
        ),
        ForeignKeyConstraint(
            ["plan_snapshot_id"],
            ["task_plan_snapshots.id"],
            ondelete="RESTRICT",
            name="fk_worker_item_placements_snapshot",
        ),
        ForeignKeyConstraint(
            ["workspace_id"],
            ["workspaces.id"],
            ondelete="RESTRICT",
            name="fk_worker_item_placements_workspace",
        ),
        ForeignKeyConstraint(
            ["root_task_id", "workspace_id"],
            ["tasks.id", "tasks.workspace_id"],
            ondelete="RESTRICT",
            name="fk_worker_item_placements_root_task",
        ),
        ForeignKeyConstraint(
            ["worker_item_id", "workspace_id"],
            ["worker_items.id", "worker_items.workspace_id"],
            ondelete="RESTRICT",
            name="fk_worker_item_placements_worker_item_workspace",
        ),
        ForeignKeyConstraint(
            ["subtask_task_id", "workspace_id"],
            ["tasks.id", "tasks.workspace_id"],
            ondelete="RESTRICT",
            name="fk_worker_item_placements_subtask_task_workspace",
        ),
        PrimaryKeyConstraint("plan_snapshot_id", "worker_item_id"),
        UniqueConstraint(
            "plan_snapshot_id",
            "worker_item_key",
            name="uq_worker_item_placements_snapshot_key",
        ),
        Index(
            "ix_worker_item_placements_workspace_root_generation",
            "workspace_id",
            "root_task_id",
            "plan_generation",
        ),
        Index(
            "ix_worker_item_placements_workspace_worker_item_generation",
            "workspace_id",
            "worker_item_id",
            "plan_generation",
        ),
    )

    plan_snapshot_id: Mapped[UUID] = mapped_column(
        PostgreSQLUUID(as_uuid=True), nullable=False
    )
    plan_generation: Mapped[int] = mapped_column(Integer, nullable=False)
    workspace_id: Mapped[UUID] = mapped_column(
        PostgreSQLUUID(as_uuid=True), nullable=False
    )
    root_task_id: Mapped[UUID] = mapped_column(
        PostgreSQLUUID(as_uuid=True), nullable=False
    )
    worker_item_id: Mapped[UUID] = mapped_column(
        PostgreSQLUUID(as_uuid=True), nullable=False
    )
    worker_item_key: Mapped[str] = mapped_column(String(500), nullable=False)
    subtask_key: Mapped[str] = mapped_column(String(500), nullable=False)
    subtask_task_id: Mapped[UUID | None] = mapped_column(PostgreSQLUUID(as_uuid=True))
    role: Mapped[str] = mapped_column(String(255), nullable=False)
    capability_requirements: Mapped[list[str]] = mapped_column(JSONB, nullable=False)
    worker_policy_ref: Mapped[str | None] = mapped_column(String(500))
    configuration_ref: Mapped[str | None] = mapped_column(String(500))


class ExecutionModel(TimestampMixin, Base):
    __tablename__ = "executions"
    __table_args__ = (
        CheckConstraint("attempt >= 1", name="ck_executions_attempt"),
        CheckConstraint("max_attempts >= attempt", name="ck_executions_max_attempts"),
        CheckConstraint(
            "last_fencing_token >= 0", name="ck_executions_last_fencing_token"
        ),
        CheckConstraint("version >= 1", name="ck_executions_version"),
        CheckConstraint(
            "jsonb_typeof(adapter_preferences) = 'array'",
            name="ck_executions_adapter_preferences_array",
        ),
        CheckConstraint(
            "jsonb_typeof(adapter_forbidden) = 'array'",
            name="ck_executions_adapter_forbidden_array",
        ),
        ForeignKeyConstraint(
            ["task_id", "workspace_id"],
            ["tasks.id", "tasks.workspace_id"],
            ondelete="RESTRICT",
            name="fk_executions_task_workspace",
        ),
        ForeignKeyConstraint(
            ["lineage_id", "workspace_id"],
            ["executions.id", "executions.workspace_id"],
            ondelete="RESTRICT",
            name="fk_executions_lineage_workspace",
        ),
        ForeignKeyConstraint(
            ["pinned_worker_id", "workspace_id"],
            ["workers.id", "workers.workspace_id"],
            ondelete="RESTRICT",
            name="fk_executions_pinned_worker_workspace",
        ),
        ForeignKeyConstraint(
            ["assigned_worker_id", "workspace_id"],
            ["workers.id", "workers.workspace_id"],
            ondelete="RESTRICT",
            name="fk_executions_assigned_worker_workspace",
        ),
        ForeignKeyConstraint(
            ["worker_item_id", "workspace_id"],
            ["worker_items.id", "worker_items.workspace_id"],
            ondelete="RESTRICT",
            name="fk_executions_worker_item_workspace",
        ),
        ForeignKeyConstraint(
            ["active_lease_id", "id", "workspace_id"],
            [
                "assignment_leases.id",
                "assignment_leases.execution_id",
                "assignment_leases.workspace_id",
            ],
            ondelete="RESTRICT",
            deferrable=True,
            initially="DEFERRED",
            name="fk_executions_active_lease",
        ),
        ForeignKeyConstraint(
            ["handoff_transition_id", "workspace_id"],
            ["handoff_transitions.id", "handoff_transitions.workspace_id"],
            ondelete="RESTRICT",
            deferrable=True,
            initially="DEFERRED",
            name="fk_executions_handoff_transition_workspace",
        ),
        UniqueConstraint("id", "workspace_id", name="uq_executions_id_workspace"),
        UniqueConstraint("lineage_id", "attempt", name="uq_executions_lineage_attempt"),
        Index(
            "ix_executions_schedulable",
            "status",
            "not_before",
            "created_at",
        ),
    )

    id: Mapped[UUID] = mapped_column(PostgreSQLUUID(as_uuid=True), primary_key=True)
    workspace_id: Mapped[UUID] = mapped_column(
        PostgreSQLUUID(as_uuid=True),
        ForeignKey("workspaces.id", ondelete="RESTRICT"),
        nullable=False,
    )
    task_id: Mapped[UUID] = mapped_column(PostgreSQLUUID(as_uuid=True), nullable=False)
    lineage_id: Mapped[UUID] = mapped_column(
        PostgreSQLUUID(as_uuid=True), nullable=False
    )
    attempt: Mapped[int] = mapped_column(Integer, nullable=False)
    max_attempts: Mapped[int] = mapped_column(Integer, nullable=False)
    purpose: Mapped[str] = mapped_column(String(255), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    capability_requirements: Mapped[list[str]] = mapped_column(JSONB, nullable=False)
    adapter_preferences: Mapped[list[str]] = mapped_column(
        JSONB, nullable=False, server_default=text("'[]'::jsonb")
    )
    adapter_forbidden: Mapped[list[str]] = mapped_column(
        JSONB, nullable=False, server_default=text("'[]'::jsonb")
    )
    pinned_worker_id: Mapped[UUID | None] = mapped_column(PostgreSQLUUID(as_uuid=True))
    assigned_worker_id: Mapped[UUID | None] = mapped_column(
        PostgreSQLUUID(as_uuid=True)
    )
    active_lease_id: Mapped[UUID | None] = mapped_column(PostgreSQLUUID(as_uuid=True))
    worker_item_id: Mapped[UUID | None] = mapped_column(PostgreSQLUUID(as_uuid=True))
    last_fencing_token: Mapped[int] = mapped_column(BigInteger, nullable=False)
    not_before: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    failure_code: Mapped[str | None] = mapped_column(String(100))
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    stop_requested: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("false")
    )
    source_execution_id: Mapped[UUID | None] = mapped_column(
        PostgreSQLUUID(as_uuid=True), ForeignKey("executions.id", ondelete="RESTRICT")
    )
    source_session_id: Mapped[UUID | None] = mapped_column(
        PostgreSQLUUID(as_uuid=True),
        ForeignKey("execution_sessions.id", ondelete="RESTRICT"),
    )
    handoff_transition_id: Mapped[UUID | None] = mapped_column(
        PostgreSQLUUID(as_uuid=True)
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )


class SchedulingBlockModel(TimestampMixin, Base):
    __tablename__ = "scheduling_blocks"
    __table_args__ = (
        CheckConstraint(
            "status IN ('active', 'resolved')",
            name="ck_scheduling_blocks_status",
        ),
        CheckConstraint(
            "jsonb_typeof(reason_details) = 'object'",
            name="ck_scheduling_blocks_reason_details_object",
        ),
        CheckConstraint(
            "occurrence_count >= 1",
            name="ck_scheduling_blocks_occurrence_count",
        ),
        CheckConstraint("version >= 1", name="ck_scheduling_blocks_version"),
        ForeignKeyConstraint(
            ["execution_id", "workspace_id"],
            ["executions.id", "executions.workspace_id"],
            ondelete="RESTRICT",
            name="fk_scheduling_blocks_execution_workspace",
        ),
        ForeignKeyConstraint(
            ["workspace_id"],
            ["workspaces.id"],
            ondelete="RESTRICT",
            name="fk_scheduling_blocks_workspace",
        ),
        UniqueConstraint("id", "workspace_id", name="uq_scheduling_blocks_identity"),
        Index(
            "uq_scheduling_blocks_active_execution",
            "execution_id",
            "workspace_id",
            unique=True,
            postgresql_where=text("status = 'active'"),
        ),
        Index(
            "ix_scheduling_blocks_workspace_status",
            "workspace_id",
            "status",
        ),
        Index(
            "ix_scheduling_blocks_workspace_execution",
            "workspace_id",
            "execution_id",
        ),
        Index(
            "ix_scheduling_blocks_workspace_last_seen",
            "workspace_id",
            "last_seen_at",
        ),
    )

    id: Mapped[UUID] = mapped_column(PostgreSQLUUID(as_uuid=True), primary_key=True)
    workspace_id: Mapped[UUID] = mapped_column(
        PostgreSQLUUID(as_uuid=True), nullable=False
    )
    execution_id: Mapped[UUID] = mapped_column(
        PostgreSQLUUID(as_uuid=True), nullable=False
    )
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    reason_code: Mapped[str] = mapped_column(String(100), nullable=False)
    reason_details: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    requires_operator: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("false")
    )
    first_seen_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    last_seen_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    occurrence_count: Mapped[int] = mapped_column(Integer, nullable=False)
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )


class AssignmentLeaseModel(TimestampMixin, Base):
    __tablename__ = "assignment_leases"
    __table_args__ = (
        CheckConstraint(
            "fencing_token >= 1", name="ck_assignment_leases_fencing_token"
        ),
        CheckConstraint("version >= 1", name="ck_assignment_leases_version"),
        ForeignKeyConstraint(
            ["execution_id", "workspace_id"],
            ["executions.id", "executions.workspace_id"],
            ondelete="RESTRICT",
            name="fk_assignment_leases_execution_workspace",
        ),
        ForeignKeyConstraint(
            ["worker_id", "workspace_id"],
            ["workers.id", "workers.workspace_id"],
            ondelete="RESTRICT",
            name="fk_assignment_leases_worker_workspace",
        ),
        UniqueConstraint(
            "execution_id", "fencing_token", name="uq_assignment_leases_execution_token"
        ),
        UniqueConstraint(
            "id", "execution_id", "workspace_id", name="uq_assignment_leases_identity"
        ),
        Index(
            "uq_assignment_leases_active_execution",
            "execution_id",
            unique=True,
            postgresql_where=text("status = 'active'"),
        ),
        Index("ix_assignment_leases_expiry", "status", "expires_at"),
    )

    id: Mapped[UUID] = mapped_column(PostgreSQLUUID(as_uuid=True), primary_key=True)
    workspace_id: Mapped[UUID] = mapped_column(
        PostgreSQLUUID(as_uuid=True),
        ForeignKey("workspaces.id", ondelete="RESTRICT"),
        nullable=False,
    )
    execution_id: Mapped[UUID] = mapped_column(
        PostgreSQLUUID(as_uuid=True), nullable=False
    )
    worker_id: Mapped[UUID] = mapped_column(
        PostgreSQLUUID(as_uuid=True), nullable=False
    )
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    issued_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    renewed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    fencing_token: Mapped[int] = mapped_column(BigInteger, nullable=False)
    assignment_reason: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )


class ExecutionSessionModel(TimestampMixin, Base):
    __tablename__ = "execution_sessions"
    __table_args__ = (
        CheckConstraint(
            "last_sequence >= 0", name="ck_execution_sessions_last_sequence"
        ),
        CheckConstraint("version >= 1", name="ck_execution_sessions_version"),
        ForeignKeyConstraint(
            ["execution_id", "workspace_id"],
            ["executions.id", "executions.workspace_id"],
            ondelete="RESTRICT",
            name="fk_execution_sessions_execution_workspace",
        ),
        ForeignKeyConstraint(
            ["worker_id", "workspace_id"],
            ["workers.id", "workers.workspace_id"],
            ondelete="RESTRICT",
            name="fk_execution_sessions_worker_workspace",
        ),
        UniqueConstraint(
            "worker_id", "adapter_session_id", name="uq_execution_sessions_adapter"
        ),
        UniqueConstraint("id", "workspace_id", name="uq_execution_sessions_identity"),
        Index(
            "uq_execution_sessions_active_execution",
            "execution_id",
            unique=True,
            postgresql_where=text("status = 'active'"),
        ),
    )

    id: Mapped[UUID] = mapped_column(PostgreSQLUUID(as_uuid=True), primary_key=True)
    workspace_id: Mapped[UUID] = mapped_column(
        PostgreSQLUUID(as_uuid=True),
        ForeignKey("workspaces.id", ondelete="RESTRICT"),
        nullable=False,
    )
    execution_id: Mapped[UUID] = mapped_column(
        PostgreSQLUUID(as_uuid=True), nullable=False
    )
    worker_id: Mapped[UUID] = mapped_column(
        PostgreSQLUUID(as_uuid=True), nullable=False
    )
    adapter_session_id: Mapped[str] = mapped_column(String(500), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    last_sequence: Mapped[int] = mapped_column(BigInteger, nullable=False)
    last_activity_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    input_mode: Mapped[str] = mapped_column(String(32), nullable=False)
    adapter_module_id: Mapped[UUID | None] = mapped_column(PostgreSQLUUID(as_uuid=True))
    adapter_config_version: Mapped[int | None] = mapped_column(Integer)
    workspace_module_id: Mapped[UUID | None] = mapped_column(
        PostgreSQLUUID(as_uuid=True)
    )
    workspace_config_version: Mapped[int | None] = mapped_column(Integer)
    capability_snapshot: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    process_ref: Mapped[str | None] = mapped_column(String(500))
    workspace_ref: Mapped[str | None] = mapped_column(String(500))
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )


class CommandModel(TimestampMixin, Base):
    __tablename__ = "commands"
    __table_args__ = (
        UniqueConstraint(
            "workspace_id",
            "actor_type",
            "actor_id",
            "idempotency_key",
            name="uq_command_idempotency",
        ),
        Index("ix_commands_target", "workspace_id", "target_type", "target_id"),
    )

    id: Mapped[UUID] = mapped_column(PostgreSQLUUID(as_uuid=True), primary_key=True)
    workspace_id: Mapped[UUID] = mapped_column(
        PostgreSQLUUID(as_uuid=True),
        ForeignKey("workspaces.id", ondelete="RESTRICT"),
        nullable=False,
    )
    command_type: Mapped[str] = mapped_column(String(100), nullable=False)
    actor_type: Mapped[str] = mapped_column(String(50), nullable=False)
    actor_id: Mapped[str] = mapped_column(String(255), nullable=False)
    target_type: Mapped[str] = mapped_column(String(50), nullable=False)
    target_id: Mapped[UUID | None] = mapped_column(PostgreSQLUUID(as_uuid=True))
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    expected_version: Mapped[int | None] = mapped_column(Integer)
    idempotency_key: Mapped[str] = mapped_column(String(255), nullable=False)
    request_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    correlation_id: Mapped[UUID] = mapped_column(
        PostgreSQLUUID(as_uuid=True), nullable=False
    )
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    result: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    rejection_reason: Mapped[str | None] = mapped_column(String(100))


class EventModel(TimestampMixin, Base):
    __tablename__ = "events"
    __table_args__ = (
        UniqueConstraint(
            "aggregate_type",
            "aggregate_id",
            "aggregate_version",
            name="uq_event_aggregate_version",
        ),
        Index("ix_events_workspace_created", "workspace_id", "created_at"),
    )

    id: Mapped[UUID] = mapped_column(PostgreSQLUUID(as_uuid=True), primary_key=True)
    workspace_id: Mapped[UUID] = mapped_column(
        PostgreSQLUUID(as_uuid=True),
        ForeignKey("workspaces.id", ondelete="RESTRICT"),
        nullable=False,
    )
    aggregate_type: Mapped[str] = mapped_column(String(50), nullable=False)
    aggregate_id: Mapped[UUID] = mapped_column(
        PostgreSQLUUID(as_uuid=True), nullable=False
    )
    aggregate_version: Mapped[int] = mapped_column(Integer, nullable=False)
    event_type: Mapped[str] = mapped_column(String(100), nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    actor_type: Mapped[str] = mapped_column(String(50), nullable=False)
    actor_id: Mapped[str] = mapped_column(String(255), nullable=False)
    causation_id: Mapped[UUID] = mapped_column(
        PostgreSQLUUID(as_uuid=True),
        ForeignKey("commands.id", ondelete="RESTRICT"),
        nullable=False,
    )
    correlation_id: Mapped[UUID] = mapped_column(
        PostgreSQLUUID(as_uuid=True), nullable=False
    )


class OutboxMessageModel(TimestampMixin, Base):
    __tablename__ = "outbox_messages"
    __table_args__ = (
        UniqueConstraint("event_id", name="uq_outbox_event_id"),
        Index(
            "ix_outbox_dispatch",
            "next_attempt_at",
            "claimed_until",
            "created_at",
        ),
    )

    id: Mapped[UUID] = mapped_column(PostgreSQLUUID(as_uuid=True), primary_key=True)
    event_id: Mapped[UUID] = mapped_column(
        PostgreSQLUUID(as_uuid=True),
        ForeignKey("events.id", ondelete="RESTRICT"),
        nullable=False,
    )
    topic: Mapped[str] = mapped_column(String(255), nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    attempts: Mapped[int] = mapped_column(Integer, server_default="0", nullable=False)
    next_attempt_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    claimed_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    claim_token: Mapped[UUID | None] = mapped_column(PostgreSQLUUID(as_uuid=True))
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_error: Mapped[str | None] = mapped_column(Text)


class ModuleDefinitionModel(TimestampMixin, Base):
    __tablename__ = "module_definitions"
    __table_args__ = (
        CheckConstraint(
            "config_schema_version >= 1", name="ck_module_definitions_schema_version"
        ),
        CheckConstraint(
            "resource_version >= 1", name="ck_module_definitions_resource_version"
        ),
        CheckConstraint(
            "lifecycle IN ('active', 'draining', 'disabled')",
            name="ck_module_definitions_lifecycle",
        ),
        UniqueConstraint("id", "workspace_id", name="uq_module_definitions_identity"),
        Index("ix_module_definitions_workspace_type", "workspace_id", "module_type"),
    )

    id: Mapped[UUID] = mapped_column(PostgreSQLUUID(as_uuid=True), primary_key=True)
    workspace_id: Mapped[UUID] = mapped_column(
        PostgreSQLUUID(as_uuid=True),
        ForeignKey("workspaces.id", ondelete="RESTRICT"),
        nullable=False,
    )
    module_type: Mapped[str] = mapped_column(String(100), nullable=False)
    provider: Mapped[str] = mapped_column(String(100), nullable=False)
    interface_version: Mapped[str] = mapped_column(String(100), nullable=False)
    config_schema_version: Mapped[int] = mapped_column(Integer, nullable=False)
    capabilities: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    lifecycle: Mapped[str] = mapped_column(String(32), nullable=False)
    resource_version: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("1")
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )


class ModuleInstanceModel(TimestampMixin, Base):
    __tablename__ = "module_instances"
    __table_args__ = (
        CheckConstraint(
            "config_version >= 1", name="ck_module_instances_config_version"
        ),
        CheckConstraint(
            "resource_version >= 1", name="ck_module_instances_resource_version"
        ),
        CheckConstraint(
            "lifecycle IN ('active', 'draining', 'disabled')",
            name="ck_module_instances_lifecycle",
        ),
        ForeignKeyConstraint(
            ["definition_id", "workspace_id"],
            ["module_definitions.id", "module_definitions.workspace_id"],
            ondelete="RESTRICT",
            name="fk_module_instances_definition_workspace",
        ),
        UniqueConstraint("id", "workspace_id", name="uq_module_instances_identity"),
        UniqueConstraint(
            "workspace_id",
            "instance_name",
            "config_version",
            name="uq_module_instances_name_version",
        ),
    )

    id: Mapped[UUID] = mapped_column(PostgreSQLUUID(as_uuid=True), primary_key=True)
    workspace_id: Mapped[UUID] = mapped_column(
        PostgreSQLUUID(as_uuid=True),
        ForeignKey("workspaces.id", ondelete="RESTRICT"),
        nullable=False,
    )
    definition_id: Mapped[UUID] = mapped_column(
        PostgreSQLUUID(as_uuid=True), nullable=False
    )
    instance_name: Mapped[str] = mapped_column(String(255), nullable=False)
    config_version: Mapped[int] = mapped_column(Integer, nullable=False)
    config_namespace: Mapped[str] = mapped_column(String(100), nullable=False)
    config: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    secret_refs: Mapped[list[str]] = mapped_column(JSONB, nullable=False)
    lifecycle: Mapped[str] = mapped_column(String(32), nullable=False)
    resource_version: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("1")
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )


class ExecutionBindingModel(TimestampMixin, Base):
    __tablename__ = "execution_bindings"
    __table_args__ = (
        CheckConstraint(
            "adapter_config_version >= 1", name="ck_bindings_adapter_config"
        ),
        CheckConstraint(
            "workspace_config_version >= 1", name="ck_bindings_workspace_config"
        ),
        CheckConstraint("fencing_token >= 1", name="ck_bindings_fencing_token"),
        CheckConstraint("version >= 1", name="ck_bindings_version"),
        ForeignKeyConstraint(
            ["execution_id", "workspace_id"],
            ["executions.id", "executions.workspace_id"],
            ondelete="RESTRICT",
            name="fk_bindings_execution_workspace",
        ),
        ForeignKeyConstraint(
            ["worker_id", "workspace_id"],
            ["workers.id", "workers.workspace_id"],
            ondelete="RESTRICT",
            name="fk_bindings_worker_workspace",
        ),
        ForeignKeyConstraint(
            ["adapter_module_id", "workspace_id"],
            ["module_instances.id", "module_instances.workspace_id"],
            ondelete="RESTRICT",
            name="fk_bindings_adapter_module_workspace",
        ),
        ForeignKeyConstraint(
            ["workspace_module_id", "workspace_id"],
            ["module_instances.id", "module_instances.workspace_id"],
            ondelete="RESTRICT",
            name="fk_bindings_workspace_module_workspace",
        ),
        ForeignKeyConstraint(
            ["lease_id", "execution_id", "workspace_id"],
            [
                "assignment_leases.id",
                "assignment_leases.execution_id",
                "assignment_leases.workspace_id",
            ],
            ondelete="RESTRICT",
            name="fk_bindings_lease_execution_workspace",
        ),
        UniqueConstraint("id", "workspace_id", name="uq_bindings_identity"),
        UniqueConstraint("execution_id", "workspace_id", name="uq_bindings_execution"),
    )

    id: Mapped[UUID] = mapped_column(PostgreSQLUUID(as_uuid=True), primary_key=True)
    workspace_id: Mapped[UUID] = mapped_column(
        PostgreSQLUUID(as_uuid=True),
        ForeignKey("workspaces.id", ondelete="RESTRICT"),
        nullable=False,
    )
    execution_id: Mapped[UUID] = mapped_column(
        PostgreSQLUUID(as_uuid=True), nullable=False
    )
    worker_id: Mapped[UUID] = mapped_column(
        PostgreSQLUUID(as_uuid=True), nullable=False
    )
    adapter_module_id: Mapped[UUID] = mapped_column(
        PostgreSQLUUID(as_uuid=True), nullable=False
    )
    workspace_module_id: Mapped[UUID] = mapped_column(
        PostgreSQLUUID(as_uuid=True), nullable=False
    )
    adapter_config_version: Mapped[int] = mapped_column(Integer, nullable=False)
    workspace_config_version: Mapped[int] = mapped_column(Integer, nullable=False)
    capability_snapshot: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    lease_id: Mapped[UUID] = mapped_column(PostgreSQLUUID(as_uuid=True), nullable=False)
    fencing_token: Mapped[int] = mapped_column(BigInteger, nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )


class ExecutionWorkspaceModel(TimestampMixin, Base):
    __tablename__ = "execution_workspaces"
    __table_args__ = (
        CheckConstraint(
            "lifecycle IN ('requested', 'preparing', 'ready', 'active', 'frozen', "
            "'retained', 'cleaning', 'released', 'quarantined')",
            name="ck_execution_workspaces_lifecycle",
        ),
        CheckConstraint(
            "retention_seconds > 0", name="ck_execution_workspaces_retention"
        ),
        CheckConstraint("fencing_token >= 1", name="ck_execution_workspaces_fencing"),
        CheckConstraint("version >= 1", name="ck_execution_workspaces_version"),
        ForeignKeyConstraint(
            ["task_id", "workspace_id"],
            ["tasks.id", "tasks.workspace_id"],
            ondelete="RESTRICT",
            name="fk_execution_workspaces_task_workspace",
        ),
        ForeignKeyConstraint(
            ["execution_id", "workspace_id"],
            ["executions.id", "executions.workspace_id"],
            ondelete="RESTRICT",
            name="fk_execution_workspaces_execution_workspace",
        ),
        UniqueConstraint("id", "workspace_id", name="uq_execution_workspaces_identity"),
        Index(
            "uq_execution_workspaces_active_execution",
            "execution_id",
            unique=True,
            postgresql_where=text(
                "lifecycle IN ('requested', 'preparing', 'ready', 'active')"
            ),
        ),
        Index(
            "uq_execution_workspaces_active_location",
            "workspace_id",
            "location_ref",
            unique=True,
            postgresql_where=text("lifecycle = 'active'"),
        ),
    )

    id: Mapped[UUID] = mapped_column(PostgreSQLUUID(as_uuid=True), primary_key=True)
    workspace_id: Mapped[UUID] = mapped_column(
        PostgreSQLUUID(as_uuid=True),
        ForeignKey("workspaces.id", ondelete="RESTRICT"),
        nullable=False,
    )
    task_id: Mapped[UUID] = mapped_column(PostgreSQLUUID(as_uuid=True), nullable=False)
    execution_id: Mapped[UUID] = mapped_column(
        PostgreSQLUUID(as_uuid=True), nullable=False
    )
    checkouts: Mapped[list[dict[str, Any]]] = mapped_column(JSONB, nullable=False)
    location_ref: Mapped[str] = mapped_column(String(500), nullable=False)
    lifecycle: Mapped[str] = mapped_column(String(32), nullable=False)
    retention_seconds: Mapped[int] = mapped_column(Integer, nullable=False)
    retain_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    fencing_token: Mapped[int] = mapped_column(BigInteger, nullable=False)
    frozen_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    released_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )


class ProcessDescriptorModel(TimestampMixin, Base):
    __tablename__ = "process_descriptors"
    __table_args__ = (
        CheckConstraint("pid >= 1", name="ck_process_descriptors_pid"),
        CheckConstraint("process_group_id >= 1", name="ck_process_descriptors_group"),
        ForeignKeyConstraint(
            ["execution_id", "workspace_id"],
            ["executions.id", "executions.workspace_id"],
            ondelete="RESTRICT",
            name="fk_process_descriptors_execution_workspace",
        ),
        ForeignKeyConstraint(
            ["session_id", "workspace_id"],
            ["execution_sessions.id", "execution_sessions.workspace_id"],
            ondelete="RESTRICT",
            name="fk_process_descriptors_session_workspace",
        ),
        UniqueConstraint("workspace_id", "nonce", name="uq_process_descriptors_nonce"),
    )

    id: Mapped[UUID] = mapped_column(PostgreSQLUUID(as_uuid=True), primary_key=True)
    workspace_id: Mapped[UUID] = mapped_column(
        PostgreSQLUUID(as_uuid=True),
        ForeignKey("workspaces.id", ondelete="RESTRICT"),
        nullable=False,
    )
    execution_id: Mapped[UUID] = mapped_column(
        PostgreSQLUUID(as_uuid=True), nullable=False
    )
    session_id: Mapped[UUID | None] = mapped_column(PostgreSQLUUID(as_uuid=True))
    pid: Mapped[int] = mapped_column(Integer, nullable=False)
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    nonce: Mapped[str] = mapped_column(String(255), nullable=False)
    process_group_id: Mapped[int] = mapped_column(Integer, nullable=False)


class GatewayOperationModel(TimestampMixin, Base):
    __tablename__ = "gateway_operations"
    __table_args__ = (
        CheckConstraint(
            "status IN ('requested', 'running', 'succeeded', 'failed', 'unknown')",
            name="ck_gateway_operations_status",
        ),
        CheckConstraint("attempt_count >= 0", name="ck_gateway_operations_attempts"),
        CheckConstraint("fencing_token >= 1", name="ck_gateway_operations_fencing"),
        CheckConstraint("version >= 1", name="ck_gateway_operations_version"),
        ForeignKeyConstraint(
            ["task_id", "workspace_id"],
            ["tasks.id", "tasks.workspace_id"],
            ondelete="RESTRICT",
            name="fk_gateway_operations_task_workspace",
        ),
        ForeignKeyConstraint(
            ["execution_id", "workspace_id"],
            ["executions.id", "executions.workspace_id"],
            ondelete="RESTRICT",
            name="fk_gateway_operations_execution_workspace",
        ),
        ForeignKeyConstraint(
            ["session_id", "workspace_id"],
            ["execution_sessions.id", "execution_sessions.workspace_id"],
            ondelete="RESTRICT",
            name="fk_gateway_operations_session_workspace",
        ),
        ForeignKeyConstraint(
            ["module_instance_id", "workspace_id"],
            ["module_instances.id", "module_instances.workspace_id"],
            ondelete="RESTRICT",
            name="fk_gateway_operations_module_workspace",
        ),
        ForeignKeyConstraint(
            ["lease_id", "execution_id", "workspace_id"],
            [
                "assignment_leases.id",
                "assignment_leases.execution_id",
                "assignment_leases.workspace_id",
            ],
            ondelete="RESTRICT",
            name="fk_gateway_operations_lease_execution_workspace",
        ),
        UniqueConstraint(
            "workspace_id",
            "operation_type",
            "operation_id",
            name="uq_gateway_operations_idempotency",
        ),
        Index("ix_gateway_operations_recovery", "status", "next_attempt_at"),
    )

    id: Mapped[UUID] = mapped_column(PostgreSQLUUID(as_uuid=True), primary_key=True)
    workspace_id: Mapped[UUID] = mapped_column(
        PostgreSQLUUID(as_uuid=True),
        ForeignKey("workspaces.id", ondelete="RESTRICT"),
        nullable=False,
    )
    operation_id: Mapped[UUID] = mapped_column(
        PostgreSQLUUID(as_uuid=True), nullable=False
    )
    task_id: Mapped[UUID] = mapped_column(PostgreSQLUUID(as_uuid=True), nullable=False)
    execution_id: Mapped[UUID] = mapped_column(
        PostgreSQLUUID(as_uuid=True), nullable=False
    )
    session_id: Mapped[UUID | None] = mapped_column(PostgreSQLUUID(as_uuid=True))
    operation_type: Mapped[str] = mapped_column(String(64), nullable=False)
    module_instance_id: Mapped[UUID | None] = mapped_column(
        PostgreSQLUUID(as_uuid=True)
    )
    config_version: Mapped[int | None] = mapped_column(Integer)
    lease_id: Mapped[UUID] = mapped_column(PostgreSQLUUID(as_uuid=True), nullable=False)
    fencing_token: Mapped[int] = mapped_column(BigInteger, nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    attempt_count: Mapped[int] = mapped_column(Integer, nullable=False)
    next_attempt_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    result_ref: Mapped[str | None] = mapped_column(String(500))
    failure_code: Mapped[str | None] = mapped_column(String(100))
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )


class ExecutionEventModel(TimestampMixin, Base):
    __tablename__ = "execution_events"
    __table_args__ = (
        CheckConstraint("sequence >= 1", name="ck_execution_events_sequence"),
        ForeignKeyConstraint(
            ["execution_id", "workspace_id"],
            ["executions.id", "executions.workspace_id"],
            ondelete="RESTRICT",
            name="fk_execution_events_execution_workspace",
        ),
        ForeignKeyConstraint(
            ["session_id", "workspace_id"],
            ["execution_sessions.id", "execution_sessions.workspace_id"],
            ondelete="RESTRICT",
            name="fk_execution_events_session_workspace",
        ),
        UniqueConstraint(
            "session_id", "sequence", name="uq_execution_events_session_sequence"
        ),
        Index("ix_execution_events_execution_sequence", "execution_id", "sequence"),
    )

    id: Mapped[UUID] = mapped_column(PostgreSQLUUID(as_uuid=True), primary_key=True)
    workspace_id: Mapped[UUID] = mapped_column(
        PostgreSQLUUID(as_uuid=True),
        ForeignKey("workspaces.id", ondelete="RESTRICT"),
        nullable=False,
    )
    execution_id: Mapped[UUID] = mapped_column(
        PostgreSQLUUID(as_uuid=True), nullable=False
    )
    session_id: Mapped[UUID] = mapped_column(
        PostgreSQLUUID(as_uuid=True), nullable=False
    )
    sequence: Mapped[int] = mapped_column(BigInteger, nullable=False)
    event_type: Mapped[str] = mapped_column(String(100), nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    occurred_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    raw_ref: Mapped[str | None] = mapped_column(String(500))


class ArtifactModel(TimestampMixin, Base):
    __tablename__ = "artifacts"
    __table_args__ = (
        CheckConstraint("size_bytes >= 0", name="ck_artifacts_size"),
        CheckConstraint(
            "content_hash ~ '^[0-9a-fA-F]{64}$'", name="ck_artifacts_content_hash"
        ),
        ForeignKeyConstraint(
            ["task_id", "workspace_id"],
            ["tasks.id", "tasks.workspace_id"],
            ondelete="RESTRICT",
            name="fk_artifacts_task_workspace",
        ),
        ForeignKeyConstraint(
            ["execution_id", "workspace_id"],
            ["executions.id", "executions.workspace_id"],
            ondelete="RESTRICT",
            name="fk_artifacts_execution_workspace",
        ),
        ForeignKeyConstraint(
            ["session_id", "workspace_id"],
            ["execution_sessions.id", "execution_sessions.workspace_id"],
            ondelete="RESTRICT",
            name="fk_artifacts_session_workspace",
        ),
        UniqueConstraint(
            "workspace_id",
            "storage_ref",
            "content_hash",
            name="uq_artifacts_storage_hash",
        ),
    )

    id: Mapped[UUID] = mapped_column(PostgreSQLUUID(as_uuid=True), primary_key=True)
    workspace_id: Mapped[UUID] = mapped_column(
        PostgreSQLUUID(as_uuid=True),
        ForeignKey("workspaces.id", ondelete="RESTRICT"),
        nullable=False,
    )
    task_id: Mapped[UUID] = mapped_column(PostgreSQLUUID(as_uuid=True), nullable=False)
    execution_id: Mapped[UUID] = mapped_column(
        PostgreSQLUUID(as_uuid=True), nullable=False
    )
    session_id: Mapped[UUID | None] = mapped_column(PostgreSQLUUID(as_uuid=True))
    artifact_type: Mapped[str] = mapped_column(String(64), nullable=False)
    storage_ref: Mapped[str] = mapped_column(String(500), nullable=False)
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    size_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False)
    media_type: Mapped[str] = mapped_column(String(255), nullable=False)
    provenance: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)


class EvidenceClaimModel(TimestampMixin, Base):
    __tablename__ = "evidence_claims"
    __table_args__ = (
        CheckConstraint(
            "status = 'candidate'", name="ck_evidence_claims_candidate_only"
        ),
        ForeignKeyConstraint(
            ["task_id", "workspace_id"],
            ["tasks.id", "tasks.workspace_id"],
            ondelete="RESTRICT",
            name="fk_evidence_claims_task_workspace",
        ),
        ForeignKeyConstraint(
            ["execution_id", "workspace_id"],
            ["executions.id", "executions.workspace_id"],
            ondelete="RESTRICT",
            name="fk_evidence_claims_execution_workspace",
        ),
    )

    id: Mapped[UUID] = mapped_column(PostgreSQLUUID(as_uuid=True), primary_key=True)
    workspace_id: Mapped[UUID] = mapped_column(
        PostgreSQLUUID(as_uuid=True),
        ForeignKey("workspaces.id", ondelete="RESTRICT"),
        nullable=False,
    )
    task_id: Mapped[UUID] = mapped_column(PostgreSQLUUID(as_uuid=True), nullable=False)
    execution_id: Mapped[UUID] = mapped_column(
        PostgreSQLUUID(as_uuid=True), nullable=False
    )
    acceptance_criterion_ref: Mapped[str] = mapped_column(String(255), nullable=False)
    conclusion: Mapped[str] = mapped_column(Text, nullable=False)
    environment_ref: Mapped[str] = mapped_column(String(500), nullable=False)
    artifact_ids: Mapped[list[str]] = mapped_column(JSONB, nullable=False)
    producer: Mapped[str] = mapped_column(String(255), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)


class ExecutionInputModel(TimestampMixin, Base):
    __tablename__ = "execution_inputs"
    __table_args__ = (
        CheckConstraint(
            "status IN ('accepted', 'queued', 'dispatching', 'delivered', 'blocked', "
            "'failed', 'cancelled', 'superseded')",
            name="ck_execution_inputs_status",
        ),
        CheckConstraint(
            "status <> 'superseded' OR superseded_reason IS NOT NULL",
            name="ck_execution_inputs_superseded_reason",
        ),
        CheckConstraint(
            "(source_input_id IS NULL) = (handoff_transition_id IS NULL)",
            name="ck_execution_inputs_handoff_lineage_pair",
        ),
        CheckConstraint(
            "source_input_id IS NULL OR source_input_id <> id",
            name="ck_execution_inputs_source_distinct",
        ),
        CheckConstraint("version >= 1", name="ck_execution_inputs_version"),
        ForeignKeyConstraint(
            ["execution_id", "workspace_id"],
            ["executions.id", "executions.workspace_id"],
            ondelete="RESTRICT",
            name="fk_execution_inputs_execution_workspace",
        ),
        ForeignKeyConstraint(
            ["session_id", "workspace_id"],
            ["execution_sessions.id", "execution_sessions.workspace_id"],
            ondelete="RESTRICT",
            name="fk_execution_inputs_session_workspace",
        ),
        ForeignKeyConstraint(
            ["source_input_id", "workspace_id"],
            ["execution_inputs.id", "execution_inputs.workspace_id"],
            ondelete="RESTRICT",
            name="fk_execution_inputs_source_input_workspace",
        ),
        ForeignKeyConstraint(
            ["handoff_transition_id", "workspace_id"],
            ["handoff_transitions.id", "handoff_transitions.workspace_id"],
            ondelete="RESTRICT",
            deferrable=True,
            initially="DEFERRED",
            name="fk_execution_inputs_handoff_transition_workspace",
        ),
        UniqueConstraint("id", "workspace_id", name="uq_execution_inputs_identity"),
        UniqueConstraint(
            "workspace_id",
            "session_id",
            "client_idempotency_key",
            name="uq_execution_inputs_idempotency",
        ),
        Index(
            "uq_execution_inputs_one_pending_session",
            "session_id",
            unique=True,
            postgresql_where=text(
                "status IN ('accepted', 'queued', 'dispatching', 'blocked')"
            ),
        ),
        Index(
            "uq_execution_inputs_handoff_source",
            "workspace_id",
            "handoff_transition_id",
            "source_input_id",
            unique=True,
            postgresql_where=text("source_input_id IS NOT NULL"),
        ),
    )

    id: Mapped[UUID] = mapped_column(PostgreSQLUUID(as_uuid=True), primary_key=True)
    workspace_id: Mapped[UUID] = mapped_column(
        PostgreSQLUUID(as_uuid=True),
        ForeignKey("workspaces.id", ondelete="RESTRICT"),
        nullable=False,
    )
    execution_id: Mapped[UUID] = mapped_column(
        PostgreSQLUUID(as_uuid=True), nullable=False
    )
    session_id: Mapped[UUID] = mapped_column(
        PostgreSQLUUID(as_uuid=True), nullable=False
    )
    client_idempotency_key: Mapped[str] = mapped_column(String(255), nullable=False)
    text: Mapped[str] = mapped_column(Text, nullable=False)
    attachment_refs: Mapped[list[str]] = mapped_column(JSONB, nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    operation_id: Mapped[UUID | None] = mapped_column(PostgreSQLUUID(as_uuid=True))
    failure_code: Mapped[str | None] = mapped_column(String(100))
    superseded_reason: Mapped[str | None] = mapped_column(String(500))
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    source_command_id: Mapped[UUID | None] = mapped_column(
        PostgreSQLUUID(as_uuid=True), ForeignKey("commands.id", ondelete="RESTRICT")
    )
    source_input_id: Mapped[UUID | None] = mapped_column(PostgreSQLUUID(as_uuid=True))
    handoff_transition_id: Mapped[UUID | None] = mapped_column(
        PostgreSQLUUID(as_uuid=True)
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )


class PlanningProfileModel(TimestampMixin, Base):
    __tablename__ = "planning_profiles"
    __table_args__ = (
        CheckConstraint("version >= 1", name="ck_planning_profiles_version"),
        CheckConstraint(
            "planner_module_config_version >= 1",
            name="ck_planning_profiles_module_config_version",
        ),
        CheckConstraint(
            "status IN ('active', 'disabled')",
            name="ck_planning_profiles_status",
        ),
        CheckConstraint(
            "jsonb_typeof(policy) = 'object'",
            name="ck_planning_profiles_policy_object",
        ),
        ForeignKeyConstraint(
            ["workspace_id"],
            ["workspaces.id"],
            ondelete="RESTRICT",
            name="fk_planning_profiles_workspace",
        ),
        ForeignKeyConstraint(
            ["planner_module_instance_id", "workspace_id"],
            ["module_instances.id", "module_instances.workspace_id"],
            ondelete="RESTRICT",
            name="fk_planning_profiles_module_workspace",
        ),
        UniqueConstraint("id", "workspace_id", name="uq_planning_profiles_identity"),
        UniqueConstraint(
            "workspace_id",
            "profile_key",
            "version",
            name="uq_planning_profiles_workspace_key_version",
        ),
        Index(
            "ix_planning_profiles_workspace_status_selectable",
            "workspace_id",
            "status",
            "user_selectable",
        ),
    )

    id: Mapped[UUID] = mapped_column(PostgreSQLUUID(as_uuid=True), primary_key=True)
    workspace_id: Mapped[UUID] = mapped_column(
        PostgreSQLUUID(as_uuid=True), nullable=False
    )
    profile_key: Mapped[str] = mapped_column(String(255), nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    display_name: Mapped[str] = mapped_column(String(255), nullable=False)
    planner_module_instance_id: Mapped[UUID] = mapped_column(
        PostgreSQLUUID(as_uuid=True), nullable=False
    )
    planner_module_config_version: Mapped[int] = mapped_column(Integer, nullable=False)
    planner_interface_version: Mapped[str] = mapped_column(String(100), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    user_selectable: Mapped[bool] = mapped_column(Boolean, nullable=False)
    policy: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)


class PlanningRequestModel(TimestampMixin, Base):
    __tablename__ = "planning_requests"
    __table_args__ = (
        CheckConstraint(
            "kind IN ('create', 'revision')", name="ck_planning_requests_kind"
        ),
        CheckConstraint(
            "status IN ('submitted', 'planning', 'waiting_for_user', "
            "'proposal_ready', 'materialized', 'failed', 'conflicted', "
            "'rejected', 'cancelled')",
            name="ck_planning_requests_status",
        ),
        CheckConstraint("version >= 1", name="ck_planning_requests_version"),
        CheckConstraint(
            "planning_profile_version >= 1",
            name="ck_planning_requests_profile_version",
        ),
        CheckConstraint(
            "jsonb_typeof(profile_snapshot) = 'object'",
            name="ck_planning_requests_profile_snapshot_object",
        ),
        CheckConstraint(
            "current_proposal_content_hash IS NULL OR "
            "current_proposal_content_hash ~ '^[0-9a-fA-F]{64}$'",
            name="ck_planning_requests_proposal_hash",
        ),
        CheckConstraint(
            "base_plan_generation IS NULL OR base_plan_generation >= 1",
            name="ck_planning_requests_base_generation",
        ),
        CheckConstraint(
            "base_root_task_version IS NULL OR base_root_task_version >= 1",
            name="ck_planning_requests_base_root_version",
        ),
        CheckConstraint(
            "base_plan_content_hash IS NULL OR "
            "base_plan_content_hash ~ '^[0-9a-f]{64}$'",
            name="ck_planning_requests_base_hash",
        ),
        CheckConstraint(
            "(kind = 'create' AND root_task_id IS NULL "
            "AND base_plan_snapshot_id IS NULL AND base_plan_generation IS NULL "
            "AND base_plan_content_hash IS NULL AND base_root_task_version IS NULL "
            "AND base_captured_at IS NULL) OR "
            "(kind = 'revision' AND root_task_id IS NOT NULL "
            "AND base_plan_snapshot_id IS NOT NULL AND base_plan_generation IS NOT NULL "
            "AND base_plan_content_hash IS NOT NULL AND base_root_task_version IS NOT NULL "
            "AND base_captured_at IS NOT NULL)",
            name="ck_planning_requests_revision_base",
        ),
        ForeignKeyConstraint(
            ["workspace_id"],
            ["workspaces.id"],
            ondelete="RESTRICT",
            name="fk_planning_requests_workspace",
        ),
        ForeignKeyConstraint(
            ["planning_profile_id", "workspace_id"],
            ["planning_profiles.id", "planning_profiles.workspace_id"],
            ondelete="RESTRICT",
            name="fk_planning_requests_profile_workspace",
        ),
        ForeignKeyConstraint(
            ["root_task_id", "workspace_id"],
            ["tasks.id", "tasks.workspace_id"],
            ondelete="RESTRICT",
            name="fk_planning_requests_root_task_workspace",
        ),
        ForeignKeyConstraint(
            ["base_plan_snapshot_id", "workspace_id"],
            ["task_plan_snapshots.id", "task_plan_snapshots.workspace_id"],
            ondelete="RESTRICT",
            name="fk_planning_requests_base_snapshot_workspace",
        ),
        ForeignKeyConstraint(
            ["id", "workspace_id", "current_brief_revision"],
            [
                "planning_brief_revisions.planning_request_id",
                "planning_brief_revisions.workspace_id",
                "planning_brief_revisions.revision",
            ],
            ondelete="RESTRICT",
            name="fk_planning_requests_current_brief",
        ),
        ForeignKeyConstraint(
            ["id", "workspace_id", "current_proposal_revision"],
            [
                "planning_proposal_revisions.planning_request_id",
                "planning_proposal_revisions.workspace_id",
                "planning_proposal_revisions.revision",
            ],
            ondelete="RESTRICT",
            name="fk_planning_requests_current_proposal",
        ),
        UniqueConstraint("id", "workspace_id", name="uq_planning_requests_identity"),
        Index("ix_planning_requests_workspace_status", "workspace_id", "status"),
        Index(
            "uq_planning_requests_active_revision",
            "workspace_id",
            "root_task_id",
            unique=True,
            postgresql_where=text(
                "status IN ('submitted', 'planning', 'waiting_for_user', "
                "'proposal_ready', 'failed') AND kind = 'revision'"
            ),
        ),
    )

    id: Mapped[UUID] = mapped_column(PostgreSQLUUID(as_uuid=True), primary_key=True)
    workspace_id: Mapped[UUID] = mapped_column(
        PostgreSQLUUID(as_uuid=True), nullable=False
    )
    kind: Mapped[str] = mapped_column(String(32), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    root_task_id: Mapped[UUID | None] = mapped_column(PostgreSQLUUID(as_uuid=True))
    base_plan_snapshot_id: Mapped[UUID | None] = mapped_column(
        PostgreSQLUUID(as_uuid=True)
    )
    base_plan_generation: Mapped[int | None] = mapped_column(Integer)
    base_plan_content_hash: Mapped[str | None] = mapped_column(String(64))
    base_root_task_version: Mapped[int | None] = mapped_column(Integer)
    base_captured_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    conflict_reason: Mapped[str | None] = mapped_column(String(100))
    current_brief_revision: Mapped[int | None] = mapped_column(Integer)
    current_proposal_revision: Mapped[int | None] = mapped_column(Integer)
    current_proposal_content_hash: Mapped[str | None] = mapped_column(String(64))
    planning_profile_id: Mapped[UUID] = mapped_column(
        PostgreSQLUUID(as_uuid=True), nullable=False
    )
    planning_profile_version: Mapped[int] = mapped_column(Integer, nullable=False)
    profile_snapshot: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    source_type: Mapped[str] = mapped_column(String(100), nullable=False)
    source_ref: Mapped[str | None] = mapped_column(String(500))
    actor: Mapped[str] = mapped_column(String(255), nullable=False)
    correlation_id: Mapped[UUID] = mapped_column(
        PostgreSQLUUID(as_uuid=True), nullable=False
    )
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )


class PlanningBriefRevisionModel(Base):
    __tablename__ = "planning_brief_revisions"
    __table_args__ = (
        CheckConstraint("revision >= 1", name="ck_planning_briefs_revision"),
        CheckConstraint(
            "jsonb_typeof(payload) = 'object'",
            name="ck_planning_briefs_payload_object",
        ),
        ForeignKeyConstraint(
            ["planning_request_id", "workspace_id"],
            ["planning_requests.id", "planning_requests.workspace_id"],
            ondelete="RESTRICT",
            name="fk_planning_briefs_request_workspace",
        ),
        PrimaryKeyConstraint("planning_request_id", "revision"),
        UniqueConstraint(
            "planning_request_id",
            "workspace_id",
            "revision",
            name="uq_planning_briefs_request_workspace_revision",
        ),
    )

    planning_request_id: Mapped[UUID] = mapped_column(
        PostgreSQLUUID(as_uuid=True), nullable=False
    )
    revision: Mapped[int] = mapped_column(Integer, nullable=False)
    workspace_id: Mapped[UUID] = mapped_column(
        PostgreSQLUUID(as_uuid=True), nullable=False
    )
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    created_by: Mapped[str] = mapped_column(String(255), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class PlanningRunModel(TimestampMixin, Base):
    __tablename__ = "planning_runs"
    __table_args__ = (
        CheckConstraint("attempt >= 1", name="ck_planning_runs_attempt"),
        CheckConstraint("version >= 1", name="ck_planning_runs_version"),
        CheckConstraint("brief_revision >= 1", name="ck_planning_runs_brief_revision"),
        CheckConstraint(
            "planning_profile_version >= 1",
            name="ck_planning_runs_profile_version",
        ),
        CheckConstraint(
            "planner_module_config_version >= 1",
            name="ck_planning_runs_module_config_version",
        ),
        CheckConstraint(
            "status IN ('queued', 'running', 'succeeded', "
            "'needs_clarification', 'failed', 'blocked', 'cancelled')",
            name="ck_planning_runs_status",
        ),
        CheckConstraint(
            "jsonb_typeof(capability_snapshot) = 'object'",
            name="ck_planning_runs_capability_snapshot_object",
        ),
        CheckConstraint(
            "jsonb_typeof(usage) = 'object'",
            name="ck_planning_runs_usage_object",
        ),
        CheckConstraint(
            "jsonb_typeof(diagnostic_refs) = 'array'",
            name="ck_planning_runs_diagnostic_refs_array",
        ),
        ForeignKeyConstraint(
            ["planning_request_id", "workspace_id"],
            ["planning_requests.id", "planning_requests.workspace_id"],
            ondelete="RESTRICT",
            name="fk_planning_runs_request_workspace",
        ),
        ForeignKeyConstraint(
            ["planning_request_id", "workspace_id", "brief_revision"],
            [
                "planning_brief_revisions.planning_request_id",
                "planning_brief_revisions.workspace_id",
                "planning_brief_revisions.revision",
            ],
            ondelete="RESTRICT",
            name="fk_planning_runs_brief_workspace",
        ),
        ForeignKeyConstraint(
            ["planning_profile_id", "workspace_id"],
            ["planning_profiles.id", "planning_profiles.workspace_id"],
            ondelete="RESTRICT",
            name="fk_planning_runs_profile_workspace",
        ),
        ForeignKeyConstraint(
            ["planner_module_instance_id", "workspace_id"],
            ["module_instances.id", "module_instances.workspace_id"],
            ondelete="RESTRICT",
            name="fk_planning_runs_module_workspace",
        ),
        UniqueConstraint("id", "workspace_id", name="uq_planning_runs_identity"),
        UniqueConstraint("operation_id", name="uq_planning_runs_operation_id"),
        UniqueConstraint(
            "planning_request_id",
            "attempt",
            name="uq_planning_runs_request_attempt",
        ),
        Index("ix_planning_runs_workspace_status", "workspace_id", "status"),
    )

    id: Mapped[UUID] = mapped_column(PostgreSQLUUID(as_uuid=True), primary_key=True)
    workspace_id: Mapped[UUID] = mapped_column(
        PostgreSQLUUID(as_uuid=True), nullable=False
    )
    planning_request_id: Mapped[UUID] = mapped_column(
        PostgreSQLUUID(as_uuid=True), nullable=False
    )
    brief_revision: Mapped[int] = mapped_column(Integer, nullable=False)
    planning_profile_id: Mapped[UUID] = mapped_column(
        PostgreSQLUUID(as_uuid=True), nullable=False
    )
    planning_profile_version: Mapped[int] = mapped_column(Integer, nullable=False)
    planner_module_instance_id: Mapped[UUID] = mapped_column(
        PostgreSQLUUID(as_uuid=True), nullable=False
    )
    planner_module_config_version: Mapped[int] = mapped_column(Integer, nullable=False)
    attempt: Mapped[int] = mapped_column(Integer, nullable=False)
    operation_id: Mapped[UUID] = mapped_column(
        PostgreSQLUUID(as_uuid=True), nullable=False
    )
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    result_kind: Mapped[str | None] = mapped_column(String(64))
    capability_snapshot: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    usage: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    provider_ref: Mapped[str | None] = mapped_column(String(500))
    error_code: Mapped[str | None] = mapped_column(String(100))
    error_message: Mapped[str | None] = mapped_column(Text)
    diagnostic_refs: Mapped[list[str]] = mapped_column(JSONB, nullable=False)
    queued_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )


class PlanningRunLeaseModel(TimestampMixin, Base):
    __tablename__ = "planning_run_leases"
    __table_args__ = (
        CheckConstraint(
            "status IN ('active', 'released', 'expired')",
            name="ck_planning_run_leases_status",
        ),
        CheckConstraint(
            "fencing_token >= 1",
            name="ck_planning_run_leases_fencing_token",
        ),
        CheckConstraint("version >= 1", name="ck_planning_run_leases_version"),
        ForeignKeyConstraint(
            ["planning_run_id", "workspace_id"],
            ["planning_runs.id", "planning_runs.workspace_id"],
            ondelete="RESTRICT",
            name="fk_planning_run_leases_run_workspace",
        ),
        UniqueConstraint("id", "workspace_id", name="uq_planning_run_leases_identity"),
        Index(
            "uq_planning_run_leases_active_run",
            "planning_run_id",
            unique=True,
            postgresql_where=text("status = 'active'"),
        ),
        Index(
            "ix_planning_run_leases_expiry",
            "status",
            "expires_at",
        ),
    )

    id: Mapped[UUID] = mapped_column(PostgreSQLUUID(as_uuid=True), primary_key=True)
    workspace_id: Mapped[UUID] = mapped_column(
        PostgreSQLUUID(as_uuid=True), nullable=False
    )
    planning_run_id: Mapped[UUID] = mapped_column(
        PostgreSQLUUID(as_uuid=True), nullable=False
    )
    owner_id: Mapped[str] = mapped_column(String(255), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    fencing_token: Mapped[int] = mapped_column(BigInteger, nullable=False)
    acquired_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    expires_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    released_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )


class PlanningClarificationModel(TimestampMixin, Base):
    __tablename__ = "planning_clarifications"
    __table_args__ = (
        CheckConstraint("sequence >= 1", name="ck_planning_clarifications_sequence"),
        CheckConstraint("version >= 1", name="ck_planning_clarifications_version"),
        CheckConstraint(
            "jsonb_typeof(options) = 'array'",
            name="ck_planning_clarifications_options_array",
        ),
        CheckConstraint(
            "answer_brief_revision IS NULL OR answer_brief_revision >= 1",
            name="ck_planning_clarifications_answer_revision",
        ),
        ForeignKeyConstraint(
            ["planning_request_id", "workspace_id"],
            ["planning_requests.id", "planning_requests.workspace_id"],
            ondelete="RESTRICT",
            name="fk_planning_clarifications_request_workspace",
        ),
        ForeignKeyConstraint(
            ["planning_run_id", "workspace_id"],
            ["planning_runs.id", "planning_runs.workspace_id"],
            ondelete="RESTRICT",
            name="fk_planning_clarifications_run_workspace",
        ),
        UniqueConstraint(
            "planning_request_id",
            "sequence",
            name="uq_planning_clarifications_request_sequence",
        ),
        Index(
            "ix_planning_clarifications_workspace_request",
            "workspace_id",
            "planning_request_id",
        ),
    )

    id: Mapped[UUID] = mapped_column(PostgreSQLUUID(as_uuid=True), primary_key=True)
    workspace_id: Mapped[UUID] = mapped_column(
        PostgreSQLUUID(as_uuid=True), nullable=False
    )
    planning_request_id: Mapped[UUID] = mapped_column(
        PostgreSQLUUID(as_uuid=True), nullable=False
    )
    planning_run_id: Mapped[UUID] = mapped_column(
        PostgreSQLUUID(as_uuid=True), nullable=False
    )
    sequence: Mapped[int] = mapped_column(Integer, nullable=False)
    question: Mapped[str] = mapped_column(Text, nullable=False)
    rationale: Mapped[str] = mapped_column(Text, nullable=False)
    answer_type: Mapped[str] = mapped_column(String(32), nullable=False)
    options: Mapped[list[str]] = mapped_column(JSONB, nullable=False)
    blocking: Mapped[bool] = mapped_column(Boolean, nullable=False)
    answer: Mapped[str | None] = mapped_column(Text)
    answered_by: Mapped[str | None] = mapped_column(String(255))
    answered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    answer_brief_revision: Mapped[int | None] = mapped_column(Integer)
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )


class PlanningProposalRevisionModel(Base):
    __tablename__ = "planning_proposal_revisions"
    __table_args__ = (
        CheckConstraint("revision >= 1", name="ck_planning_proposals_revision"),
        CheckConstraint(
            "brief_revision >= 1", name="ck_planning_proposals_brief_revision"
        ),
        CheckConstraint(
            "proposal_schema_version >= 1",
            name="ck_planning_proposals_schema_version",
        ),
        CheckConstraint(
            "profile_version >= 1", name="ck_planning_proposals_profile_version"
        ),
        CheckConstraint(
            "planner_module_config_version >= 1",
            name="ck_planning_proposals_module_config_version",
        ),
        CheckConstraint(
            "kind IN ('create', 'revision')", name="ck_planning_proposals_kind"
        ),
        CheckConstraint(
            "jsonb_typeof(decision_summary) = 'object'",
            name="ck_planning_proposals_summary_object",
        ),
        CheckConstraint(
            "jsonb_typeof(plan_template) = 'object'",
            name="ck_planning_proposals_template_object",
        ),
        CheckConstraint(
            "jsonb_typeof(execution_intents) = 'array'",
            name="ck_planning_proposals_intents_array",
        ),
        CheckConstraint(
            "jsonb_typeof(impact_policy) = 'object'",
            name="ck_planning_proposals_impact_object",
        ),
        CheckConstraint(
            "jsonb_typeof(derived_diff) = 'object'",
            name="ck_planning_proposals_diff_object",
        ),
        CheckConstraint(
            "jsonb_typeof(canonical_payload) = 'object'",
            name="ck_planning_proposals_canonical_object",
        ),
        CheckConstraint(
            "content_hash ~ '^[0-9a-fA-F]{64}$'",
            name="ck_planning_proposals_content_hash",
        ),
        ForeignKeyConstraint(
            ["planning_request_id", "workspace_id"],
            ["planning_requests.id", "planning_requests.workspace_id"],
            ondelete="RESTRICT",
            name="fk_planning_proposals_request_workspace",
        ),
        ForeignKeyConstraint(
            ["planning_request_id", "workspace_id", "brief_revision"],
            [
                "planning_brief_revisions.planning_request_id",
                "planning_brief_revisions.workspace_id",
                "planning_brief_revisions.revision",
            ],
            ondelete="RESTRICT",
            name="fk_planning_proposals_brief_workspace",
        ),
        ForeignKeyConstraint(
            ["planning_run_id", "workspace_id"],
            ["planning_runs.id", "planning_runs.workspace_id"],
            ondelete="RESTRICT",
            name="fk_planning_proposals_run_workspace",
        ),
        ForeignKeyConstraint(
            ["profile_id", "workspace_id"],
            ["planning_profiles.id", "planning_profiles.workspace_id"],
            ondelete="RESTRICT",
            name="fk_planning_proposals_profile_workspace",
        ),
        ForeignKeyConstraint(
            ["planner_module_instance_id", "workspace_id"],
            ["module_instances.id", "module_instances.workspace_id"],
            ondelete="RESTRICT",
            name="fk_planning_proposals_module_workspace",
        ),
        PrimaryKeyConstraint("id"),
        UniqueConstraint("id", "workspace_id", name="uq_planning_proposals_identity"),
        UniqueConstraint(
            "planning_request_id",
            "workspace_id",
            "revision",
            name="uq_planning_proposals_request_workspace_revision",
        ),
        Index(
            "ix_planning_proposals_workspace_request_revision",
            "workspace_id",
            "planning_request_id",
            "revision",
        ),
    )

    id: Mapped[UUID] = mapped_column(PostgreSQLUUID(as_uuid=True), primary_key=True)
    planning_request_id: Mapped[UUID] = mapped_column(
        PostgreSQLUUID(as_uuid=True), nullable=False
    )
    revision: Mapped[int] = mapped_column(Integer, nullable=False)
    workspace_id: Mapped[UUID] = mapped_column(
        PostgreSQLUUID(as_uuid=True), nullable=False
    )
    brief_revision: Mapped[int] = mapped_column(Integer, nullable=False)
    planning_run_id: Mapped[UUID] = mapped_column(
        PostgreSQLUUID(as_uuid=True), nullable=False
    )
    proposal_schema_version: Mapped[int] = mapped_column(Integer, nullable=False)
    kind: Mapped[str] = mapped_column(String(32), nullable=False)
    profile_id: Mapped[UUID] = mapped_column(
        PostgreSQLUUID(as_uuid=True), nullable=False
    )
    profile_version: Mapped[int] = mapped_column(Integer, nullable=False)
    planner_module_instance_id: Mapped[UUID] = mapped_column(
        PostgreSQLUUID(as_uuid=True), nullable=False
    )
    planner_module_config_version: Mapped[int] = mapped_column(Integer, nullable=False)
    decision_summary: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    plan_template: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    execution_intents: Mapped[list[dict[str, Any]]] = mapped_column(
        JSONB, nullable=False
    )
    impact_policy: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    derived_diff: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    canonical_payload: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class ProposalMaterializationModel(TimestampMixin, Base):
    __tablename__ = "proposal_materializations"
    __table_args__ = (
        CheckConstraint(
            "plan_generation >= 1",
            name="ck_proposal_materializations_generation",
        ),
        CheckConstraint(
            "jsonb_typeof(subtask_task_mapping) = 'object'",
            name="ck_proposal_materializations_subtasks_object",
        ),
        CheckConstraint(
            "jsonb_typeof(worker_item_mapping) = 'object'",
            name="ck_proposal_materializations_workers_object",
        ),
        CheckConstraint(
            "jsonb_typeof(execution_request_mapping) = 'object'",
            name="ck_proposal_materializations_requests_object",
        ),
        CheckConstraint(
            "kind IN ('create', 'revision')",
            name="ck_proposal_materializations_kind",
        ),
        CheckConstraint(
            "diff_hash IS NULL OR diff_hash ~ '^[0-9a-f]{64}$'",
            name="ck_proposal_materializations_diff_hash",
        ),
        ForeignKeyConstraint(
            ["planning_request_id", "workspace_id"],
            ["planning_requests.id", "planning_requests.workspace_id"],
            ondelete="RESTRICT",
            name="fk_proposal_materializations_request_workspace",
        ),
        ForeignKeyConstraint(
            ["proposal_revision_id", "workspace_id"],
            [
                "planning_proposal_revisions.id",
                "planning_proposal_revisions.workspace_id",
            ],
            ondelete="RESTRICT",
            name="fk_proposal_materializations_proposal_workspace",
        ),
        ForeignKeyConstraint(
            ["root_task_id", "workspace_id"],
            ["tasks.id", "tasks.workspace_id"],
            ondelete="RESTRICT",
            name="fk_proposal_materializations_root_task_workspace",
        ),
        ForeignKeyConstraint(
            ["plan_snapshot_id", "workspace_id"],
            ["task_plan_snapshots.id", "task_plan_snapshots.workspace_id"],
            ondelete="RESTRICT",
            name="fk_proposal_materializations_snapshot_workspace",
        ),
        ForeignKeyConstraint(
            ["plan_transition_id", "workspace_id"],
            ["plan_transitions.id", "plan_transitions.workspace_id"],
            ondelete="RESTRICT",
            name="fk_proposal_materializations_transition_workspace",
        ),
        UniqueConstraint(
            "planning_request_id", name="uq_proposal_materializations_request"
        ),
        UniqueConstraint(
            "id", "workspace_id", name="uq_proposal_materializations_identity"
        ),
        Index(
            "ix_proposal_materializations_workspace_created",
            "workspace_id",
            "created_at",
        ),
    )

    id: Mapped[UUID] = mapped_column(PostgreSQLUUID(as_uuid=True), primary_key=True)
    workspace_id: Mapped[UUID] = mapped_column(
        PostgreSQLUUID(as_uuid=True), nullable=False
    )
    planning_request_id: Mapped[UUID] = mapped_column(
        PostgreSQLUUID(as_uuid=True), nullable=False
    )
    kind: Mapped[str] = mapped_column(String(32), nullable=False)
    proposal_revision_id: Mapped[UUID] = mapped_column(
        PostgreSQLUUID(as_uuid=True), nullable=False
    )
    root_task_id: Mapped[UUID] = mapped_column(
        PostgreSQLUUID(as_uuid=True), nullable=False
    )
    plan_snapshot_id: Mapped[UUID] = mapped_column(
        PostgreSQLUUID(as_uuid=True), nullable=False
    )
    plan_generation: Mapped[int] = mapped_column(Integer, nullable=False)
    diff_hash: Mapped[str | None] = mapped_column(String(64))
    plan_transition_id: Mapped[UUID | None] = mapped_column(
        PostgreSQLUUID(as_uuid=True)
    )
    subtask_task_mapping: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    worker_item_mapping: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    execution_request_mapping: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False
    )
    deep_link: Mapped[str] = mapped_column(String(500), nullable=False)
    confirmed_by: Mapped[str] = mapped_column(String(255), nullable=False)


class ExecutionRequestModel(TimestampMixin, Base):
    __tablename__ = "execution_requests"
    __table_args__ = (
        CheckConstraint(
            "plan_generation >= 1", name="ck_execution_requests_generation"
        ),
        CheckConstraint("max_attempts >= 1", name="ck_execution_requests_attempts"),
        CheckConstraint("version >= 1", name="ck_execution_requests_version"),
        CheckConstraint(
            "status IN ('blocked', 'ready', 'materialized', 'cancelled', 'obsolete')",
            name="ck_execution_requests_status",
        ),
        CheckConstraint(
            "collaboration_gate_mode IN ('unmanaged', 'release_required')",
            name="ck_execution_requests_collaboration_gate_mode",
        ),
        CheckConstraint(
            "jsonb_typeof(capability_requirements) = 'array'",
            name="ck_execution_requests_capabilities_array",
        ),
        CheckConstraint(
            "jsonb_typeof(adapter_preferences) = 'array'",
            name="ck_execution_requests_preferences_array",
        ),
        CheckConstraint(
            "jsonb_typeof(adapter_forbidden) = 'array'",
            name="ck_execution_requests_adapter_forbidden_array",
        ),
        CheckConstraint(
            "jsonb_typeof(expected_outputs) = 'array'",
            name="ck_execution_requests_outputs_array",
        ),
        CheckConstraint(
            "jsonb_typeof(expected_artifact_refs) = 'array'",
            name="ck_execution_requests_artifacts_array",
        ),
        CheckConstraint(
            "jsonb_typeof(expected_evidence_refs) = 'array'",
            name="ck_execution_requests_evidence_array",
        ),
        CheckConstraint(
            "jsonb_typeof(gate_details) = 'object'",
            name="ck_execution_requests_gate_details_object",
        ),
        CheckConstraint(
            "(status = 'materialized' AND execution_lineage_id IS NOT NULL "
            "AND first_execution_id IS NOT NULL) OR "
            "(status = 'obsolete' AND handoff_transition_id IS NOT NULL "
            "AND execution_lineage_id IS NOT NULL "
            "AND first_execution_id IS NOT NULL) OR "
            "(status IN ('blocked', 'ready', 'cancelled', 'obsolete') "
            "AND execution_lineage_id IS NULL AND first_execution_id IS NULL)",
            name="ck_execution_requests_materialized_lineage",
        ),
        CheckConstraint(
            "handoff_transition_id IS NULL OR "
            "(source_execution_id IS NOT NULL AND source_session_id IS NOT NULL)",
            name="ck_execution_requests_handoff_lineage",
        ),
        ForeignKeyConstraint(
            ["planning_request_id", "workspace_id"],
            ["planning_requests.id", "planning_requests.workspace_id"],
            ondelete="RESTRICT",
            name="fk_execution_requests_request_workspace",
        ),
        ForeignKeyConstraint(
            ["proposal_revision_id", "workspace_id"],
            [
                "planning_proposal_revisions.id",
                "planning_proposal_revisions.workspace_id",
            ],
            ondelete="RESTRICT",
            name="fk_execution_requests_proposal_workspace",
        ),
        ForeignKeyConstraint(
            ["plan_snapshot_id", "workspace_id"],
            ["task_plan_snapshots.id", "task_plan_snapshots.workspace_id"],
            ondelete="RESTRICT",
            name="fk_execution_requests_snapshot_workspace",
        ),
        ForeignKeyConstraint(
            ["root_task_id", "workspace_id"],
            ["tasks.id", "tasks.workspace_id"],
            ondelete="RESTRICT",
            name="fk_execution_requests_root_task_workspace",
        ),
        ForeignKeyConstraint(
            ["subtask_task_id", "workspace_id"],
            ["tasks.id", "tasks.workspace_id"],
            ondelete="RESTRICT",
            name="fk_execution_requests_subtask_workspace",
        ),
        ForeignKeyConstraint(
            ["worker_item_id", "workspace_id"],
            ["worker_items.id", "worker_items.workspace_id"],
            ondelete="RESTRICT",
            name="fk_execution_requests_worker_item_workspace",
        ),
        ForeignKeyConstraint(
            ["execution_lineage_id", "workspace_id"],
            ["executions.id", "executions.workspace_id"],
            ondelete="RESTRICT",
            name="fk_execution_requests_lineage_workspace",
        ),
        ForeignKeyConstraint(
            ["first_execution_id", "workspace_id"],
            ["executions.id", "executions.workspace_id"],
            ondelete="RESTRICT",
            name="fk_execution_requests_first_execution_workspace",
        ),
        ForeignKeyConstraint(
            ["source_execution_id", "workspace_id"],
            ["executions.id", "executions.workspace_id"],
            ondelete="RESTRICT",
            name="fk_execution_requests_source_execution_workspace",
        ),
        ForeignKeyConstraint(
            ["source_session_id", "workspace_id"],
            ["execution_sessions.id", "execution_sessions.workspace_id"],
            ondelete="RESTRICT",
            name="fk_execution_requests_source_session_workspace",
        ),
        ForeignKeyConstraint(
            ["handoff_transition_id", "workspace_id"],
            ["handoff_transitions.id", "handoff_transitions.workspace_id"],
            ondelete="RESTRICT",
            deferrable=True,
            initially="DEFERRED",
            name="fk_execution_requests_handoff_transition_workspace",
        ),
        UniqueConstraint("id", "workspace_id", name="uq_execution_requests_identity"),
        Index(
            "uq_execution_requests_proposal_worker_item",
            "proposal_revision_id",
            "worker_item_key",
            unique=True,
            postgresql_where=text("handoff_transition_id IS NULL"),
        ),
        Index(
            "uq_execution_requests_handoff_transition",
            "workspace_id",
            "handoff_transition_id",
            unique=True,
            postgresql_where=text(
                "handoff_transition_id IS NOT NULL AND status = 'materialized'"
            ),
        ),
        Index(
            "uq_execution_requests_lineage",
            "workspace_id",
            "execution_lineage_id",
            unique=True,
            postgresql_where=text("execution_lineage_id IS NOT NULL"),
        ),
        Index(
            "ix_execution_requests_workspace_status",
            "workspace_id",
            "status",
            "not_before",
        ),
    )

    id: Mapped[UUID] = mapped_column(PostgreSQLUUID(as_uuid=True), primary_key=True)
    workspace_id: Mapped[UUID] = mapped_column(
        PostgreSQLUUID(as_uuid=True), nullable=False
    )
    planning_request_id: Mapped[UUID] = mapped_column(
        PostgreSQLUUID(as_uuid=True), nullable=False
    )
    proposal_revision_id: Mapped[UUID] = mapped_column(
        PostgreSQLUUID(as_uuid=True), nullable=False
    )
    plan_snapshot_id: Mapped[UUID] = mapped_column(
        PostgreSQLUUID(as_uuid=True), nullable=False
    )
    plan_generation: Mapped[int] = mapped_column(Integer, nullable=False)
    root_task_id: Mapped[UUID] = mapped_column(
        PostgreSQLUUID(as_uuid=True), nullable=False
    )
    subtask_task_id: Mapped[UUID] = mapped_column(
        PostgreSQLUUID(as_uuid=True), nullable=False
    )
    worker_item_id: Mapped[UUID] = mapped_column(
        PostgreSQLUUID(as_uuid=True), nullable=False
    )
    worker_item_key: Mapped[str] = mapped_column(String(500), nullable=False)
    intent_key: Mapped[str] = mapped_column(String(500), nullable=False)
    purpose: Mapped[str] = mapped_column(Text, nullable=False)
    capability_requirements: Mapped[list[str]] = mapped_column(JSONB, nullable=False)
    adapter_preferences: Mapped[list[str]] = mapped_column(JSONB, nullable=False)
    adapter_forbidden: Mapped[list[str]] = mapped_column(
        JSONB, nullable=False, server_default=text("'[]'::jsonb")
    )
    budget_class: Mapped[str] = mapped_column(String(100), nullable=False)
    not_before: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    max_attempts: Mapped[int] = mapped_column(Integer, nullable=False)
    expected_outputs: Mapped[list[str]] = mapped_column(JSONB, nullable=False)
    expected_artifact_refs: Mapped[list[str]] = mapped_column(JSONB, nullable=False)
    expected_evidence_refs: Mapped[list[str]] = mapped_column(JSONB, nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    collaboration_gate_mode: Mapped[str] = mapped_column(
        String(32), nullable=False, default="unmanaged"
    )
    gate_reason: Mapped[str] = mapped_column(String(255), nullable=False)
    gate_details: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    gate_evaluated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    status_changed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    execution_lineage_id: Mapped[UUID | None] = mapped_column(
        PostgreSQLUUID(as_uuid=True)
    )
    first_execution_id: Mapped[UUID | None] = mapped_column(
        PostgreSQLUUID(as_uuid=True)
    )
    source_execution_id: Mapped[UUID | None] = mapped_column(
        PostgreSQLUUID(as_uuid=True)
    )
    source_session_id: Mapped[UUID | None] = mapped_column(PostgreSQLUUID(as_uuid=True))
    handoff_transition_id: Mapped[UUID | None] = mapped_column(
        PostgreSQLUUID(as_uuid=True)
    )
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )


class ExecutionRequestDependencyModel(Base):
    __tablename__ = "execution_request_dependencies"
    __table_args__ = (
        CheckConstraint(
            "enforcement IN ('hard', 'informational')",
            name="ck_execution_request_dependencies_enforcement",
        ),
        CheckConstraint(
            "downstream_request_id <> upstream_request_id",
            name="ck_execution_request_dependencies_not_self",
        ),
        CheckConstraint(
            "jsonb_typeof(required_artifact_refs) = 'array'",
            name="ck_execution_request_dependencies_artifacts_array",
        ),
        CheckConstraint(
            "jsonb_typeof(required_evidence_refs) = 'array'",
            name="ck_execution_request_dependencies_evidence_array",
        ),
        ForeignKeyConstraint(
            ["downstream_request_id", "workspace_id"],
            ["execution_requests.id", "execution_requests.workspace_id"],
            ondelete="RESTRICT",
            name="fk_execution_request_dependencies_downstream_workspace",
        ),
        ForeignKeyConstraint(
            ["upstream_request_id", "workspace_id"],
            ["execution_requests.id", "execution_requests.workspace_id"],
            ondelete="RESTRICT",
            name="fk_execution_request_dependencies_upstream_workspace",
        ),
        PrimaryKeyConstraint(
            "workspace_id", "downstream_request_id", "upstream_request_id"
        ),
        Index(
            "ix_execution_request_dependencies_upstream",
            "workspace_id",
            "upstream_request_id",
        ),
    )

    workspace_id: Mapped[UUID] = mapped_column(
        PostgreSQLUUID(as_uuid=True), nullable=False
    )
    downstream_request_id: Mapped[UUID] = mapped_column(
        PostgreSQLUUID(as_uuid=True), nullable=False
    )
    upstream_request_id: Mapped[UUID] = mapped_column(
        PostgreSQLUUID(as_uuid=True), nullable=False
    )
    enforcement: Mapped[str] = mapped_column(String(32), nullable=False)
    required_artifact_refs: Mapped[list[str]] = mapped_column(JSONB, nullable=False)
    required_evidence_refs: Mapped[list[str]] = mapped_column(JSONB, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class CollaborationRunModel(TimestampMixin, Base):
    __tablename__ = "collaboration_runs"
    __table_args__ = (
        CheckConstraint(
            "plan_generation >= 1", name="ck_collaboration_runs_generation"
        ),
        CheckConstraint(
            "runtime_config_version >= 1",
            name="ck_collaboration_runs_runtime_config_version",
        ),
        CheckConstraint(
            "last_evaluated_change_cursor >= 0",
            name="ck_collaboration_runs_change_cursor",
        ),
        CheckConstraint(
            "consecutive_failure_count >= 0",
            name="ck_collaboration_runs_failure_count",
        ),
        CheckConstraint(
            "resource_version >= 1", name="ck_collaboration_runs_resource_version"
        ),
        CheckConstraint(
            "plan_content_hash ~ '^[0-9a-f]{64}$'",
            name="ck_collaboration_runs_plan_hash",
        ),
        CheckConstraint(
            "status IN ('pending', 'active', 'blocked', 'completed', 'superseded')",
            name="ck_collaboration_runs_status",
        ),
        CheckConstraint(
            "jsonb_typeof(runtime_capability_snapshot_json) = 'object'",
            name="ck_collaboration_runs_capability_object",
        ),
        CheckConstraint(
            "jsonb_typeof(policy_snapshot_json) = 'object'",
            name="ck_collaboration_runs_policy_object",
        ),
        ForeignKeyConstraint(
            ["workspace_id"],
            ["workspaces.id"],
            ondelete="RESTRICT",
            name="fk_collaboration_runs_workspace",
        ),
        ForeignKeyConstraint(
            ["root_task_id", "workspace_id"],
            ["tasks.id", "tasks.workspace_id"],
            ondelete="RESTRICT",
            name="fk_collaboration_runs_root_task_workspace",
        ),
        ForeignKeyConstraint(
            ["plan_snapshot_id", "workspace_id"],
            ["task_plan_snapshots.id", "task_plan_snapshots.workspace_id"],
            ondelete="RESTRICT",
            name="fk_collaboration_runs_plan_snapshot_workspace",
        ),
        ForeignKeyConstraint(
            ["runtime_definition_id", "workspace_id"],
            ["module_definitions.id", "module_definitions.workspace_id"],
            ondelete="RESTRICT",
            name="fk_collaboration_runs_runtime_definition_workspace",
        ),
        ForeignKeyConstraint(
            ["runtime_instance_id", "workspace_id"],
            ["module_instances.id", "module_instances.workspace_id"],
            ondelete="RESTRICT",
            name="fk_collaboration_runs_runtime_instance_workspace",
        ),
        ForeignKeyConstraint(
            ["last_projection_snapshot_id", "workspace_id"],
            [
                "runtime_projection_snapshots.id",
                "runtime_projection_snapshots.workspace_id",
            ],
            ondelete="RESTRICT",
            name="fk_collaboration_runs_projection_workspace",
        ),
        ForeignKeyConstraint(
            ["last_decision_id", "workspace_id"],
            ["runtime_decisions.id", "runtime_decisions.workspace_id"],
            ondelete="RESTRICT",
            name="fk_collaboration_runs_decision_workspace",
        ),
        UniqueConstraint("id", "workspace_id", name="uq_collaboration_runs_identity"),
        UniqueConstraint(
            "workspace_id",
            "root_task_id",
            "plan_snapshot_id",
            "plan_generation",
            name="uq_collaboration_runs_plan_identity",
        ),
        Index(
            "uq_collaboration_runs_root_active",
            "workspace_id",
            "root_task_id",
            unique=True,
            postgresql_where=text("status IN ('pending', 'active', 'blocked')"),
        ),
        Index(
            "ix_collaboration_runs_recovery",
            "workspace_id",
            "status",
            "next_evaluation_at",
        ),
    )

    id: Mapped[UUID] = mapped_column(PostgreSQLUUID(as_uuid=True), primary_key=True)
    workspace_id: Mapped[UUID] = mapped_column(
        PostgreSQLUUID(as_uuid=True), nullable=False
    )
    root_task_id: Mapped[UUID] = mapped_column(
        PostgreSQLUUID(as_uuid=True), nullable=False
    )
    plan_snapshot_id: Mapped[UUID] = mapped_column(
        PostgreSQLUUID(as_uuid=True), nullable=False
    )
    plan_generation: Mapped[int] = mapped_column(Integer, nullable=False)
    plan_content_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    runtime_definition_id: Mapped[UUID] = mapped_column(
        PostgreSQLUUID(as_uuid=True), nullable=False
    )
    runtime_instance_id: Mapped[UUID] = mapped_column(
        PostgreSQLUUID(as_uuid=True), nullable=False
    )
    runtime_interface_version: Mapped[str] = mapped_column(
        String(100), nullable=False
    )
    runtime_config_version: Mapped[int] = mapped_column(Integer, nullable=False)
    runtime_capability_snapshot_json: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False
    )
    policy_snapshot_json: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False
    )
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    last_projection_snapshot_id: Mapped[UUID | None] = mapped_column(
        PostgreSQLUUID(as_uuid=True)
    )
    last_decision_id: Mapped[UUID | None] = mapped_column(
        PostgreSQLUUID(as_uuid=True)
    )
    last_evaluated_change_cursor: Mapped[int] = mapped_column(BigInteger, nullable=False)
    next_evaluation_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True)
    )
    consecutive_failure_count: Mapped[int] = mapped_column(Integer, nullable=False)
    blocked_reason: Mapped[str | None] = mapped_column(String(16384))
    resource_version: Mapped[int] = mapped_column(Integer, nullable=False)
    activated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    superseded_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )


class RuntimeProjectionSnapshotModel(TimestampMixin, Base):
    __tablename__ = "runtime_projection_snapshots"
    __table_args__ = (
        CheckConstraint(
            "schema_version >= 1", name="ck_runtime_projection_schema_version"
        ),
        CheckConstraint(
            "plan_generation >= 1", name="ck_runtime_projection_generation"
        ),
        CheckConstraint(
            "fact_cursor >= 0", name="ck_runtime_projection_fact_cursor"
        ),
        CheckConstraint("size_bytes >= 1", name="ck_runtime_projection_size"),
        CheckConstraint(
            "plan_content_hash ~ '^[0-9a-f]{64}$'",
            name="ck_runtime_projection_plan_hash",
        ),
        CheckConstraint(
            "content_hash ~ '^[0-9a-f]{64}$'",
            name="ck_runtime_projection_content_hash",
        ),
        CheckConstraint(
            "jsonb_typeof(payload_json) = 'object'",
            name="ck_runtime_projection_payload_object",
        ),
        ForeignKeyConstraint(
            ["run_id", "workspace_id"],
            ["collaboration_runs.id", "collaboration_runs.workspace_id"],
            ondelete="RESTRICT",
            name="fk_runtime_projection_run_workspace",
        ),
        ForeignKeyConstraint(
            ["plan_snapshot_id", "workspace_id"],
            ["task_plan_snapshots.id", "task_plan_snapshots.workspace_id"],
            ondelete="RESTRICT",
            name="fk_runtime_projection_plan_snapshot_workspace",
        ),
        UniqueConstraint("id", "workspace_id", name="uq_runtime_projection_identity"),
        UniqueConstraint(
            "run_id",
            "schema_version",
            "content_hash",
            name="uq_runtime_projection_run_schema_hash",
        ),
        Index(
            "ix_runtime_projection_run_created",
            "workspace_id",
            "run_id",
            "created_at",
        ),
    )

    id: Mapped[UUID] = mapped_column(PostgreSQLUUID(as_uuid=True), primary_key=True)
    workspace_id: Mapped[UUID] = mapped_column(
        PostgreSQLUUID(as_uuid=True), nullable=False
    )
    run_id: Mapped[UUID] = mapped_column(PostgreSQLUUID(as_uuid=True), nullable=False)
    schema_version: Mapped[int] = mapped_column(Integer, nullable=False)
    plan_snapshot_id: Mapped[UUID] = mapped_column(
        PostgreSQLUUID(as_uuid=True), nullable=False
    )
    plan_generation: Mapped[int] = mapped_column(Integer, nullable=False)
    plan_content_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    fact_cursor: Mapped[int] = mapped_column(BigInteger, nullable=False)
    payload_json: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    size_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False)


class CollaborationEvaluationModel(TimestampMixin, Base):
    __tablename__ = "collaboration_evaluations"
    __table_args__ = (
        CheckConstraint(
            "status IN ('pending', 'evaluating', 'retry_wait', 'completed', 'blocked')",
            name="ck_collaboration_evaluations_status",
        ),
        CheckConstraint(
            "attempt >= 1", name="ck_collaboration_evaluations_attempt"
        ),
        CheckConstraint(
            "resource_version >= 1",
            name="ck_collaboration_evaluations_resource_version",
        ),
        CheckConstraint(
            "response_hash IS NULL OR response_hash ~ '^[0-9a-f]{64}$'",
            name="ck_collaboration_evaluations_response_hash",
        ),
        ForeignKeyConstraint(
            ["run_id", "workspace_id"],
            ["collaboration_runs.id", "collaboration_runs.workspace_id"],
            ondelete="RESTRICT",
            name="fk_collaboration_evaluations_run_workspace",
        ),
        ForeignKeyConstraint(
            ["projection_snapshot_id", "workspace_id"],
            [
                "runtime_projection_snapshots.id",
                "runtime_projection_snapshots.workspace_id",
            ],
            ondelete="RESTRICT",
            name="fk_collaboration_evaluations_projection_workspace",
        ),
        UniqueConstraint(
            "id", "workspace_id", name="uq_collaboration_evaluations_identity"
        ),
        UniqueConstraint(
            "workspace_id",
            "run_id",
            "projection_snapshot_id",
            name="uq_collaboration_evaluations_run_projection",
        ),
        UniqueConstraint(
            "workspace_id",
            "operation_id",
            name="uq_collaboration_evaluations_operation",
        ),
        Index(
            "ix_collaboration_evaluations_claimable",
            "workspace_id",
            "status",
            "next_attempt_at",
            "lease_expires_at",
        ),
    )

    id: Mapped[UUID] = mapped_column(PostgreSQLUUID(as_uuid=True), primary_key=True)
    workspace_id: Mapped[UUID] = mapped_column(
        PostgreSQLUUID(as_uuid=True), nullable=False
    )
    run_id: Mapped[UUID] = mapped_column(PostgreSQLUUID(as_uuid=True), nullable=False)
    projection_snapshot_id: Mapped[UUID] = mapped_column(
        PostgreSQLUUID(as_uuid=True), nullable=False
    )
    operation_id: Mapped[UUID] = mapped_column(
        PostgreSQLUUID(as_uuid=True), nullable=False
    )
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    attempt: Mapped[int] = mapped_column(Integer, nullable=False)
    lease_owner: Mapped[str | None] = mapped_column(String(255))
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    next_attempt_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_error_code: Mapped[str | None] = mapped_column(String(100))
    response_hash: Mapped[str | None] = mapped_column(String(64))
    diagnostic_summary: Mapped[str | None] = mapped_column(String(16384))
    resource_version: Mapped[int] = mapped_column(Integer, nullable=False)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )


class RuntimeDecisionModel(TimestampMixin, Base):
    __tablename__ = "runtime_decisions"
    __table_args__ = (
        CheckConstraint(
            "schema_version >= 1", name="ck_runtime_decisions_schema_version"
        ),
        CheckConstraint("sequence >= 1", name="ck_runtime_decisions_sequence"),
        CheckConstraint(
            "runtime_duration_ms >= 0", name="ck_runtime_decisions_duration"
        ),
        CheckConstraint(
            "outcome IN ('intents', 'no_change')",
            name="ck_runtime_decisions_outcome",
        ),
        CheckConstraint(
            "projection_hash ~ '^[0-9a-f]{64}$'",
            name="ck_runtime_decisions_projection_hash",
        ),
        CheckConstraint(
            "content_hash ~ '^[0-9a-f]{64}$'",
            name="ck_runtime_decisions_content_hash",
        ),
        CheckConstraint(
            "jsonb_typeof(strategy_state_json) = 'object'",
            name="ck_runtime_decisions_strategy_state_object",
        ),
        ForeignKeyConstraint(
            ["run_id", "workspace_id"],
            ["collaboration_runs.id", "collaboration_runs.workspace_id"],
            ondelete="RESTRICT",
            name="fk_runtime_decisions_run_workspace",
        ),
        ForeignKeyConstraint(
            ["evaluation_id", "workspace_id"],
            [
                "collaboration_evaluations.id",
                "collaboration_evaluations.workspace_id",
            ],
            ondelete="RESTRICT",
            name="fk_runtime_decisions_evaluation_workspace",
        ),
        ForeignKeyConstraint(
            ["projection_snapshot_id", "workspace_id"],
            [
                "runtime_projection_snapshots.id",
                "runtime_projection_snapshots.workspace_id",
            ],
            ondelete="RESTRICT",
            name="fk_runtime_decisions_projection_workspace",
        ),
        UniqueConstraint("id", "workspace_id", name="uq_runtime_decisions_identity"),
        UniqueConstraint(
            "workspace_id", "run_id", "sequence", name="uq_runtime_decisions_sequence"
        ),
        Index(
            "ix_runtime_decisions_run_sequence",
            "workspace_id",
            "run_id",
            "sequence",
        ),
    )

    id: Mapped[UUID] = mapped_column(PostgreSQLUUID(as_uuid=True), primary_key=True)
    workspace_id: Mapped[UUID] = mapped_column(
        PostgreSQLUUID(as_uuid=True), nullable=False
    )
    run_id: Mapped[UUID] = mapped_column(PostgreSQLUUID(as_uuid=True), nullable=False)
    evaluation_id: Mapped[UUID] = mapped_column(
        PostgreSQLUUID(as_uuid=True), nullable=False
    )
    projection_snapshot_id: Mapped[UUID] = mapped_column(
        PostgreSQLUUID(as_uuid=True), nullable=False
    )
    projection_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    operation_id: Mapped[UUID] = mapped_column(
        PostgreSQLUUID(as_uuid=True), nullable=False
    )
    schema_version: Mapped[int] = mapped_column(Integer, nullable=False)
    sequence: Mapped[int] = mapped_column(BigInteger, nullable=False)
    outcome: Mapped[str] = mapped_column(String(32), nullable=False)
    reason_code: Mapped[str] = mapped_column(String(255), nullable=False)
    summary: Mapped[str] = mapped_column(String(16384), nullable=False)
    strategy_state_json: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    runtime_duration_ms: Mapped[int] = mapped_column(BigInteger, nullable=False)


class RuntimeIntentModel(TimestampMixin, Base):
    __tablename__ = "runtime_intents"
    __table_args__ = (
        CheckConstraint("ordinal >= 0", name="ck_runtime_intents_ordinal"),
        CheckConstraint(
            "target_request_version >= 1",
            name="ck_runtime_intents_request_version",
        ),
        CheckConstraint(
            "intent_type = 'release_execution_request'",
            name="ck_runtime_intents_type",
        ),
        CheckConstraint(
            "jsonb_typeof(payload_json) = 'object'",
            name="ck_runtime_intents_payload_object",
        ),
        ForeignKeyConstraint(
            ["run_id", "workspace_id"],
            ["collaboration_runs.id", "collaboration_runs.workspace_id"],
            ondelete="RESTRICT",
            name="fk_runtime_intents_run_workspace",
        ),
        ForeignKeyConstraint(
            ["decision_id", "workspace_id"],
            ["runtime_decisions.id", "runtime_decisions.workspace_id"],
            ondelete="RESTRICT",
            name="fk_runtime_intents_decision_workspace",
        ),
        ForeignKeyConstraint(
            ["target_execution_request_id", "workspace_id"],
            ["execution_requests.id", "execution_requests.workspace_id"],
            ondelete="RESTRICT",
            name="fk_runtime_intents_request_workspace",
        ),
        UniqueConstraint("id", "workspace_id", name="uq_runtime_intents_identity"),
        UniqueConstraint(
            "workspace_id",
            "decision_id",
            "ordinal",
            name="uq_runtime_intents_decision_ordinal",
        ),
        UniqueConstraint(
            "workspace_id",
            "idempotency_key",
            name="uq_runtime_intents_idempotency",
        ),
        Index(
            "ix_runtime_intents_decision_ordinal",
            "workspace_id",
            "decision_id",
            "ordinal",
        ),
    )

    id: Mapped[UUID] = mapped_column(PostgreSQLUUID(as_uuid=True), primary_key=True)
    workspace_id: Mapped[UUID] = mapped_column(
        PostgreSQLUUID(as_uuid=True), nullable=False
    )
    run_id: Mapped[UUID] = mapped_column(PostgreSQLUUID(as_uuid=True), nullable=False)
    decision_id: Mapped[UUID] = mapped_column(
        PostgreSQLUUID(as_uuid=True), nullable=False
    )
    ordinal: Mapped[int] = mapped_column(Integer, nullable=False)
    intent_type: Mapped[str] = mapped_column(String(100), nullable=False)
    target_execution_request_id: Mapped[UUID] = mapped_column(
        PostgreSQLUUID(as_uuid=True), nullable=False
    )
    target_request_version: Mapped[int] = mapped_column(Integer, nullable=False)
    reason_code: Mapped[str] = mapped_column(String(255), nullable=False)
    summary: Mapped[str] = mapped_column(String(16384), nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(255), nullable=False)
    payload_json: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)


class RuntimeReleaseModel(TimestampMixin, Base):
    __tablename__ = "runtime_releases"
    __table_args__ = (
        CheckConstraint(
            "plan_generation >= 1", name="ck_runtime_releases_generation"
        ),
        CheckConstraint(
            "request_version_at_release >= 1",
            name="ck_runtime_releases_request_version",
        ),
        CheckConstraint(
            "plan_content_hash ~ '^[0-9a-f]{64}$'",
            name="ck_runtime_releases_plan_hash",
        ),
        ForeignKeyConstraint(
            ["run_id", "workspace_id"],
            ["collaboration_runs.id", "collaboration_runs.workspace_id"],
            ondelete="RESTRICT",
            name="fk_runtime_releases_run_workspace",
        ),
        ForeignKeyConstraint(
            ["plan_snapshot_id", "workspace_id"],
            ["task_plan_snapshots.id", "task_plan_snapshots.workspace_id"],
            ondelete="RESTRICT",
            name="fk_runtime_releases_plan_snapshot_workspace",
        ),
        ForeignKeyConstraint(
            ["decision_id", "workspace_id"],
            ["runtime_decisions.id", "runtime_decisions.workspace_id"],
            ondelete="RESTRICT",
            name="fk_runtime_releases_decision_workspace",
        ),
        ForeignKeyConstraint(
            ["intent_id", "workspace_id"],
            ["runtime_intents.id", "runtime_intents.workspace_id"],
            ondelete="RESTRICT",
            name="fk_runtime_releases_intent_workspace",
        ),
        ForeignKeyConstraint(
            ["execution_request_id", "workspace_id"],
            ["execution_requests.id", "execution_requests.workspace_id"],
            ondelete="RESTRICT",
            name="fk_runtime_releases_request_workspace",
        ),
        UniqueConstraint("id", "workspace_id", name="uq_runtime_releases_identity"),
        UniqueConstraint(
            "workspace_id",
            "execution_request_id",
            name="uq_runtime_releases_workspace_request",
        ),
        UniqueConstraint("intent_id", name="uq_runtime_releases_intent"),
        Index(
            "ix_runtime_releases_run_issued",
            "workspace_id",
            "run_id",
            "issued_at",
        ),
    )

    id: Mapped[UUID] = mapped_column(PostgreSQLUUID(as_uuid=True), primary_key=True)
    workspace_id: Mapped[UUID] = mapped_column(
        PostgreSQLUUID(as_uuid=True), nullable=False
    )
    run_id: Mapped[UUID] = mapped_column(PostgreSQLUUID(as_uuid=True), nullable=False)
    plan_snapshot_id: Mapped[UUID] = mapped_column(
        PostgreSQLUUID(as_uuid=True), nullable=False
    )
    plan_generation: Mapped[int] = mapped_column(Integer, nullable=False)
    plan_content_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    decision_id: Mapped[UUID] = mapped_column(
        PostgreSQLUUID(as_uuid=True), nullable=False
    )
    intent_id: Mapped[UUID] = mapped_column(
        PostgreSQLUUID(as_uuid=True), nullable=False
    )
    execution_request_id: Mapped[UUID] = mapped_column(
        PostgreSQLUUID(as_uuid=True), nullable=False
    )
    request_version_at_release: Mapped[int] = mapped_column(Integer, nullable=False)
    issued_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )


class RuntimeIntentApplicationModel(TimestampMixin, Base):
    __tablename__ = "runtime_intent_applications"
    __table_args__ = (
        CheckConstraint(
            "status IN ('pending', 'applied', 'rejected', 'stale', 'duplicate')",
            name="ck_runtime_intent_applications_status",
        ),
        CheckConstraint(
            "expected_run_version >= 1 AND observed_run_version >= 1",
            name="ck_runtime_intent_applications_run_versions",
        ),
        CheckConstraint(
            "expected_request_version >= 1 AND "
            "(observed_request_version IS NULL OR observed_request_version >= 1)",
            name="ck_runtime_intent_applications_request_versions",
        ),
        CheckConstraint(
            "resource_version >= 1",
            name="ck_runtime_intent_applications_resource_version",
        ),
        ForeignKeyConstraint(
            ["intent_id", "workspace_id"],
            ["runtime_intents.id", "runtime_intents.workspace_id"],
            ondelete="RESTRICT",
            name="fk_runtime_intent_applications_intent_workspace",
        ),
        ForeignKeyConstraint(
            ["release_id", "workspace_id"],
            ["runtime_releases.id", "runtime_releases.workspace_id"],
            ondelete="RESTRICT",
            name="fk_runtime_intent_applications_release_workspace",
        ),
        UniqueConstraint(
            "id", "workspace_id", name="uq_runtime_intent_applications_identity"
        ),
        UniqueConstraint(
            "intent_id", name="uq_runtime_intent_applications_intent"
        ),
        Index(
            "ix_runtime_intent_applications_status",
            "workspace_id",
            "status",
            "created_at",
        ),
    )

    id: Mapped[UUID] = mapped_column(PostgreSQLUUID(as_uuid=True), primary_key=True)
    workspace_id: Mapped[UUID] = mapped_column(
        PostgreSQLUUID(as_uuid=True), nullable=False
    )
    intent_id: Mapped[UUID] = mapped_column(
        PostgreSQLUUID(as_uuid=True), nullable=False
    )
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    expected_run_version: Mapped[int] = mapped_column(Integer, nullable=False)
    observed_run_version: Mapped[int] = mapped_column(Integer, nullable=False)
    expected_request_version: Mapped[int] = mapped_column(Integer, nullable=False)
    observed_request_version: Mapped[int | None] = mapped_column(Integer)
    release_id: Mapped[UUID | None] = mapped_column(PostgreSQLUUID(as_uuid=True))
    result_code: Mapped[str] = mapped_column(String(255), nullable=False)
    result_summary: Mapped[str] = mapped_column(String(16384), nullable=False)
    resource_version: Mapped[int] = mapped_column(Integer, nullable=False)
    applied_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )


class WorkContextModel(TimestampMixin, Base):
    __tablename__ = "work_contexts"
    __table_args__ = (
        CheckConstraint(
            "status IN ('active', 'archived', 'legacy_incomplete')",
            name="ck_work_contexts_status",
        ),
        CheckConstraint("version >= 1", name="ck_work_contexts_version"),
        CheckConstraint(
            "current_revision_number >= 1",
            name="ck_work_contexts_current_revision_number",
        ),
        ForeignKeyConstraint(
            ["workspace_id"],
            ["workspaces.id"],
            ondelete="RESTRICT",
            name="fk_work_contexts_workspace",
        ),
        ForeignKeyConstraint(
            ["planning_request_id", "workspace_id"],
            ["planning_requests.id", "planning_requests.workspace_id"],
            ondelete="RESTRICT",
            name="fk_work_contexts_planning_request_workspace",
        ),
        ForeignKeyConstraint(
            ["root_task_id", "workspace_id"],
            ["tasks.id", "tasks.workspace_id"],
            ondelete="RESTRICT",
            name="fk_work_contexts_root_task_workspace",
        ),
        ForeignKeyConstraint(
            ["current_revision_id"],
            ["work_context_revisions.id"],
            ondelete="RESTRICT",
            deferrable=True,
            initially="DEFERRED",
            name="fk_work_contexts_current_revision",
        ),
        UniqueConstraint("id", "workspace_id", name="uq_work_contexts_identity"),
        UniqueConstraint(
            "planning_request_id",
            "workspace_id",
            name="uq_work_contexts_planning_request",
        ),
        Index(
            "uq_work_contexts_root_task",
            "root_task_id",
            "workspace_id",
            unique=True,
            postgresql_where=text("root_task_id IS NOT NULL"),
        ),
        Index(
            "ix_work_contexts_workspace_status",
            "workspace_id",
            "status",
        ),
    )

    id: Mapped[UUID] = mapped_column(PostgreSQLUUID(as_uuid=True), primary_key=True)
    workspace_id: Mapped[UUID] = mapped_column(
        PostgreSQLUUID(as_uuid=True), nullable=False
    )
    planning_request_id: Mapped[UUID] = mapped_column(
        PostgreSQLUUID(as_uuid=True), nullable=False
    )
    root_task_id: Mapped[UUID | None] = mapped_column(PostgreSQLUUID(as_uuid=True))
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    current_revision_id: Mapped[UUID] = mapped_column(
        PostgreSQLUUID(as_uuid=True), nullable=False
    )
    current_revision_number: Mapped[int] = mapped_column(Integer, nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )


class WorkContextRevisionModel(Base):
    __tablename__ = "work_context_revisions"
    __table_args__ = (
        CheckConstraint(
            "revision_number >= 1",
            name="ck_work_context_revisions_revision_number",
        ),
        CheckConstraint(
            "brief_revision >= 1",
            name="ck_work_context_revisions_brief_revision",
        ),
        CheckConstraint(
            "jsonb_typeof(snapshot) = 'object'",
            name="ck_work_context_revisions_snapshot_object",
        ),
        CheckConstraint(
            "content_hash ~ '^[0-9a-f]{64}$'",
            name="ck_work_context_revisions_content_hash",
        ),
        CheckConstraint(
            "reason IN ('request_created', 'clarification_accepted', "
            "'proposal_confirmed', 'user_intervention', 'replanned', "
            "'knowledge_attached', 'legacy_backfill')",
            name="ck_work_context_revisions_reason",
        ),
        ForeignKeyConstraint(
            ["work_context_id", "workspace_id"],
            ["work_contexts.id", "work_contexts.workspace_id"],
            ondelete="RESTRICT",
            name="fk_work_context_revisions_context_workspace",
        ),
        ForeignKeyConstraint(
            ["brief_planning_request_id", "workspace_id", "brief_revision"],
            [
                "planning_brief_revisions.planning_request_id",
                "planning_brief_revisions.workspace_id",
                "planning_brief_revisions.revision",
            ],
            ondelete="RESTRICT",
            name="fk_work_context_revisions_brief",
        ),
        ForeignKeyConstraint(
            ["proposal_revision_id", "workspace_id"],
            [
                "planning_proposal_revisions.id",
                "planning_proposal_revisions.workspace_id",
            ],
            ondelete="RESTRICT",
            name="fk_work_context_revisions_proposal",
        ),
        ForeignKeyConstraint(
            ["plan_snapshot_id", "workspace_id"],
            ["task_plan_snapshots.id", "task_plan_snapshots.workspace_id"],
            ondelete="RESTRICT",
            name="fk_work_context_revisions_plan_snapshot",
        ),
        UniqueConstraint(
            "work_context_id",
            "revision_number",
            name="uq_work_context_revisions_context_revision",
        ),
        UniqueConstraint(
            "id", "workspace_id", name="uq_work_context_revisions_identity"
        ),
        Index(
            "ix_work_context_revisions_workspace_context_number",
            "workspace_id",
            "work_context_id",
            "revision_number",
        ),
    )

    id: Mapped[UUID] = mapped_column(PostgreSQLUUID(as_uuid=True), primary_key=True)
    work_context_id: Mapped[UUID] = mapped_column(
        PostgreSQLUUID(as_uuid=True), nullable=False
    )
    workspace_id: Mapped[UUID] = mapped_column(
        PostgreSQLUUID(as_uuid=True), nullable=False
    )
    revision_number: Mapped[int] = mapped_column(Integer, nullable=False)
    brief_planning_request_id: Mapped[UUID] = mapped_column(
        PostgreSQLUUID(as_uuid=True), nullable=False
    )
    brief_revision: Mapped[int] = mapped_column(Integer, nullable=False)
    proposal_revision_id: Mapped[UUID | None] = mapped_column(
        PostgreSQLUUID(as_uuid=True)
    )
    plan_snapshot_id: Mapped[UUID | None] = mapped_column(PostgreSQLUUID(as_uuid=True))
    reason: Mapped[str] = mapped_column(String(32), nullable=False)
    snapshot: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    created_by: Mapped[str] = mapped_column(String(255), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class ContextAssemblyRequestModel(TimestampMixin, Base):
    __tablename__ = "context_assembly_requests"
    __table_args__ = (
        CheckConstraint(
            "purpose IN ('planning_run', 'execution')",
            name="ck_context_assembly_requests_purpose",
        ),
        CheckConstraint(
            "consumer_type IN ('planner', 'worker')",
            name="ck_context_assembly_requests_consumer_type",
        ),
        CheckConstraint(
            "(purpose = 'planning_run' AND consumer_type = 'planner' AND "
            "worker_item_id IS NULL AND context_version IS NULL) OR "
            "(purpose = 'execution' AND consumer_type = 'worker' AND "
            "worker_item_id IS NOT NULL AND context_version >= 1)",
            name="ck_context_assembly_requests_consumer_fields",
        ),
        CheckConstraint(
            "status IN ('queued', 'assembling', 'ready', 'blocked', 'retry_wait', 'failed')",
            name="ck_context_assembly_requests_status",
        ),
        CheckConstraint(
            "jsonb_typeof(knowledge_search_scope) = 'object'",
            name="ck_context_assembly_requests_search_scope_object",
        ),
        CheckConstraint(
            "jsonb_typeof(knowledge_filter_refs) = 'array'",
            name="ck_context_assembly_requests_filter_refs_array",
        ),
        CheckConstraint(
            "jsonb_typeof(fact_scope_refs) = 'array'",
            name="ck_context_assembly_requests_fact_scope_refs_array",
        ),
        CheckConstraint(
            "jsonb_typeof(budget) = 'object'",
            name="ck_context_assembly_requests_budget_object",
        ),
        CheckConstraint(
            "attempt >= 1",
            name="ck_context_assembly_requests_attempt",
        ),
        CheckConstraint(
            "request_fingerprint ~ '^[0-9a-f]{64}$'",
            name="ck_context_assembly_requests_fingerprint",
        ),
        CheckConstraint(
            "version >= 1",
            name="ck_context_assembly_requests_version",
        ),
        ForeignKeyConstraint(
            ["workspace_id"],
            ["workspaces.id"],
            ondelete="RESTRICT",
            name="fk_context_assembly_requests_workspace",
        ),
        ForeignKeyConstraint(
            ["worker_item_id", "workspace_id"],
            ["worker_items.id", "worker_items.workspace_id"],
            ondelete="RESTRICT",
            name="fk_context_assembly_requests_worker_item",
        ),
        ForeignKeyConstraint(
            ["work_context_revision_id", "workspace_id"],
            ["work_context_revisions.id", "work_context_revisions.workspace_id"],
            ondelete="RESTRICT",
            name="fk_context_assembly_requests_context_revision",
        ),
        UniqueConstraint(
            "id", "workspace_id", name="uq_context_assembly_requests_identity"
        ),
        UniqueConstraint(
            "purpose",
            "consumer_id",
            name="uq_context_assembly_requests_purpose_consumer",
        ),
        Index(
            "ix_context_assembly_requests_claimable",
            "status",
            "next_attempt_at",
        ),
    )

    id: Mapped[UUID] = mapped_column(PostgreSQLUUID(as_uuid=True), primary_key=True)
    workspace_id: Mapped[UUID] = mapped_column(
        PostgreSQLUUID(as_uuid=True), nullable=False
    )
    purpose: Mapped[str] = mapped_column(String(32), nullable=False)
    consumer_type: Mapped[str] = mapped_column(String(32), nullable=False)
    consumer_id: Mapped[UUID] = mapped_column(
        PostgreSQLUUID(as_uuid=True), nullable=False
    )
    worker_item_id: Mapped[UUID | None] = mapped_column(PostgreSQLUUID(as_uuid=True))
    work_context_revision_id: Mapped[UUID] = mapped_column(
        PostgreSQLUUID(as_uuid=True), nullable=False
    )
    context_version: Mapped[int | None] = mapped_column(Integer)
    knowledge_search_scope: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False
    )
    knowledge_filter_refs: Mapped[list[Any]] = mapped_column(JSONB, nullable=False)
    fact_scope_refs: Mapped[list[Any]] = mapped_column(JSONB, nullable=False)
    budget: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    expires_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    lease_owner: Mapped[str | None] = mapped_column(String(255))
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    attempt: Mapped[int] = mapped_column(Integer, nullable=False)
    next_attempt_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    error_code: Mapped[str | None] = mapped_column(String(100))
    request_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(255), nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )


class ContextPackageModel(TimestampMixin, Base):
    __tablename__ = "context_packages"
    __table_args__ = (
        CheckConstraint(
            "purpose IN ('planning_run', 'execution')",
            name="ck_context_packages_purpose",
        ),
        CheckConstraint(
            "consumer_type IN ('planner', 'worker')",
            name="ck_context_packages_consumer_type",
        ),
        CheckConstraint(
            "(purpose = 'planning_run' AND consumer_type = 'planner' AND "
            "worker_item_id IS NULL AND context_version IS NULL) OR "
            "(purpose = 'execution' AND consumer_type = 'worker' AND "
            "worker_item_id IS NOT NULL AND context_version >= 1)",
            name="ck_context_packages_consumer_fields",
        ),
        CheckConstraint(
            "jsonb_typeof(control_plane_fact_snapshot) = 'object'",
            name="ck_context_packages_fact_snapshot_object",
        ),
        CheckConstraint(
            "jsonb_typeof(artifact_refs) = 'array' AND "
            "jsonb_typeof(evidence_refs) = 'array'",
            name="ck_context_packages_refs_array",
        ),
        CheckConstraint(
            "jsonb_typeof(budget) = 'object'",
            name="ck_context_packages_budget_object",
        ),
        CheckConstraint(
            "package_hash ~ '^[0-9a-f]{64}$'",
            name="ck_context_packages_manifest_hash",
        ),
        CheckConstraint(
            "status IN ('active', 'revoked', 'corrupt')",
            name="ck_context_packages_status",
        ),
        CheckConstraint(
            "revocation_scope IS NULL OR revocation_scope IN ('new_starts', 'all_materialization')",
            name="ck_context_packages_revocation_scope",
        ),
        ForeignKeyConstraint(
            ["workspace_id"],
            ["workspaces.id"],
            ondelete="RESTRICT",
            name="fk_context_packages_workspace",
        ),
        ForeignKeyConstraint(
            ["assembly_request_id", "workspace_id"],
            [
                "context_assembly_requests.id",
                "context_assembly_requests.workspace_id",
            ],
            ondelete="RESTRICT",
            name="fk_context_packages_assembly_request",
        ),
        ForeignKeyConstraint(
            ["worker_item_id", "workspace_id"],
            ["worker_items.id", "worker_items.workspace_id"],
            ondelete="RESTRICT",
            name="fk_context_packages_worker_item",
        ),
        ForeignKeyConstraint(
            ["work_context_revision_id", "workspace_id"],
            ["work_context_revisions.id", "work_context_revisions.workspace_id"],
            ondelete="RESTRICT",
            name="fk_context_packages_context_revision",
        ),
        UniqueConstraint("id", "workspace_id", name="uq_context_packages_identity"),
        UniqueConstraint(
            "assembly_request_id",
            name="uq_context_packages_assembly_request",
        ),
        UniqueConstraint(
            "purpose",
            "consumer_id",
            name="uq_context_packages_purpose_consumer",
        ),
        Index(
            "ix_context_packages_workspace_status_expiry",
            "workspace_id",
            "status",
            "expires_at",
        ),
    )

    id: Mapped[UUID] = mapped_column(PostgreSQLUUID(as_uuid=True), primary_key=True)
    workspace_id: Mapped[UUID] = mapped_column(
        PostgreSQLUUID(as_uuid=True), nullable=False
    )
    assembly_request_id: Mapped[UUID] = mapped_column(
        PostgreSQLUUID(as_uuid=True), nullable=False
    )
    purpose: Mapped[str] = mapped_column(String(32), nullable=False)
    consumer_type: Mapped[str] = mapped_column(String(32), nullable=False)
    consumer_id: Mapped[UUID] = mapped_column(
        PostgreSQLUUID(as_uuid=True), nullable=False
    )
    worker_item_id: Mapped[UUID | None] = mapped_column(PostgreSQLUUID(as_uuid=True))
    work_context_revision_id: Mapped[UUID] = mapped_column(
        PostgreSQLUUID(as_uuid=True), nullable=False
    )
    context_version: Mapped[int | None] = mapped_column(Integer)
    control_plane_fact_snapshot: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False
    )
    artifact_refs: Mapped[list[Any]] = mapped_column(JSONB, nullable=False)
    evidence_refs: Mapped[list[Any]] = mapped_column(JSONB, nullable=False)
    budget: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    expires_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    package_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    revocation_reason: Mapped[str | None] = mapped_column(String(100))
    revocation_scope: Mapped[str | None] = mapped_column(String(32))
    idempotency_key: Mapped[str] = mapped_column(String(255), nullable=False)
    materialized_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )


class ContextPackageKnowledgeRefModel(Base):
    __tablename__ = "context_package_knowledge_refs"
    __table_args__ = (
        CheckConstraint(
            "ordinal >= 0",
            name="ck_context_package_knowledge_refs_ordinal",
        ),
        CheckConstraint(
            "revision_hash ~ '^[0-9a-f]{64}$' AND section_hash ~ '^[0-9a-f]{64}$'",
            name="ck_context_package_knowledge_refs_hashes",
        ),
        CheckConstraint(
            "jsonb_typeof(source_ids) = 'array'",
            name="ck_context_package_knowledge_refs_source_ids_array",
        ),
        CheckConstraint(
            "selection_reason IN ('explicit', 'work_context', 'retrieval')",
            name="ck_context_package_knowledge_refs_selection_reason",
        ),
        CheckConstraint(
            "trust_level IN ('workspace_confirmed', 'external_reference')",
            name="ck_context_package_knowledge_refs_trust_level",
        ),
        ForeignKeyConstraint(
            ["package_id"],
            ["context_packages.id"],
            ondelete="RESTRICT",
            name="fk_context_package_knowledge_refs_package",
        ),
        PrimaryKeyConstraint("package_id", "ordinal"),
    )

    package_id: Mapped[UUID] = mapped_column(
        PostgreSQLUUID(as_uuid=True), nullable=False
    )
    ordinal: Mapped[int] = mapped_column(Integer, nullable=False)
    page_id: Mapped[UUID] = mapped_column(PostgreSQLUUID(as_uuid=True), nullable=False)
    revision_id: Mapped[UUID] = mapped_column(
        PostgreSQLUUID(as_uuid=True), nullable=False
    )
    revision_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    section_key: Mapped[str] = mapped_column(String(500), nullable=False)
    section_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    source_ids: Mapped[list[Any]] = mapped_column(JSONB, nullable=False)
    selection_reason: Mapped[str] = mapped_column(String(32), nullable=False)
    trust_level: Mapped[str] = mapped_column(String(32), nullable=False)


class ContextPackageAcknowledgementModel(Base):
    __tablename__ = "context_package_acknowledgements"
    __table_args__ = (
        CheckConstraint(
            "consumer_type IN ('planner', 'worker')",
            name="ck_context_package_acknowledgements_consumer_type",
        ),
        CheckConstraint(
            "(consumer_type = 'planner' AND context_version IS NULL) OR "
            "(consumer_type = 'worker' AND context_version >= 1)",
            name="ck_context_package_acknowledgements_context_version",
        ),
        CheckConstraint(
            "acknowledged_package_hash ~ '^[0-9a-f]{64}$'",
            name="ck_context_package_acknowledgements_package_hash",
        ),
        CheckConstraint(
            "adapter_config_version >= 1",
            name="ck_context_package_acknowledgements_adapter_version",
        ),
        ForeignKeyConstraint(
            ["package_id", "workspace_id"],
            ["context_packages.id", "context_packages.workspace_id"],
            ondelete="RESTRICT",
            name="fk_context_package_acknowledgements_package",
        ),
        ForeignKeyConstraint(
            ["adapter_module_instance_id", "workspace_id"],
            ["module_instances.id", "module_instances.workspace_id"],
            ondelete="RESTRICT",
            name="fk_context_package_acknowledgements_adapter_module",
        ),
        PrimaryKeyConstraint("package_id", "consumer_type", "consumer_id"),
    )

    package_id: Mapped[UUID] = mapped_column(
        PostgreSQLUUID(as_uuid=True), nullable=False
    )
    workspace_id: Mapped[UUID] = mapped_column(
        PostgreSQLUUID(as_uuid=True), nullable=False
    )
    consumer_type: Mapped[str] = mapped_column(String(32), nullable=False)
    consumer_id: Mapped[UUID] = mapped_column(
        PostgreSQLUUID(as_uuid=True), nullable=False
    )
    context_version: Mapped[int | None] = mapped_column(Integer)
    acknowledged_package_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    adapter_module_instance_id: Mapped[UUID] = mapped_column(
        PostgreSQLUUID(as_uuid=True), nullable=False
    )
    adapter_config_version: Mapped[int] = mapped_column(Integer, nullable=False)
    acknowledged_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class ContextPackageMaterializationAuditModel(Base):
    __tablename__ = "context_package_materialization_audits"
    __table_args__ = (
        CheckConstraint(
            "consumer_type IN ('planner', 'worker')",
            name="ck_context_package_materialization_audits_consumer_type",
        ),
        CheckConstraint(
            "(consumer_type = 'planner' AND context_version IS NULL) OR "
            "(consumer_type = 'worker' AND context_version >= 1)",
            name="ck_context_package_materialization_audits_context_version",
        ),
        CheckConstraint(
            "attempt >= 1",
            name="ck_context_package_materialization_audits_attempt",
        ),
        CheckConstraint(
            "outcome IN ('succeeded', 'failed')",
            name="ck_context_package_materialization_audits_outcome",
        ),
        CheckConstraint(
            "section_count >= 0 AND byte_count >= 0 AND input_token_count >= 0",
            name="ck_context_package_materialization_audits_counts",
        ),
        ForeignKeyConstraint(
            ["package_id", "workspace_id"],
            ["context_packages.id", "context_packages.workspace_id"],
            ondelete="RESTRICT",
            name="fk_context_package_materialization_audits_package",
        ),
        UniqueConstraint(
            "package_id",
            "consumer_type",
            "consumer_id",
            "attempt",
            name="uq_context_package_materialization_audits_attempt",
        ),
        Index(
            "ix_context_package_materialization_audits_package_occurred",
            "package_id",
            "occurred_at",
        ),
    )

    id: Mapped[UUID] = mapped_column(PostgreSQLUUID(as_uuid=True), primary_key=True)
    package_id: Mapped[UUID] = mapped_column(
        PostgreSQLUUID(as_uuid=True), nullable=False
    )
    workspace_id: Mapped[UUID] = mapped_column(
        PostgreSQLUUID(as_uuid=True), nullable=False
    )
    consumer_type: Mapped[str] = mapped_column(String(32), nullable=False)
    consumer_id: Mapped[UUID] = mapped_column(
        PostgreSQLUUID(as_uuid=True), nullable=False
    )
    context_version: Mapped[int | None] = mapped_column(Integer)
    attempt: Mapped[int] = mapped_column(Integer, nullable=False)
    outcome: Mapped[str] = mapped_column(String(32), nullable=False)
    section_count: Mapped[int] = mapped_column(Integer, nullable=False)
    byte_count: Mapped[int] = mapped_column(Integer, nullable=False)
    input_token_count: Mapped[int] = mapped_column(Integer, nullable=False)
    failure_code: Mapped[str | None] = mapped_column(String(100))
    occurred_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )


class KnowledgeCandidateSubmissionModel(TimestampMixin, Base):
    __tablename__ = "knowledge_candidate_submissions"
    __table_args__ = (
        CheckConstraint(
            "status IN ('waiting_for_task', 'queued', 'submitting', 'retry_wait', "
            "'submitted', 'rejected', 'discarded')",
            name="ck_knowledge_candidate_submissions_status",
        ),
        CheckConstraint(
            "proposal_hash ~ '^[0-9a-f]{64}$'",
            name="ck_knowledge_candidate_submissions_proposal_hash",
        ),
        CheckConstraint(
            "jsonb_typeof(artifact_refs) = 'array'",
            name="ck_knowledge_candidate_submissions_artifacts_array",
        ),
        CheckConstraint(
            "jsonb_typeof(evidence_refs) = 'array'",
            name="ck_knowledge_candidate_submissions_evidence_array",
        ),
        CheckConstraint(
            "content_bytes >= 0",
            name="ck_knowledge_candidate_submissions_content_bytes",
        ),
        CheckConstraint(
            "attempt >= 1",
            name="ck_knowledge_candidate_submissions_attempt",
        ),
        CheckConstraint(
            "version >= 1",
            name="ck_knowledge_candidate_submissions_version",
        ),
        ForeignKeyConstraint(
            ["workspace_id"],
            ["workspaces.id"],
            ondelete="RESTRICT",
            name="fk_knowledge_candidate_submissions_workspace",
        ),
        ForeignKeyConstraint(
            ["planning_run_id", "workspace_id"],
            ["planning_runs.id", "planning_runs.workspace_id"],
            ondelete="RESTRICT",
            name="fk_knowledge_candidate_submissions_planning_run",
        ),
        ForeignKeyConstraint(
            ["execution_id", "workspace_id"],
            ["executions.id", "executions.workspace_id"],
            ondelete="RESTRICT",
            name="fk_knowledge_candidate_submissions_execution",
        ),
        ForeignKeyConstraint(
            ["worker_item_id", "workspace_id"],
            ["worker_items.id", "worker_items.workspace_id"],
            ondelete="RESTRICT",
            name="fk_knowledge_candidate_submissions_worker_item",
        ),
        ForeignKeyConstraint(
            ["task_id", "workspace_id"],
            ["tasks.id", "tasks.workspace_id"],
            ondelete="RESTRICT",
            name="fk_knowledge_candidate_submissions_task",
        ),
        ForeignKeyConstraint(
            ["subtask_task_id", "workspace_id"],
            ["tasks.id", "tasks.workspace_id"],
            ondelete="RESTRICT",
            name="fk_knowledge_candidate_submissions_subtask_task",
        ),
        ForeignKeyConstraint(
            ["knowledge_command_id"],
            ["commands.id"],
            ondelete="RESTRICT",
            name="fk_knowledge_candidate_submissions_command",
        ),
        UniqueConstraint(
            "producer_type",
            "producer_id",
            "proposal_hash",
            name="uq_knowledge_candidate_submissions_producer_hash",
        ),
        Index(
            "ix_knowledge_candidate_submissions_claimable",
            "status",
            "next_attempt_at",
        ),
        Index(
            "ix_knowledge_candidate_submissions_workspace_producer",
            "workspace_id",
            "producer_type",
            "producer_id",
        ),
    )

    id: Mapped[UUID] = mapped_column(PostgreSQLUUID(as_uuid=True), primary_key=True)
    workspace_id: Mapped[UUID] = mapped_column(
        PostgreSQLUUID(as_uuid=True), nullable=False
    )
    producer_type: Mapped[str] = mapped_column(String(64), nullable=False)
    producer_id: Mapped[UUID] = mapped_column(
        PostgreSQLUUID(as_uuid=True), nullable=False
    )
    planning_run_id: Mapped[UUID | None] = mapped_column(PostgreSQLUUID(as_uuid=True))
    execution_id: Mapped[UUID | None] = mapped_column(PostgreSQLUUID(as_uuid=True))
    worker_item_id: Mapped[UUID | None] = mapped_column(PostgreSQLUUID(as_uuid=True))
    task_id: Mapped[UUID | None] = mapped_column(PostgreSQLUUID(as_uuid=True))
    subtask_task_id: Mapped[UUID | None] = mapped_column(PostgreSQLUUID(as_uuid=True))
    artifact_refs: Mapped[list[Any]] = mapped_column(JSONB, nullable=False)
    evidence_refs: Mapped[list[Any]] = mapped_column(JSONB, nullable=False)
    proposal_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    content_ref: Mapped[str] = mapped_column(String(1000), nullable=False)
    content_bytes: Mapped[int] = mapped_column(Integer, nullable=False)
    knowledge_command_id: Mapped[UUID | None] = mapped_column(
        PostgreSQLUUID(as_uuid=True)
    )
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    attempt: Mapped[int] = mapped_column(Integer, nullable=False)
    lease_owner: Mapped[str | None] = mapped_column(String(255))
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    next_attempt_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    terminal_candidate_id: Mapped[UUID | None] = mapped_column(
        PostgreSQLUUID(as_uuid=True)
    )
    error_code: Mapped[str | None] = mapped_column(String(100))
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )


class HandoffProposalModel(TimestampMixin, Base):
    __tablename__ = "handoff_proposals"
    __table_args__ = (
        CheckConstraint(
            "proposal_schema_version >= 1",
            name="ck_handoff_proposals_schema_version",
        ),
        CheckConstraint(
            "source_execution_version >= 1 AND source_session_version >= 1 "
            "AND source_worker_version >= 1 "
            "AND source_workspace_manifest_version >= 1 "
            "AND source_worker_context_version >= 1 "
            "AND observed_plan_generation >= 1 "
            "AND observed_plan_head_version >= 1 "
            "AND target_worker_version >= 1",
            name="ck_handoff_proposals_positive_versions",
        ),
        CheckConstraint(
            "source_session_last_sequence >= 0",
            name="ck_handoff_proposals_source_sequence",
        ),
        CheckConstraint(
            "transfer_mode = 'reuse_exact'",
            name="ck_handoff_proposals_transfer_mode",
        ),
        CheckConstraint(
            "status IN ('prepared', 'confirmed', 'stale', 'cancelled', 'expired')",
            name="ck_handoff_proposals_status",
        ),
        CheckConstraint(
            "source_channel = 'web'",
            name="ck_handoff_proposals_source_channel",
        ),
        CheckConstraint(
            "proposal_hash ~ '^[0-9a-f]{64}$' AND "
            "source_context_package_hash ~ '^[0-9a-f]{64}$' AND "
            "target_capability_snapshot_hash ~ '^[0-9a-f]{64}$'",
            name="ck_handoff_proposals_hashes",
        ),
        CheckConstraint(
            "jsonb_typeof(payload) = 'object' AND "
            "jsonb_typeof(source_runtime_snapshot) = 'object' AND "
            "jsonb_typeof(target_runtime_snapshot) = 'object' AND "
            "jsonb_typeof(target_policy_reason) = 'object' AND "
            "jsonb_typeof(target_policy_facts) = 'object' AND "
            "jsonb_typeof(input_plan) = 'object' AND "
            "jsonb_typeof(impact_summary) = 'object'",
            name="ck_handoff_proposals_json_objects",
        ),
        CheckConstraint(
            "version >= 1",
            name="ck_handoff_proposals_version",
        ),
        ForeignKeyConstraint(
            ["workspace_id"],
            ["workspaces.id"],
            ondelete="RESTRICT",
            name="fk_handoff_proposals_workspace",
        ),
        ForeignKeyConstraint(
            ["root_task_id", "workspace_id"],
            ["tasks.id", "tasks.workspace_id"],
            ondelete="RESTRICT",
            name="fk_handoff_proposals_root_task_workspace",
        ),
        ForeignKeyConstraint(
            ["subtask_id", "workspace_id"],
            ["tasks.id", "tasks.workspace_id"],
            ondelete="RESTRICT",
            name="fk_handoff_proposals_subtask_workspace",
        ),
        ForeignKeyConstraint(
            ["worker_item_id", "workspace_id"],
            ["worker_items.id", "worker_items.workspace_id"],
            ondelete="RESTRICT",
            name="fk_handoff_proposals_worker_item_workspace",
        ),
        ForeignKeyConstraint(
            ["source_execution_id", "workspace_id"],
            ["executions.id", "executions.workspace_id"],
            ondelete="RESTRICT",
            name="fk_handoff_proposals_source_execution_workspace",
        ),
        ForeignKeyConstraint(
            ["source_session_id", "workspace_id"],
            ["execution_sessions.id", "execution_sessions.workspace_id"],
            ondelete="RESTRICT",
            name="fk_handoff_proposals_source_session_workspace",
        ),
        ForeignKeyConstraint(
            ["source_worker_id", "workspace_id"],
            ["workers.id", "workers.workspace_id"],
            ondelete="RESTRICT",
            name="fk_handoff_proposals_source_worker_workspace",
        ),
        ForeignKeyConstraint(
            ["source_workspace_manifest_id", "workspace_id"],
            ["execution_workspaces.id", "execution_workspaces.workspace_id"],
            ondelete="RESTRICT",
            name="fk_handoff_proposals_source_manifest_workspace",
        ),
        ForeignKeyConstraint(
            ["source_context_package_id", "workspace_id"],
            ["context_packages.id", "context_packages.workspace_id"],
            ondelete="RESTRICT",
            name="fk_handoff_proposals_source_package_workspace",
        ),
        ForeignKeyConstraint(
            ["observed_plan_snapshot_id", "workspace_id"],
            ["task_plan_snapshots.id", "task_plan_snapshots.workspace_id"],
            ondelete="RESTRICT",
            name="fk_handoff_proposals_plan_snapshot_workspace",
        ),
        ForeignKeyConstraint(
            ["target_worker_id", "workspace_id"],
            ["workers.id", "workers.workspace_id"],
            ondelete="RESTRICT",
            name="fk_handoff_proposals_target_worker_workspace",
        ),
        UniqueConstraint("id", "workspace_id", name="uq_handoff_proposals_identity"),
        Index(
            "uq_handoff_proposals_prepared_worker_item",
            "workspace_id",
            "worker_item_id",
            unique=True,
            postgresql_where=text("status = 'prepared'"),
        ),
        Index(
            "ix_handoff_proposals_workspace_status",
            "workspace_id",
            "status",
            "expires_at",
        ),
    )

    id: Mapped[UUID] = mapped_column(PostgreSQLUUID(as_uuid=True), primary_key=True)
    workspace_id: Mapped[UUID] = mapped_column(
        PostgreSQLUUID(as_uuid=True), nullable=False
    )
    root_task_id: Mapped[UUID] = mapped_column(
        PostgreSQLUUID(as_uuid=True), nullable=False
    )
    subtask_id: Mapped[UUID] = mapped_column(
        PostgreSQLUUID(as_uuid=True), nullable=False
    )
    worker_item_id: Mapped[UUID] = mapped_column(
        PostgreSQLUUID(as_uuid=True), nullable=False
    )
    source_execution_id: Mapped[UUID] = mapped_column(
        PostgreSQLUUID(as_uuid=True), nullable=False
    )
    source_execution_version: Mapped[int] = mapped_column(Integer, nullable=False)
    source_session_id: Mapped[UUID] = mapped_column(
        PostgreSQLUUID(as_uuid=True), nullable=False
    )
    source_session_version: Mapped[int] = mapped_column(Integer, nullable=False)
    source_session_last_sequence: Mapped[int] = mapped_column(
        BigInteger, nullable=False
    )
    source_worker_id: Mapped[UUID] = mapped_column(
        PostgreSQLUUID(as_uuid=True), nullable=False
    )
    source_worker_version: Mapped[int] = mapped_column(Integer, nullable=False)
    source_runtime_snapshot: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False
    )
    source_workspace_manifest_id: Mapped[UUID] = mapped_column(
        PostgreSQLUUID(as_uuid=True), nullable=False
    )
    source_workspace_manifest_version: Mapped[int] = mapped_column(
        Integer, nullable=False
    )
    source_context_package_id: Mapped[UUID] = mapped_column(
        PostgreSQLUUID(as_uuid=True), nullable=False
    )
    source_context_package_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    source_worker_context_version: Mapped[int] = mapped_column(Integer, nullable=False)
    observed_plan_snapshot_id: Mapped[UUID] = mapped_column(
        PostgreSQLUUID(as_uuid=True), nullable=False
    )
    observed_plan_generation: Mapped[int] = mapped_column(Integer, nullable=False)
    observed_plan_head_version: Mapped[int] = mapped_column(Integer, nullable=False)
    target_worker_id: Mapped[UUID] = mapped_column(
        PostgreSQLUUID(as_uuid=True), nullable=False
    )
    target_worker_version: Mapped[int] = mapped_column(Integer, nullable=False)
    target_runtime_snapshot: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False
    )
    target_capability_snapshot_hash: Mapped[str] = mapped_column(
        String(64), nullable=False
    )
    target_policy_reason: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    target_policy_facts: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    transfer_mode: Mapped[str] = mapped_column(String(32), nullable=False)
    handoff_reason: Mapped[str] = mapped_column(Text, nullable=False)
    handoff_instruction: Mapped[str] = mapped_column(Text, nullable=False)
    input_plan: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    impact_summary: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    proposal_schema_version: Mapped[int] = mapped_column(Integer, nullable=False)
    proposal_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    stale_reason: Mapped[str | None] = mapped_column(String(255))
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    created_by_type: Mapped[str] = mapped_column(String(50), nullable=False)
    created_by_id: Mapped[str] = mapped_column(String(255), nullable=False)
    source_channel: Mapped[str] = mapped_column(String(32), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    confirmed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    cancelled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )


class HandoffTransitionModel(TimestampMixin, Base):
    __tablename__ = "handoff_transitions"
    __table_args__ = (
        CheckConstraint(
            "status IN ('pending', 'running', 'blocked', 'blocked_unknown', 'failed', 'converged')",
            name="ck_handoff_transitions_status",
        ),
        CheckConstraint(
            "phase IN ('checkpoint_source', 'stop_source', 'freeze_source_workspace', "
            "'create_target_execution', 'assemble_bundle', 'transfer_workspace', "
            "'start_target_session', 'acknowledge_context', 'transfer_safe_inputs', "
            "'activate_target', 'complete')",
            name="ck_handoff_transitions_phase",
        ),
        CheckConstraint(
            "writer_state IN ('source_active', 'source_stop_requested', 'writer_none', "
            "'target_starting', 'target_active', 'unknown')",
            name="ck_handoff_transitions_writer_state",
        ),
        CheckConstraint(
            "target_context_version >= 1 AND source_input_cutoff_sequence >= 0 "
            "AND logical_attempt >= 1 AND version >= 1",
            name="ck_handoff_transitions_positive_fields",
        ),
        CheckConstraint(
            "(source_checkpoint_sequence IS NULL OR source_checkpoint_sequence >= 0) AND "
            "(source_stop_sequence IS NULL OR source_stop_sequence >= 0)",
            name="ck_handoff_transitions_source_sequences",
        ),
        CheckConstraint(
            "confirmed_source_channel = 'web'",
            name="ck_handoff_transitions_source_channel",
        ),
        CheckConstraint(
            "jsonb_typeof(target_runtime_snapshot) = 'object' AND "
            "(source_checkpoint_facts IS NULL OR jsonb_typeof(source_checkpoint_facts) = 'object') AND "
            "(source_stop_facts IS NULL OR jsonb_typeof(source_stop_facts) = 'object') AND "
            "(source_workspace_freeze_facts IS NULL OR jsonb_typeof(source_workspace_freeze_facts) = 'object')",
            name="ck_handoff_transitions_json_objects",
        ),
        ForeignKeyConstraint(
            ["workspace_id"],
            ["workspaces.id"],
            ondelete="RESTRICT",
            name="fk_handoff_transitions_workspace",
        ),
        ForeignKeyConstraint(
            ["worker_item_id", "workspace_id"],
            ["worker_items.id", "worker_items.workspace_id"],
            ondelete="RESTRICT",
            name="fk_handoff_transitions_worker_item_workspace",
        ),
        ForeignKeyConstraint(
            ["proposal_id", "workspace_id"],
            ["handoff_proposals.id", "handoff_proposals.workspace_id"],
            ondelete="RESTRICT",
            name="fk_handoff_transitions_proposal_workspace",
        ),
        ForeignKeyConstraint(
            ["source_execution_id", "workspace_id"],
            ["executions.id", "executions.workspace_id"],
            ondelete="RESTRICT",
            name="fk_handoff_transitions_source_execution_workspace",
        ),
        ForeignKeyConstraint(
            ["source_session_id", "workspace_id"],
            ["execution_sessions.id", "execution_sessions.workspace_id"],
            ondelete="RESTRICT",
            name="fk_handoff_transitions_source_session_workspace",
        ),
        ForeignKeyConstraint(
            ["source_workspace_manifest_id", "workspace_id"],
            ["execution_workspaces.id", "execution_workspaces.workspace_id"],
            ondelete="RESTRICT",
            name="fk_handoff_transitions_source_manifest_workspace",
        ),
        ForeignKeyConstraint(
            ["target_worker_id", "workspace_id"],
            ["workers.id", "workers.workspace_id"],
            ondelete="RESTRICT",
            name="fk_handoff_transitions_target_worker_workspace",
        ),
        ForeignKeyConstraint(
            ["target_execution_id", "workspace_id"],
            ["executions.id", "executions.workspace_id"],
            ondelete="RESTRICT",
            name="fk_handoff_transitions_target_execution_workspace",
        ),
        ForeignKeyConstraint(
            ["target_session_id", "workspace_id"],
            ["execution_sessions.id", "execution_sessions.workspace_id"],
            ondelete="RESTRICT",
            name="fk_handoff_transitions_target_session_workspace",
        ),
        ForeignKeyConstraint(
            ["target_workspace_manifest_id", "workspace_id"],
            ["execution_workspaces.id", "execution_workspaces.workspace_id"],
            ondelete="RESTRICT",
            name="fk_handoff_transitions_target_manifest_workspace",
        ),
        ForeignKeyConstraint(
            ["handoff_context_bundle_id", "workspace_id"],
            ["handoff_context_bundles.id", "handoff_context_bundles.workspace_id"],
            ondelete="RESTRICT",
            deferrable=True,
            initially="DEFERRED",
            name="fk_handoff_transitions_context_bundle_workspace",
        ),
        ForeignKeyConstraint(
            ["target_context_package_id", "workspace_id"],
            ["context_packages.id", "context_packages.workspace_id"],
            ondelete="RESTRICT",
            name="fk_handoff_transitions_target_package_workspace",
        ),
        UniqueConstraint("id", "workspace_id", name="uq_handoff_transitions_identity"),
        UniqueConstraint("proposal_id", name="uq_handoff_transitions_proposal"),
        Index(
            "uq_handoff_transitions_active_worker_item",
            "workspace_id",
            "worker_item_id",
            unique=True,
            postgresql_where=text(
                "status IN ('pending', 'running', 'blocked', 'blocked_unknown', 'failed')"
            ),
        ),
        Index(
            "uq_handoff_transitions_active_source_session",
            "workspace_id",
            "source_session_id",
            unique=True,
            postgresql_where=text(
                "status IN ('pending', 'running', 'blocked', 'blocked_unknown', 'failed')"
            ),
        ),
        Index(
            "ix_handoff_transitions_coordinator",
            "workspace_id",
            "status",
            "next_attempt_at",
        ),
    )

    id: Mapped[UUID] = mapped_column(PostgreSQLUUID(as_uuid=True), primary_key=True)
    workspace_id: Mapped[UUID] = mapped_column(
        PostgreSQLUUID(as_uuid=True), nullable=False
    )
    worker_item_id: Mapped[UUID] = mapped_column(
        PostgreSQLUUID(as_uuid=True), nullable=False
    )
    proposal_id: Mapped[UUID] = mapped_column(
        PostgreSQLUUID(as_uuid=True), nullable=False
    )
    source_execution_id: Mapped[UUID] = mapped_column(
        PostgreSQLUUID(as_uuid=True), nullable=False
    )
    source_session_id: Mapped[UUID] = mapped_column(
        PostgreSQLUUID(as_uuid=True), nullable=False
    )
    source_workspace_manifest_id: Mapped[UUID] = mapped_column(
        PostgreSQLUUID(as_uuid=True), nullable=False
    )
    target_worker_id: Mapped[UUID] = mapped_column(
        PostgreSQLUUID(as_uuid=True), nullable=False
    )
    target_runtime_snapshot: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False
    )
    target_execution_id: Mapped[UUID | None] = mapped_column(
        PostgreSQLUUID(as_uuid=True)
    )
    target_session_id: Mapped[UUID | None] = mapped_column(PostgreSQLUUID(as_uuid=True))
    target_workspace_manifest_id: Mapped[UUID | None] = mapped_column(
        PostgreSQLUUID(as_uuid=True)
    )
    handoff_context_bundle_id: Mapped[UUID | None] = mapped_column(
        PostgreSQLUUID(as_uuid=True)
    )
    target_context_version: Mapped[int] = mapped_column(Integer, nullable=False)
    target_context_package_id: Mapped[UUID | None] = mapped_column(
        PostgreSQLUUID(as_uuid=True)
    )
    target_context_package_hash: Mapped[str | None] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    phase: Mapped[str] = mapped_column(String(64), nullable=False)
    writer_state: Mapped[str] = mapped_column(String(32), nullable=False)
    source_checkpoint_sequence: Mapped[int | None] = mapped_column(BigInteger)
    source_checkpoint_facts: Mapped[dict[str, Any] | None] = mapped_column(
        JSONB(none_as_null=True)
    )
    source_stop_sequence: Mapped[int | None] = mapped_column(BigInteger)
    source_stop_facts: Mapped[dict[str, Any] | None] = mapped_column(
        JSONB(none_as_null=True)
    )
    source_workspace_freeze_operation_id: Mapped[UUID | None] = mapped_column(
        PostgreSQLUUID(as_uuid=True)
    )
    source_workspace_freeze_facts: Mapped[dict[str, Any] | None] = mapped_column(
        JSONB(none_as_null=True)
    )
    source_input_cutoff_sequence: Mapped[int] = mapped_column(
        BigInteger, nullable=False
    )
    failure_code: Mapped[str | None] = mapped_column(String(100))
    blocked_operation_id: Mapped[UUID | None] = mapped_column(
        PostgreSQLUUID(as_uuid=True)
    )
    logical_attempt: Mapped[int] = mapped_column(Integer, nullable=False)
    lease_owner: Mapped[str | None] = mapped_column(String(255))
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    next_attempt_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    confirmed_by_type: Mapped[str] = mapped_column(String(50), nullable=False)
    confirmed_by_id: Mapped[str] = mapped_column(String(255), nullable=False)
    confirmed_source_channel: Mapped[str] = mapped_column(String(32), nullable=False)
    confirmed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )


class HandoffContextBundleModel(TimestampMixin, Base):
    __tablename__ = "handoff_context_bundles"
    __table_args__ = (
        CheckConstraint(
            "schema_version >= 1", name="ck_handoff_context_bundles_schema_version"
        ),
        CheckConstraint(
            "logical_attempt >= 1 AND source_checkpoint_sequence >= 0 "
            "AND source_last_accepted_sequence >= source_checkpoint_sequence "
            "AND source_worker_context_version >= 1 AND target_context_version >= 1 "
            "AND source_workspace_version >= 1",
            name="ck_handoff_context_bundles_positive_fields",
        ),
        CheckConstraint(
            "jsonb_typeof(repository_state) = 'array' AND "
            "jsonb_typeof(artifact_refs) = 'array' AND "
            "jsonb_typeof(evidence_refs) = 'array' AND "
            "jsonb_typeof(delivered_input_refs) = 'array' AND "
            "jsonb_typeof(transferable_input_refs) = 'array' AND "
            "jsonb_typeof(excluded_input_refs) = 'array'",
            name="ck_handoff_context_bundles_json_arrays",
        ),
        CheckConstraint(
            "source_context_package_hash ~ '^[0-9a-f]{64}$' AND "
            "target_runtime_snapshot_hash ~ '^[0-9a-f]{64}$' AND "
            "workspace_location_ref_hash ~ '^[0-9a-f]{64}$' AND "
            "bundle_hash ~ '^[0-9a-f]{64}$'",
            name="ck_handoff_context_bundles_hashes",
        ),
        CheckConstraint("version >= 1", name="ck_handoff_context_bundles_version"),
        ForeignKeyConstraint(
            ["workspace_id"],
            ["workspaces.id"],
            ondelete="RESTRICT",
            name="fk_handoff_context_bundles_workspace",
        ),
        ForeignKeyConstraint(
            ["worker_item_id", "workspace_id"],
            ["worker_items.id", "worker_items.workspace_id"],
            ondelete="RESTRICT",
            name="fk_handoff_context_bundles_worker_item_workspace",
        ),
        ForeignKeyConstraint(
            ["handoff_transition_id", "workspace_id"],
            ["handoff_transitions.id", "handoff_transitions.workspace_id"],
            ondelete="RESTRICT",
            name="fk_handoff_context_bundles_transition_workspace",
        ),
        ForeignKeyConstraint(
            ["recovery_decision_id", "workspace_id"],
            [
                "handoff_recovery_decisions.id",
                "handoff_recovery_decisions.workspace_id",
            ],
            ondelete="RESTRICT",
            name="fk_handoff_context_bundles_recovery_workspace",
        ),
        ForeignKeyConstraint(
            ["source_execution_id", "workspace_id"],
            ["executions.id", "executions.workspace_id"],
            ondelete="RESTRICT",
            name="fk_handoff_context_bundles_source_execution_workspace",
        ),
        ForeignKeyConstraint(
            ["source_session_id", "workspace_id"],
            ["execution_sessions.id", "execution_sessions.workspace_id"],
            ondelete="RESTRICT",
            name="fk_handoff_context_bundles_source_session_workspace",
        ),
        ForeignKeyConstraint(
            ["source_context_package_id", "workspace_id"],
            ["context_packages.id", "context_packages.workspace_id"],
            ondelete="RESTRICT",
            name="fk_handoff_context_bundles_source_package_workspace",
        ),
        ForeignKeyConstraint(
            ["target_execution_id", "workspace_id"],
            ["executions.id", "executions.workspace_id"],
            ondelete="RESTRICT",
            name="fk_handoff_context_bundles_target_execution_workspace",
        ),
        ForeignKeyConstraint(
            ["target_worker_id", "workspace_id"],
            ["workers.id", "workers.workspace_id"],
            ondelete="RESTRICT",
            name="fk_handoff_context_bundles_target_worker_workspace",
        ),
        ForeignKeyConstraint(
            ["source_workspace_manifest_id", "workspace_id"],
            ["execution_workspaces.id", "execution_workspaces.workspace_id"],
            ondelete="RESTRICT",
            name="fk_handoff_context_bundles_source_manifest_workspace",
        ),
        UniqueConstraint(
            "id", "workspace_id", name="uq_handoff_context_bundles_identity"
        ),
        UniqueConstraint(
            "workspace_id",
            "handoff_transition_id",
            "logical_attempt",
            name="uq_handoff_context_bundles_attempt",
        ),
        Index(
            "uq_handoff_context_bundles_hash",
            "workspace_id",
            "bundle_hash",
            unique=True,
        ),
    )

    id: Mapped[UUID] = mapped_column(PostgreSQLUUID(as_uuid=True), primary_key=True)
    schema_version: Mapped[int] = mapped_column(Integer, nullable=False)
    workspace_id: Mapped[UUID] = mapped_column(
        PostgreSQLUUID(as_uuid=True), nullable=False
    )
    worker_item_id: Mapped[UUID] = mapped_column(
        PostgreSQLUUID(as_uuid=True), nullable=False
    )
    handoff_transition_id: Mapped[UUID] = mapped_column(
        PostgreSQLUUID(as_uuid=True), nullable=False
    )
    logical_attempt: Mapped[int] = mapped_column(Integer, nullable=False)
    recovery_decision_id: Mapped[UUID | None] = mapped_column(
        PostgreSQLUUID(as_uuid=True)
    )
    source_execution_id: Mapped[UUID] = mapped_column(
        PostgreSQLUUID(as_uuid=True), nullable=False
    )
    source_session_id: Mapped[UUID] = mapped_column(
        PostgreSQLUUID(as_uuid=True), nullable=False
    )
    source_checkpoint_sequence: Mapped[int] = mapped_column(BigInteger, nullable=False)
    source_last_accepted_sequence: Mapped[int] = mapped_column(
        BigInteger, nullable=False
    )
    source_context_package_id: Mapped[UUID] = mapped_column(
        PostgreSQLUUID(as_uuid=True), nullable=False
    )
    source_context_package_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    source_worker_context_version: Mapped[int] = mapped_column(Integer, nullable=False)
    target_execution_id: Mapped[UUID] = mapped_column(
        PostgreSQLUUID(as_uuid=True), nullable=False
    )
    target_worker_id: Mapped[UUID] = mapped_column(
        PostgreSQLUUID(as_uuid=True), nullable=False
    )
    target_runtime_snapshot_hash: Mapped[str] = mapped_column(
        String(64), nullable=False
    )
    target_context_version: Mapped[int] = mapped_column(Integer, nullable=False)
    source_workspace_manifest_id: Mapped[UUID] = mapped_column(
        PostgreSQLUUID(as_uuid=True), nullable=False
    )
    source_workspace_version: Mapped[int] = mapped_column(Integer, nullable=False)
    workspace_location_ref_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    repository_state: Mapped[list[dict[str, Any]]] = mapped_column(
        JSONB, nullable=False
    )
    artifact_refs: Mapped[list[str]] = mapped_column(JSONB, nullable=False)
    evidence_refs: Mapped[list[str]] = mapped_column(JSONB, nullable=False)
    handoff_reason: Mapped[str] = mapped_column(Text, nullable=False)
    handoff_instruction: Mapped[str] = mapped_column(Text, nullable=False)
    delivered_input_refs: Mapped[list[str]] = mapped_column(JSONB, nullable=False)
    transferable_input_refs: Mapped[list[str]] = mapped_column(JSONB, nullable=False)
    excluded_input_refs: Mapped[list[str]] = mapped_column(JSONB, nullable=False)
    bundle_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    version: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("1")
    )


class HandoffRecoveryDecisionModel(TimestampMixin, Base):
    __tablename__ = "handoff_recovery_decisions"
    __table_args__ = (
        CheckConstraint(
            "expected_transition_version >= 1 AND version >= 1",
            name="ck_handoff_recovery_decisions_versions",
        ),
        CheckConstraint(
            "strategy IN ('retry_current_phase', 'retry_same_target', 'select_new_target', "
            "'continue_with_source_agent', 'confirm_source_stopped', 'confirm_target_absent', "
            "'confirm_target_active')",
            name="ck_handoff_recovery_decisions_strategy",
        ),
        CheckConstraint(
            "source_channel = 'web'",
            name="ck_handoff_recovery_decisions_source_channel",
        ),
        CheckConstraint(
            "jsonb_typeof(evidence_refs) = 'array' AND "
            "jsonb_typeof(resulting_operation_refs) = 'array' AND "
            "(replacement_target_snapshot IS NULL OR jsonb_typeof(replacement_target_snapshot) = 'object')",
            name="ck_handoff_recovery_decisions_json_shapes",
        ),
        ForeignKeyConstraint(
            ["workspace_id"],
            ["workspaces.id"],
            ondelete="RESTRICT",
            name="fk_handoff_recovery_decisions_workspace",
        ),
        ForeignKeyConstraint(
            ["handoff_transition_id", "workspace_id"],
            ["handoff_transitions.id", "handoff_transitions.workspace_id"],
            ondelete="RESTRICT",
            name="fk_handoff_recovery_decisions_transition_workspace",
        ),
        UniqueConstraint(
            "id", "workspace_id", name="uq_handoff_recovery_decisions_identity"
        ),
        Index(
            "ix_handoff_recovery_decisions_transition_created",
            "workspace_id",
            "handoff_transition_id",
            "created_at",
        ),
    )

    id: Mapped[UUID] = mapped_column(PostgreSQLUUID(as_uuid=True), primary_key=True)
    workspace_id: Mapped[UUID] = mapped_column(
        PostgreSQLUUID(as_uuid=True), nullable=False
    )
    handoff_transition_id: Mapped[UUID] = mapped_column(
        PostgreSQLUUID(as_uuid=True), nullable=False
    )
    expected_transition_version: Mapped[int] = mapped_column(Integer, nullable=False)
    strategy: Mapped[str] = mapped_column(String(64), nullable=False)
    actor_type: Mapped[str] = mapped_column(String(50), nullable=False)
    actor_id: Mapped[str] = mapped_column(String(255), nullable=False)
    source_channel: Mapped[str] = mapped_column(String(32), nullable=False)
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    evidence_refs: Mapped[list[str]] = mapped_column(JSONB, nullable=False)
    replacement_target_snapshot: Mapped[dict[str, Any] | None] = mapped_column(
        JSONB(none_as_null=True)
    )
    resulting_operation_refs: Mapped[list[str]] = mapped_column(JSONB, nullable=False)
    version: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("1")
    )


class ExecutionWorkspaceTransferModel(TimestampMixin, Base):
    __tablename__ = "execution_workspace_transfers"
    __table_args__ = (
        CheckConstraint(
            "logical_attempt >= 1 AND source_fencing_token >= 1 AND "
            "target_fencing_token > source_fencing_token AND version >= 1",
            name="ck_execution_workspace_transfers_fencing",
        ),
        CheckConstraint(
            "transfer_mode = 'reuse_exact'",
            name="ck_execution_workspace_transfers_mode",
        ),
        CheckConstraint(
            "status IN ('requested', 'succeeded', 'failed', 'unknown')",
            name="ck_execution_workspace_transfers_status",
        ),
        CheckConstraint(
            "status = 'succeeded' AND target_manifest_id IS NOT NULL OR "
            "status <> 'succeeded'",
            name="ck_execution_workspace_transfers_success_target",
        ),
        ForeignKeyConstraint(
            ["workspace_id"],
            ["workspaces.id"],
            ondelete="RESTRICT",
            name="fk_execution_workspace_transfers_workspace",
        ),
        ForeignKeyConstraint(
            ["handoff_transition_id", "workspace_id"],
            ["handoff_transitions.id", "handoff_transitions.workspace_id"],
            ondelete="RESTRICT",
            name="fk_execution_workspace_transfers_transition_workspace",
        ),
        ForeignKeyConstraint(
            ["source_execution_id", "workspace_id"],
            ["executions.id", "executions.workspace_id"],
            ondelete="RESTRICT",
            name="fk_execution_workspace_transfers_source_execution_workspace",
        ),
        ForeignKeyConstraint(
            ["target_execution_id", "workspace_id"],
            ["executions.id", "executions.workspace_id"],
            ondelete="RESTRICT",
            name="fk_execution_workspace_transfers_target_execution_workspace",
        ),
        ForeignKeyConstraint(
            ["source_manifest_id", "workspace_id"],
            ["execution_workspaces.id", "execution_workspaces.workspace_id"],
            ondelete="RESTRICT",
            name="fk_execution_workspace_transfers_source_manifest_workspace",
        ),
        ForeignKeyConstraint(
            ["target_manifest_id", "workspace_id"],
            ["execution_workspaces.id", "execution_workspaces.workspace_id"],
            ondelete="RESTRICT",
            name="fk_execution_workspace_transfers_target_manifest_workspace",
        ),
        UniqueConstraint(
            "id", "workspace_id", name="uq_execution_workspace_transfers_identity"
        ),
        UniqueConstraint(
            "workspace_id",
            "handoff_transition_id",
            "logical_attempt",
            name="uq_execution_workspace_transfers_attempt",
        ),
        UniqueConstraint(
            "workspace_id",
            "source_manifest_id",
            "target_manifest_id",
            name="uq_execution_workspace_transfers_manifest_pair",
        ),
        UniqueConstraint(
            "workspace_id",
            "provider_operation_id",
            name="uq_execution_workspace_transfers_provider_operation",
        ),
        Index(
            "ix_execution_workspace_transfers_coordinator",
            "workspace_id",
            "status",
            "updated_at",
        ),
    )

    id: Mapped[UUID] = mapped_column(PostgreSQLUUID(as_uuid=True), primary_key=True)
    workspace_id: Mapped[UUID] = mapped_column(
        PostgreSQLUUID(as_uuid=True), nullable=False
    )
    handoff_transition_id: Mapped[UUID] = mapped_column(
        PostgreSQLUUID(as_uuid=True), nullable=False
    )
    logical_attempt: Mapped[int] = mapped_column(Integer, nullable=False)
    operation_id: Mapped[UUID] = mapped_column(
        PostgreSQLUUID(as_uuid=True), nullable=False
    )
    source_manifest_id: Mapped[UUID] = mapped_column(
        PostgreSQLUUID(as_uuid=True), nullable=False
    )
    target_manifest_id: Mapped[UUID | None] = mapped_column(
        PostgreSQLUUID(as_uuid=True)
    )
    source_execution_id: Mapped[UUID] = mapped_column(
        PostgreSQLUUID(as_uuid=True), nullable=False
    )
    target_execution_id: Mapped[UUID] = mapped_column(
        PostgreSQLUUID(as_uuid=True), nullable=False
    )
    source_fencing_token: Mapped[int] = mapped_column(BigInteger, nullable=False)
    target_fencing_token: Mapped[int] = mapped_column(BigInteger, nullable=False)
    location_ref_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    repository_state_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    transfer_mode: Mapped[str] = mapped_column(String(32), nullable=False)
    provider_operation_id: Mapped[UUID] = mapped_column(
        PostgreSQLUUID(as_uuid=True), nullable=False
    )
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    failure_code: Mapped[str | None] = mapped_column(String(100))
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )
