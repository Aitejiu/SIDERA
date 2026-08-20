from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import (
    BigInteger,
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
