from dataclasses import dataclass
from datetime import datetime
from typing import Literal, Protocol
from uuid import UUID

from pydantic import JsonValue, TypeAdapter
from sidera_agents.contracts import ProfileRef
from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.engine import Engine
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from .models import AgentGrantRow, BrowserSessionRow, LocalBootstrapRow, ToolReceiptRow
from .store import ProductStoreError


@dataclass(frozen=True, slots=True)
class BootstrapRecord:
    bootstrap_id: UUID
    workspace_id: UUID
    principal_id: str
    token_digest: str
    created_at: datetime
    expires_at: datetime
    consumed_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class BrowserSessionRecord:
    session_id: UUID
    workspace_id: UUID
    principal_id: str
    token_digest: str
    csrf_digest: str | None
    created_at: datetime
    expires_at: datetime
    revoked_at: datetime | None = None


BootstrapExchange = Literal["accepted", "missing", "expired", "consumed"]
type GrantRole = Literal["main", "worker", "planner"]
type GrantSource = Literal["product", "control_plane"]
type ReceiptOutcome = Literal["dispatching", "applied", "rejected", "unknown"]
type SettledOutcome = Literal["applied", "rejected", "unknown"]


@dataclass(frozen=True, slots=True)
class ToolReceiptRecord:
    receipt_id: UUID
    grant_id: UUID
    run_id: UUID
    response_id: UUID
    tool_name: str
    logical_request_id: UUID
    payload_hash: str
    command_id: UUID
    intent: dict[str, JsonValue]
    outcome: ReceiptOutcome
    result: dict[str, JsonValue] | None
    created_at: datetime
    updated_at: datetime


class ReceiptStore(Protocol):
    def find_receipt(
        self,
        *,
        grant_id: UUID,
        tool_name: str | None = None,
        logical_request_id: UUID | None = None,
        command_id: UUID | None = None,
    ) -> ToolReceiptRecord | None: ...

    def save_receipt(
        self, record: ToolReceiptRecord
    ) -> tuple[ToolReceiptRecord, bool]: ...

    def settle_receipt(
        self,
        receipt_id: UUID,
        *,
        outcome: SettledOutcome,
        result: dict[str, JsonValue],
        now: datetime,
    ) -> ToolReceiptRecord: ...


def _tool_receipt(row: ToolReceiptRow) -> ToolReceiptRecord:
    return ToolReceiptRecord(
        row.receipt_id,
        row.grant_id,
        row.run_id,
        row.response_id,
        row.tool_name,
        row.logical_request_id,
        row.payload_hash,
        row.command_id,
        row.intent,
        TypeAdapter[ReceiptOutcome](ReceiptOutcome).validate_python(row.outcome),
        row.result,
        row.created_at,
        row.updated_at,
    )


@dataclass(frozen=True, slots=True)
class AgentGrantRecord:
    grant_id: UUID
    source: GrantSource
    role: GrantRole
    workspace_id: UUID
    principal_id: str
    profile_ref: ProfileRef
    start_operation_id: UUID
    native_grant_id: UUID | None
    connection_id: UUID
    parent_session_id: UUID | None
    run_id: UUID | None
    root_task_id: UUID | None
    execution_id: UUID | None
    worker_item_id: UUID | None
    execution_version: int | None
    binding_version: int | None
    planning_request_id: UUID | None
    planning_run_id: UUID | None
    lease_id: UUID | None
    fencing_token: int | None
    authority_refs: dict[str, JsonValue]
    created_at: datetime
    expires_at: datetime
    revoked_at: datetime | None = None
    bearer_digest: str | None = None


class GrantStore(Protocol):
    def save_grant(self, record: AgentGrantRecord) -> tuple[AgentGrantRecord, bool]: ...

    def find_grant(
        self, *, grant_id: UUID | None = None, bearer_digest: str | None = None
    ) -> AgentGrantRecord | None: ...

    def bind_grant(
        self, grant_id: UUID, *, run_id: UUID, bearer_digest: str
    ) -> tuple[AgentGrantRecord, bool]: ...

    def revoke_grant(self, grant_id: UUID, *, now: datetime) -> None: ...


def _agent_grant(row: AgentGrantRow) -> AgentGrantRecord:
    return AgentGrantRecord(
        grant_id=row.grant_id,
        source=TypeAdapter[GrantSource](GrantSource).validate_python(row.source),
        role=TypeAdapter[GrantRole](GrantRole).validate_python(row.role),
        workspace_id=row.workspace_id,
        principal_id=row.principal_id,
        profile_ref=ProfileRef.model_validate(row.profile_ref),
        start_operation_id=row.start_operation_id,
        native_grant_id=row.native_grant_id,
        connection_id=row.connection_id,
        parent_session_id=row.parent_session_id,
        run_id=row.run_id,
        root_task_id=row.root_task_id,
        execution_id=row.execution_id,
        worker_item_id=row.worker_item_id,
        execution_version=row.execution_version,
        binding_version=row.binding_version,
        planning_request_id=row.planning_request_id,
        planning_run_id=row.planning_run_id,
        lease_id=row.lease_id,
        fencing_token=row.fencing_token,
        authority_refs=row.authority_refs,
        created_at=row.created_at,
        expires_at=row.expires_at,
        revoked_at=row.revoked_at,
        bearer_digest=row.bearer_digest,
    )


class SessionStore(Protocol):
    def save_bootstrap(self, record: BootstrapRecord) -> None: ...

    def exchange_bootstrap(
        self, digest: str, session: BrowserSessionRecord, *, now: datetime
    ) -> BootstrapExchange: ...

    def find_session(
        self, *, token_digest: str | None = None, session_id: UUID | None = None
    ) -> BrowserSessionRecord | None: ...

    def revoke_session(self, session_id: UUID, *, now: datetime) -> None: ...


def _browser_session(row: BrowserSessionRow) -> BrowserSessionRecord:
    return BrowserSessionRecord(
        row.session_id,
        row.workspace_id,
        row.principal_id,
        row.token_digest,
        row.csrf_digest,
        row.created_at,
        row.expires_at,
        row.revoked_at,
    )


class PostgresAuthorityStore:
    def __init__(self, engine: Engine) -> None:
        self.engine = engine

    def save_bootstrap(self, record: BootstrapRecord) -> None:
        try:
            with Session(self.engine) as session, session.begin():
                session.add(
                    LocalBootstrapRow(
                        bootstrap_id=record.bootstrap_id,
                        workspace_id=record.workspace_id,
                        principal_id=record.principal_id,
                        token_digest=record.token_digest,
                        created_at=record.created_at,
                        expires_at=record.expires_at,
                        consumed_at=record.consumed_at,
                    )
                )
        except SQLAlchemyError:
            raise ProductStoreError("product_store_unavailable") from None

    def exchange_bootstrap(
        self, digest: str, session: BrowserSessionRecord, *, now: datetime
    ) -> BootstrapExchange:
        try:
            with Session(self.engine) as connection, connection.begin():
                row = connection.scalar(
                    select(LocalBootstrapRow)
                    .where(
                        LocalBootstrapRow.token_digest == digest,
                        LocalBootstrapRow.workspace_id == session.workspace_id,
                        LocalBootstrapRow.principal_id == session.principal_id,
                    )
                    .with_for_update()
                )
                if row is None:
                    return "missing"
                if row.consumed_at is not None:
                    return "consumed"
                if row.expires_at <= now:
                    return "expired"
                row.consumed_at = now
                connection.add(
                    BrowserSessionRow(
                        session_id=session.session_id,
                        workspace_id=session.workspace_id,
                        principal_id=session.principal_id,
                        token_digest=session.token_digest,
                        csrf_digest=session.csrf_digest,
                        created_at=session.created_at,
                        expires_at=session.expires_at,
                        revoked_at=session.revoked_at,
                    )
                )
                return "accepted"
        except SQLAlchemyError:
            raise ProductStoreError("product_store_unavailable") from None

    def find_session(
        self, *, token_digest: str | None = None, session_id: UUID | None = None
    ) -> BrowserSessionRecord | None:
        if (token_digest is None) == (session_id is None):
            raise ProductStoreError("invalid_session_lookup")
        statement = select(BrowserSessionRow).where(
            BrowserSessionRow.session_id == session_id
            if session_id is not None
            else BrowserSessionRow.token_digest == token_digest
        )
        try:
            with Session(self.engine) as session:
                row = session.scalar(statement)
                return None if row is None else _browser_session(row)
        except SQLAlchemyError:
            raise ProductStoreError("product_store_unavailable") from None

    def revoke_session(self, session_id: UUID, *, now: datetime) -> None:
        try:
            with Session(self.engine) as session, session.begin():
                session.execute(
                    update(BrowserSessionRow)
                    .where(
                        BrowserSessionRow.session_id == session_id,
                        BrowserSessionRow.revoked_at.is_(None),
                    )
                    .values(revoked_at=now)
                )
        except SQLAlchemyError:
            raise ProductStoreError("product_store_unavailable") from None

    def save_grant(self, record: AgentGrantRecord) -> tuple[AgentGrantRecord, bool]:
        values: dict[str, object] = {
            column.key: getattr(record, column.key)
            for column in AgentGrantRow.__table__.columns
            if column.key != "profile_ref"
        }
        values["profile_ref"] = record.profile_ref.model_dump(mode="json")
        statement = (
            insert(AgentGrantRow)
            .values(**values)
            .on_conflict_do_nothing(
                index_elements=["workspace_id", "start_operation_id", "connection_id"]
            )
            .returning(AgentGrantRow)
        )
        try:
            with Session(self.engine) as session, session.begin():
                inserted = session.execute(statement).scalar_one_or_none()
                if isinstance(inserted, AgentGrantRow):
                    return _agent_grant(inserted), True
                row = session.scalar(
                    select(AgentGrantRow)
                    .where(
                        AgentGrantRow.workspace_id == record.workspace_id,
                        AgentGrantRow.start_operation_id == record.start_operation_id,
                        AgentGrantRow.connection_id == record.connection_id,
                    )
                    .with_for_update()
                )
                if row is None:
                    raise ProductStoreError("grant_conflict")
                return _agent_grant(row), False
        except SQLAlchemyError:
            raise ProductStoreError("product_store_unavailable") from None

    def find_grant(
        self, *, grant_id: UUID | None = None, bearer_digest: str | None = None
    ) -> AgentGrantRecord | None:
        if (grant_id is None) == (bearer_digest is None):
            raise ProductStoreError("invalid_grant_lookup")
        statement = select(AgentGrantRow).where(
            AgentGrantRow.grant_id == grant_id
            if grant_id is not None
            else AgentGrantRow.bearer_digest == bearer_digest
        )
        try:
            with Session(self.engine) as session:
                row = session.scalar(statement)
                return None if row is None else _agent_grant(row)
        except SQLAlchemyError:
            raise ProductStoreError("product_store_unavailable") from None

    def bind_grant(
        self, grant_id: UUID, *, run_id: UUID, bearer_digest: str
    ) -> tuple[AgentGrantRecord, bool]:
        try:
            with Session(self.engine) as session, session.begin():
                row = session.scalar(
                    select(AgentGrantRow)
                    .where(
                        AgentGrantRow.grant_id == grant_id,
                    )
                    .with_for_update()
                )
                if row is None or (row.run_id is not None and row.run_id != run_id):
                    raise ProductStoreError("grant_conflict")
                winner = row.bearer_digest is None
                row.run_id = run_id
                if winner:
                    row.bearer_digest = bearer_digest
                return _agent_grant(row), winner
        except SQLAlchemyError:
            raise ProductStoreError("product_store_unavailable") from None

    def revoke_grant(self, grant_id: UUID, *, now: datetime) -> None:
        try:
            with Session(self.engine) as session, session.begin():
                session.execute(
                    update(AgentGrantRow)
                    .where(
                        AgentGrantRow.grant_id == grant_id,
                        AgentGrantRow.revoked_at.is_(None),
                    )
                    .values(revoked_at=now)
                )
        except SQLAlchemyError:
            raise ProductStoreError("product_store_unavailable") from None

    def find_receipt(
        self,
        *,
        grant_id: UUID,
        tool_name: str | None = None,
        logical_request_id: UUID | None = None,
        command_id: UUID | None = None,
    ) -> ToolReceiptRecord | None:
        if command_id is not None:
            if tool_name is not None or logical_request_id is not None:
                raise ProductStoreError("invalid_receipt_lookup")
            statement = select(ToolReceiptRow).where(
                ToolReceiptRow.grant_id == grant_id,
                ToolReceiptRow.command_id == command_id,
            )
        else:
            if tool_name is None or logical_request_id is None:
                raise ProductStoreError("invalid_receipt_lookup")
            statement = select(ToolReceiptRow).where(
                ToolReceiptRow.grant_id == grant_id,
                ToolReceiptRow.tool_name == tool_name,
                ToolReceiptRow.logical_request_id == logical_request_id,
            )
        try:
            with Session(self.engine) as session:
                row = session.scalar(statement)
                return None if row is None else _tool_receipt(row)
        except SQLAlchemyError:
            raise ProductStoreError("product_store_unavailable") from None

    def save_receipt(self, record: ToolReceiptRecord) -> tuple[ToolReceiptRecord, bool]:
        statement = (
            insert(ToolReceiptRow)
            .values(
                receipt_id=record.receipt_id,
                grant_id=record.grant_id,
                run_id=record.run_id,
                response_id=record.response_id,
                tool_name=record.tool_name,
                logical_request_id=record.logical_request_id,
                payload_hash=record.payload_hash,
                command_id=record.command_id,
                intent=record.intent,
                outcome=record.outcome,
                result=record.result,
                created_at=record.created_at,
                updated_at=record.updated_at,
            )
            .on_conflict_do_nothing(
                index_elements=["grant_id", "tool_name", "logical_request_id"],
            )
            .returning(ToolReceiptRow)
        )
        try:
            with Session(self.engine) as session, session.begin():
                inserted = session.execute(statement).scalar_one_or_none()
                if isinstance(inserted, ToolReceiptRow):
                    return _tool_receipt(inserted), True
                row = session.scalar(
                    select(ToolReceiptRow)
                    .where(
                        ToolReceiptRow.grant_id == record.grant_id,
                        ToolReceiptRow.tool_name == record.tool_name,
                        ToolReceiptRow.logical_request_id == record.logical_request_id,
                    )
                    .with_for_update()
                )
                if row is None:
                    raise ProductStoreError("receipt_conflict")
                return _tool_receipt(row), False
        except SQLAlchemyError:
            raise ProductStoreError("product_store_unavailable") from None

    def settle_receipt(
        self,
        receipt_id: UUID,
        *,
        outcome: SettledOutcome,
        result: dict[str, JsonValue],
        now: datetime,
    ) -> ToolReceiptRecord:
        try:
            with Session(self.engine) as session, session.begin():
                row = session.scalar(
                    select(ToolReceiptRow)
                    .where(
                        ToolReceiptRow.receipt_id == receipt_id,
                    )
                    .with_for_update()
                )
                if row is None:
                    raise ProductStoreError("receipt_conflict")
                if row.outcome not in {"applied", "rejected"}:
                    row.outcome, row.result, row.updated_at = outcome, result, now
                return _tool_receipt(row)
        except SQLAlchemyError:
            raise ProductStoreError("product_store_unavailable") from None
