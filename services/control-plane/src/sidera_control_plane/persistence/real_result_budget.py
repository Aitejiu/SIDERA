from __future__ import annotations

from dataclasses import dataclass
from uuid import UUID, uuid4

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as postgres_insert

from sidera_control_plane.domain.result_operations import ResultError
from sidera_control_plane.persistence.database import Database
from sidera_control_plane.persistence.models import (
    RealResultExecutionReservationModel,
    RealResultRunBudgetModel,
)


@dataclass(frozen=True, slots=True)
class RealResultExecutionReservation:
    """One durable external execution reservation."""

    id: UUID
    run_id: UUID
    sequence_no: int
    step_key: str


class RealResultExecutionBudgetStore:
    """Persist and fence the bounded real-provider execution budget."""

    def __init__(self, database: Database) -> None:
        if not isinstance(database, Database):
            raise TypeError("real result budget store requires Database")
        self._database = database

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
