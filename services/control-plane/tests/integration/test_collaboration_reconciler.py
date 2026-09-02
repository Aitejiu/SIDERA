from datetime import UTC, datetime
from uuid import UUID, uuid4

import pytest
from sqlalchemy import select, update

from sidera_control_plane.adapters.reference_collaboration_runtime import (
    ReferenceCollaborationRuntime,
)
from sidera_control_plane.application.collaboration_reconciler import (
    CollaborationReconciler,
)
from sidera_control_plane.application.collaboration_runtime_resolver import (
    CollaborationRuntimeResolver,
    RegistryCollaborationRuntimeFactory,
)
from sidera_control_plane.domain.collaboration import (
    CollaborationPolicyEntry,
    CollaborationPolicySnapshotV1,
    CollaborationStrategy,
)
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
from sidera_control_plane.persistence.models import (
    CollaborationEvaluationModel,
    CollaborationRunModel,
    ExecutionRequestModel,
    ModuleDefinitionModel,
    PlanSnapshotModel,
    RootTaskPlanHeadModel,
)
from sidera_control_plane.persistence.module_repository import (
    capability_profile_to_payload,
)

from .test_r5_1_execution_request_schema import (
    request_row,
    seed_execution_request_facts,
)

pytestmark = pytest.mark.integration

NOW = datetime(2026, 9, 3, 12, 0, tzinfo=UTC)


def _capability() -> CollaborationRuntimeCapabilityProfile:
    return CollaborationRuntimeCapabilityProfile(
        interface_version="collaboration-runtime.v1",
        projection_schema_versions=(1,),
        decision_schema_versions=(1,),
        strategy_ids=(CollaborationStrategy.ALL_REQUIRED.value,),
        idempotency=IdempotencyCapability.OPERATION_ID,
        max_projection_bytes=262_144,
        max_decision_bytes=65_536,
        max_intents_per_decision=128,
        max_strategy_state_bytes=16_384,
        stability=StabilityLevel.STABLE,
    )


def _policy(definition_id: UUID, instance_id: UUID) -> CollaborationPolicySnapshotV1:
    return CollaborationPolicySnapshotV1(
        schema_version=1,
        runtime_definition_id=definition_id,
        runtime_instance_id=instance_id,
        runtime_interface_version="collaboration-runtime.v1",
        runtime_config_version=1,
        runtime_capability_snapshot=_capability(),
        default_policy=CollaborationPolicyEntry(
            enabled=True,
            strategy_id=CollaborationStrategy.ALL_REQUIRED,
        ),
        subtask_overrides=(),
    )


def _snapshot(
    *,
    workspace_id: UUID,
    root_task_id: UUID,
    snapshot_id: UUID,
    definition_id: UUID,
    instance_id: UUID,
) -> TaskPlanSnapshot:
    return TaskPlanSnapshot.create(
        snapshot_id=snapshot_id,
        schema_version=3,
        workspace_id=workspace_id,
        root_task_id=root_task_id,
        generation=1,
        parent_snapshot_id=None,
        source_type="integration",
        source_ref="collaboration-reconciler",
        title="Collaboration reconciler plan",
        goal_summary="Prepare a durable collaboration evaluation",
        acceptance_refs=("acceptance:test",),
        published_by="integration",
        published_at=NOW,
        collaboration_policy=_policy(definition_id, instance_id),
    )


def test_reconciler_persists_one_pinned_run_and_evaluation(
    database,
    workspace_id: UUID,
    uow_factory,
) -> None:
    ids = seed_execution_request_facts(database, workspace_id)
    snapshot = _snapshot(
        workspace_id=workspace_id,
        root_task_id=ids["root_task"],
        snapshot_id=ids["plan"],
        definition_id=ids["module_definition"],
        instance_id=ids["module_instance"],
    )
    snapshot_hash = plan_snapshot_content_hash(snapshot)
    capability_payload = capability_profile_to_payload(_capability())

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
                capabilities=capability_payload,
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
                active_snapshot_hash=snapshot_hash,
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
    )
    prepared = reconciler.prepare_next(
        owner_id="collaboration-integration",
        now=NOW,
        workspace_id=workspace_id,
        root_task_id=snapshot.root_task_id,
    )

    assert prepared is not None
    assert prepared.run.plan_snapshot_id == snapshot.id
    assert prepared.run.plan_generation == snapshot.generation
    assert prepared.evaluation.operation_id == prepared.operation_id
    assert prepared.evaluation.lease_owner == "collaboration-integration"

    repeated = reconciler.prepare_next(
        owner_id="collaboration-integration-2",
        now=NOW,
        workspace_id=workspace_id,
        root_task_id=snapshot.root_task_id,
    )
    assert repeated is None

    with database.session() as session:
        runs = session.scalars(
            select(CollaborationRunModel).where(
                CollaborationRunModel.workspace_id == workspace_id,
                CollaborationRunModel.root_task_id == snapshot.root_task_id,
            )
        ).all()
        evaluations = session.scalars(
            select(CollaborationEvaluationModel).where(
                CollaborationEvaluationModel.workspace_id == workspace_id,
                CollaborationEvaluationModel.run_id == prepared.run.id,
            )
        ).all()

    assert len(runs) == 1
    assert runs[0].plan_snapshot_id == snapshot.id
    assert runs[0].plan_generation == snapshot.generation
    assert len(evaluations) == 1
    assert evaluations[0].operation_id == prepared.operation_id
    assert evaluations[0].lease_owner == "collaboration-integration"
