from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any, cast
from uuid import UUID, uuid4

from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.engine import CursorResult
from sqlalchemy.orm import Session

from sidera_control_plane.application.result_ports import (
    ResultOperation,
    ResultOperationAttemptV1,
    ResultRecord,
)
from sidera_control_plane.domain.code_results import (
    CodeResultSnapshotV1,
    IntegrationCandidateV1,
)
from sidera_control_plane.domain.errors import ValidationError, VersionConflict
from sidera_control_plane.domain.result_operations import (
    ResultApplyOperationV1,
    ResultError,
    ResultOperationStatus,
    ReworkRecordV1,
    TaskResultHead,
)
from sidera_control_plane.domain.result_verification import (
    TaskAcceptanceDecisionV1,
    VerificationResultV1,
)
from sidera_control_plane.persistence.models import (
    CodeResultSnapshotModel,
    IntegrationCandidateModel,
    ResultApplyOperationModel,
    ResultOperationAttemptModel,
    ResultReworkRecordModel,
    ResultVerificationResultModel,
    TaskAcceptanceDecisionModel,
    TaskResultHeadModel,
)

_KIND_ALIASES = {
    "code_snapshot": "code_snapshot",
    "code_result_snapshot": "code_snapshot",
    "snapshot": "code_snapshot",
    "candidate": "candidate",
    "integration_candidate": "candidate",
    "verification": "verification_result",
    "verification_result": "verification_result",
    "acceptance": "acceptance",
    "acceptance_decision": "acceptance",
    "rework": "rework",
    "rework_record": "rework",
    "apply": "apply_operation",
    "apply_operation": "apply_operation",
}


class ResultRepository:
    """Transactional persistence boundary for R6 result facts and operations."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def get_head(
        self,
        workspace_id: UUID,
        root_task_id: UUID,
        *,
        for_update: bool = False,
    ) -> TaskResultHead | None:
        statement = select(TaskResultHeadModel).where(
            TaskResultHeadModel.workspace_id == workspace_id,
            TaskResultHeadModel.root_task_id == root_task_id,
        )
        if for_update:
            statement = statement.with_for_update()
        row = self._session.scalar(statement)
        return _to_head(row) if row is not None else None

    def save_head(self, head: TaskResultHead, *, expected_version: int) -> None:
        if type(expected_version) is not int or expected_version < 0:
            raise ValidationError("result head expected_version must be non-negative")
        now = datetime.now(UTC)
        values = _head_values(head, now=now)
        if expected_version == 0:
            try:
                with self._session.begin_nested():
                    self._session.add(TaskResultHeadModel(**values))
                    self._session.flush()
            except Exception as exc:
                if not _is_integrity_error(exc):
                    raise
                current = self.get_head(head.workspace_id, head.root_task_id)
                raise VersionConflict(0, current.result_version if current else 0) from exc
            return

        result = self._session.execute(
            update(TaskResultHeadModel)
            .where(
                TaskResultHeadModel.workspace_id == head.workspace_id,
                TaskResultHeadModel.root_task_id == head.root_task_id,
                TaskResultHeadModel.result_version == expected_version,
            )
            .values(**_head_update_values(head, now=now))
        )
        if not isinstance(result, CursorResult) or result.rowcount != 1:
            current = self.get_head(head.workspace_id, head.root_task_id)
            raise VersionConflict(
                expected_version, current.result_version if current else 0
            )

    def add_immutable(self, kind: str, record: ResultRecord) -> ResultRecord:
        canonical_kind = _normalize_kind(kind)
        model, values_factory, parser, expected_type = _record_spec(canonical_kind)
        if not isinstance(record, expected_type):
            raise ValidationError(
                f"result record kind {canonical_kind} has an incompatible type"
            )
        values = values_factory(record)
        self._session.execute(
            insert(model).values(**values).on_conflict_do_nothing()
        )
        record_id = cast(Any, record).id
        workspace_id = cast(Any, record).workspace_id
        row = self._session.scalar(
            select(model).where(
                cast(Any, model).id == record_id,
                cast(Any, model).workspace_id == workspace_id,
            )
        )
        if row is None:
            raise ResultError(
                "result_record_conflict",
                {"kind": canonical_kind, "record_id": str(record_id)},
            )
        stored = parser(row)
        if stored.to_mapping() != record.to_mapping():
            raise ResultError(
                "result_record_conflict",
                {"kind": canonical_kind, "record_id": str(record_id)},
            )
        return stored

    def get_record(
        self, kind: str, record_id: UUID, workspace_id: UUID
    ) -> ResultRecord | None:
        canonical_kind = _normalize_kind(kind)
        model, _values_factory, parser, _expected_type = _record_spec(canonical_kind)
        row = self._session.scalar(
            select(model).where(
                cast(Any, model).id == record_id,
                cast(Any, model).workspace_id == workspace_id,
            )
        )
        return parser(row) if row is not None else None

    def get_code_result_by_capture_operation(
        self,
        operation_id: UUID,
        workspace_id: UUID,
        *,
        for_update: bool = False,
    ) -> CodeResultSnapshotV1 | None:
        statement = select(CodeResultSnapshotModel).where(
            CodeResultSnapshotModel.capture_operation_id == operation_id,
            CodeResultSnapshotModel.workspace_id == workspace_id,
        )
        if for_update:
            statement = statement.with_for_update()
        row = self._session.scalar(statement)
        return _to_snapshot(row) if row is not None else None

    def get_operation(
        self,
        operation_id: UUID,
        workspace_id: UUID,
        *,
        for_update: bool = False,
    ) -> ResultOperation | None:
        statement = select(ResultOperationAttemptModel).where(
            ResultOperationAttemptModel.operation_id == operation_id,
            ResultOperationAttemptModel.workspace_id == workspace_id,
        )
        if for_update:
            statement = statement.with_for_update()
        row = self._session.scalar(statement)
        if row is not None:
            return _to_operation_attempt(row)
        statement = select(ResultApplyOperationModel).where(
            ResultApplyOperationModel.id == operation_id,
            ResultApplyOperationModel.workspace_id == workspace_id,
        )
        if for_update:
            statement = statement.with_for_update()
        row = self._session.scalar(statement)
        return _to_apply(row) if row is not None else None

    def get_verification_by_operation(
        self,
        operation_id: UUID,
        workspace_id: UUID,
        *,
        for_update: bool = False,
    ) -> VerificationResultV1 | None:
        statement = select(ResultVerificationResultModel).where(
            ResultVerificationResultModel.operation_id == operation_id,
            ResultVerificationResultModel.workspace_id == workspace_id,
        )
        if for_update:
            statement = statement.with_for_update()
        row = self._session.scalar(statement)
        return _to_verification(row) if row is not None else None

    def list_verification_results(
        self, candidate_id: UUID, workspace_id: UUID
    ) -> tuple[VerificationResultV1, ...]:
        rows = self._session.scalars(
            select(ResultVerificationResultModel)
            .where(
                ResultVerificationResultModel.candidate_id == candidate_id,
                ResultVerificationResultModel.workspace_id == workspace_id,
            )
            .order_by(
                ResultVerificationResultModel.check_key.asc(),
                ResultVerificationResultModel.attempt.asc(),
                ResultVerificationResultModel.created_at.asc(),
                ResultVerificationResultModel.id.asc(),
            )
        ).all()
        return tuple(_to_verification(row) for row in rows)

    def claim_operations(
        self, kind: str, now: datetime, limit: int
    ) -> tuple[ResultOperationAttemptV1, ...]:
        if not isinstance(kind, str) or not kind.strip():
            raise ValidationError("operation kind must be a non-empty string")
        if type(limit) is not int or limit < 1:
            raise ValidationError("operation claim limit must be positive")
        now = _aware(now, "operation claim time")
        rows = self._session.scalars(
            select(ResultOperationAttemptModel)
            .where(
                ResultOperationAttemptModel.operation_kind == kind,
                ResultOperationAttemptModel.status.in_(("pending", "running")),
                (ResultOperationAttemptModel.lease_expiry.is_(None))
                | (ResultOperationAttemptModel.lease_expiry <= now),
            )
            .order_by(
                ResultOperationAttemptModel.created_at.asc(),
                ResultOperationAttemptModel.operation_id.asc(),
            )
            .with_for_update(skip_locked=True)
            .limit(limit)
        ).all()
        if not rows:
            return ()

        owner = f"result-repository:{uuid4()}"
        expiry = now + timedelta(minutes=5)
        for row in rows:
            row.status = ResultOperationStatus.RUNNING.value
            row.lease_owner = owner
            row.lease_token = str(uuid4())
            row.lease_expiry = expiry
            row.version += 1
            row.updated_at = now
        return tuple(_to_operation_attempt(row) for row in rows)

    def save_operation(
        self,
        operation: ResultOperation,
        *,
        expected_version: int,
    ) -> ResultOperation:
        if type(expected_version) is not int or expected_version < 0:
            raise ValidationError("operation expected_version must be non-negative")
        if isinstance(operation, ResultOperationAttemptV1):
            return self._save_attempt(operation, expected_version=expected_version)
        if isinstance(operation, ResultApplyOperationV1):
            return self._save_apply(operation, expected_version=expected_version)
        raise ValidationError("unsupported result operation type")

    def list_rework(
        self, root_task_id: UUID, workspace_id: UUID
    ) -> tuple[ReworkRecordV1, ...]:
        rows = self._session.scalars(
            select(ResultReworkRecordModel)
            .where(
                ResultReworkRecordModel.root_task_id == root_task_id,
                ResultReworkRecordModel.workspace_id == workspace_id,
            )
            .order_by(
                ResultReworkRecordModel.created_at.asc(),
                ResultReworkRecordModel.id.asc(),
            )
        ).all()
        return tuple(_to_rework(row) for row in rows)

    def get_rework_by_source_command(
        self,
        source_command_id: UUID,
        workspace_id: UUID,
        *,
        for_update: bool = False,
    ) -> ReworkRecordV1 | None:
        statement = select(ResultReworkRecordModel).where(
            ResultReworkRecordModel.source_command_id == source_command_id,
            ResultReworkRecordModel.workspace_id == workspace_id,
        )
        if for_update:
            statement = statement.with_for_update()
        row = self._session.scalar(statement)
        return _to_rework(row) if row is not None else None

    def retained_code_results(
        self, workspace_id: UUID
    ) -> tuple[CodeResultSnapshotV1, ...]:
        rows = self._session.scalars(
            select(CodeResultSnapshotModel)
            .where(CodeResultSnapshotModel.workspace_id == workspace_id)
            .order_by(
                CodeResultSnapshotModel.created_at.asc(),
                CodeResultSnapshotModel.id.asc(),
            )
        ).all()
        return tuple(_to_snapshot(row) for row in rows)

    def _save_attempt(
        self,
        operation: ResultOperationAttemptV1,
        *,
        expected_version: int,
    ) -> ResultOperationAttemptV1:
        values = _attempt_values(operation)
        if expected_version == 0:
            self._session.execute(
                insert(ResultOperationAttemptModel)
                .values(**values)
                .on_conflict_do_nothing()
            )
        else:
            current = self._get_attempt(operation.operation_id, operation.workspace_id)
            if current is None:
                raise ResultError(
                    "result_operation_not_found",
                    {"operation_id": str(operation.operation_id)},
                )
            _assert_operation_transition(current.status, operation.status)
            result = self._session.execute(
                update(ResultOperationAttemptModel)
                .where(
                    ResultOperationAttemptModel.operation_id == operation.operation_id,
                    ResultOperationAttemptModel.workspace_id == operation.workspace_id,
                    ResultOperationAttemptModel.version == expected_version,
                )
                .values(**values)
            )
            if not isinstance(result, CursorResult) or result.rowcount != 1:
                current = self._get_attempt(operation.operation_id, operation.workspace_id)
                if current is None:
                    raise ResultError(
                        "result_operation_not_found",
                        {"operation_id": str(operation.operation_id)},
                    )
                raise VersionConflict(expected_version, current.version)
        stored = self._get_attempt(operation.operation_id, operation.workspace_id)
        if stored is None:
            raise ResultError(
                "result_operation_not_found",
                {"operation_id": str(operation.operation_id)},
            )
        _assert_operation_transition(stored.status, operation.status)
        if stored.to_mapping() != operation.to_mapping():
            raise ResultError(
                "result_operation_conflict",
                {"operation_id": str(operation.operation_id)},
            )
        return stored

    def _save_apply(
        self,
        operation: ResultApplyOperationV1,
        *,
        expected_version: int,
    ) -> ResultApplyOperationV1:
        now = datetime.now(UTC)
        values = _apply_values(operation, now=now)
        if expected_version == 0:
            self._session.execute(
                insert(ResultApplyOperationModel)
                .values(**values)
                .on_conflict_do_nothing()
            )
        else:
            current = self.get_record(
                "apply_operation", operation.id, operation.workspace_id
            )
            if not isinstance(current, ResultApplyOperationV1):
                raise ResultError(
                    "result_operation_not_found", {"operation_id": str(operation.id)}
                )
            _assert_operation_transition(current.status, operation.status)
            result = self._session.execute(
                update(ResultApplyOperationModel)
                .where(
                    ResultApplyOperationModel.id == operation.id,
                    ResultApplyOperationModel.workspace_id == operation.workspace_id,
                    ResultApplyOperationModel.version == expected_version,
                )
                .values(**values)
            )
            if not isinstance(result, CursorResult) or result.rowcount != 1:
                current = self.get_record(
                    "apply_operation", operation.id, operation.workspace_id
                )
                if not isinstance(current, ResultApplyOperationV1):
                    raise ResultError(
                        "result_operation_not_found",
                        {"operation_id": str(operation.id)},
                    )
                raise VersionConflict(expected_version, current.version)
        stored = self.get_record("apply_operation", operation.id, operation.workspace_id)
        if not isinstance(stored, ResultApplyOperationV1):
            raise ResultError(
                "result_operation_not_found", {"operation_id": str(operation.id)}
            )
        _assert_operation_transition(stored.status, operation.status)
        if stored.to_mapping() != operation.to_mapping():
            raise ResultError(
                "result_operation_conflict", {"operation_id": str(operation.id)}
            )
        return stored

    def _get_attempt(
        self, operation_id: UUID, workspace_id: UUID
    ) -> ResultOperationAttemptV1 | None:
        row = self._session.scalar(
            select(ResultOperationAttemptModel).where(
                ResultOperationAttemptModel.operation_id == operation_id,
                ResultOperationAttemptModel.workspace_id == workspace_id,
            )
        )
        return _to_operation_attempt(row) if row is not None else None


def _normalize_kind(kind: str) -> str:
    if not isinstance(kind, str) or not kind.strip():
        raise ValidationError("result record kind must be a non-empty string")
    canonical = _KIND_ALIASES.get(kind.strip())
    if canonical is None:
        raise ResultError("result_record_kind_unsupported", {"kind": kind})
    return canonical


def _record_spec(
    kind: str,
) -> tuple[
    type[Any],
    Callable[[Any], dict[str, object]],
    Callable[[Any], ResultRecord],
    type[Any],
]:
    specs: dict[
        str,
        tuple[
            type[Any],
            Callable[[Any], dict[str, object]],
            Callable[[Any], ResultRecord],
            type[Any],
        ],
    ] = {
        "code_snapshot": (
            CodeResultSnapshotModel,
            _snapshot_values,
            _to_snapshot,
            CodeResultSnapshotV1,
        ),
        "candidate": (
            IntegrationCandidateModel,
            _candidate_values,
            _to_candidate,
            IntegrationCandidateV1,
        ),
        "verification_result": (
            ResultVerificationResultModel,
            _verification_values,
            _to_verification,
            VerificationResultV1,
        ),
        "acceptance": (
            TaskAcceptanceDecisionModel,
            _acceptance_values,
            _to_acceptance,
            TaskAcceptanceDecisionV1,
        ),
        "rework": (
            ResultReworkRecordModel,
            _rework_values,
            _to_rework,
            ReworkRecordV1,
        ),
        "apply_operation": (
            ResultApplyOperationModel,
            _apply_values,
            _to_apply,
            ResultApplyOperationV1,
        ),
    }
    return specs[kind]


def _head_values(head: TaskResultHead, *, now: datetime) -> dict[str, object]:
    return {
        "workspace_id": head.workspace_id,
        "root_task_id": head.root_task_id,
        "current_candidate_id": head.current_candidate_id,
        "current_acceptance_id": head.current_acceptance_id,
        "result_version": head.result_version,
        "active_operation_id": head.active_operation_id,
        "auto_rounds_used": head.auto_rounds_used,
        "auto_rounds_limit": head.auto_rounds_limit,
        "auto_seconds_limit": head.auto_seconds_limit,
        "auto_started_at": head.auto_started_at,
        "auto_deadline": head.auto_deadline,
        "created_at": now,
        "updated_at": now,
    }


def _head_update_values(head: TaskResultHead, *, now: datetime) -> dict[str, object]:
    values = _head_values(head, now=now)
    values.pop("created_at")
    return values


def _snapshot_values(value: CodeResultSnapshotV1) -> dict[str, object]:
    mapping = value.to_mapping()
    return {
        "id": value.id,
        "schema_version": value.schema_version,
        "workspace_id": value.workspace_id,
        "root_task_id": value.root_task_id,
        "worker_item_id": value.worker_item_id,
        "request_id": value.request_id,
        "execution_id": value.execution_id,
        "repository_ref": value.repository_ref,
        "input_commit": value.input_commit,
        "input_tree": value.input_tree,
        "result_commit": value.result_commit,
        "result_tree": value.result_tree,
        "consumed_result_ids": mapping["consumed_result_ids"],
        "artifact_ids": mapping["artifact_ids"],
        "capture_operation_id": value.capture_operation_id,
        "content_hash": value.content_hash,
        "payload": mapping,
        "created_at": value.created_at,
    }


def _candidate_values(value: IntegrationCandidateV1) -> dict[str, object]:
    mapping = value.to_mapping()
    return {
        "id": value.id,
        "schema_version": value.schema_version,
        "workspace_id": value.workspace_id,
        "root_task_id": value.root_task_id,
        "plan_snapshot_id": value.plan_snapshot_id,
        "plan_content_hash": value.plan_content_hash,
        "integration_execution_id": value.integration_execution_id,
        "base_commit": value.base_commit,
        "source_result_ids": mapping["source_result_ids"],
        "input_selection_id": value.input_selection_id,
        "input_selection_hash": value.input_selection_hash,
        "code_result_id": value.code_result_id,
        "previous_candidate_id": value.previous_candidate_id,
        "content_hash": value.content_hash,
        "payload": mapping,
        "created_at": value.created_at,
    }


def _verification_values(value: VerificationResultV1) -> dict[str, object]:
    return {
        "id": value.id,
        "schema_version": value.schema_version,
        "workspace_id": value.workspace_id,
        "root_task_id": value.root_task_id,
        "candidate_id": value.candidate_id,
        "execution_id": value.execution_id,
        "check_key": value.check_key,
        "check_version": value.check_version,
        "check_hash": value.check_hash,
        "environment_ref": value.environment_ref,
        "environment_hash": value.environment_hash,
        "actual_commit": value.actual_commit,
        "actual_tree": value.actual_tree,
        "operation_id": value.operation_id,
        "attempt": value.attempt,
        "status": value.status.value,
        "exit_code": value.exit_code,
        "started_at": value.started_at,
        "finished_at": value.finished_at,
        "artifact_ids": value.to_mapping()["artifact_ids"],
        "failure_code": value.failure_code,
        "payload": value.to_mapping(),
        "created_at": datetime.now(UTC),
    }


def _acceptance_values(value: TaskAcceptanceDecisionV1) -> dict[str, object]:
    return {
        "id": value.id,
        "workspace_id": value.workspace_id,
        "root_task_id": value.root_task_id,
        "candidate_id": value.candidate_id,
        "candidate_hash": value.candidate_hash,
        "plan_content_hash": value.plan_content_hash,
        "acceptance_basis_hash": value.acceptance_basis_hash,
        "result_version": value.result_version,
        "criteria_json": [item.to_mapping() for item in value.criteria],
        "actor_type": value.actor_type,
        "actor_id": value.actor_id,
        "command_id": value.command_id,
        "created_at": value.created_at,
    }


def _rework_values(value: ReworkRecordV1) -> dict[str, object]:
    return {
        "id": value.id,
        "workspace_id": value.workspace_id,
        "root_task_id": value.root_task_id,
        "candidate_id": value.candidate_id,
        "source_issue_keys_json": list(value.source_issue_keys),
        "target_worker_item_ids_json": [str(item) for item in value.target_worker_item_ids],
        "impact_worker_item_ids_json": [str(item) for item in value.impact_worker_item_ids],
        "reason": value.reason,
        "mode": value.mode.value,
        "round_number": value.round_number,
        "source_command_id": value.source_command_id,
        "request_ids_json": [str(item) for item in value.request_ids],
        "status": value.status.value,
        "created_at": value.created_at,
    }


def _apply_values(
    value: ResultApplyOperationV1, *, now: datetime | None = None
) -> dict[str, object]:
    current = now or datetime.now(UTC)
    return {
        "id": value.id,
        "workspace_id": value.workspace_id,
        "root_task_id": value.root_task_id,
        "acceptance_id": value.acceptance_id,
        "candidate_id": value.candidate_id,
        "repository_ref": value.repository_ref,
        "target_ref": value.target_ref,
        "expected_old_commit": value.expected_old_commit,
        "accepted_commit": value.accepted_commit,
        "status": value.status.value,
        "attempt": value.attempt,
        "lease_owner": value.lease_owner,
        "lease_token": value.lease_token,
        "lease_expiry": value.lease_expiry,
        "receipt_ref": value.receipt_ref,
        "failure_code": value.failure_code,
        "version": value.version,
        "created_at": current,
        "updated_at": current,
    }


def _attempt_values(value: ResultOperationAttemptV1) -> dict[str, object]:
    return {
        "operation_id": value.operation_id,
        "workspace_id": value.workspace_id,
        "root_task_id": value.root_task_id,
        "operation_kind": value.operation_kind,
        "attempt": value.attempt,
        "status": value.status.value,
        "payload": dict(value.payload),
        "lease_owner": value.lease_owner,
        "lease_token": value.lease_token,
        "lease_expiry": value.lease_expiry,
        "receipt_ref": value.receipt_ref,
        "failure_code": value.failure_code,
        "version": value.version,
        "created_at": value.created_at,
        "updated_at": value.updated_at,
    }


def _to_head(row: TaskResultHeadModel) -> TaskResultHead:
    return TaskResultHead(
        workspace_id=row.workspace_id,
        root_task_id=row.root_task_id,
        current_candidate_id=row.current_candidate_id,
        current_acceptance_id=row.current_acceptance_id,
        result_version=row.result_version,
        active_operation_id=row.active_operation_id,
        auto_rounds_used=row.auto_rounds_used,
        auto_rounds_limit=row.auto_rounds_limit,
        auto_seconds_limit=row.auto_seconds_limit,
        auto_started_at=row.auto_started_at,
        auto_deadline=row.auto_deadline,
    )


def _to_snapshot(row: CodeResultSnapshotModel) -> CodeResultSnapshotV1:
    return CodeResultSnapshotV1.from_mapping(row.payload)


def _to_candidate(row: IntegrationCandidateModel) -> IntegrationCandidateV1:
    return IntegrationCandidateV1.from_mapping(row.payload)


def _to_verification(row: ResultVerificationResultModel) -> VerificationResultV1:
    return VerificationResultV1.from_mapping(row.payload)


def _to_acceptance(row: TaskAcceptanceDecisionModel) -> TaskAcceptanceDecisionV1:
    return TaskAcceptanceDecisionV1.from_mapping(
        {
            "id": str(row.id),
            "workspace_id": str(row.workspace_id),
            "root_task_id": str(row.root_task_id),
            "candidate_id": str(row.candidate_id),
            "candidate_hash": row.candidate_hash,
            "plan_content_hash": row.plan_content_hash,
            "acceptance_basis_hash": row.acceptance_basis_hash,
            "result_version": row.result_version,
            "criteria": row.criteria_json,
            "actor_type": row.actor_type,
            "actor_id": row.actor_id,
            "command_id": str(row.command_id),
            "created_at": row.created_at.isoformat(),
        }
    )


def _to_rework(row: ResultReworkRecordModel) -> ReworkRecordV1:
    return ReworkRecordV1.from_mapping(
        {
            "id": str(row.id),
            "workspace_id": str(row.workspace_id),
            "root_task_id": str(row.root_task_id),
            "candidate_id": str(row.candidate_id),
            "source_issue_keys": row.source_issue_keys_json,
            "target_worker_item_ids": row.target_worker_item_ids_json,
            "impact_worker_item_ids": row.impact_worker_item_ids_json,
            "reason": row.reason,
            "mode": row.mode,
            "round_number": row.round_number,
            "source_command_id": str(row.source_command_id),
            "request_ids": row.request_ids_json,
            "status": row.status,
            "created_at": row.created_at.isoformat(),
        }
    )


def _to_apply(row: ResultApplyOperationModel) -> ResultApplyOperationV1:
    return ResultApplyOperationV1.from_mapping(
        {
            "id": str(row.id),
            "workspace_id": str(row.workspace_id),
            "root_task_id": str(row.root_task_id),
            "acceptance_id": str(row.acceptance_id),
            "candidate_id": str(row.candidate_id),
            "repository_ref": row.repository_ref,
            "target_ref": row.target_ref,
            "expected_old_commit": row.expected_old_commit,
            "accepted_commit": row.accepted_commit,
            "status": row.status,
            "attempt": row.attempt,
            "lease_owner": row.lease_owner,
            "lease_token": row.lease_token,
            "lease_expiry": (
                None if row.lease_expiry is None else row.lease_expiry.isoformat()
            ),
            "receipt_ref": row.receipt_ref,
            "failure_code": row.failure_code,
            "version": row.version,
        }
    )


def _to_operation_attempt(row: ResultOperationAttemptModel) -> ResultOperationAttemptV1:
    try:
        status = ResultOperationStatus(row.status)
    except ValueError as exc:
        raise ResultError(
            "result_operation_status_invalid", {"operation_id": str(row.operation_id)}
        ) from exc
    return ResultOperationAttemptV1(
        operation_id=row.operation_id,
        workspace_id=row.workspace_id,
        root_task_id=row.root_task_id,
        operation_kind=row.operation_kind,
        attempt=row.attempt,
        status=status,
        payload=row.payload,
        lease_owner=row.lease_owner,
        lease_token=row.lease_token,
        lease_expiry=row.lease_expiry,
        receipt_ref=row.receipt_ref,
        failure_code=row.failure_code,
        version=row.version,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


def _assert_operation_transition(
    current: ResultOperationStatus, requested: ResultOperationStatus
) -> None:
    if current is ResultOperationStatus.SUCCEEDED and requested is not current:
        raise ResultError("result_operation_terminal", {})
    if current is ResultOperationStatus.UNKNOWN and requested is not current:
        raise ResultError("result_operation_inspect_required", {})


def _aware(value: datetime, label: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValidationError(f"{label} must be timezone-aware")
    return value.astimezone(UTC)


def _is_integrity_error(value: BaseException) -> bool:
    from sqlalchemy.exc import IntegrityError

    return isinstance(value, IntegrityError)


__all__ = ["ResultRepository"]
