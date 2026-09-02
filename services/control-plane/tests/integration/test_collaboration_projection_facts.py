from datetime import UTC, datetime
from uuid import UUID, uuid4

import pytest
from sqlalchemy import update

from sidera_control_plane.application.collaboration_projection import (
    CollaborationProjectionAssembler,
)
from sidera_control_plane.domain.collaboration import CollaborationRun
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
from sidera_control_plane.persistence.models import (
    ArtifactModel,
    EvidenceClaimModel,
    PlanSnapshotModel,
    RootTaskPlanHeadModel,
)

from .test_r5_1_execution_request_schema import (
    NOW,
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
        policy_snapshot={"schema_version": 1, "strategy": "all_required"},
        now=NOW,
    )
    with uow_factory() as uow:
        uow.collaboration_runs.add(run)

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

    run.last_evaluated_change_cursor = 8
    with pytest.raises(ValidationError, match="behind the run watermark"):
        SqlAlchemyCollaborationFactStore(database).load(
            workspace_id, run, fact_cursor=7
        )
