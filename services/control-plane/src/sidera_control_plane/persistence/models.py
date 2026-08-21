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
        CheckConstraint(
            "schema_version >= 1", name="ck_plan_snapshots_schema_version"
        ),
        UniqueConstraint(
            "root_task_id", "generation", name="uq_plan_snapshots_root_generation"
        ),
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


class ExecutionModel(TimestampMixin, Base):
    __tablename__ = "executions"
    __table_args__ = (
        CheckConstraint("attempt >= 1", name="ck_executions_attempt"),
        CheckConstraint(
            "max_attempts >= attempt", name="ck_executions_max_attempts"
        ),
        CheckConstraint(
            "last_fencing_token >= 0", name="ck_executions_last_fencing_token"
        ),
        CheckConstraint("version >= 1", name="ck_executions_version"),
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
            ["active_lease_id", "id", "workspace_id"],
            ["assignment_leases.id", "assignment_leases.execution_id", "assignment_leases.workspace_id"],
            ondelete="RESTRICT",
            deferrable=True,
            initially="DEFERRED",
            name="fk_executions_active_lease",
        ),
        UniqueConstraint("id", "workspace_id", name="uq_executions_id_workspace"),
        UniqueConstraint(
            "lineage_id", "attempt", name="uq_executions_lineage_attempt"
        ),
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
    pinned_worker_id: Mapped[UUID | None] = mapped_column(
        PostgreSQLUUID(as_uuid=True)
    )
    assigned_worker_id: Mapped[UUID | None] = mapped_column(
        PostgreSQLUUID(as_uuid=True)
    )
    active_lease_id: Mapped[UUID | None] = mapped_column(
        PostgreSQLUUID(as_uuid=True)
    )
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
    worker_id: Mapped[UUID] = mapped_column(PostgreSQLUUID(as_uuid=True), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    issued_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
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
    worker_id: Mapped[UUID] = mapped_column(PostgreSQLUUID(as_uuid=True), nullable=False)
    adapter_session_id: Mapped[str] = mapped_column(String(500), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    last_sequence: Mapped[int] = mapped_column(BigInteger, nullable=False)
    last_activity_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    input_mode: Mapped[str] = mapped_column(String(32), nullable=False)
    adapter_module_id: Mapped[UUID | None] = mapped_column(PostgreSQLUUID(as_uuid=True))
    adapter_config_version: Mapped[int | None] = mapped_column(Integer)
    workspace_module_id: Mapped[UUID | None] = mapped_column(PostgreSQLUUID(as_uuid=True))
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
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )


class ModuleInstanceModel(TimestampMixin, Base):
    __tablename__ = "module_instances"
    __table_args__ = (
        CheckConstraint("config_version >= 1", name="ck_module_instances_config_version"),
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
            "workspace_id", "instance_name", "config_version",
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
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )


class ExecutionBindingModel(TimestampMixin, Base):
    __tablename__ = "execution_bindings"
    __table_args__ = (
        CheckConstraint("adapter_config_version >= 1", name="ck_bindings_adapter_config"),
        CheckConstraint("workspace_config_version >= 1", name="ck_bindings_workspace_config"),
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
        UniqueConstraint(
            "execution_id", "workspace_id", name="uq_bindings_execution"
        ),
    )

    id: Mapped[UUID] = mapped_column(PostgreSQLUUID(as_uuid=True), primary_key=True)
    workspace_id: Mapped[UUID] = mapped_column(
        PostgreSQLUUID(as_uuid=True),
        ForeignKey("workspaces.id", ondelete="RESTRICT"),
        nullable=False,
    )
    execution_id: Mapped[UUID] = mapped_column(PostgreSQLUUID(as_uuid=True), nullable=False)
    worker_id: Mapped[UUID] = mapped_column(PostgreSQLUUID(as_uuid=True), nullable=False)
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
        CheckConstraint("retention_seconds > 0", name="ck_execution_workspaces_retention"),
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
            postgresql_where=text("lifecycle IN ('requested', 'preparing', 'ready', 'active')"),
        ),
    )

    id: Mapped[UUID] = mapped_column(PostgreSQLUUID(as_uuid=True), primary_key=True)
    workspace_id: Mapped[UUID] = mapped_column(
        PostgreSQLUUID(as_uuid=True),
        ForeignKey("workspaces.id", ondelete="RESTRICT"),
        nullable=False,
    )
    task_id: Mapped[UUID] = mapped_column(PostgreSQLUUID(as_uuid=True), nullable=False)
    execution_id: Mapped[UUID] = mapped_column(PostgreSQLUUID(as_uuid=True), nullable=False)
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
    execution_id: Mapped[UUID] = mapped_column(PostgreSQLUUID(as_uuid=True), nullable=False)
    session_id: Mapped[UUID | None] = mapped_column(PostgreSQLUUID(as_uuid=True))
    pid: Mapped[int] = mapped_column(Integer, nullable=False)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
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
            "workspace_id", "operation_type", "operation_id",
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
    operation_id: Mapped[UUID] = mapped_column(PostgreSQLUUID(as_uuid=True), nullable=False)
    task_id: Mapped[UUID] = mapped_column(PostgreSQLUUID(as_uuid=True), nullable=False)
    execution_id: Mapped[UUID] = mapped_column(PostgreSQLUUID(as_uuid=True), nullable=False)
    session_id: Mapped[UUID | None] = mapped_column(PostgreSQLUUID(as_uuid=True))
    operation_type: Mapped[str] = mapped_column(String(64), nullable=False)
    module_instance_id: Mapped[UUID | None] = mapped_column(PostgreSQLUUID(as_uuid=True))
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
    execution_id: Mapped[UUID] = mapped_column(PostgreSQLUUID(as_uuid=True), nullable=False)
    session_id: Mapped[UUID] = mapped_column(PostgreSQLUUID(as_uuid=True), nullable=False)
    sequence: Mapped[int] = mapped_column(BigInteger, nullable=False)
    event_type: Mapped[str] = mapped_column(String(100), nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
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
            "workspace_id", "storage_ref", "content_hash",
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
    execution_id: Mapped[UUID] = mapped_column(PostgreSQLUUID(as_uuid=True), nullable=False)
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
    execution_id: Mapped[UUID] = mapped_column(PostgreSQLUUID(as_uuid=True), nullable=False)
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
            "status IN ('accepted', 'queued', 'dispatching', 'delivered', 'blocked', 'failed', 'cancelled')",
            name="ck_execution_inputs_status",
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
        UniqueConstraint(
            "workspace_id", "session_id", "client_idempotency_key",
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
    )

    id: Mapped[UUID] = mapped_column(PostgreSQLUUID(as_uuid=True), primary_key=True)
    workspace_id: Mapped[UUID] = mapped_column(
        PostgreSQLUUID(as_uuid=True),
        ForeignKey("workspaces.id", ondelete="RESTRICT"),
        nullable=False,
    )
    execution_id: Mapped[UUID] = mapped_column(PostgreSQLUUID(as_uuid=True), nullable=False)
    session_id: Mapped[UUID] = mapped_column(PostgreSQLUUID(as_uuid=True), nullable=False)
    client_idempotency_key: Mapped[str] = mapped_column(String(255), nullable=False)
    text: Mapped[str] = mapped_column(Text, nullable=False)
    attachment_refs: Mapped[list[str]] = mapped_column(JSONB, nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    operation_id: Mapped[UUID | None] = mapped_column(PostgreSQLUUID(as_uuid=True))
    failure_code: Mapped[str | None] = mapped_column(String(100))
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    source_command_id: Mapped[UUID | None] = mapped_column(
        PostgreSQLUUID(as_uuid=True), ForeignKey("commands.id", ondelete="RESTRICT")
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )
