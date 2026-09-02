from datetime import timedelta
from uuid import UUID, uuid4

import pytest

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
from sidera_control_plane.domain.modules import (
    CollaborationRuntimeCapabilityProfile,
    IdempotencyCapability,
    StabilityLevel,
)
from sidera_control_plane.persistence.collaboration_repository import (
    CollaborationEvaluationRepository,
)
from sidera_control_plane.persistence.projection_change_repository import (
    ProjectionChangeRepository,
)
from sidera_control_plane.persistence.unit_of_work import SqlAlchemyUnitOfWork

from .test_r5_1_execution_request_schema import (
    NOW,
    request_row,
    seed_execution_request_facts,
)

pytestmark = pytest.mark.integration

WORKSPACE_B = UUID("00000000-0000-0000-0000-000000000002")


def _capability() -> CollaborationRuntimeCapabilityProfile:
    return CollaborationRuntimeCapabilityProfile(
        interface_version="collaboration-runtime-v1",
        projection_schema_versions=(1,),
        decision_schema_versions=(1,),
        strategy_ids=("all_required",),
        idempotency=IdempotencyCapability.OPERATION_ID,
        max_projection_bytes=262_144,
        max_decision_bytes=65_536,
        max_intents_per_decision=128,
        max_strategy_state_bytes=16_384,
        stability=StabilityLevel.EXPERIMENTAL,
    )


def _run(ids: dict[str, UUID], workspace_id: UUID) -> CollaborationRun:
    return CollaborationRun.create(
        run_id=uuid4(),
        workspace_id=workspace_id,
        root_task_id=ids["root_task"],
        plan_snapshot_id=ids["plan"],
        plan_generation=1,
        plan_content_hash="a" * 64,
        runtime_definition_id=ids["module_definition"],
        runtime_instance_id=ids["module_instance"],
        runtime_interface_version="collaboration-runtime-v1",
        runtime_config_version=1,
        runtime_capability_snapshot=_capability(),
        policy_snapshot={"schema_version": 1, "strategy": "all_required"},
        now=NOW,
    )


def _projection(run: CollaborationRun, *, content_hash: str = "b" * 64) -> RuntimeProjectionSnapshot:
    return RuntimeProjectionSnapshot(
        id=uuid4(),
        workspace_id=run.workspace_id,
        run_id=run.id,
        schema_version=1,
        plan_snapshot_id=run.plan_snapshot_id,
        plan_generation=run.plan_generation,
        plan_content_hash=run.plan_content_hash,
        fact_cursor=7,
        payload={"root_task_id": str(run.root_task_id)},
        content_hash=content_hash,
        size_bytes=64,
        created_at=NOW,
    )


def _evaluation(
    run: CollaborationRun, projection: RuntimeProjectionSnapshot
) -> CollaborationEvaluation:
    return CollaborationEvaluation.create(
        evaluation_id=uuid4(),
        workspace_id=run.workspace_id,
        run_id=run.id,
        projection_snapshot_id=projection.id,
        operation_id=uuid4(),
        now=NOW,
    )


def _decision(
    run: CollaborationRun,
    projection: RuntimeProjectionSnapshot,
    evaluation: CollaborationEvaluation,
    *,
    sequence: int,
) -> RuntimeDecision:
    return RuntimeDecision(
        id=uuid4(),
        workspace_id=run.workspace_id,
        run_id=run.id,
        evaluation_id=evaluation.id,
        projection_snapshot_id=projection.id,
        projection_hash=projection.content_hash,
        operation_id=evaluation.operation_id,
        schema_version=1,
        sequence=sequence,
        outcome="intents",
        reason_code="release_ready",
        summary="release one request",
        strategy_state={"cursor": projection.fact_cursor},
        content_hash="c" * 64,
        runtime_duration_ms=12,
        created_at=NOW,
    )


def _intent(
    run: CollaborationRun, decision: RuntimeDecision, request_id: UUID
) -> RuntimeIntent:
    return RuntimeIntent(
        id=uuid4(),
        workspace_id=run.workspace_id,
        run_id=run.id,
        decision_id=decision.id,
        ordinal=0,
        intent_type="release_execution_request",
        target_execution_request_id=request_id,
        target_request_version=1,
        reason_code="release_ready",
        summary="release request",
        idempotency_key=f"release:{request_id}",
        payload_json={"request_id": str(request_id)},
        created_at=NOW,
    )


def test_collaboration_repositories_round_trip_and_scope_every_read(
    database,
    workspace_id: UUID,
    uow_factory,
) -> None:
    ids = seed_execution_request_facts(database, workspace_id)
    with database.session() as session:
        session.add(request_row(ids, request_id=ids["request"]))
        session.commit()

    run = _run(ids, workspace_id)
    projection = _projection(run)
    evaluation = _evaluation(run, projection)

    with uow_factory() as uow:
        assert isinstance(uow, SqlAlchemyUnitOfWork)
        uow.collaboration_runs.add(run)
        uow.flush()
        stored_projection = uow.collaboration_ledger.get_or_add_projection(projection)
        stored_evaluation = uow.collaboration_evaluations.get_or_add(evaluation)
        assert stored_projection.id == projection.id
        assert stored_evaluation.id == evaluation.id

    with uow_factory() as uow:
        loaded = uow.collaboration_runs.get(workspace_id, run.id, for_update=True)
        assert loaded is not None
        assert loaded.status is CollaborationRunStatus.PENDING
        assert loaded.runtime_capability_snapshot == run.runtime_capability_snapshot
        assert dict(loaded.policy_snapshot) == dict(run.policy_snapshot)
        assert uow.collaboration_runs.get(WORKSPACE_B, run.id) is None
        assert uow.collaboration_runs.get_current(WORKSPACE_B, run.root_task_id) is None


def test_collaboration_run_save_uses_optimistic_versioning(
    database,
    workspace_id: UUID,
    uow_factory,
) -> None:
    ids = seed_execution_request_facts(database, workspace_id)
    run = _run(ids, workspace_id)
    with uow_factory() as uow:
        uow.collaboration_runs.add(run)

    with uow_factory() as uow:
        stored = uow.collaboration_runs.get(workspace_id, run.id)
        assert stored is not None
        previous_version = stored.resource_version
        stored.activate(now=NOW + timedelta(minutes=1))
        uow.collaboration_runs.save(stored, previous_version)

    with uow_factory() as uow:
        stored = uow.collaboration_runs.get(workspace_id, run.id)
        assert stored is not None
        assert stored.status is CollaborationRunStatus.ACTIVE
        assert stored.resource_version == 2


def test_evaluation_claim_recovery_and_live_run_exclusion(
    database,
    workspace_id: UUID,
    uow_factory,
) -> None:
    ids = seed_execution_request_facts(database, workspace_id)
    run = _run(ids, workspace_id)
    projection = _projection(run)
    second_projection = _projection(run, content_hash="d" * 64)
    first = _evaluation(run, projection)
    second = _evaluation(run, second_projection)
    with uow_factory() as uow:
        uow.collaboration_runs.add(run)
        uow.flush()
        uow.collaboration_ledger.get_or_add_projection(projection)
        uow.collaboration_ledger.get_or_add_projection(second_projection)
        uow.collaboration_evaluations.get_or_add(first)
        uow.collaboration_evaluations.get_or_add(second)

    claim_time = NOW + timedelta(minutes=1)
    with uow_factory() as uow:
        claimed = uow.collaboration_evaluations.claim_next(
            workspace_id=workspace_id,
            owner_id="collaboration-worker-1",
            now=claim_time,
            lease_ttl=timedelta(minutes=5),
            limit=2,
        )
        assert len(claimed) == 1
        assert claimed[0].status is CollaborationEvaluationStatus.EVALUATING
        assert claimed[0].lease_owner == "collaboration-worker-1"
        claimed_id = claimed[0].id

    with uow_factory() as uow:
        recovered = uow.collaboration_evaluations.recover_expired(
            now=claim_time + timedelta(minutes=6), limit=10
        )
        assert [item.id for item in recovered] == [claimed_id]
        assert recovered[0].status is CollaborationEvaluationStatus.RETRY_WAIT
        assert recovered[0].attempt == 2

    with uow_factory() as uow:
        claimed = uow.collaboration_evaluations.claim_next(
            workspace_id=workspace_id,
            owner_id="collaboration-worker-2",
            now=claim_time + timedelta(minutes=6),
            lease_ttl=timedelta(minutes=5),
            limit=2,
        )
        assert len(claimed) == 1
        assert claimed[0].id in {first.id, second.id}
        assert (
            uow.collaboration_evaluations.claim_next(
                workspace_id=workspace_id,
                owner_id="collaboration-worker-3",
                now=claim_time + timedelta(minutes=6),
                lease_ttl=timedelta(minutes=5),
                limit=2,
            )
            == ()
        )


def test_claim_next_skips_a_run_locked_by_another_worker(
    database,
    workspace_id: UUID,
    uow_factory,
) -> None:
    ids = seed_execution_request_facts(database, workspace_id)
    run = _run(ids, workspace_id)
    projection = _projection(run)
    evaluation = _evaluation(run, projection)
    with uow_factory() as uow:
        uow.collaboration_runs.add(run)
        uow.flush()
        uow.collaboration_ledger.get_or_add_projection(projection)
        uow.collaboration_evaluations.get_or_add(evaluation)

    with database.session() as first_session:
        first_repository = CollaborationEvaluationRepository(first_session)
        claimed = first_repository.claim_next(
            workspace_id=workspace_id,
            owner_id="worker-a",
            now=NOW,
            lease_ttl=timedelta(minutes=1),
            limit=1,
        )
        assert len(claimed) == 1
        with database.session() as second_session:
            second_repository = CollaborationEvaluationRepository(second_session)
            assert (
                second_repository.claim_next(
                    workspace_id=workspace_id,
                    owner_id="worker-b",
                    now=NOW,
                    lease_ttl=timedelta(minutes=1),
                    limit=1,
                )
                == ()
            )
        first_session.commit()


def test_ledger_deduplicates_projection_advances_sequence_and_reads_release(
    database,
    workspace_id: UUID,
    uow_factory,
) -> None:
    ids = seed_execution_request_facts(database, workspace_id)
    with database.session() as session:
        session.add(request_row(ids, request_id=ids["request"]))
        session.commit()
    run = _run(ids, workspace_id)
    projection = _projection(run)
    evaluation = _evaluation(run, projection)
    decision = _decision(run, projection, evaluation, sequence=1)
    intent = _intent(run, decision, ids["request"])
    application = RuntimeIntentApplication(
        id=uuid4(),
        workspace_id=workspace_id,
        intent_id=intent.id,
        status=RuntimeIntentApplicationStatus.PENDING,
        expected_run_version=1,
        observed_run_version=1,
        expected_request_version=1,
        observed_request_version=None,
        release_id=None,
        result_code="pending",
        result_summary="pending",
        resource_version=1,
        created_at=NOW,
        applied_at=None,
    )
    release = RuntimeRelease(
        id=uuid4(),
        workspace_id=workspace_id,
        run_id=run.id,
        plan_snapshot_id=run.plan_snapshot_id,
        plan_generation=run.plan_generation,
        plan_content_hash=run.plan_content_hash,
        decision_id=decision.id,
        intent_id=intent.id,
        execution_request_id=ids["request"],
        request_version_at_release=1,
        issued_at=NOW,
    )

    with uow_factory() as uow:
        uow.collaboration_runs.add(run)
        uow.flush()
        stored_projection = uow.collaboration_ledger.get_or_add_projection(projection)
        uow.collaboration_evaluations.get_or_add(evaluation)
        assert uow.collaboration_ledger.next_decision_sequence(run.id, workspace_id) == 1
        uow.collaboration_ledger.add_decision_bundle(decision, (intent,))
        uow.collaboration_ledger.add_applications((application,))
        uow.collaboration_ledger.add_releases((release,))
        uow.flush()
        duplicate = uow.collaboration_ledger.get_or_add_projection(
            _projection(run, content_hash=stored_projection.content_hash)
        )
        assert duplicate.id == stored_projection.id

    with uow_factory() as uow:
        stored_release = uow.collaboration_ledger.get_release_for_request(
            workspace_id, ids["request"]
        )
        assert stored_release is not None
        assert stored_release.id == release.id
        assert uow.collaboration_ledger.get_release_for_request(
            WORKSPACE_B, ids["request"]
        ) is None


def test_projection_repository_can_find_latest_cursor_for_one_target(
    database,
    workspace_id: UUID,
    uow_factory,
) -> None:
    target_id = uuid4()
    with uow_factory() as uow:
        first = uow.projection_changes.changed(
            workspace_id=workspace_id,
            projection_type="collaboration_run",
            resource_id=target_id,
            change_type="created",
            resource_version=1,
            correlation_id=uuid4(),
        )
        second = uow.projection_changes.changed(
            workspace_id=workspace_id,
            projection_type="collaboration_run",
            resource_id=target_id,
            change_type="updated",
            resource_version=2,
            correlation_id=uuid4(),
        )
        uow.projection_changes.changed(
            workspace_id=workspace_id,
            projection_type="worker_item",
            resource_id=target_id,
            change_type="updated",
            resource_version=3,
            correlation_id=uuid4(),
        )

    with database.session() as session:
        repository = ProjectionChangeRepository(session)
        assert repository.latest_target_cursor(
            workspace_id, "collaboration_run", target_id
        ) == second.cursor
        assert repository.latest_target_cursor(
            workspace_id, "collaboration_run", uuid4()
        ) == 0
        assert first.cursor < second.cursor


def test_collaboration_records_follow_uow_rollback(
    database,
    workspace_id: UUID,
    uow_factory,
) -> None:
    ids = seed_execution_request_facts(database, workspace_id)
    run = _run(ids, workspace_id)

    with pytest.raises(RuntimeError, match="rollback collaboration"), uow_factory() as uow:
        uow.collaboration_runs.add(run)
        uow.flush()
        raise RuntimeError("rollback collaboration")

    with uow_factory() as uow:
        assert uow.collaboration_runs.get(workspace_id, run.id) is None
