from __future__ import annotations

from datetime import UTC, datetime, timedelta
from hashlib import sha256
from typing import Any
from uuid import UUID, uuid4

import pytest
from sqlalchemy import select, update

from sidera_control_plane.adapters.reference_collaboration_runtime import (
    ReferenceCollaborationRuntime,
)
from sidera_control_plane.application.collaboration_decisions import (
    CollaborationDecisionConflict,
    CollaborationDecisionService,
)
from sidera_control_plane.application.collaboration_runtime_invoker import (
    RuntimeInvocationResult,
)
from sidera_control_plane.domain.collaboration_runtime import (
    ReleaseExecutionRequestIntentV1,
    RuntimeDecisionOutcome,
    RuntimeDecisionV1,
)
from sidera_control_plane.persistence.collaboration_fact_store import (
    SqlAlchemyCollaborationFactStore,
)
from sidera_control_plane.persistence.models import (
    CollaborationEvaluationModel,
    CollaborationRunModel,
    EventModel,
    ExecutionRequestModel,
    OutboxMessageModel,
    RootTaskPlanHeadModel,
    RuntimeDecisionModel,
    RuntimeIntentApplicationModel,
    RuntimeReleaseModel,
)

from .test_collaboration_evaluation_recovery import _invoker, _prepare
from .test_r5_1_execution_request_schema import request_row

pytestmark = pytest.mark.integration

NOW = datetime(2026, 9, 3, 12, 0, tzinfo=UTC)


def _service(uow_factory: Any) -> CollaborationDecisionService:
    return CollaborationDecisionService(
        uow_factory=uow_factory,
        fact_store_factory=lambda uow: SqlAlchemyCollaborationFactStore(
            session=uow.session
        ),
        owner_id="collaboration-apply-test",
    )


def _manual_result(
    prepared: Any,
    *,
    intents: tuple[ReleaseExecutionRequestIntentV1, ...],
    outcome: RuntimeDecisionOutcome = RuntimeDecisionOutcome.INTENTS,
) -> RuntimeInvocationResult:
    decision = RuntimeDecisionV1(
        schema_version=1,
        operation_id=prepared.operation_id,
        run_id=prepared.run.id,
        projection_id=prepared.projection.projection_id,
        projection_hash=prepared.projection.content_hash,
        outcome=outcome,
        reason_code="test_decision",
        summary="test decision",
        strategy_state={"test": True},
        intents=intents,
    )
    response_bytes = decision.canonical_bytes()
    return RuntimeInvocationResult(
        response_bytes=response_bytes,
        response_hash=sha256(response_bytes).hexdigest(),
        decision=decision,
        duration_ms=4,
    )


def _intent(
    prepared: Any,
    *,
    ordinal: int,
    request_id: UUID,
    request_version: int,
) -> ReleaseExecutionRequestIntentV1:
    return ReleaseExecutionRequestIntentV1(
        ordinal=ordinal,
        target_execution_request_id=request_id,
        target_request_version=request_version,
        reason_code="test_release",
        summary="release request in test",
        idempotency_key=(
            f"test-release:{prepared.run.id}:{request_id}:{ordinal}:{request_version}"
        ),
    )


def test_valid_decision_issues_release_and_updates_request_gate_atomically(
    database,
    workspace_id: UUID,
    uow_factory,
) -> None:
    ids, _snapshot, _reconciler, prepared = _prepare(
        database, workspace_id, uow_factory
    )
    invocation = _invoker(ReferenceCollaborationRuntime(clock=lambda: NOW)).invoke(
        prepared,
        now=NOW,
    )
    assert isinstance(invocation, RuntimeInvocationResult)

    result = _service(uow_factory).apply(prepared, invocation, now=NOW)

    assert result.status == "applied"
    assert len(result.release_ids) == 1
    assert len(result.applications) == 1
    assert result.applications[0].result_code == "applied"

    with database.session() as session:
        request = session.scalar(
            select(ExecutionRequestModel).where(
                ExecutionRequestModel.workspace_id == workspace_id,
                ExecutionRequestModel.id == ids["request"],
            )
        )
        evaluation = session.scalar(
            select(CollaborationEvaluationModel).where(
                CollaborationEvaluationModel.id == prepared.evaluation.id,
                CollaborationEvaluationModel.workspace_id == workspace_id,
            )
        )
        run = session.scalar(
            select(CollaborationRunModel).where(
                CollaborationRunModel.id == prepared.run.id,
                CollaborationRunModel.workspace_id == workspace_id,
            )
        )
        releases = session.scalars(
            select(RuntimeReleaseModel).where(
                RuntimeReleaseModel.workspace_id == workspace_id,
                RuntimeReleaseModel.execution_request_id == ids["request"],
            )
        ).all()
        decisions = session.scalars(
            select(RuntimeDecisionModel).where(
                RuntimeDecisionModel.workspace_id == workspace_id,
                RuntimeDecisionModel.run_id == prepared.run.id,
            )
        ).all()
        events = session.scalars(
            select(EventModel).where(
                EventModel.workspace_id == workspace_id,
                EventModel.correlation_id == prepared.run.id,
            )
        ).all()

    assert request is not None
    assert request.status == "ready"
    assert request.gate_reason == "ready"
    assert evaluation is not None
    assert evaluation.status == "completed"
    assert run is not None
    assert run.last_decision_id == result.decision.id
    assert len(releases) == 1
    assert len(decisions) == 1
    assert {event.event_type for event in events} >= {
        "CollaborationDecisionRecorded",
        "ExecutionRequestReleased",
    }


def test_matching_existing_release_is_applied_as_duplicate(
    database,
    workspace_id: UUID,
    uow_factory,
) -> None:
    ids, _snapshot, reconciler, prepared = _prepare(database, workspace_id, uow_factory)
    first = _invoker(ReferenceCollaborationRuntime(clock=lambda: NOW)).invoke(
        prepared,
        now=NOW,
    )
    assert isinstance(first, RuntimeInvocationResult)
    first_applied = _service(uow_factory).apply(prepared, first, now=NOW)
    assert first_applied.status == "applied"

    with uow_factory() as uow:
        uow.projection_changes.changed(
            workspace_id=workspace_id,
            projection_type="collaboration_runtime",
            resource_id=prepared.run.root_task_id,
            change_type="test.duplicate_wakeup",
            resource_version=1,
            correlation_id=uuid4(),
        )

    second = reconciler.prepare_next(
        owner_id="collaboration-duplicate",
        now=NOW + timedelta(seconds=1),
        workspace_id=workspace_id,
        root_task_id=prepared.run.root_task_id,
    )
    assert second is not None
    duplicate = _service(uow_factory).apply(
        second,
        _manual_result(
            second,
            intents=(
                _intent(
                    second,
                    ordinal=0,
                    request_id=ids["request"],
                    request_version=1,
                ),
            ),
        ),
        now=NOW + timedelta(seconds=1),
    )

    assert duplicate.status == "duplicate"
    assert duplicate.release_ids == ()
    assert duplicate.applications[0].status.value == "duplicate"
    assert duplicate.applications[0].release_id == first_applied.releases[0].id

    with database.session() as session:
        releases = session.scalars(
            select(RuntimeReleaseModel).where(
                RuntimeReleaseModel.workspace_id == workspace_id,
                RuntimeReleaseModel.execution_request_id == ids["request"],
            )
        ).all()
    assert len(releases) == 1


def test_changed_request_version_marks_the_whole_decision_stale(
    database,
    workspace_id: UUID,
    uow_factory,
) -> None:
    ids, _snapshot, _reconciler, prepared = _prepare(
        database, workspace_id, uow_factory
    )
    invocation = _invoker(ReferenceCollaborationRuntime(clock=lambda: NOW)).invoke(
        prepared,
        now=NOW,
    )
    assert isinstance(invocation, RuntimeInvocationResult)

    with database.session() as session:
        session.execute(
            update(ExecutionRequestModel)
            .where(
                ExecutionRequestModel.workspace_id == workspace_id,
                ExecutionRequestModel.id == ids["request"],
            )
            .values(version=2)
        )
        session.commit()

    applied = _service(uow_factory).apply(prepared, invocation, now=NOW)

    assert applied.status == "stale"
    assert all(item.status.value == "stale" for item in applied.applications)
    assert applied.release_ids == ()


def test_changed_join_fingerprint_marks_the_decision_stale(
    database,
    workspace_id: UUID,
    uow_factory,
) -> None:
    ids, _snapshot, _reconciler, prepared = _prepare(
        database, workspace_id, uow_factory
    )
    invocation = _invoker(ReferenceCollaborationRuntime(clock=lambda: NOW)).invoke(
        prepared,
        now=NOW,
    )
    assert isinstance(invocation, RuntimeInvocationResult)

    with database.session() as session:
        session.execute(
            update(ExecutionRequestModel)
            .where(
                ExecutionRequestModel.workspace_id == workspace_id,
                ExecutionRequestModel.id == ids["request"],
            )
            .values(expected_evidence_refs=["acceptance:changed"])
        )
        session.commit()

    applied = _service(uow_factory).apply(prepared, invocation, now=NOW)

    assert applied.status == "stale"
    assert applied.release_ids == ()
    assert all(item.status.value == "stale" for item in applied.applications)


def test_unknown_intent_target_is_rejected_before_ledger_insert(
    database,
    workspace_id: UUID,
    uow_factory,
) -> None:
    _ids, _snapshot, _reconciler, prepared = _prepare(
        database, workspace_id, uow_factory
    )
    result = _manual_result(
        prepared,
        intents=(
            _intent(
                prepared,
                ordinal=0,
                request_id=uuid4(),
                request_version=1,
            ),
        ),
    )

    with pytest.raises(
        CollaborationDecisionConflict, match="unknown request in this workspace"
    ):
        _service(uow_factory).apply(prepared, result, now=NOW)

    with database.session() as session:
        assert (
            session.scalar(
                select(RuntimeDecisionModel.id).where(
                    RuntimeDecisionModel.workspace_id == workspace_id,
                    RuntimeDecisionModel.evaluation_id == prepared.evaluation.id,
                )
            )
            is None
        )
        evaluation = session.scalar(
            select(CollaborationEvaluationModel).where(
                CollaborationEvaluationModel.workspace_id == workspace_id,
                CollaborationEvaluationModel.id == prepared.evaluation.id,
            )
        )

    assert evaluation is not None
    assert evaluation.status == "evaluating"


def test_one_stale_intent_rejects_the_whole_decision_without_a_release(
    database,
    workspace_id: UUID,
    uow_factory,
) -> None:
    ids, _snapshot, _reconciler, prepared = _prepare(
        database, workspace_id, uow_factory
    )
    second_request_id = uuid4()
    with database.session() as session:
        session.add(
            request_row(
                ids,
                request_id=second_request_id,
                worker_key="frontend",
            )
        )
        session.commit()

    # The original Projection was prepared before the second request existed;
    # make the test response explicitly use the prepared projection contract.
    result = _manual_result(
        prepared,
        intents=(
            _intent(
                prepared,
                ordinal=0,
                request_id=ids["request"],
                request_version=1,
            ),
            _intent(
                prepared,
                ordinal=1,
                request_id=second_request_id,
                request_version=99,
            ),
        ),
    )

    applied = _service(uow_factory).apply(prepared, result, now=NOW)

    assert applied.status == "stale"
    assert applied.release_ids == ()
    assert {item.status.value for item in applied.applications} == {"stale"}

    with database.session() as session:
        releases = session.scalars(
            select(RuntimeReleaseModel).where(
                RuntimeReleaseModel.workspace_id == workspace_id,
                RuntimeReleaseModel.run_id == prepared.run.id,
            )
        ).all()
        applications = session.scalars(
            select(RuntimeIntentApplicationModel).where(
                RuntimeIntentApplicationModel.workspace_id == workspace_id,
            )
        ).all()

    assert releases == []
    assert len(applications) == 2


def test_new_plan_head_makes_old_decision_stale_and_keeps_ledger_audit(
    database,
    workspace_id: UUID,
    uow_factory,
) -> None:
    ids, _snapshot, _reconciler, prepared = _prepare(
        database, workspace_id, uow_factory
    )
    with database.session() as session:
        session.execute(
            update(RootTaskPlanHeadModel)
            .where(
                RootTaskPlanHeadModel.workspace_id == workspace_id,
                RootTaskPlanHeadModel.root_task_id == ids["root_task"],
            )
            .values(active_generation=2, active_snapshot_hash="d" * 64, version=2)
        )
        session.commit()

    invocation = _invoker(ReferenceCollaborationRuntime(clock=lambda: NOW)).invoke(
        prepared,
        now=NOW,
    )
    assert isinstance(invocation, RuntimeInvocationResult)
    result = _service(uow_factory).apply(prepared, invocation, now=NOW)

    assert result.status == "stale"
    assert result.release_ids == ()
    assert all(item.status.value == "stale" for item in result.applications)

    with database.session() as session:
        assert (
            session.scalar(
                select(RuntimeDecisionModel.id).where(
                    RuntimeDecisionModel.workspace_id == workspace_id,
                    RuntimeDecisionModel.id == result.decision.id,
                )
            )
            == result.decision.id
        )
        assert (
            session.scalar(
                select(RuntimeReleaseModel.id).where(
                    RuntimeReleaseModel.workspace_id == workspace_id,
                    RuntimeReleaseModel.run_id == prepared.run.id,
                )
            )
            is None
        )


def test_no_change_decision_completes_without_intent_application(
    database,
    workspace_id: UUID,
    uow_factory,
) -> None:
    _ids, _snapshot, _reconciler, prepared = _prepare(
        database, workspace_id, uow_factory
    )
    result = _manual_result(
        prepared,
        intents=(),
        outcome=RuntimeDecisionOutcome.NO_CHANGE,
    )

    applied = _service(uow_factory).apply(prepared, result, now=NOW)

    assert applied.status == "no_change"
    assert applied.intents == ()
    assert applied.applications == ()
    assert applied.release_ids == ()

    with database.session() as session:
        evaluation = session.scalar(
            select(CollaborationEvaluationModel).where(
                CollaborationEvaluationModel.id == prepared.evaluation.id,
                CollaborationEvaluationModel.workspace_id == workspace_id,
            )
        )
    assert evaluation is not None
    assert evaluation.status == "completed"


def test_decision_application_rolls_back_everything_when_gate_composition_fails(
    database,
    workspace_id: UUID,
    uow_factory,
) -> None:
    ids, _snapshot, _reconciler, prepared = _prepare(
        database, workspace_id, uow_factory
    )
    invocation = _invoker(ReferenceCollaborationRuntime(clock=lambda: NOW)).invoke(
        prepared,
        now=NOW,
    )
    assert isinstance(invocation, RuntimeInvocationResult)

    def fail_gate_composition(_uow: Any, _request: Any) -> bool:
        raise RuntimeError("gate composition failed")

    service = CollaborationDecisionService(
        uow_factory=uow_factory,
        fact_store_factory=lambda uow: SqlAlchemyCollaborationFactStore(
            session=uow.session
        ),
        owner_id="collaboration-apply-test",
        plan_transition_allows=fail_gate_composition,
    )

    with pytest.raises(RuntimeError, match="gate composition failed"):
        service.apply(prepared, invocation, now=NOW)

    with database.session() as session:
        assert (
            session.scalar(
                select(RuntimeDecisionModel.id).where(
                    RuntimeDecisionModel.workspace_id == workspace_id,
                    RuntimeDecisionModel.evaluation_id == prepared.evaluation.id,
                )
            )
            is None
        )
        assert (
            session.scalar(
                select(RuntimeReleaseModel.id).where(
                    RuntimeReleaseModel.workspace_id == workspace_id,
                    RuntimeReleaseModel.run_id == prepared.run.id,
                )
            )
            is None
        )
        assert (
            session.scalar(
                select(RuntimeIntentApplicationModel.id).where(
                    RuntimeIntentApplicationModel.workspace_id == workspace_id,
                )
            )
            is None
        )
        assert (
            session.scalar(
                select(EventModel.id).where(
                    EventModel.workspace_id == workspace_id,
                    EventModel.event_type.in_(
                        {
                            "CollaborationEvaluationCompleted",
                            "CollaborationDecisionRecorded",
                            "ExecutionRequestReleased",
                        }
                    ),
                )
            )
            is None
        )
        assert (
            session.scalar(
                select(OutboxMessageModel.id).where(
                    OutboxMessageModel.topic.in_(
                        {
                            "collaboration_evaluation.CollaborationEvaluationCompleted",
                            "runtime_decision.CollaborationDecisionRecorded",
                            "execution_request.ExecutionRequestReleased",
                        }
                    )
                )
            )
            is None
        )
        evaluation = session.scalar(
            select(CollaborationEvaluationModel).where(
                CollaborationEvaluationModel.workspace_id == workspace_id,
                CollaborationEvaluationModel.id == prepared.evaluation.id,
            )
        )
        run = session.scalar(
            select(CollaborationRunModel).where(
                CollaborationRunModel.workspace_id == workspace_id,
                CollaborationRunModel.id == prepared.run.id,
            )
        )
        request = session.scalar(
            select(ExecutionRequestModel).where(
                ExecutionRequestModel.workspace_id == workspace_id,
                ExecutionRequestModel.id == ids["request"],
            )
        )

    assert evaluation is not None
    assert evaluation.status == "evaluating"
    assert evaluation.response_hash is None
    assert run is not None
    assert run.last_decision_id is None
    assert request is not None
    assert request.status == "blocked"
