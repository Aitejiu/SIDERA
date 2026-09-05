from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime, timedelta
from enum import StrEnum
from typing import Any
from uuid import UUID

from sqlalchemy import and_, func, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.engine import CursorResult
from sqlalchemy.orm import Session, aliased

from sidera_control_plane.domain.collaboration import (
    CollaborationEvaluation,
    CollaborationEvaluationStatus,
    CollaborationRun,
    CollaborationRunStatus,
    RuntimeDecision,
    RuntimeIntent,
    RuntimeIntentApplication,
    RuntimeIntentApplicationStatus,
    RuntimeProjectionSnapshot,
    RuntimeRelease,
)
from sidera_control_plane.domain.errors import VersionConflict
from sidera_control_plane.domain.modules import (
    CollaborationRuntimeCapabilityProfile,
    IdempotencyCapability,
    StabilityLevel,
)
from sidera_control_plane.persistence.models import (
    CollaborationEvaluationModel,
    CollaborationRunModel,
    RuntimeDecisionModel,
    RuntimeIntentApplicationModel,
    RuntimeIntentModel,
    RuntimeProjectionSnapshotModel,
    RuntimeReleaseModel,
)


class CollaborationRunRepository:
    def __init__(self, session: Session) -> None:
        self._session = session

    def get_current(
        self,
        workspace_id: UUID,
        root_task_id: UUID,
        *,
        for_update: bool = False,
    ) -> CollaborationRun | None:
        statement = select(CollaborationRunModel).where(
            CollaborationRunModel.workspace_id == workspace_id,
            CollaborationRunModel.root_task_id == root_task_id,
            CollaborationRunModel.status.in_(
                (
                    CollaborationRunStatus.PENDING.value,
                    CollaborationRunStatus.ACTIVE.value,
                    CollaborationRunStatus.BLOCKED.value,
                )
            ),
        )
        if for_update:
            statement = statement.with_for_update()
        row = self._session.scalar(statement)
        return _to_run(row) if row is not None else None

    def get(
        self,
        workspace_id: UUID,
        run_id: UUID,
        *,
        for_update: bool = False,
    ) -> CollaborationRun | None:
        statement = select(CollaborationRunModel).where(
            CollaborationRunModel.workspace_id == workspace_id,
            CollaborationRunModel.id == run_id,
        )
        if for_update:
            statement = statement.with_for_update()
        row = self._session.scalar(statement)
        return _to_run(row) if row is not None else None

    def get_for_plan(
        self,
        workspace_id: UUID,
        plan_snapshot_id: UUID,
        plan_generation: int,
        *,
        for_update: bool = False,
    ) -> CollaborationRun | None:
        statement = select(CollaborationRunModel).where(
            CollaborationRunModel.workspace_id == workspace_id,
            CollaborationRunModel.plan_snapshot_id == plan_snapshot_id,
            CollaborationRunModel.plan_generation == plan_generation,
        )
        if for_update:
            statement = statement.with_for_update()
        row = self._session.scalar(statement)
        return _to_run(row) if row is not None else None

    def add(self, run: CollaborationRun) -> None:
        self._session.add(_run_model(run))

    def save(self, run: CollaborationRun, previous_version: int) -> None:
        result = self._session.execute(
            update(CollaborationRunModel)
            .where(
                CollaborationRunModel.workspace_id == run.workspace_id,
                CollaborationRunModel.id == run.id,
                CollaborationRunModel.resource_version == previous_version,
            )
            .values(
                plan_snapshot_id=run.plan_snapshot_id,
                plan_generation=run.plan_generation,
                plan_content_hash=run.plan_content_hash,
                runtime_definition_id=run.runtime_definition_id,
                runtime_instance_id=run.runtime_instance_id,
                runtime_interface_version=run.runtime_interface_version,
                runtime_config_version=run.runtime_config_version,
                runtime_capability_snapshot_json=_plain_json(
                    _capability_mapping(run.runtime_capability_snapshot)
                ),
                policy_snapshot_json=_plain_json(run.policy_snapshot),
                status=run.status.value,
                last_projection_snapshot_id=run.last_projection_snapshot_id,
                last_decision_id=run.last_decision_id,
                last_evaluated_change_cursor=run.last_evaluated_change_cursor,
                next_evaluation_at=run.next_evaluation_at,
                consecutive_failure_count=run.consecutive_failure_count,
                blocked_reason=run.blocked_reason,
                resource_version=run.resource_version,
                activated_at=run.activated_at,
                completed_at=run.completed_at,
                superseded_at=run.superseded_at,
                updated_at=run.updated_at,
            )
        )
        if not isinstance(result, CursorResult) or result.rowcount != 1:
            raise VersionConflict(
                previous_version,
                self._current_version(run.id, run.workspace_id),
            )

    def list_recovery_candidates(
        self, *, now: datetime, limit: int
    ) -> tuple[CollaborationRun, ...]:
        _positive_limit(limit)
        rows = self._session.scalars(
            select(CollaborationRunModel)
            .where(
                CollaborationRunModel.status.in_(
                    (
                        CollaborationRunStatus.PENDING.value,
                        CollaborationRunStatus.ACTIVE.value,
                    )
                ),
                CollaborationRunModel.next_evaluation_at <= now,
            )
            .order_by(
                CollaborationRunModel.next_evaluation_at.asc(),
                CollaborationRunModel.created_at.asc(),
                CollaborationRunModel.id.asc(),
            )
            .limit(limit)
        ).all()
        return tuple(_to_run(row) for row in rows)

    def _current_version(self, run_id: UUID, workspace_id: UUID) -> int:
        version = self._session.scalar(
            select(CollaborationRunModel.resource_version).where(
                CollaborationRunModel.id == run_id,
                CollaborationRunModel.workspace_id == workspace_id,
            )
        )
        return int(version or 0)


class CollaborationEvaluationRepository:
    def __init__(self, session: Session) -> None:
        self._session = session

    def get(
        self,
        workspace_id: UUID,
        evaluation_id: UUID,
        *,
        for_update: bool = False,
    ) -> CollaborationEvaluation | None:
        statement = select(CollaborationEvaluationModel).where(
            CollaborationEvaluationModel.workspace_id == workspace_id,
            CollaborationEvaluationModel.id == evaluation_id,
        )
        if for_update:
            statement = statement.with_for_update()
        row = self._session.scalar(statement)
        return _to_evaluation(row) if row is not None else None

    def get_for_projection(
        self, workspace_id: UUID, projection_snapshot_id: UUID
    ) -> CollaborationEvaluation | None:
        row = self._session.scalar(
            select(CollaborationEvaluationModel).where(
                CollaborationEvaluationModel.workspace_id == workspace_id,
                CollaborationEvaluationModel.projection_snapshot_id
                == projection_snapshot_id,
            )
        )
        return _to_evaluation(row) if row is not None else None

    def get_live_for_run(
        self, workspace_id: UUID, run_id: UUID, *, now: datetime
    ) -> CollaborationEvaluation | None:
        row = self._session.scalar(
            select(CollaborationEvaluationModel)
            .where(
                CollaborationEvaluationModel.workspace_id == workspace_id,
                CollaborationEvaluationModel.run_id == run_id,
                CollaborationEvaluationModel.status
                == CollaborationEvaluationStatus.EVALUATING.value,
                CollaborationEvaluationModel.lease_expires_at > now,
            )
            .order_by(CollaborationEvaluationModel.id.asc())
        )
        return _to_evaluation(row) if row is not None else None

    def get_or_add(
        self, evaluation: CollaborationEvaluation
    ) -> CollaborationEvaluation:
        existing = self._session.scalar(
            select(CollaborationEvaluationModel).where(
                CollaborationEvaluationModel.workspace_id == evaluation.workspace_id,
                CollaborationEvaluationModel.run_id == evaluation.run_id,
                CollaborationEvaluationModel.projection_snapshot_id
                == evaluation.projection_snapshot_id,
            )
        )
        if existing is not None:
            return _to_evaluation(existing)

        self._session.execute(
            insert(CollaborationEvaluationModel)
            .values(**_evaluation_values(evaluation))
            .on_conflict_do_nothing(
                constraint="uq_collaboration_evaluations_run_projection"
            )
        )
        stored = self._session.scalar(
            select(CollaborationEvaluationModel).where(
                CollaborationEvaluationModel.workspace_id == evaluation.workspace_id,
                CollaborationEvaluationModel.run_id == evaluation.run_id,
                CollaborationEvaluationModel.projection_snapshot_id
                == evaluation.projection_snapshot_id,
            )
        )
        if stored is None:
            raise ValueError("evaluation operation_id is already in use")
        return _to_evaluation(stored)

    def claim_next(
        self,
        *,
        workspace_id: UUID | None = None,
        run_id: UUID | None = None,
        owner_id: str,
        now: datetime,
        lease_ttl: timedelta,
        limit: int,
    ) -> tuple[CollaborationEvaluation, ...]:
        _positive_limit(limit)
        if lease_ttl <= timedelta(0):
            raise ValueError("lease_ttl must be positive")

        claimed: list[CollaborationEvaluation] = []
        while len(claimed) < limit:
            live_row = aliased(CollaborationEvaluationModel)
            live_evaluation = (
                select(live_row.id)
                .where(
                    live_row.workspace_id == CollaborationEvaluationModel.workspace_id,
                    live_row.run_id == CollaborationEvaluationModel.run_id,
                    live_row.status == CollaborationEvaluationStatus.EVALUATING.value,
                    live_row.lease_expires_at > now,
                )
                .correlate(CollaborationEvaluationModel)
                .exists()
            )
            conditions = [
                CollaborationEvaluationModel.status.in_(
                    (
                        CollaborationEvaluationStatus.PENDING.value,
                        CollaborationEvaluationStatus.RETRY_WAIT.value,
                    )
                ),
                CollaborationEvaluationModel.next_attempt_at <= now,
                ~live_evaluation,
            ]
            if workspace_id is not None:
                conditions.append(
                    CollaborationEvaluationModel.workspace_id == workspace_id
                )
            if run_id is not None:
                conditions.append(CollaborationEvaluationModel.run_id == run_id)
            statement = (
                select(CollaborationEvaluationModel)
                .join(
                    CollaborationRunModel,
                    and_(
                        CollaborationRunModel.id == CollaborationEvaluationModel.run_id,
                        CollaborationRunModel.workspace_id
                        == CollaborationEvaluationModel.workspace_id,
                    ),
                )
                .where(*conditions)
                .order_by(
                    CollaborationEvaluationModel.next_attempt_at.asc(),
                    CollaborationEvaluationModel.created_at.asc(),
                    CollaborationEvaluationModel.id.asc(),
                )
                .limit(1)
                .with_for_update(skip_locked=True)
            )
            row = self._session.scalars(statement).first()
            if row is None:
                break
            item = _to_evaluation(row)
            previous_version = item.resource_version
            item.claim(
                owner_id=owner_id,
                lease_expires_at=now + lease_ttl,
                now=now,
            )
            self.save(item, previous_version)
            claimed.append(item)
        return tuple(claimed)

    def save(self, evaluation: CollaborationEvaluation, previous_version: int) -> None:
        result = self._session.execute(
            update(CollaborationEvaluationModel)
            .where(
                CollaborationEvaluationModel.id == evaluation.id,
                CollaborationEvaluationModel.workspace_id == evaluation.workspace_id,
                CollaborationEvaluationModel.resource_version == previous_version,
            )
            .values(
                status=evaluation.status.value,
                attempt=evaluation.attempt,
                lease_owner=evaluation.lease_owner,
                lease_expires_at=evaluation.lease_expires_at,
                next_attempt_at=evaluation.next_attempt_at,
                last_error_code=evaluation.last_error_code,
                response_hash=evaluation.response_hash,
                diagnostic_summary=evaluation.diagnostic_summary,
                resource_version=evaluation.resource_version,
                completed_at=evaluation.completed_at,
                updated_at=evaluation.updated_at,
            )
        )
        if not isinstance(result, CursorResult) or result.rowcount != 1:
            raise VersionConflict(
                previous_version,
                self._current_version(evaluation.id, evaluation.workspace_id),
            )

    def recover_expired(
        self,
        *,
        now: datetime,
        limit: int,
        workspace_id: UUID | None = None,
    ) -> tuple[CollaborationEvaluation, ...]:
        _positive_limit(limit)
        conditions = [
            CollaborationEvaluationModel.status
            == CollaborationEvaluationStatus.EVALUATING.value,
            CollaborationEvaluationModel.lease_expires_at <= now,
        ]
        if workspace_id is not None:
            conditions.append(CollaborationEvaluationModel.workspace_id == workspace_id)
        rows = self._session.scalars(
            select(CollaborationEvaluationModel)
            .where(*conditions)
            .order_by(
                CollaborationEvaluationModel.lease_expires_at.asc(),
                CollaborationEvaluationModel.created_at.asc(),
                CollaborationEvaluationModel.id.asc(),
            )
            .limit(limit)
            .with_for_update(skip_locked=True)
        ).all()
        recovered: list[CollaborationEvaluation] = []
        for row in rows:
            item = _to_evaluation(row)
            previous_version = item.resource_version
            item.schedule_retry(
                error_code="evaluation_lease_expired",
                diagnostic_summary="evaluation lease expired before completion",
                next_attempt_at=now,
                now=now,
            )
            self.save(item, previous_version)
            recovered.append(item)
        return tuple(recovered)

    def _current_version(self, evaluation_id: UUID, workspace_id: UUID) -> int:
        version = self._session.scalar(
            select(CollaborationEvaluationModel.resource_version).where(
                CollaborationEvaluationModel.id == evaluation_id,
                CollaborationEvaluationModel.workspace_id == workspace_id,
            )
        )
        return int(version or 0)


class CollaborationLedgerRepository:
    def __init__(self, session: Session) -> None:
        self._session = session

    def get_projection(
        self, workspace_id: UUID, projection_snapshot_id: UUID
    ) -> RuntimeProjectionSnapshot | None:
        row = self._session.scalar(
            select(RuntimeProjectionSnapshotModel).where(
                RuntimeProjectionSnapshotModel.workspace_id == workspace_id,
                RuntimeProjectionSnapshotModel.id == projection_snapshot_id,
            )
        )
        return _to_projection(row) if row is not None else None

    def get_or_add_projection(
        self, snapshot: RuntimeProjectionSnapshot
    ) -> RuntimeProjectionSnapshot:
        existing = self._session.scalar(
            select(RuntimeProjectionSnapshotModel).where(
                RuntimeProjectionSnapshotModel.workspace_id == snapshot.workspace_id,
                RuntimeProjectionSnapshotModel.run_id == snapshot.run_id,
                RuntimeProjectionSnapshotModel.schema_version
                == snapshot.schema_version,
                RuntimeProjectionSnapshotModel.content_hash == snapshot.content_hash,
            )
        )
        if existing is None:
            self._session.execute(
                insert(RuntimeProjectionSnapshotModel)
                .values(**_projection_values(snapshot))
                .on_conflict_do_nothing(
                    constraint="uq_runtime_projection_run_schema_hash"
                )
            )
            existing = self._session.scalar(
                select(RuntimeProjectionSnapshotModel).where(
                    RuntimeProjectionSnapshotModel.workspace_id
                    == snapshot.workspace_id,
                    RuntimeProjectionSnapshotModel.run_id == snapshot.run_id,
                    RuntimeProjectionSnapshotModel.schema_version
                    == snapshot.schema_version,
                    RuntimeProjectionSnapshotModel.content_hash
                    == snapshot.content_hash,
                )
            )
        if existing is None:
            raise RuntimeError("projection snapshot insert returned no row")
        return _to_projection(existing)

    def next_decision_sequence(self, run_id: UUID, workspace_id: UUID) -> int:
        run_exists = self._session.scalar(
            select(CollaborationRunModel.id)
            .where(
                CollaborationRunModel.id == run_id,
                CollaborationRunModel.workspace_id == workspace_id,
            )
            .with_for_update()
        )
        if run_exists is None:
            raise ValueError("collaboration run does not exist in workspace")
        sequence = self._session.scalar(
            select(func.max(RuntimeDecisionModel.sequence)).where(
                RuntimeDecisionModel.run_id == run_id,
                RuntimeDecisionModel.workspace_id == workspace_id,
            )
        )
        return int(sequence or 0) + 1

    def add_decision_bundle(
        self, decision: RuntimeDecision, intents: tuple[RuntimeIntent, ...]
    ) -> None:
        for intent in intents:
            if (
                intent.workspace_id != decision.workspace_id
                or intent.run_id != decision.run_id
                or intent.decision_id != decision.id
            ):
                raise ValueError("decision bundle contains an unrelated intent")
        self._session.add(_decision_model(decision))
        self._session.add_all(_intent_model(intent) for intent in intents)
        self._session.flush()

    def add_applications(
        self, applications: tuple[RuntimeIntentApplication, ...]
    ) -> None:
        self._session.add_all(_application_model(item) for item in applications)

    def add_releases(self, releases: tuple[RuntimeRelease, ...]) -> None:
        self._session.add_all(_release_model(item) for item in releases)

    def get_release_for_request(
        self,
        workspace_id: UUID,
        request_id: UUID,
        *,
        for_update: bool = False,
    ) -> RuntimeRelease | None:
        statement = select(RuntimeReleaseModel).where(
            RuntimeReleaseModel.workspace_id == workspace_id,
            RuntimeReleaseModel.execution_request_id == request_id,
        )
        if for_update:
            statement = statement.with_for_update()
        row = self._session.scalar(statement)
        return _to_release(row) if row is not None else None

    def get_decision(
        self, workspace_id: UUID, decision_id: UUID, *, for_update: bool = False
    ) -> RuntimeDecision | None:
        statement = select(RuntimeDecisionModel).where(
            RuntimeDecisionModel.workspace_id == workspace_id,
            RuntimeDecisionModel.id == decision_id,
        )
        if for_update:
            statement = statement.with_for_update()
        row = self._session.scalar(statement)
        return _to_decision(row) if row is not None else None

    def get_intent(
        self, workspace_id: UUID, intent_id: UUID, *, for_update: bool = False
    ) -> RuntimeIntent | None:
        statement = select(RuntimeIntentModel).where(
            RuntimeIntentModel.workspace_id == workspace_id,
            RuntimeIntentModel.id == intent_id,
        )
        if for_update:
            statement = statement.with_for_update()
        row = self._session.scalar(statement)
        return _to_intent(row) if row is not None else None

    def get_application(
        self, workspace_id: UUID, application_id: UUID, *, for_update: bool = False
    ) -> RuntimeIntentApplication | None:
        statement = select(RuntimeIntentApplicationModel).where(
            RuntimeIntentApplicationModel.workspace_id == workspace_id,
            RuntimeIntentApplicationModel.id == application_id,
        )
        if for_update:
            statement = statement.with_for_update()
        row = self._session.scalar(statement)
        return _to_application(row) if row is not None else None

    def save_application(
        self, application: RuntimeIntentApplication, previous_version: int
    ) -> None:
        result = self._session.execute(
            update(RuntimeIntentApplicationModel)
            .where(
                RuntimeIntentApplicationModel.id == application.id,
                RuntimeIntentApplicationModel.workspace_id == application.workspace_id,
                RuntimeIntentApplicationModel.resource_version == previous_version,
            )
            .values(
                status=application.status.value,
                release_id=application.release_id,
                result_code=application.result_code,
                result_summary=application.result_summary,
                resource_version=application.resource_version,
                applied_at=application.applied_at,
                updated_at=application.applied_at or application.created_at,
            )
        )
        if not isinstance(result, CursorResult) or result.rowcount != 1:
            current = self._session.scalar(
                select(RuntimeIntentApplicationModel.resource_version).where(
                    RuntimeIntentApplicationModel.id == application.id,
                    RuntimeIntentApplicationModel.workspace_id == application.workspace_id,
                )
            )
            raise VersionConflict(previous_version, int(current or 0))


def _run_model(run: CollaborationRun) -> CollaborationRunModel:
    return CollaborationRunModel(
        id=run.id,
        workspace_id=run.workspace_id,
        root_task_id=run.root_task_id,
        plan_snapshot_id=run.plan_snapshot_id,
        plan_generation=run.plan_generation,
        plan_content_hash=run.plan_content_hash,
        runtime_definition_id=run.runtime_definition_id,
        runtime_instance_id=run.runtime_instance_id,
        runtime_interface_version=run.runtime_interface_version,
        runtime_config_version=run.runtime_config_version,
        runtime_capability_snapshot_json=_plain_json(
            _capability_mapping(run.runtime_capability_snapshot)
        ),
        policy_snapshot_json=_plain_json(run.policy_snapshot),
        status=run.status.value,
        last_projection_snapshot_id=run.last_projection_snapshot_id,
        last_decision_id=run.last_decision_id,
        last_evaluated_change_cursor=run.last_evaluated_change_cursor,
        next_evaluation_at=run.next_evaluation_at,
        consecutive_failure_count=run.consecutive_failure_count,
        blocked_reason=run.blocked_reason,
        resource_version=run.resource_version,
        activated_at=run.activated_at,
        completed_at=run.completed_at,
        superseded_at=run.superseded_at,
        created_at=run.created_at,
        updated_at=run.updated_at,
    )


def _evaluation_values(evaluation: CollaborationEvaluation) -> dict[str, object]:
    return {
        "id": evaluation.id,
        "workspace_id": evaluation.workspace_id,
        "run_id": evaluation.run_id,
        "projection_snapshot_id": evaluation.projection_snapshot_id,
        "operation_id": evaluation.operation_id,
        "status": evaluation.status.value,
        "attempt": evaluation.attempt,
        "lease_owner": evaluation.lease_owner,
        "lease_expires_at": evaluation.lease_expires_at,
        "next_attempt_at": evaluation.next_attempt_at,
        "last_error_code": evaluation.last_error_code,
        "response_hash": evaluation.response_hash,
        "diagnostic_summary": evaluation.diagnostic_summary,
        "resource_version": evaluation.resource_version,
        "completed_at": evaluation.completed_at,
        "created_at": evaluation.created_at,
        "updated_at": evaluation.updated_at,
    }


def _projection_values(snapshot: RuntimeProjectionSnapshot) -> dict[str, object]:
    return {
        "id": snapshot.id,
        "workspace_id": snapshot.workspace_id,
        "run_id": snapshot.run_id,
        "schema_version": snapshot.schema_version,
        "plan_snapshot_id": snapshot.plan_snapshot_id,
        "plan_generation": snapshot.plan_generation,
        "plan_content_hash": snapshot.plan_content_hash,
        "fact_cursor": snapshot.fact_cursor,
        "payload_json": _plain_json(snapshot.payload),
        "content_hash": snapshot.content_hash,
        "size_bytes": snapshot.size_bytes,
        "created_at": snapshot.created_at,
    }


def _decision_model(decision: RuntimeDecision) -> RuntimeDecisionModel:
    return RuntimeDecisionModel(
        id=decision.id,
        workspace_id=decision.workspace_id,
        run_id=decision.run_id,
        evaluation_id=decision.evaluation_id,
        projection_snapshot_id=decision.projection_snapshot_id,
        projection_hash=decision.projection_hash,
        operation_id=decision.operation_id,
        schema_version=decision.schema_version,
        sequence=decision.sequence,
        outcome=decision.outcome,
        reason_code=decision.reason_code,
        summary=decision.summary,
        strategy_state_json=_plain_json(decision.strategy_state),
        content_hash=decision.content_hash,
        runtime_duration_ms=decision.runtime_duration_ms,
        created_at=decision.created_at,
    )


def _intent_model(intent: RuntimeIntent) -> RuntimeIntentModel:
    return RuntimeIntentModel(
        id=intent.id,
        workspace_id=intent.workspace_id,
        run_id=intent.run_id,
        decision_id=intent.decision_id,
        ordinal=intent.ordinal,
        intent_type=intent.intent_type,
        target_execution_request_id=intent.target_execution_request_id,
        target_request_version=intent.target_request_version,
        reason_code=intent.reason_code,
        summary=intent.summary,
        idempotency_key=intent.idempotency_key,
        payload_json=_plain_json(intent.payload_json),
        created_at=intent.created_at,
    )


def _application_model(
    application: RuntimeIntentApplication,
) -> RuntimeIntentApplicationModel:
    return RuntimeIntentApplicationModel(
        id=application.id,
        workspace_id=application.workspace_id,
        intent_id=application.intent_id,
        status=application.status.value,
        expected_run_version=application.expected_run_version,
        observed_run_version=application.observed_run_version,
        expected_request_version=application.expected_request_version,
        observed_request_version=application.observed_request_version,
        release_id=application.release_id,
        result_code=application.result_code,
        result_summary=application.result_summary,
        resource_version=application.resource_version,
        applied_at=application.applied_at,
        created_at=application.created_at,
        updated_at=application.created_at,
    )


def _release_model(release: RuntimeRelease) -> RuntimeReleaseModel:
    return RuntimeReleaseModel(
        id=release.id,
        workspace_id=release.workspace_id,
        run_id=release.run_id,
        plan_snapshot_id=release.plan_snapshot_id,
        plan_generation=release.plan_generation,
        plan_content_hash=release.plan_content_hash,
        decision_id=release.decision_id,
        intent_id=release.intent_id,
        execution_request_id=release.execution_request_id,
        request_version_at_release=release.request_version_at_release,
        input_selection_id=release.input_selection_id,
        input_selection_hash=release.input_selection_hash,
        issued_at=release.issued_at,
    )


def _to_run(row: CollaborationRunModel) -> CollaborationRun:
    return CollaborationRun(
        id=row.id,
        workspace_id=row.workspace_id,
        root_task_id=row.root_task_id,
        plan_snapshot_id=row.plan_snapshot_id,
        plan_generation=row.plan_generation,
        plan_content_hash=row.plan_content_hash,
        runtime_definition_id=row.runtime_definition_id,
        runtime_instance_id=row.runtime_instance_id,
        runtime_interface_version=row.runtime_interface_version,
        runtime_config_version=row.runtime_config_version,
        runtime_capability_snapshot=_capability_from_mapping(
            row.runtime_capability_snapshot_json
        ),
        policy_snapshot=row.policy_snapshot_json,
        status=CollaborationRunStatus(row.status),
        last_projection_snapshot_id=row.last_projection_snapshot_id,
        last_decision_id=row.last_decision_id,
        last_evaluated_change_cursor=row.last_evaluated_change_cursor,
        next_evaluation_at=row.next_evaluation_at,
        consecutive_failure_count=row.consecutive_failure_count,
        blocked_reason=row.blocked_reason,
        resource_version=row.resource_version,
        created_at=row.created_at,
        activated_at=row.activated_at,
        completed_at=row.completed_at,
        superseded_at=row.superseded_at,
        updated_at=row.updated_at,
    )


def _to_evaluation(row: CollaborationEvaluationModel) -> CollaborationEvaluation:
    return CollaborationEvaluation(
        id=row.id,
        workspace_id=row.workspace_id,
        run_id=row.run_id,
        projection_snapshot_id=row.projection_snapshot_id,
        operation_id=row.operation_id,
        status=CollaborationEvaluationStatus(row.status),
        attempt=row.attempt,
        lease_owner=row.lease_owner,
        lease_expires_at=row.lease_expires_at,
        next_attempt_at=row.next_attempt_at,
        last_error_code=row.last_error_code,
        response_hash=row.response_hash,
        diagnostic_summary=row.diagnostic_summary,
        resource_version=row.resource_version,
        created_at=row.created_at,
        updated_at=row.updated_at,
        completed_at=row.completed_at,
    )


def _to_projection(row: RuntimeProjectionSnapshotModel) -> RuntimeProjectionSnapshot:
    return RuntimeProjectionSnapshot(
        id=row.id,
        workspace_id=row.workspace_id,
        run_id=row.run_id,
        schema_version=row.schema_version,
        plan_snapshot_id=row.plan_snapshot_id,
        plan_generation=row.plan_generation,
        plan_content_hash=row.plan_content_hash,
        fact_cursor=row.fact_cursor,
        payload=row.payload_json,
        content_hash=row.content_hash,
        size_bytes=row.size_bytes,
        created_at=row.created_at,
    )


def _to_release(row: RuntimeReleaseModel) -> RuntimeRelease:
    return RuntimeRelease(
        id=row.id,
        workspace_id=row.workspace_id,
        run_id=row.run_id,
        plan_snapshot_id=row.plan_snapshot_id,
        plan_generation=row.plan_generation,
        plan_content_hash=row.plan_content_hash,
        decision_id=row.decision_id,
        intent_id=row.intent_id,
        execution_request_id=row.execution_request_id,
        request_version_at_release=row.request_version_at_release,
        input_selection_id=row.input_selection_id,
        input_selection_hash=row.input_selection_hash,
        issued_at=row.issued_at,
    )


def _to_decision(row: RuntimeDecisionModel) -> RuntimeDecision:
    return RuntimeDecision(
        id=row.id,
        workspace_id=row.workspace_id,
        run_id=row.run_id,
        evaluation_id=row.evaluation_id,
        projection_snapshot_id=row.projection_snapshot_id,
        projection_hash=row.projection_hash,
        operation_id=row.operation_id,
        schema_version=row.schema_version,
        sequence=row.sequence,
        outcome=row.outcome,
        reason_code=row.reason_code,
        summary=row.summary,
        strategy_state=row.strategy_state_json,
        content_hash=row.content_hash,
        runtime_duration_ms=row.runtime_duration_ms,
        created_at=row.created_at,
    )


def _to_intent(row: RuntimeIntentModel) -> RuntimeIntent:
    return RuntimeIntent(
        id=row.id,
        workspace_id=row.workspace_id,
        run_id=row.run_id,
        decision_id=row.decision_id,
        ordinal=row.ordinal,
        intent_type=row.intent_type,
        target_execution_request_id=row.target_execution_request_id,
        target_request_version=row.target_request_version,
        reason_code=row.reason_code,
        summary=row.summary,
        idempotency_key=row.idempotency_key,
        payload_json=row.payload_json,
        created_at=row.created_at,
    )


def _to_application(row: RuntimeIntentApplicationModel) -> RuntimeIntentApplication:
    return RuntimeIntentApplication(
        id=row.id,
        workspace_id=row.workspace_id,
        intent_id=row.intent_id,
        status=RuntimeIntentApplicationStatus(row.status),
        expected_run_version=row.expected_run_version,
        observed_run_version=row.observed_run_version,
        expected_request_version=row.expected_request_version,
        observed_request_version=row.observed_request_version,
        release_id=row.release_id,
        result_code=row.result_code,
        result_summary=row.result_summary,
        resource_version=row.resource_version,
        created_at=row.created_at,
        applied_at=row.applied_at,
    )


def _capability_mapping(
    capability: CollaborationRuntimeCapabilityProfile,
) -> dict[str, object]:
    return {
        "interface_version": capability.interface_version,
        "projection_schema_versions": list(capability.projection_schema_versions),
        "decision_schema_versions": list(capability.decision_schema_versions),
        "strategy_ids": list(capability.strategy_ids),
        "idempotency": capability.idempotency.value,
        "max_projection_bytes": capability.max_projection_bytes,
        "max_decision_bytes": capability.max_decision_bytes,
        "max_intents_per_decision": capability.max_intents_per_decision,
        "max_strategy_state_bytes": capability.max_strategy_state_bytes,
        "stability": capability.stability.value,
    }


def _capability_from_mapping(
    value: Mapping[str, Any],
) -> CollaborationRuntimeCapabilityProfile:
    return CollaborationRuntimeCapabilityProfile(
        interface_version=value["interface_version"],
        projection_schema_versions=tuple(value["projection_schema_versions"]),
        decision_schema_versions=tuple(value["decision_schema_versions"]),
        strategy_ids=tuple(value["strategy_ids"]),
        idempotency=IdempotencyCapability(value["idempotency"]),
        max_projection_bytes=value["max_projection_bytes"],
        max_decision_bytes=value["max_decision_bytes"],
        max_intents_per_decision=value["max_intents_per_decision"],
        max_strategy_state_bytes=value["max_strategy_state_bytes"],
        stability=StabilityLevel(value["stability"]),
    )


def _plain_json(value: object) -> Any:
    if isinstance(value, Mapping):
        return {str(key): _plain_json(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_plain_json(item) for item in value]
    if isinstance(value, StrEnum):
        return value.value
    if isinstance(value, UUID):
        return str(value)
    if isinstance(value, datetime):
        return value.isoformat()
    return value


def _positive_limit(limit: int) -> None:
    if limit < 1:
        raise ValueError("limit must be positive")
