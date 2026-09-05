from datetime import UTC, datetime
from uuid import UUID, uuid4

import pytest
from sqlalchemy import update

from sidera_control_plane.application.collaboration_projection import (
    CollaborationProjectionAssembler,
)
from sidera_control_plane.application.result_input_sources import (
    resolve_result_input_source,
)
from sidera_control_plane.domain.collaboration import (
    CollaborationPolicyEntry,
    CollaborationPolicySnapshotV1,
    CollaborationRun,
    CollaborationStrategy,
)
from sidera_control_plane.domain.errors import ValidationError
from sidera_control_plane.domain.modules import (
    CollaborationRuntimeCapabilityProfile,
    IdempotencyCapability,
    StabilityLevel,
)
from sidera_control_plane.domain.plan_revisions import plan_snapshot_content_hash
from sidera_control_plane.domain.plans import TaskPlanSnapshot
from sidera_control_plane.persistence.collaboration_fact_store import (
    SqlAlchemyCollaborationFactStore,
)
from sidera_control_plane.persistence.collaboration_query_store import (
    SqlAlchemyCollaborationQueryStore,
)
from sidera_control_plane.persistence.models import (
    ArtifactModel,
    CollaborationGovernanceHoldModel,
    CollaborationReleaseProposalModel,
    EvidenceClaimModel,
    ExecutionModel,
    HandoffTransitionModel,
    PlanSnapshotModel,
    RootTaskPlanHeadModel,
    TaskModel,
    WorkerItemModel,
    WorkerItemPlacementModel,
)

from .test_handoff_repository import _seed as seed_handoff
from .test_r5_1_execution_request_schema import (
    NOW,
    WORKSPACE_B,
    request_row,
    seed_execution_request_facts,
)

pytestmark = pytest.mark.integration

WORKSPACE_ID = UUID("00000000-0000-0000-0000-000000000001")


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
        stability=StabilityLevel.STABLE,
    )


def _snapshot(
    *,
    workspace_id: UUID,
    root_task_id: UUID,
    snapshot_id: UUID,
    generation: int,
    parent_snapshot_id: UUID | None,
) -> TaskPlanSnapshot:
    return TaskPlanSnapshot.create(
        snapshot_id=snapshot_id,
        schema_version=2,
        workspace_id=workspace_id,
        root_task_id=root_task_id,
        generation=generation,
        parent_snapshot_id=parent_snapshot_id,
        source_type="integration",
        source_ref=f"collaboration-projection:{generation}",
        title="Projection plan",
        goal_summary="Bounded collaboration facts",
        acceptance_refs=("acceptance:projection",),
        published_by="integration",
        published_at=datetime(2026, 9, 3, 12, 0, tzinfo=UTC),
    )


def _persist_run(
    *,
    uow_factory,
    ids: dict[str, UUID],
    snapshot: TaskPlanSnapshot,
) -> CollaborationRun:
    capability = _capability()
    policy = CollaborationPolicySnapshotV1(
        schema_version=1,
        runtime_definition_id=ids["module_definition"],
        runtime_instance_id=ids["module_instance"],
        runtime_interface_version="collaboration-runtime-v1",
        runtime_config_version=1,
        runtime_capability_snapshot=capability,
        default_policy=CollaborationPolicyEntry(
            enabled=True,
            strategy_id=CollaborationStrategy.ALL_REQUIRED,
        ),
        subtask_overrides=(),
    )
    run = CollaborationRun.create(
        run_id=uuid4(),
        workspace_id=snapshot.workspace_id,
        root_task_id=snapshot.root_task_id,
        plan_snapshot_id=snapshot.id,
        plan_generation=snapshot.generation,
        plan_content_hash=plan_snapshot_content_hash(snapshot),
        runtime_definition_id=ids["module_definition"],
        runtime_instance_id=ids["module_instance"],
        runtime_interface_version="collaboration-runtime-v1",
        runtime_config_version=1,
        runtime_capability_snapshot=capability,
        policy_snapshot=policy.to_mapping(),
        now=NOW,
    )
    with uow_factory() as uow:
        uow.collaboration_runs.add(run)
    return run


def _seed_placed_source_run(database, workspace_id: UUID, uow_factory):
    ids = seed_execution_request_facts(database, workspace_id)
    snapshot = _snapshot(
        workspace_id=workspace_id,
        root_task_id=ids["root_task"],
        snapshot_id=ids["plan"],
        generation=1,
        parent_snapshot_id=None,
    )
    snapshot_hash = plan_snapshot_content_hash(snapshot)
    with database.session() as session:
        session.execute(
            update(PlanSnapshotModel)
            .where(PlanSnapshotModel.id == snapshot.id)
            .values(payload=snapshot.to_payload())
        )
        session.add(
            RootTaskPlanHeadModel(
                workspace_id=workspace_id,
                root_task_id=ids["root_task"],
                active_snapshot_id=snapshot.id,
                active_generation=1,
                active_snapshot_hash=snapshot_hash,
                version=1,
                updated_at=NOW,
            )
        )
        session.add(
            WorkerItemPlacementModel(
                plan_snapshot_id=snapshot.id,
                plan_generation=1,
                workspace_id=workspace_id,
                root_task_id=ids["root_task"],
                worker_item_id=ids["worker_item"],
                worker_item_key="backend",
                subtask_key="implementation",
                subtask_task_id=ids["subtask"],
                role="implementer",
                capability_requirements=["python"],
                worker_policy_ref=None,
                configuration_ref=None,
            )
        )
        request = request_row(ids, request_id=ids["request"])
        request.status = "materialized"
        request.execution_lineage_id = ids["execution"]
        request.first_execution_id = ids["execution"]
        session.add(request)
        session.commit()
    return ids, snapshot, _persist_run(
        uow_factory=uow_factory,
        ids=ids,
        snapshot=snapshot,
    )


def test_fact_store_is_current_generation_scoped_and_private_column_safe(
    database,
    workspace_id: UUID,
    uow_factory,
) -> None:
    ids = seed_execution_request_facts(database, workspace_id)
    current_snapshot = _snapshot(
        workspace_id=workspace_id,
        root_task_id=ids["root_task"],
        snapshot_id=ids["plan"],
        generation=1,
        parent_snapshot_id=None,
    )
    old_snapshot = _snapshot(
        workspace_id=workspace_id,
        root_task_id=ids["root_task"],
        snapshot_id=uuid4(),
        generation=2,
        parent_snapshot_id=current_snapshot.id,
    )
    current_hash = plan_snapshot_content_hash(current_snapshot)

    with database.session() as session:
        session.execute(
            update(PlanSnapshotModel)
            .where(PlanSnapshotModel.id == current_snapshot.id)
            .values(payload=current_snapshot.to_payload())
        )
        session.add(
            PlanSnapshotModel(
                id=old_snapshot.id,
                schema_version=old_snapshot.schema_version,
                workspace_id=old_snapshot.workspace_id,
                root_task_id=old_snapshot.root_task_id,
                generation=old_snapshot.generation,
                parent_snapshot_id=old_snapshot.parent_snapshot_id,
                source_type=old_snapshot.source_type,
                source_ref=old_snapshot.source_ref,
                title=old_snapshot.title,
                goal_summary=old_snapshot.goal_summary,
                payload=old_snapshot.to_payload(),
                published_by=old_snapshot.published_by,
                published_at=old_snapshot.published_at,
            )
        )
        session.add(
            RootTaskPlanHeadModel(
                workspace_id=workspace_id,
                root_task_id=ids["root_task"],
                active_snapshot_id=current_snapshot.id,
                active_generation=current_snapshot.generation,
                active_snapshot_hash=current_hash,
                version=1,
                updated_at=NOW,
            )
        )
        session.flush()
        current_request = request_row(ids, request_id=ids["request"])
        current_request.status = "materialized"
        current_request.execution_lineage_id = ids["execution"]
        current_request.first_execution_id = ids["execution"]
        session.add(current_request)
        old_request = request_row(ids, worker_key="legacy")
        old_request.plan_snapshot_id = old_snapshot.id
        old_request.plan_generation = old_snapshot.generation
        session.add(old_request)
        session.add(
            ArtifactModel(
                id=uuid4(),
                workspace_id=workspace_id,
                task_id=ids["subtask"],
                execution_id=ids["execution"],
                artifact_type="diff",
                storage_ref="private://trace-body",
                content_hash="a" * 64,
                size_bytes=128,
                media_type="text/plain",
                provenance={"trace": "trace body"},
                created_at=NOW,
            )
        )
        session.add(
            EvidenceClaimModel(
                id=uuid4(),
                workspace_id=workspace_id,
                task_id=ids["subtask"],
                execution_id=ids["execution"],
                acceptance_criterion_ref="acceptance:test",
                conclusion="knowledge body",
                environment_ref="private://environment",
                artifact_ids=[],
                producer="test",
                status="candidate",
                created_at=NOW,
            )
        )
        session.commit()

    policy = CollaborationPolicySnapshotV1(
        schema_version=1,
        runtime_definition_id=ids["module_definition"],
        runtime_instance_id=ids["module_instance"],
        runtime_interface_version="collaboration-runtime-v1",
        runtime_config_version=1,
        runtime_capability_snapshot=_capability(),
        default_policy=CollaborationPolicyEntry(
            enabled=True,
            strategy_id=CollaborationStrategy.ALL_REQUIRED,
        ),
        subtask_overrides=(),
    )
    run = CollaborationRun.create(
        run_id=uuid4(),
        workspace_id=workspace_id,
        root_task_id=ids["root_task"],
        plan_snapshot_id=current_snapshot.id,
        plan_generation=current_snapshot.generation,
        plan_content_hash=current_hash,
        runtime_definition_id=ids["module_definition"],
        runtime_instance_id=ids["module_instance"],
        runtime_interface_version="collaboration-runtime-v1",
        runtime_config_version=1,
        runtime_capability_snapshot=_capability(),
        policy_snapshot=policy.to_mapping(),
        now=NOW,
    )
    with uow_factory() as uow:
        uow.collaboration_runs.add(run)

    hold_id = uuid4()
    proposal_id = uuid4()
    with database.session() as session:
        session.add(
            CollaborationGovernanceHoldModel(
                id=hold_id,
                workspace_id=workspace_id,
                root_task_id=ids["root_task"],
                run_id=run.id,
                plan_snapshot_id=current_snapshot.id,
                plan_generation=current_snapshot.generation,
                plan_content_hash=current_hash,
                worker_item_id=ids["worker_item"],
                status="active",
                created_by_actor_id="aitejiu",
                created_via="web",
                reason="r" * 2000,
                resource_version=1,
                resolved_at=None,
                created_at=NOW,
            )
        )
        session.add(
            CollaborationReleaseProposalModel(
                id=proposal_id,
                workspace_id=workspace_id,
                hold_id=hold_id,
                run_id=run.id,
                projection_snapshot_id=uuid4(),
                projection_hash="b" * 64,
                decision_id=uuid4(),
                intent_id=uuid4(),
                execution_request_id=ids["request"],
                target_request_version=1,
                input_fingerprint_version=1,
                input_fingerprint="c" * 64,
                status="pending",
                reason_code="release_recommended",
                summary="s" * 3000,
                resource_version=1,
                resolved_at=None,
                created_at=NOW,
            )
        )
        session.commit()

    facts = SqlAlchemyCollaborationFactStore(database).load(
        workspace_id, run, fact_cursor=7
    )

    assert [item.id for item in facts.requests] == [ids["request"]]
    assert [item.id for item in facts.executions] == [ids["execution"]]
    projection = CollaborationProjectionAssembler().assemble(facts, fact_cursor=7)
    encoded = projection.canonical_bytes()
    assert b"trace-body" not in encoded
    assert b"trace body" not in encoded
    assert b"knowledge body" not in encoded

    query_facts = SqlAlchemyCollaborationQueryStore(database).load(
        workspace_id, ids["root_task"]
    )
    assert query_facts is not None
    assert [item.id for item in query_facts.holds] == [hold_id]
    assert [item.id for item in query_facts.proposals] == [proposal_id]
    assert len(query_facts.holds[0].reason or "") == 2000
    assert len(query_facts.proposals[0].summary) == 2000
    assert query_facts.proposals[0].worker_item_id == ids["worker_item"]
    assert query_facts.applications == ()

    run.last_evaluated_change_cursor = 8
    with pytest.raises(ValidationError, match="behind the run watermark"):
        SqlAlchemyCollaborationFactStore(database).load(
            workspace_id, run, fact_cursor=7
        )


def test_result_source_facts_exclude_same_worker_key_from_another_root(
    database,
    workspace_id: UUID,
    uow_factory,
) -> None:
    ids, _, run = _seed_placed_source_run(database, workspace_id, uow_factory)
    current_artifact_id = uuid4()
    other_artifact_id = uuid4()
    other_root_id = uuid4()
    other_subtask_id = uuid4()
    other_plan_id = uuid4()
    other_worker_item_id = uuid4()
    other_execution_id = uuid4()

    with database.session() as session:
        session.add_all(
            [
                ArtifactModel(
                    id=current_artifact_id,
                    workspace_id=workspace_id,
                    task_id=ids["subtask"],
                    execution_id=ids["execution"],
                    artifact_type="report",
                    storage_ref="private://current-report",
                    content_hash="1" * 64,
                    size_bytes=64,
                    media_type="text/plain",
                    provenance={},
                    created_at=NOW,
                ),
                TaskModel(
                    id=other_root_id,
                    workspace_id=workspace_id,
                    title="Other root",
                    description="Must not enter current source facts",
                    acceptance_criteria=["acceptance:other"],
                    status="running",
                    priority=50,
                    requested_by="integration",
                    version=1,
                    created_at=NOW,
                ),
                TaskModel(
                    id=other_subtask_id,
                    workspace_id=workspace_id,
                    title="Other subtask",
                    description="Same worker key in another root",
                    acceptance_criteria=["acceptance:other"],
                    status="running",
                    priority=50,
                    requested_by="integration",
                    version=1,
                    created_at=NOW,
                ),
            ]
        )
        session.flush()
        session.add(
            PlanSnapshotModel(
                id=other_plan_id,
                schema_version=1,
                workspace_id=workspace_id,
                root_task_id=other_root_id,
                generation=1,
                parent_snapshot_id=None,
                source_type="integration",
                source_ref="test:other-root",
                title="Other plan",
                goal_summary="Scope isolation",
                payload={},
                published_by="integration",
                published_at=NOW,
                created_at=NOW,
            )
        )
        session.add(
            WorkerItemModel(
                id=other_worker_item_id,
                workspace_id=workspace_id,
                root_task_id=other_root_id,
                stable_key="backend",
                title="Other backend",
                responsibility="Must remain isolated",
                status="active",
                context_version=1,
                acknowledged_context_version=1,
                derived_from_worker_item_ids=[],
                version=1,
                created_at=NOW,
            )
        )
        session.flush()
        session.add(
            WorkerItemPlacementModel(
                plan_snapshot_id=other_plan_id,
                plan_generation=1,
                workspace_id=workspace_id,
                root_task_id=other_root_id,
                worker_item_id=other_worker_item_id,
                worker_item_key="backend",
                subtask_key="implementation",
                subtask_task_id=other_subtask_id,
                role="implementer",
                capability_requirements=["python"],
                worker_policy_ref=None,
                configuration_ref=None,
            )
        )
        session.add(
            ExecutionModel(
                id=other_execution_id,
                workspace_id=workspace_id,
                task_id=other_subtask_id,
                lineage_id=other_execution_id,
                attempt=1,
                max_attempts=1,
                purpose="scope isolation",
                status="succeeded",
                capability_requirements=["python"],
                adapter_preferences=[],
                adapter_forbidden=[],
                worker_item_id=other_worker_item_id,
                last_fencing_token=0,
                not_before=NOW,
                started_at=NOW,
                finished_at=NOW,
                version=1,
                created_at=NOW,
            )
        )
        session.flush()
        session.add(
            ArtifactModel(
                id=other_artifact_id,
                workspace_id=workspace_id,
                task_id=other_subtask_id,
                execution_id=other_execution_id,
                artifact_type="report",
                storage_ref="private://other-root-report",
                content_hash="2" * 64,
                size_bytes=64,
                media_type="text/plain",
                provenance={},
                created_at=NOW,
            )
        )
        session.commit()

    facts = SqlAlchemyCollaborationFactStore(database).load(
        workspace_id, run, fact_cursor=0
    )
    assert {item.worker_item_id for item in facts.worker_placements} == {
        ids["worker_item"]
    }
    assert {item.id for item in facts.executions} == {ids["execution"]}
    assert {item.id for item in facts.artifacts} == {current_artifact_id}
    assert other_artifact_id not in {
        item.artifact_id
        for item in resolve_result_input_source(
            worker_item_key="backend", facts=facts
        ).artifacts
    }


def test_result_source_facts_do_not_resolve_a_foreign_workspace_artifact_id(
    database,
    workspace_id: UUID,
    uow_factory,
) -> None:
    ids, _, run = _seed_placed_source_run(database, workspace_id, uow_factory)
    foreign_worker_item_id = uuid4()
    foreign_execution_id = uuid4()
    foreign_artifact_id = uuid4()

    with database.session() as session:
        session.add(
            WorkerItemModel(
                id=foreign_worker_item_id,
                workspace_id=WORKSPACE_B,
                root_task_id=ids["workspace_b_task"],
                stable_key="backend",
                title="Foreign backend",
                responsibility="Foreign workspace source",
                status="active",
                context_version=1,
                acknowledged_context_version=1,
                derived_from_worker_item_ids=[],
                version=1,
                created_at=NOW,
            )
        )
        session.flush()
        session.add(
            ExecutionModel(
                id=foreign_execution_id,
                workspace_id=WORKSPACE_B,
                task_id=ids["workspace_b_task"],
                lineage_id=foreign_execution_id,
                attempt=1,
                max_attempts=1,
                purpose="foreign workspace source",
                status="succeeded",
                capability_requirements=[],
                adapter_preferences=[],
                adapter_forbidden=[],
                worker_item_id=foreign_worker_item_id,
                last_fencing_token=0,
                not_before=NOW,
                started_at=NOW,
                finished_at=NOW,
                version=1,
                created_at=NOW,
            )
        )
        session.flush()
        session.add(
            ArtifactModel(
                id=foreign_artifact_id,
                workspace_id=WORKSPACE_B,
                task_id=ids["workspace_b_task"],
                execution_id=foreign_execution_id,
                artifact_type="report",
                storage_ref="private://foreign-report",
                content_hash="3" * 64,
                size_bytes=64,
                media_type="text/plain",
                provenance={},
                created_at=NOW,
            )
        )
        session.commit()

    facts = SqlAlchemyCollaborationFactStore(database).load(
        workspace_id, run, fact_cursor=0
    )
    assert foreign_execution_id not in {item.id for item in facts.executions}
    assert foreign_artifact_id not in {item.id for item in facts.artifacts}
    assert foreign_artifact_id not in {
        item.artifact_id
        for item in resolve_result_input_source(
            worker_item_key="backend", facts=facts
        ).artifacts
    }


def test_converged_handoff_retires_source_execution_artifacts(
    database,
    workspace_id: UUID,
    uow_factory,
) -> None:
    ids = seed_execution_request_facts(database, workspace_id)
    handoff = seed_handoff(database, workspace_id, with_transition=True)
    root_task_id = handoff.proposal.payload.root_task_id
    plan_snapshot_id = handoff.proposal.payload.observed_plan_snapshot_id
    worker_key = f"handoff-worker-{handoff.worker_item_id.hex[:8]}"
    snapshot = _snapshot(
        workspace_id=workspace_id,
        root_task_id=root_task_id,
        snapshot_id=plan_snapshot_id,
        generation=1,
        parent_snapshot_id=None,
    )
    snapshot_hash = plan_snapshot_content_hash(snapshot)
    source_request_id = uuid4()
    target_request_id = uuid4()
    source_artifact_id = uuid4()
    target_artifact_id = uuid4()

    with database.session() as session:
        session.execute(
            update(PlanSnapshotModel)
            .where(PlanSnapshotModel.id == plan_snapshot_id)
            .values(schema_version=2, payload=snapshot.to_payload())
        )
        session.add(
            RootTaskPlanHeadModel(
                workspace_id=workspace_id,
                root_task_id=root_task_id,
                active_snapshot_id=plan_snapshot_id,
                active_generation=1,
                active_snapshot_hash=snapshot_hash,
                version=1,
                updated_at=NOW,
            )
        )
        session.add(
            WorkerItemPlacementModel(
                plan_snapshot_id=plan_snapshot_id,
                plan_generation=1,
                workspace_id=workspace_id,
                root_task_id=root_task_id,
                worker_item_id=handoff.worker_item_id,
                worker_item_key=worker_key,
                subtask_key="handoff",
                subtask_task_id=root_task_id,
                role="implementer",
                capability_requirements=["code"],
                worker_policy_ref=None,
                configuration_ref=None,
            )
        )
        session.execute(
            update(ExecutionModel)
            .where(ExecutionModel.id == handoff.source_execution_id)
            .values(status="succeeded", finished_at=NOW)
        )
        session.execute(
            update(ExecutionModel)
            .where(ExecutionModel.id == handoff.target_execution_id)
            .values(
                lineage_id=handoff.target_execution_id,
                attempt=1,
                status="succeeded",
                finished_at=NOW,
            )
        )
        session.execute(
            update(HandoffTransitionModel)
            .where(HandoffTransitionModel.id == handoff.transition.id)
            .values(
                target_execution_id=handoff.target_execution_id,
                status="converged",
                phase="complete",
                writer_state="target_active",
                completed_at=NOW,
                version=2,
            )
        )

        source_request = request_row(
            ids, request_id=source_request_id, worker_key=worker_key
        )
        source_request.plan_snapshot_id = plan_snapshot_id
        source_request.root_task_id = root_task_id
        source_request.subtask_task_id = root_task_id
        source_request.worker_item_id = handoff.worker_item_id
        source_request.status = "materialized"
        source_request.execution_lineage_id = handoff.source_execution_id
        source_request.first_execution_id = handoff.source_execution_id
        session.add(source_request)

        target_request = request_row(
            ids, request_id=target_request_id, worker_key=worker_key
        )
        target_request.plan_snapshot_id = plan_snapshot_id
        target_request.root_task_id = root_task_id
        target_request.subtask_task_id = root_task_id
        target_request.worker_item_id = handoff.worker_item_id
        target_request.intent_key = f"{worker_key}-handoff"
        target_request.status = "materialized"
        target_request.execution_lineage_id = handoff.target_execution_id
        target_request.first_execution_id = handoff.target_execution_id
        target_request.source_execution_id = handoff.source_execution_id
        target_request.source_session_id = handoff.source_session_id
        target_request.handoff_transition_id = handoff.transition.id
        session.add(target_request)
        session.add_all(
            [
                ArtifactModel(
                    id=source_artifact_id,
                    workspace_id=workspace_id,
                    task_id=root_task_id,
                    execution_id=handoff.source_execution_id,
                    artifact_type="report",
                    storage_ref="private://retired-source",
                    content_hash="4" * 64,
                    size_bytes=64,
                    media_type="text/plain",
                    provenance={},
                    created_at=NOW,
                ),
                ArtifactModel(
                    id=target_artifact_id,
                    workspace_id=workspace_id,
                    task_id=root_task_id,
                    execution_id=handoff.target_execution_id,
                    artifact_type="report",
                    storage_ref="private://handoff-target",
                    content_hash="5" * 64,
                    size_bytes=64,
                    media_type="text/plain",
                    provenance={},
                    created_at=NOW,
                ),
            ]
        )
        session.commit()

    run = _persist_run(uow_factory=uow_factory, ids=ids, snapshot=snapshot)
    facts = SqlAlchemyCollaborationFactStore(database).load(
        workspace_id, run, fact_cursor=0
    )
    source = resolve_result_input_source(worker_item_key=worker_key, facts=facts)

    assert source.reason_code is None
    assert source.effective_request_id == target_request_id
    assert source.effective_execution_id == handoff.target_execution_id
    assert source.allowed_lineage_ids == (
        handoff.source_execution_id,
        handoff.target_execution_id,
    )
    assert {item.artifact_id for item in source.artifacts} == {target_artifact_id}
    assert source_artifact_id not in {item.artifact_id for item in source.artifacts}
