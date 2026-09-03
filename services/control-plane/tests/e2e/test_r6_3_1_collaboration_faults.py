"""Failure and compatibility cases for the R6.3.1 collaboration loop."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from typing import Any, cast
from uuid import NAMESPACE_URL, UUID, uuid4, uuid5

import pytest
from sqlalchemy import select, update

from sidera_control_plane.application.collaboration_runtime_invoker import (
    RuntimeInvocationFailure,
    RuntimeInvocationResult,
)
from sidera_control_plane.collaboration_worker import CollaborationWorker
from sidera_control_plane.domain.collaboration import CollaborationGateMode
from sidera_control_plane.domain.collaboration_runtime import (
    ReleaseExecutionRequestIntentV1,
    RuntimeDecisionOutcome,
    RuntimeDecisionV1,
    RuntimeProjectionV1,
)
from sidera_control_plane.domain.execution import ExecutionStatus
from sidera_control_plane.domain.modules import ModuleLifecycle
from sidera_control_plane.domain.plan_revisions import plan_snapshot_content_hash
from sidera_control_plane.persistence.models import (
    CollaborationRunModel,
    EventModel,
    ExecutionModel,
    ExecutionRequestModel,
    PlanSnapshotModel,
    RootTaskPlanHeadModel,
    RuntimeDecisionModel,
    RuntimeIntentApplicationModel,
    RuntimeProjectionSnapshotModel,
    RuntimeReleaseModel,
)

from .r6_3_1_fake_runtime import CollaborationScenario
from .seed_r6_3_1_collaboration import CollaborationSeed, seed_collaboration_plan

pytestmark = [
    pytest.mark.integration,
    pytest.mark.usefixtures("clean_database"),
]

NOW = datetime(2026, 9, 3, 12, 0, tzinfo=UTC)


class _TimeoutRuntime:
    def evaluate(
        self,
        projection: RuntimeProjectionV1,
        *,
        operation_id: UUID,
        deadline: datetime,
    ) -> RuntimeDecisionV1:
        raise TimeoutError("fixture runtime deadline exceeded")


class _UnknownIntentRuntime:
    def __init__(self, run_id: UUID) -> None:
        self._unknown_request_id = uuid5(
            NAMESPACE_URL, f"r6.3.1:unknown-intent:{run_id}"
        )

    def evaluate(
        self,
        projection: RuntimeProjectionV1,
        *,
        operation_id: UUID,
        deadline: datetime,
    ) -> Any:
        intent = {
            "intent_type": "release_execution_request",
            "ordinal": 0,
            "target_execution_request_id": str(self._unknown_request_id),
            "target_request_version": 1,
            "reason_code": "fixture_unknown_target",
            "summary": "target an unknown request",
            "idempotency_key": f"r6.3.1:unknown:{projection.run_id}",
        }
        return {
            "schema_version": 1,
            "operation_id": str(operation_id),
            "run_id": str(projection.run_id),
            "projection_id": str(projection.projection_id),
            "projection_hash": projection.content_hash,
            "outcome": "intents",
            "reason_code": "fixture_unknown_intent",
            "summary": "fixture decision contains an unknown target",
            "strategy_state": {},
            "intents": [intent],
        }


class _RuntimeOverrideInvoker:
    def __init__(self, scenario: CollaborationScenario, runtime: object) -> None:
        self._scenario = scenario
        self._runtime = runtime

    def invoke(
        self,
        prepared: Any,
        *,
        now: datetime,
    ) -> RuntimeInvocationResult | RuntimeInvocationFailure:
        return self._scenario.collaboration_invoker.invoke(
            prepared,
            now=now,
            runtime=cast(Any, self._runtime),
        )


def _scenario(database, workspace_id) -> tuple[CollaborationSeed, CollaborationScenario]:
    seed = seed_collaboration_plan(database, workspace_id, now=NOW)
    return seed, CollaborationScenario(database, seed, now=NOW)


def _wake(scenario: CollaborationScenario, label: str) -> None:
    with scenario.uow_factory() as uow:
        uow.projection_changes.changed(
            workspace_id=scenario.seed.workspace_id,
            projection_type="collaboration_runtime",
            resource_id=scenario.seed.root_task_id,
            change_type=f"r6.3.1.{label}",
            resource_version=1,
            correlation_id=uuid4(),
        )


def _manual_result(
    prepared: Any,
    *,
    intents: tuple[ReleaseExecutionRequestIntentV1, ...],
) -> RuntimeInvocationResult:
    decision = RuntimeDecisionV1(
        schema_version=1,
        operation_id=prepared.operation_id,
        run_id=prepared.run.id,
        projection_id=prepared.projection.projection_id,
        projection_hash=prepared.projection.content_hash,
        outcome=RuntimeDecisionOutcome.INTENTS,
        reason_code="fixture_decision",
        summary="fixture decision",
        strategy_state={"fixture": True},
        intents=intents,
    )
    response_bytes = decision.canonical_bytes()
    return RuntimeInvocationResult(
        response_bytes=response_bytes,
        response_hash=sha256(response_bytes).hexdigest(),
        decision=decision,
        duration_ms=1,
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
        reason_code="fixture_release",
        summary="fixture release intent",
        idempotency_key=(
            f"r6.3.1:release:{prepared.run.id}:{request_id}:"
            f"{ordinal}:{request_version}"
        ),
    )


def _publish_next_generation(scenario: CollaborationScenario) -> UUID:
    with scenario.uow_factory() as uow:
        head = uow.plans.get_head(
            scenario.seed.root_task_id,
            scenario.seed.workspace_id,
            for_update=True,
        )
        if head is None:
            raise AssertionError("plan head is missing")
        current = uow.plans.get(head.active_snapshot_id, scenario.seed.workspace_id)
        if current is None:
            raise AssertionError("current plan snapshot is missing")
        next_snapshot = replace(
            current,
            id=uuid5(
                NAMESPACE_URL,
                f"r6.3.1:next-plan:{scenario.seed.workspace_id}",
            ),
            generation=head.active_generation + 1,
            parent_snapshot_id=head.active_snapshot_id,
            source_ref="r6.3.1-fault:new-generation",
            published_at=NOW + timedelta(seconds=1),
        )
        uow.plans.add_snapshot_and_advance_head(
            next_snapshot,
            expected_head=head,
        )
        return next_snapshot.id


def _start_without_completion(scenario: CollaborationScenario, key: str) -> UUID:
    execution = scenario._materialize_and_schedule(key)
    with scenario.uow_factory() as uow:
        current = uow.executions.get(
            execution.id,
            scenario.seed.workspace_id,
            for_update=True,
        )
        if current is None:
            raise AssertionError("execution disappeared")
        lease = uow.leases.get_active_for_execution(
            current.id,
            scenario.seed.workspace_id,
            for_update=True,
        )
        if lease is None:
            raise AssertionError("execution lease disappeared")
        worker = (
            uow.workers.get(
                current.assigned_worker_id,
                scenario.seed.workspace_id,
                for_update=True,
            )
            if current.assigned_worker_id is not None
            else None
        )
        previous_execution_version = current.version
        previous_worker_version = worker.version if worker is not None else None
        if current.status is ExecutionStatus.LEASED:
            current.start(
                lease_id=lease.id,
                fencing_token=lease.fencing_token,
                now=scenario.now,
            )
            if worker is not None:
                worker.mark_running()
        uow.executions.save(current, previous_execution_version)
        if worker is not None and previous_worker_version is not None:
            uow.workers.save(worker, previous_worker_version)
    return execution.id


def _set_runtime_lifecycle(
    scenario: CollaborationScenario,
    lifecycle: ModuleLifecycle,
) -> None:
    with scenario.uow_factory() as uow:
        instance = uow.modules.get_instance(
            scenario.seed.runtime_instance_id,
            scenario.seed.workspace_id,
        )
        if instance is None:
            raise AssertionError("runtime instance is missing")
        changed = instance.with_lifecycle(lifecycle)
        uow.modules.save_instance(changed, instance.resource_version)


def _make_legacy_policy_null(scenario: CollaborationScenario) -> None:
    with scenario.uow_factory() as uow:
        snapshot = uow.plans.get(
            scenario.seed.plan_snapshot_id,
            scenario.seed.workspace_id,
        )
        head = uow.plans.get_head(
            scenario.seed.root_task_id,
            scenario.seed.workspace_id,
            for_update=True,
        )
        if snapshot is None or head is None:
            raise AssertionError("legacy fixture plan is missing")
        legacy = replace(snapshot, schema_version=2, collaboration_policy=None)
        uow.session.execute(
            update(PlanSnapshotModel)
            .where(
                PlanSnapshotModel.id == snapshot.id,
                PlanSnapshotModel.workspace_id == scenario.seed.workspace_id,
            )
            .values(
                schema_version=legacy.schema_version,
                payload=legacy.to_payload(),
            )
        )
        uow.session.execute(
            update(RootTaskPlanHeadModel)
            .where(
                RootTaskPlanHeadModel.root_task_id == scenario.seed.root_task_id,
                RootTaskPlanHeadModel.workspace_id == scenario.seed.workspace_id,
            )
            .values(
                active_snapshot_hash=plan_snapshot_content_hash(legacy),
                version=head.version + 1,
            )
        )
        uow.session.execute(
            update(ExecutionRequestModel)
            .where(
                ExecutionRequestModel.workspace_id == scenario.seed.workspace_id,
                ExecutionRequestModel.root_task_id == scenario.seed.root_task_id,
            )
            .values(collaboration_gate_mode=CollaborationGateMode.UNMANAGED.value)
        )


def test_new_generation_stales_in_flight_intents_without_a_release(
    database,
    workspace_id,
) -> None:
    _seed, scenario = _scenario(database, workspace_id)
    prepared = scenario.collaboration_reconciler.prepare_next(
        owner_id=scenario.owner_id,
        now=NOW,
    )
    assert prepared is not None
    result = scenario.collaboration_invoker.invoke(prepared, now=NOW)
    assert isinstance(result, RuntimeInvocationResult)

    next_snapshot_id = _publish_next_generation(scenario)
    assert scenario.collaboration_reconciler.complete_success(
        prepared,
        result,
        now=NOW + timedelta(seconds=1),
    )
    applied = scenario.collaboration_reconciler.complete_decision(
        prepared,
        result,
        now=NOW + timedelta(seconds=1),
    )

    assert applied.status == "stale"
    assert applied.release_ids == ()
    with database.session() as session:
        head = session.scalar(
            select(RootTaskPlanHeadModel).where(
                RootTaskPlanHeadModel.workspace_id == workspace_id,
                RootTaskPlanHeadModel.root_task_id == scenario.seed.root_task_id,
            )
        )
        releases = session.scalars(
            select(RuntimeReleaseModel).where(
                RuntimeReleaseModel.workspace_id == workspace_id,
                RuntimeReleaseModel.run_id == prepared.run.id,
            )
        ).all()
    assert head is not None
    assert head.active_snapshot_id == next_snapshot_id
    assert head.active_generation == 2
    assert releases == []


def test_timeout_keeps_running_upstream_and_retries_the_same_operation(
    database,
    workspace_id,
) -> None:
    _seed, scenario = _scenario(database, workspace_id)
    scenario.run_collaboration_until_idle()
    execution_id = _start_without_completion(scenario, "upstream-a")
    _wake(scenario, "timeout")

    prepared = scenario.collaboration_reconciler.prepare_next(
        owner_id=scenario.owner_id,
        now=NOW + timedelta(seconds=1),
    )
    assert prepared is not None
    failure = scenario.collaboration_invoker.invoke(
        prepared,
        now=NOW + timedelta(seconds=1),
        runtime=_TimeoutRuntime(),
    )
    assert isinstance(failure, RuntimeInvocationFailure)
    assert failure.code == "runtime_timeout"
    retried = scenario.collaboration_reconciler.complete_failure(
        prepared,
        failure,
        now=NOW + timedelta(seconds=1),
        max_attempts=3,
        backoff_seconds=1,
        backoff_max_seconds=1,
    )
    assert retried is not None

    recovered = scenario.collaboration_reconciler.prepare_next(
        owner_id="r6-3-1-timeout-retry",
        now=NOW + timedelta(seconds=2),
    )
    assert recovered is not None
    assert recovered.operation_id == prepared.operation_id
    assert recovered.evaluation.attempt == 2

    with database.session() as session:
        releases = session.scalars(
            select(RuntimeReleaseModel).where(
                RuntimeReleaseModel.workspace_id == workspace_id,
                RuntimeReleaseModel.run_id == prepared.run.id,
            )
        ).all()
        execution = session.scalar(
            select(ExecutionModel).where(
                ExecutionModel.workspace_id == workspace_id,
                ExecutionModel.id == execution_id,
            )
        )
    assert len(releases) == 2
    assert execution is not None
    assert execution.status == ExecutionStatus.RUNNING.value


def test_runtime_return_before_apply_recovers_and_replays_safely(
    database,
    workspace_id,
) -> None:
    _seed, scenario = _scenario(database, workspace_id)
    prepared = scenario.collaboration_reconciler.prepare_next(
        owner_id=scenario.owner_id,
        now=NOW,
    )
    assert prepared is not None
    result = scenario.collaboration_invoker.invoke(prepared, now=NOW)
    assert isinstance(result, RuntimeInvocationResult)
    assert scenario.collaboration_reconciler.complete_success(
        prepared,
        result,
        now=NOW,
    )

    recovered = scenario.collaboration_reconciler.prepare_next(
        owner_id="r6-3-1-crash-recovery",
        now=NOW + timedelta(seconds=6),
    )
    assert recovered is not None
    assert recovered.operation_id == prepared.operation_id
    assert recovered.projection.projection_id == prepared.projection.projection_id
    applied = scenario.collaboration_reconciler.complete_decision(
        recovered,
        result,
        now=NOW + timedelta(seconds=6),
    )

    assert applied.status == "applied"
    with scenario.database.session() as session:
        assert (
            session.scalar(
                select(RuntimeProjectionSnapshotModel.id).where(
                    RuntimeProjectionSnapshotModel.workspace_id
                    == scenario.seed.workspace_id,
                    RuntimeProjectionSnapshotModel.id
                    == prepared.projection.projection_id,
                )
            )
            == prepared.projection.projection_id
        )
        assert len(
            session.scalars(
                select(RuntimeDecisionModel).where(
                    RuntimeDecisionModel.workspace_id == scenario.seed.workspace_id,
                    RuntimeDecisionModel.run_id == prepared.run.id,
                )
            ).all()
        ) == 1
        assert len(
            session.scalars(
                select(RuntimeReleaseModel).where(
                    RuntimeReleaseModel.workspace_id == scenario.seed.workspace_id,
                    RuntimeReleaseModel.run_id == prepared.run.id,
                )
            ).all()
        ) == 2


def test_duplicate_projection_decision_and_activation_are_idempotent(
    database,
    workspace_id,
) -> None:
    seed, scenario = _scenario(database, workspace_id)
    first = scenario.collaboration_reconciler.prepare_next(
        owner_id=scenario.owner_id,
        now=NOW,
    )
    assert first is not None
    first_result = scenario.collaboration_invoker.invoke(first, now=NOW)
    assert isinstance(first_result, RuntimeInvocationResult)
    assert scenario.collaboration_reconciler.complete_success(
        first,
        first_result,
        now=NOW,
    )
    replay = scenario.collaboration_reconciler.prepare_next(
        owner_id="r6-3-1-duplicate-replay",
        now=NOW + timedelta(seconds=6),
    )
    assert replay is not None
    assert replay.projection.projection_id == first.projection.projection_id
    assert scenario.collaboration_reconciler.complete_decision(
        replay,
        first_result,
        now=NOW + timedelta(seconds=6),
    ).status == "applied"

    _wake(scenario, "duplicate-decision")
    duplicate = scenario.collaboration_reconciler.prepare_next(
        owner_id="r6-3-1-duplicate-decision",
        now=NOW + timedelta(seconds=7),
    )
    assert duplicate is not None
    duplicate_result = _manual_result(
        duplicate,
        intents=(
            _intent(
                duplicate,
                ordinal=0,
                request_id=seed.request_ids["upstream-a"],
                request_version=1,
            ),
        ),
    )
    assert scenario.collaboration_reconciler.complete_success(
        duplicate,
        duplicate_result,
        now=NOW + timedelta(seconds=7),
    )
    duplicate_application = scenario.collaboration_reconciler.complete_decision(
        duplicate,
        duplicate_result,
        now=NOW + timedelta(seconds=7),
    )
    assert duplicate_application.status == "duplicate"
    assert duplicate_application.release_ids == ()

    first_execution = scenario._materialize_and_schedule("upstream-a")
    second_execution = scenario._materialize_and_schedule("upstream-a")
    assert first_execution.id == second_execution.id
    with database.session() as session:
        releases = session.scalars(
            select(RuntimeReleaseModel).where(
                RuntimeReleaseModel.workspace_id == workspace_id,
                RuntimeReleaseModel.run_id == replay.run.id,
            )
        ).all()
        executions = session.scalars(
            select(ExecutionModel).where(
                ExecutionModel.workspace_id == workspace_id,
                ExecutionModel.lineage_id == first_execution.lineage_id,
            )
        ).all()
    assert len(releases) == 2
    assert len(executions) == 1


def test_unknown_intent_blocks_with_attention_without_decision_or_release(
    database,
    workspace_id,
) -> None:
    seed, scenario = _scenario(database, workspace_id)
    clock = [NOW]
    worker = CollaborationWorker(
        reconciler=scenario.collaboration_reconciler,
        invoker=_RuntimeOverrideInvoker(
            scenario,
            _UnknownIntentRuntime(seed.root_task_id),
        ),
        owner_id="r6-3-1-invalid-intent-worker",
        clock=lambda: clock[0],
        max_attempts=2,
        backoff_seconds=1,
        backoff_max_seconds=1,
    )

    first = worker.run_once(1)
    assert first.failed == 1
    assert first.retried == 1
    clock[0] = NOW + timedelta(seconds=1)
    second = worker.run_once(1)
    assert second.failed == 1
    assert second.blocked == 1

    with database.session() as session:
        run = session.scalar(
            select(CollaborationRunModel).where(
                CollaborationRunModel.workspace_id == workspace_id,
                CollaborationRunModel.root_task_id == seed.root_task_id,
            )
        )
        decisions = session.scalars(
            select(RuntimeDecisionModel).where(
                RuntimeDecisionModel.workspace_id == workspace_id,
            )
        ).all()
        intents = session.scalars(
            select(RuntimeIntentApplicationModel).where(
                RuntimeIntentApplicationModel.workspace_id == workspace_id,
            )
        ).all()
        releases = session.scalars(
            select(RuntimeReleaseModel).where(
                RuntimeReleaseModel.workspace_id == workspace_id,
            )
        ).all()
        blocked_events = session.scalars(
            select(EventModel).where(
                EventModel.workspace_id == workspace_id,
                EventModel.event_type == "CollaborationRunBlocked",
            )
        ).all()
    assert run is not None
    assert run.status == "blocked"
    assert decisions == []
    assert intents == []
    assert releases == []
    assert len(blocked_events) == 1
    assert blocked_events[0].payload["attention_required"] is True
    assert blocked_events[0].payload["detail_href"].endswith("/collaboration")


def test_one_bad_intent_rejects_the_complete_decision_without_releases(
    database,
    workspace_id,
) -> None:
    seed, scenario = _scenario(database, workspace_id)
    prepared = scenario.collaboration_reconciler.prepare_next(
        owner_id=scenario.owner_id,
        now=NOW,
    )
    assert prepared is not None
    result = _manual_result(
        prepared,
        intents=(
            _intent(
                prepared,
                ordinal=0,
                request_id=seed.request_ids["upstream-a"],
                request_version=1,
            ),
            _intent(
                prepared,
                ordinal=1,
                request_id=seed.request_ids["upstream-a"],
                request_version=1,
            ),
        ),
    )
    assert scenario.collaboration_reconciler.complete_success(
        prepared,
        result,
        now=NOW,
    )
    applied = scenario.collaboration_reconciler.complete_decision(
        prepared,
        result,
        now=NOW,
    )

    assert applied.status == "rejected"
    assert applied.release_ids == ()
    assert {item.status.value for item in applied.applications} == {"rejected"}
    with database.session() as session:
        assert (
            session.scalar(
                select(RuntimeDecisionModel.id).where(
                    RuntimeDecisionModel.workspace_id == workspace_id,
                    RuntimeDecisionModel.id == applied.decision.id,
                )
            )
            == applied.decision.id
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


def test_static_dependency_regression_keeps_release_but_blocks_request(
    database,
    workspace_id,
) -> None:
    seed, scenario = _scenario(database, workspace_id)
    scenario.run_collaboration_until_idle()
    scenario.succeed("upstream-a")
    scenario.succeed("upstream-b", evidence=("verification-report",))
    scenario.run_collaboration_until_idle()
    assert "downstream" in scenario.released_keys()

    with database.session() as session:
        upstream_request_id = seed.request_ids["upstream-b"]
        upstream_execution_id = session.scalar(
            select(ExecutionRequestModel.first_execution_id).where(
                ExecutionRequestModel.workspace_id == workspace_id,
                ExecutionRequestModel.id == upstream_request_id,
            )
        )
        downstream_release = session.scalar(
            select(RuntimeReleaseModel).where(
                RuntimeReleaseModel.workspace_id == workspace_id,
                RuntimeReleaseModel.execution_request_id
                == seed.request_ids["downstream"],
            )
        )
    assert upstream_execution_id is not None
    assert downstream_release is not None

    with database.session() as session:
        session.execute(
            update(ExecutionModel)
            .where(
                ExecutionModel.workspace_id == workspace_id,
                ExecutionModel.id == upstream_execution_id,
            )
            .values(
                status=ExecutionStatus.RUNNING.value,
                finished_at=None,
                version=ExecutionModel.version + 1,
            )
        )
        session.commit()

    activation = scenario.activation.run_once(limit=20)
    assert activation.failed == 0
    with scenario.uow_factory() as uow:
        request = uow.execution_requests.get(
            seed.request_ids["downstream"],
            workspace_id,
        )
    assert request is not None
    assert request.status.value == "blocked"
    assert request.gate_reason == "dependencies_blocked"
    with database.session() as session:
        release = session.scalar(
            select(RuntimeReleaseModel).where(
                RuntimeReleaseModel.workspace_id == workspace_id,
                RuntimeReleaseModel.id == downstream_release.id,
            )
        )
        execution_id = session.scalar(
            select(ExecutionRequestModel.first_execution_id).where(
                ExecutionRequestModel.workspace_id == workspace_id,
                ExecutionRequestModel.id == seed.request_ids["downstream"],
            )
        )
    assert release is not None
    assert execution_id is None


def test_unmanaged_handoff_target_runs_without_a_second_collaboration_release(
    database,
    workspace_id,
) -> None:
    seed, scenario = _scenario(database, workspace_id)
    with database.session() as session:
        session.execute(
            update(ExecutionRequestModel)
            .where(
                ExecutionRequestModel.workspace_id == workspace_id,
                ExecutionRequestModel.id == seed.request_ids["downstream"],
            )
            .values(collaboration_gate_mode=CollaborationGateMode.UNMANAGED.value)
        )
        session.commit()

    scenario.run_collaboration_until_idle()
    scenario.succeed("upstream-a")
    scenario.succeed("upstream-b", evidence=("verification-report",))
    scenario.run_collaboration_until_idle()
    execution = scenario._materialize_and_schedule("downstream")

    assert execution.status in {
        ExecutionStatus.LEASED,
        ExecutionStatus.RUNNING,
    }
    with database.session() as session:
        downstream_release = session.scalar(
            select(RuntimeReleaseModel.id).where(
                RuntimeReleaseModel.workspace_id == workspace_id,
                RuntimeReleaseModel.execution_request_id == seed.request_ids["downstream"],
            )
        )
        request = session.scalar(
            select(ExecutionRequestModel).where(
                ExecutionRequestModel.workspace_id == workspace_id,
                ExecutionRequestModel.id == seed.request_ids["downstream"],
            )
        )
    assert downstream_release is None
    assert request is not None
    assert request.status == "materialized"


def test_policy_null_legacy_plan_has_no_run_and_keeps_pre_r6_3_1_activation(
    database,
    workspace_id,
) -> None:
    seed, scenario = _scenario(database, workspace_id)
    _make_legacy_policy_null(scenario)

    scenario.run_collaboration_until_idle()
    with database.session() as session:
        runs = session.scalars(
            select(CollaborationRunModel).where(
                CollaborationRunModel.workspace_id == workspace_id,
                CollaborationRunModel.root_task_id == seed.root_task_id,
            )
        ).all()
    assert runs == []

    execution = scenario._materialize_and_schedule("upstream-a")
    assert execution.status in {
        ExecutionStatus.LEASED,
        ExecutionStatus.RUNNING,
    }
    with database.session() as session:
        request = session.scalar(
            select(ExecutionRequestModel).where(
                ExecutionRequestModel.workspace_id == workspace_id,
                ExecutionRequestModel.id == seed.request_ids["upstream-a"],
            )
        )
    assert request is not None
    assert request.collaboration_gate_mode == CollaborationGateMode.UNMANAGED.value
    assert request.status == "materialized"


def test_blocked_exact_runtime_recovers_same_binding_without_fallback(
    database,
    workspace_id,
) -> None:
    seed, scenario = _scenario(database, workspace_id)
    _set_runtime_lifecycle(scenario, ModuleLifecycle.DRAINING)

    assert (
        scenario.collaboration_reconciler.prepare_next(
            owner_id=scenario.owner_id,
            now=NOW,
        )
        is None
    )
    with database.session() as session:
        blocked = session.scalar(
            select(CollaborationRunModel).where(
                CollaborationRunModel.workspace_id == workspace_id,
                CollaborationRunModel.root_task_id == seed.root_task_id,
            )
        )
    assert blocked is not None
    assert blocked.status == "blocked"
    assert blocked.blocked_reason == "runtime_module_disabled"

    _set_runtime_lifecycle(scenario, ModuleLifecycle.ACTIVE)
    recovered = scenario.collaboration_reconciler.prepare_next(
        owner_id="r6-3-1-exact-runtime-recovery",
        now=NOW + timedelta(seconds=1),
    )
    assert recovered is not None
    assert recovered.run.id == blocked.id
    assert recovered.run.runtime_definition_id == seed.runtime_definition_id
    assert recovered.run.runtime_instance_id == seed.runtime_instance_id
    with database.session() as session:
        runs = session.scalars(
            select(CollaborationRunModel).where(
                CollaborationRunModel.workspace_id == workspace_id,
                CollaborationRunModel.root_task_id == seed.root_task_id,
            )
        ).all()
    assert len(runs) == 1
