from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import UUID, uuid4

from sqlalchemy import or_, select
from sqlalchemy.dialects.postgresql import insert as postgres_insert

from sidera_control_plane.domain.result_operations import ResultError
from sidera_control_plane.persistence.database import Database
from sidera_control_plane.persistence.models import (
    ExecutionModel,
    ExecutionSessionModel,
    RealResultExecutionReservationModel,
    RealResultRunBudgetModel,
)

_FINGERPRINT = re.compile(r"^[0-9a-f]{64}$")


@dataclass(frozen=True, slots=True)
class RealResultExecutionReservation:
    """One durable external execution reservation."""

    id: UUID
    run_id: UUID
    sequence_no: int
    step_key: str
    execution_id: UUID | None = None
    session_id: UUID | None = None


@dataclass(frozen=True, slots=True)
class RealResultRunIdentity:
    """The immutable identity and deadline bound to a new real run."""

    run_id: UUID
    profile_fingerprint: str
    max_executions: int
    expires_at: datetime


class RealResultExecutionBudgetStore:
    """Persist and fence the bounded real-provider execution budget."""

    def __init__(self, database: Database) -> None:
        if not isinstance(database, Database):
            raise TypeError("real result budget store requires Database")
        self._database = database

    def begin_run(
        self,
        *,
        run_id: UUID,
        profile_fingerprint: str,
        max_executions: int,
        expires_at: datetime,
    ) -> RealResultRunIdentity:
        """Create or idempotently reopen a run without moving its deadline."""

        _validate_run_id(run_id)
        if (
            not isinstance(profile_fingerprint, str)
            or _FINGERPRINT.fullmatch(profile_fingerprint.strip().lower()) is None
        ):
            raise ResultError("real_profile_fingerprint_invalid", {})
        if type(max_executions) is not int or max_executions < 1:
            raise ResultError("real_budget_invalid", {})
        deadline = _deadline(expires_at)
        with self._database.session() as session, session.begin():
            session.execute(
                postgres_insert(RealResultRunBudgetModel)
                .values(
                    run_id=run_id,
                    max_executions=max_executions,
                    reserved_executions=0,
                    profile_fingerprint=profile_fingerprint.strip().lower(),
                    expires_at=deadline,
                )
                .on_conflict_do_nothing(index_elements=["run_id"])
            )
            budget = session.scalar(
                select(RealResultRunBudgetModel)
                .where(RealResultRunBudgetModel.run_id == run_id)
                .with_for_update()
            )
            if budget is None:
                raise RuntimeError("real result run row was not created")
            if budget.profile_fingerprint is None or budget.expires_at is None:
                raise ResultError("real_run_legacy_identity", {})
            if (
                budget.max_executions != max_executions
                or budget.profile_fingerprint != profile_fingerprint.strip().lower()
                or _as_utc(budget.expires_at) != deadline
            ):
                raise ResultError("real_run_identity_conflict", {})
            return RealResultRunIdentity(
                run_id=run_id,
                profile_fingerprint=budget.profile_fingerprint,
                max_executions=budget.max_executions,
                expires_at=_as_utc(budget.expires_at),
            )

    def reserve(
        self,
        *,
        run_id: UUID,
        max_executions: int,
        step_key: str,
    ) -> RealResultExecutionReservation:
        _validate_run_id(run_id)
        if type(max_executions) is not int or max_executions < 1:
            raise ResultError("real_budget_invalid", {})
        normalized_step = _step_key(step_key)
        with self._database.session() as session, session.begin():
            session.execute(
                postgres_insert(RealResultRunBudgetModel)
                .values(
                    run_id=run_id,
                    max_executions=max_executions,
                    reserved_executions=0,
                )
                .on_conflict_do_nothing(index_elements=["run_id"])
            )
            budget = session.scalar(
                select(RealResultRunBudgetModel)
                .where(RealResultRunBudgetModel.run_id == run_id)
                .with_for_update()
            )
            if budget is None:
                raise RuntimeError("real result budget row was not created")
            if budget.max_executions != max_executions:
                raise ResultError("real_budget_mismatch", {})
            if budget.expires_at is not None and _as_utc(
                budget.expires_at
            ) <= datetime.now(UTC):
                raise ResultError("real_run_deadline_expired", {})
            existing = session.scalar(
                select(RealResultExecutionReservationModel)
                .where(
                    RealResultExecutionReservationModel.run_id == run_id,
                    RealResultExecutionReservationModel.step_key == normalized_step,
                )
                .with_for_update()
            )
            if existing is not None:
                raise ResultError("real_run_reservation_replayed", {})
            if budget.reserved_executions >= budget.max_executions:
                raise ResultError("real_run_budget_exhausted", {})
            sequence_no = budget.reserved_executions + 1
            budget.reserved_executions = sequence_no
            reservation = RealResultExecutionReservationModel(
                id=uuid4(),
                run_id=run_id,
                sequence_no=sequence_no,
                step_key=normalized_step,
                status="started",
            )
            session.add(reservation)
            session.flush()
            return RealResultExecutionReservation(
                id=reservation.id,
                run_id=reservation.run_id,
                sequence_no=reservation.sequence_no,
                step_key=reservation.step_key,
            )

    def bind_execution(
        self,
        reservation_id: UUID,
        execution_id: UUID,
        session_id: UUID,
    ) -> None:
        """Bind one reservation to the exact persisted Execution and Session."""

        _validate_run_id(reservation_id)
        _validate_run_id(execution_id)
        _validate_run_id(session_id)
        with self._database.session() as session, session.begin():
            reservation = session.scalar(
                select(RealResultExecutionReservationModel)
                .where(RealResultExecutionReservationModel.id == reservation_id)
                .with_for_update()
            )
            if reservation is None:
                raise ResultError("real_reservation_missing", {})
            if reservation.status != "started":
                raise ResultError("real_reservation_status_conflict", {})
            if (
                reservation.execution_id is not None
                or reservation.session_id is not None
            ):
                if (
                    reservation.execution_id == execution_id
                    and reservation.session_id == session_id
                ):
                    return
                raise ResultError("real_reservation_binding_conflict", {})
            execution = session.get(ExecutionModel, execution_id)
            execution_session = session.get(ExecutionSessionModel, session_id)
            if execution is None or execution_session is None:
                raise ResultError("real_execution_binding_missing", {})
            if execution_session.execution_id != execution_id:
                raise ResultError("real_execution_binding_mismatch", {})
            duplicate = session.scalar(
                select(RealResultExecutionReservationModel)
                .where(
                    RealResultExecutionReservationModel.id != reservation_id,
                    or_(
                        RealResultExecutionReservationModel.execution_id == execution_id,
                        RealResultExecutionReservationModel.session_id == session_id,
                    ),
                )
                .with_for_update()
            )
            if duplicate is not None:
                raise ResultError("real_reservation_binding_conflict", {})
            reservation.execution_id = execution_id
            reservation.session_id = session_id
            session.flush()

    def complete(self, reservation_id: UUID, *, status: str) -> None:
        _validate_run_id(reservation_id)
        if status not in {"succeeded", "unknown"}:
            raise ResultError("real_reservation_status_invalid", {})
        with self._database.session() as session, session.begin():
            reservation = session.scalar(
                select(RealResultExecutionReservationModel)
                .where(RealResultExecutionReservationModel.id == reservation_id)
                .with_for_update()
            )
            if reservation is None:
                raise ResultError("real_reservation_missing", {})
            if reservation.status == status:
                return
            if reservation.status != "started":
                raise ResultError("real_reservation_status_conflict", {})
            reservation.status = status
            session.flush()

    def reserved_count(self, run_id: UUID) -> int:
        _validate_run_id(run_id)
        with self._database.session() as session:
            value = session.scalar(
                select(RealResultRunBudgetModel.reserved_executions).where(
                    RealResultRunBudgetModel.run_id == run_id
                )
            )
            return 0 if value is None else int(value)

    def run_identity(self, run_id: UUID) -> RealResultRunIdentity | None:
        _validate_run_id(run_id)
        with self._database.session() as session:
            budget = session.get(RealResultRunBudgetModel, run_id)
            if (
                budget is None
                or budget.profile_fingerprint is None
                or budget.expires_at is None
            ):
                return None
            return RealResultRunIdentity(
                run_id=run_id,
                profile_fingerprint=budget.profile_fingerprint,
                max_executions=budget.max_executions,
                expires_at=_as_utc(budget.expires_at),
            )


def _validate_run_id(value: UUID) -> None:
    if not isinstance(value, UUID):
        raise TypeError("real result run id must be a UUID")


def _step_key(value: str) -> str:
    if not isinstance(value, str):
        raise TypeError("real result step key must be a string")
    normalized = value.strip()
    if (
        not normalized
        or len(normalized) > 64
        or any(char.isspace() for char in normalized)
    ):
        raise ValueError("real result step key must be a compact non-empty string")
    return normalized


def _deadline(value: datetime) -> datetime:
    if (
        not isinstance(value, datetime)
        or value.tzinfo is None
        or value.utcoffset() is None
    ):
        raise ResultError("real_run_deadline_invalid", {})
    normalized = value.astimezone(UTC)
    if normalized <= datetime.now(UTC):
        raise ResultError("real_run_deadline_invalid", {})
    return normalized


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)
