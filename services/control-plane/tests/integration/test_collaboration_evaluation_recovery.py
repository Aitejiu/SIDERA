from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

import pytest
from sqlalchemy import select, update

from sidera_control_plane.adapters.reference_collaboration_runtime import (
    ReferenceCollaborationRuntime,
)
from sidera_control_plane.application.collaboration_reconciler import (
    CollaborationReconciler,
)
from sidera_control_plane.application.collaboration_runtime_invoker import (
    CollaborationRuntimeInvoker,
    RuntimeInvocationFailure,
    RuntimeInvocationResult,
)
from sidera_control_plane.application.collaboration_runtime_resolver import (
    CollaborationRuntimeResolver,
    RegistryCollaborationRuntimeFactory,
    ResolvedCollaborationRuntime,
)
from sidera_control_plane.domain.collaboration import CollaborationRun
from sidera_control_plane.domain.collaboration_runtime import RuntimeProjectionV1
from sidera_control_plane.domain.plan_revisions import plan_snapshot_content_hash
from sidera_control_plane.persistence.collaboration_fact_store import (
    SqlAlchemyCollaborationFactStore,
)
from sidera_control_plane.persistence.models import (
    CollaborationEvaluationModel,
    CollaborationRunModel,
    EventModel,
    ExecutionModel,
    ExecutionRequestModel,
    ModuleDefinitionModel,
    PlanSnapshotModel,
    RootTaskPlanHeadModel,
    RuntimeDecisionModel,
    RuntimeReleaseModel,
)
from sidera_control_plane.persistence.module_repository import (
    capability_profile_to_payload,
)

from .test_collaboration_reconciler import (
    _capability,
    _snapshot,
)
from .test_r5_1_execution_request_schema import (
    request_row,
    seed_execution_request_facts,
)

pytestmark = pytest.mark.integration

NOW = datetime(2026, 9, 3, 12, 0, tzinfo=UTC)


class StaticResolver:
    def __init__(self, runtime: Any) -> None:
        self.runtime = runtime

    def resolve(self, run: CollaborationRun) -> ResolvedCollaborationRuntime:
        return ResolvedCollaborationRuntime(
            runtime=self.runtime,
            provider="test",
            interface_version=run.runtime_interface_version,
            config_version=run.runtime_config_version,
        )


class TimeoutRuntime:
    def evaluate(
        self,
        projection: RuntimeProjectionV1,
        *,
        operation_id: UUID,
        deadline: datetime,
    ) -> object:
        raise TimeoutError("provider deadline exceeded")


class InvalidOutputRuntime:
    def evaluate(
        self,
        projection: RuntimeProjectionV1,
        *,
        operation_id: UUID,
        deadline: datetime,
    ) -> object:
        return b'{"unknown":"provider response"}'


def _prepare_fixture(
    database, workspace_id: UUID, uow_factory
) -> tuple[dict[str, UUID], Any, CollaborationReconciler]:
    ids = seed_execution_request_facts(database, workspace_id)
    snapshot = _snapshot(
        workspace_id=workspace_id,
        root_task_id=ids["root_task"],
        snapshot_id=ids["plan"],
        definition_id=ids["module_definition"],
        instance_id=ids["module_instance"],
    )
    with database.session() as session:
        session.add(request_row(ids, request_id=ids["request"]))
        session.flush()
        session.execute(
            update(ModuleDefinitionModel)
            .where(
                ModuleDefinitionModel.id == ids["module_definition"],
                ModuleDefinitionModel.workspace_id == workspace_id,
            )
            .values(
                module_type="collaboration_runtime",
                provider="reference",
                interface_version="collaboration-runtime.v1",
                capabilities=capability_profile_to_payload(_capability()),
            )
        )
        session.execute(
            update(PlanSnapshotModel)
            .where(
                PlanSnapshotModel.id == snapshot.id,
                PlanSnapshotModel.workspace_id == workspace_id,
            )
            .values(
                schema_version=snapshot.schema_version,
                root_task_id=snapshot.root_task_id,
                generation=snapshot.generation,
                parent_snapshot_id=snapshot.parent_snapshot_id,
                source_type=snapshot.source_type,
                source_ref=snapshot.source_ref,
                title=snapshot.title,
                goal_summary=snapshot.goal_summary,
                payload=snapshot.to_payload(),
                published_by=snapshot.published_by,
                published_at=snapshot.published_at,
            )
        )
        session.add(
            RootTaskPlanHeadModel(
                workspace_id=workspace_id,
                root_task_id=snapshot.root_task_id,
                active_snapshot_id=snapshot.id,
                active_generation=snapshot.generation,
                active_snapshot_hash=plan_snapshot_content_hash(snapshot),
                version=1,
                updated_at=NOW,
            )
        )
        session.execute(
            update(ExecutionRequestModel)
            .where(
                ExecutionRequestModel.workspace_id == workspace_id,
                ExecutionRequestModel.plan_snapshot_id == snapshot.id,
            )
            .values(
                collaboration_gate_mode="release_required",
                gate_reason="collaboration_run_pending",
                gate_details={"source": "integration"},
            )
        )
        session.commit()

    with uow_factory() as uow:
        uow.projection_changes.changed(
            workspace_id=workspace_id,
            projection_type="collaboration_runtime",
            resource_id=snapshot.root_task_id,
            change_type="plan.published",
            resource_version=1,
            correlation_id=uuid4(),
        )

    reconciler = CollaborationReconciler(
        uow_factory=uow_factory,
        fact_store_factory=lambda uow: SqlAlchemyCollaborationFactStore(
            session=uow.session
        ),
        runtime_resolver_factory=lambda modules: CollaborationRuntimeResolver(
            modules,
            RegistryCollaborationRuntimeFactory(
                {
                    "reference": lambda _instance: ReferenceCollaborationRuntime(
                        clock=lambda: NOW
                    )
                }
            ),
        ),
        evaluation_lease_ttl=timedelta(seconds=5),
    )
    return ids, snapshot, reconciler


def _prepare(
    database, workspace_id: UUID, uow_factory
) -> tuple[dict[str, UUID], Any, CollaborationReconciler, Any]:
    ids, snapshot, reconciler = _prepare_fixture(database, workspace_id, uow_factory)
    prepared = reconciler.prepare_next(
        owner_id="collaboration-integration",
        now=NOW,
        workspace_id=workspace_id,
        root_task_id=snapshot.root_task_id,
    )
    assert prepared is not None
    return ids, snapshot, reconciler, prepared


def _invoker(runtime: Any) -> CollaborationRuntimeInvoker:
    return CollaborationRuntimeInvoker(
        runtime_resolver=StaticResolver(runtime),
        evaluation_timeout=timedelta(seconds=30),
        lease_safety_margin=timedelta(seconds=1),
        monotonic_clock=iter((0.0, 0.1)).__next__,
    )


def test_timeout_reuses_operation_id_and_schedules_bounded_retry(
    database,
    workspace_id: UUID,
    uow_factory,
) -> None:
    _ids, snapshot, reconciler, prepared = _prepare(database, workspace_id, uow_factory)
    result = _invoker(TimeoutRuntime()).invoke(prepared, now=NOW)

    assert isinstance(result, RuntimeInvocationFailure)
    assert result.code == "runtime_timeout"
    retried = reconciler.complete_failure(
        prepared,
        result,
        now=NOW,
        max_attempts=3,
        backoff_seconds=2,
        backoff_max_seconds=3,
    )
    assert retried is not None

    recovered = reconciler.prepare_next(
        owner_id="collaboration-integration-retry",
        now=NOW + timedelta(seconds=2),
        workspace_id=workspace_id,
        root_task_id=snapshot.root_task_id,
    )

    assert recovered is not None
    assert recovered.operation_id == prepared.operation_id
    assert recovered.evaluation.attempt == 2
    with database.session() as session:
        events = session.scalars(
            select(EventModel).where(
                EventModel.workspace_id == workspace_id,
                EventModel.event_type == "CollaborationEvaluationRetryScheduled",
            )
        ).all()
    assert len(events) == 1


def test_process_crash_after_runtime_call_recovers_same_operation_after_lease_expiry(
    database,
    workspace_id: UUID,
    uow_factory,
) -> None:
    _ids, snapshot, reconciler, prepared = _prepare(database, workspace_id, uow_factory)

    class SuccessfulRuntime:
        def evaluate(
            self,
            projection: RuntimeProjectionV1,
            *,
            operation_id: UUID,
            deadline: datetime,
        ) -> object:
            return {
                "schema_version": 1,
                "operation_id": str(operation_id),
                "run_id": str(projection.run_id),
                "projection_id": str(projection.projection_id),
                "projection_hash": projection.content_hash,
                "outcome": "no_change",
                "reason_code": "no_eligible_requests",
                "summary": "no release-required request is eligible",
                "strategy_state": {},
                "intents": [],
            }

    result = _invoker(SuccessfulRuntime()).invoke(prepared, now=NOW)
    assert isinstance(result, RuntimeInvocationResult)

    recovered = reconciler.prepare_next(
        owner_id="collaboration-integration-recovered",
        now=NOW + timedelta(seconds=6),
        workspace_id=workspace_id,
        root_task_id=snapshot.root_task_id,
    )

    assert recovered is not None
    assert recovered.operation_id == prepared.operation_id
    assert recovered.evaluation.attempt == 2


def test_invalid_output_blocks_run_without_decision_or_release_and_preserves_execution(
    database,
    workspace_id: UUID,
    uow_factory,
) -> None:
    ids, _snapshot_value, reconciler, prepared = _prepare(
        database, workspace_id, uow_factory
    )
    with database.session() as session:
        session.execute(
            update(ExecutionModel)
            .where(
                ExecutionModel.id == ids["execution"],
                ExecutionModel.workspace_id == workspace_id,
            )
            .values(status="running", version=2)
        )
        session.commit()

    result = _invoker(InvalidOutputRuntime()).invoke(prepared, now=NOW)
    assert isinstance(result, RuntimeInvocationFailure)
    assert result.code == "runtime_output_invalid"
    assert result.response_hash is not None
    blocked = reconciler.complete_failure(prepared, result, now=NOW)
    assert blocked is not None

    with database.session() as session:
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
        decisions = session.scalars(
            select(RuntimeDecisionModel).where(
                RuntimeDecisionModel.workspace_id == workspace_id,
                RuntimeDecisionModel.run_id == prepared.run.id,
            )
        ).all()
        releases = session.scalars(
            select(RuntimeReleaseModel).where(
                RuntimeReleaseModel.workspace_id == workspace_id,
                RuntimeReleaseModel.run_id == prepared.run.id,
            )
        ).all()
        execution = session.scalar(
            select(ExecutionModel).where(
                ExecutionModel.id == ids["execution"],
                ExecutionModel.workspace_id == workspace_id,
            )
        )
        blocked_events = session.scalars(
            select(EventModel).where(
                EventModel.workspace_id == workspace_id,
                EventModel.event_type == "CollaborationRunBlocked",
            )
        ).all()

    assert evaluation is not None
    assert evaluation.status == "blocked"
    assert evaluation.response_hash == result.response_hash
    assert run is not None
    assert run.status == "blocked"
    assert decisions == []
    assert releases == []
    assert execution is not None
    assert execution.status == "running"
    assert execution.version == 2
    assert len(blocked_events) == 1
    assert blocked_events[0].payload["attention_required"] is True
    assert blocked_events[0].payload["detail_href"].endswith("/collaboration")
