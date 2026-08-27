from datetime import UTC, datetime
from uuid import UUID, uuid4

import pytest
from sqlalchemy import inspect, text, update
from sqlalchemy.exc import IntegrityError

from sidera_control_plane.persistence.database import Database
from sidera_control_plane.persistence.models import (
    ContextPatchAcknowledgementModel,
    ContextPatchModel,
    ModuleDefinitionModel,
    ModuleInstanceModel,
    PlanEntityLineageModel,
    PlanningBriefRevisionModel,
    PlanningProfileModel,
    PlanningProposalRevisionModel,
    PlanningRequestModel,
    PlanningRunModel,
    PlanSnapshotModel,
    PlanTransitionModel,
    RootTaskPlanHeadModel,
    TaskModel,
    WorkerItemModel,
    WorkerTransitionModel,
)

pytestmark = pytest.mark.integration


EXPECTED_TABLES = {
    "root_task_plan_heads",
    "plan_transitions",
    "worker_transitions",
    "plan_entity_lineages",
    "context_patches",
    "context_patch_acknowledgements",
}

NOW = datetime(2026, 8, 28, 12, 0, tzinfo=UTC)


def _task(task_id: UUID, workspace_id: UUID, title: str) -> TaskModel:
    return TaskModel(
        id=task_id,
        workspace_id=workspace_id,
        title=title,
        description=title,
        acceptance_criteria=["schema behavior"],
        status="pending",
        priority=50,
        requested_by="r6.1-schema",
        version=1,
    )


def _snapshot(
    snapshot_id: UUID,
    workspace_id: UUID,
    root_task_id: UUID,
    generation: int,
) -> PlanSnapshotModel:
    return PlanSnapshotModel(
        id=snapshot_id,
        schema_version=1,
        workspace_id=workspace_id,
        root_task_id=root_task_id,
        generation=generation,
        parent_snapshot_id=None,
        source_type="test",
        source_ref=f"r6.1-schema:{generation}",
        title=f"Schema plan {generation}",
        goal_summary="Exercise revision schema",
        payload={"generation": generation},
        published_by="r6.1-schema",
        published_at=NOW,
    )


def _planning_dependencies(
    workspace_id: UUID,
) -> tuple[UUID, UUID, UUID, UUID, UUID, UUID]:
    module_definition_id = uuid4()
    module_instance_id = uuid4()
    profile_id = uuid4()
    request_id = uuid4()
    run_id = uuid4()
    proposal_id = uuid4()
    return (
        module_definition_id,
        module_instance_id,
        profile_id,
        request_id,
        run_id,
        proposal_id,
    )


def _add_planning_dependencies(
    session,
    workspace_id: UUID,
    *,
    request_kind: str,
    root_task_id: UUID | None = None,
    base_snapshot_id: UUID | None = None,
) -> tuple[UUID, UUID, UUID, UUID]:
    (
        module_definition_id,
        module_instance_id,
        profile_id,
        request_id,
        run_id,
        proposal_id,
    ) = _planning_dependencies(workspace_id)
    session.add(
        ModuleDefinitionModel(
            id=module_definition_id,
            workspace_id=workspace_id,
            module_type="planner_adapter",
            provider="fake",
            interface_version="sidera.planner.v1",
            config_schema_version=1,
            capabilities={},
            lifecycle="active",
        )
    )
    session.add(
        ModuleInstanceModel(
            id=module_instance_id,
            workspace_id=workspace_id,
            definition_id=module_definition_id,
            instance_name=f"r6.1-schema-{uuid4().hex[:8]}",
            config_version=1,
            config_namespace="planning",
            config={},
            secret_refs=[],
            lifecycle="active",
        )
    )
    session.add(
        PlanningProfileModel(
            id=profile_id,
            workspace_id=workspace_id,
            profile_key=f"r6.1-schema-{uuid4().hex[:8]}",
            version=1,
            display_name="R6.1 schema planner",
            planner_module_instance_id=module_instance_id,
            planner_module_config_version=1,
            planner_interface_version="sidera.planner.v1",
            status="active",
            user_selectable=True,
            policy={},
        )
    )
    session.add(
        PlanningRequestModel(
            id=request_id,
            workspace_id=workspace_id,
            kind=request_kind,
            status="submitted",
            current_brief_revision=None,
            current_proposal_revision=None,
            current_proposal_content_hash=None,
            root_task_id=root_task_id if request_kind == "revision" else None,
            base_plan_snapshot_id=(
                base_snapshot_id if request_kind == "revision" else None
            ),
            base_plan_generation=1 if request_kind == "revision" else None,
            base_plan_content_hash="0" * 64 if request_kind == "revision" else None,
            base_root_task_version=1 if request_kind == "revision" else None,
            base_captured_at=NOW if request_kind == "revision" else None,
            planning_profile_id=profile_id,
            planning_profile_version=1,
            profile_snapshot={"profile_version": 1},
            source_type="test",
            source_ref="r6.1-schema",
            actor="integration",
            correlation_id=uuid4(),
            version=1,
        )
    )
    session.flush()
    if request_kind == "create":
        session.add(
            PlanningBriefRevisionModel(
                planning_request_id=request_id,
                revision=1,
                workspace_id=workspace_id,
                payload={"objective": "schema"},
                created_by="integration",
            )
        )
        session.add(
            PlanningRunModel(
                id=run_id,
                workspace_id=workspace_id,
                planning_request_id=request_id,
                brief_revision=1,
                planning_profile_id=profile_id,
                planning_profile_version=1,
                planner_module_instance_id=module_instance_id,
                planner_module_config_version=1,
                attempt=1,
                operation_id=uuid4(),
                status="succeeded",
                result_kind="proposal",
                capability_snapshot={},
                usage={},
                provider_ref="fake",
                error_code=None,
                error_message=None,
                diagnostic_refs=[],
                queued_at=NOW,
                started_at=NOW,
                finished_at=NOW,
                version=1,
            )
        )
        session.flush()
        session.add(
            PlanningProposalRevisionModel(
                id=proposal_id,
                planning_request_id=request_id,
                revision=1,
                workspace_id=workspace_id,
                brief_revision=1,
                planning_run_id=run_id,
                proposal_schema_version=2,
                kind="create",
                profile_id=profile_id,
                profile_version=1,
                planner_module_instance_id=module_instance_id,
                planner_module_config_version=1,
                decision_summary={},
                plan_template={},
                execution_intents=[],
                impact_policy={},
                derived_diff={},
                canonical_payload={},
                content_hash="0" * 64,
            )
        )
        session.flush()
    return request_id, proposal_id, module_instance_id, profile_id


def _constraint_names(connection, table_name: str) -> set[str]:
    rows = connection.execute(
        text(
            """
            SELECT constraint_name
            FROM information_schema.table_constraints
            WHERE table_schema = current_schema()
              AND table_name = :table_name
            """
        ),
        {"table_name": table_name},
    )
    return {row.constraint_name for row in rows}


def _index_definitions(connection, table_name: str) -> dict[str, str]:
    rows = connection.execute(
        text(
            """
            SELECT indexname, indexdef
            FROM pg_indexes
            WHERE schemaname = current_schema()
              AND tablename = :table_name
            """
        ),
        {"table_name": table_name},
    )
    return {row.indexname: row.indexdef for row in rows}


def test_r6_1_schema_creates_revision_transition_and_patch_tables(
    database: Database,
) -> None:
    assert {
        RootTaskPlanHeadModel.__tablename__,
        PlanTransitionModel.__tablename__,
        WorkerTransitionModel.__tablename__,
        PlanEntityLineageModel.__tablename__,
        ContextPatchModel.__tablename__,
        ContextPatchAcknowledgementModel.__tablename__,
    } == EXPECTED_TABLES
    with database.engine.connect() as connection:
        inspector = inspect(connection)
        assert EXPECTED_TABLES <= set(inspector.get_table_names())

        expected_columns = {
            "root_task_plan_heads": {
                "workspace_id",
                "root_task_id",
                "active_snapshot_id",
                "active_generation",
                "active_snapshot_hash",
                "version",
                "created_at",
                "updated_at",
            },
            "plan_transitions": {
                "id",
                "workspace_id",
                "root_task_id",
                "planning_request_id",
                "proposal_revision_id",
                "from_snapshot_id",
                "from_generation",
                "to_snapshot_id",
                "to_generation",
                "status",
                "started_at",
                "completed_at",
                "version",
                "created_at",
            },
            "worker_transitions": {
                "id",
                "plan_transition_id",
                "workspace_id",
                "root_task_id",
                "worker_item_id",
                "from_subtask_id",
                "to_subtask_id",
                "action",
                "fallback_action",
                "target_context_version",
                "source_execution_id",
                "source_session_id",
                "target_execution_id",
                "target_session_id",
                "writer_state",
                "status",
                "failure_code",
                "lease_id",
                "fencing_token",
                "attempt_count",
                "next_attempt_at",
                "started_at",
                "completed_at",
                "version",
                "created_at",
            },
            "plan_entity_lineages": {
                "id",
                "plan_transition_id",
                "workspace_id",
                "target_type",
                "target_id",
                "source_type",
                "source_id",
                "reason",
                "created_at",
            },
            "context_patches": {
                "id",
                "workspace_id",
                "root_task_id",
                "worker_item_id",
                "plan_transition_id",
                "worker_transition_id",
                "base_plan_snapshot_id",
                "base_generation",
                "target_plan_snapshot_id",
                "target_generation",
                "base_context_package_id",
                "base_package_hash",
                "base_work_context_revision_id",
                "base_context_hash",
                "target_context_version",
                "scope",
                "changed_requirements",
                "changed_acceptance_refs",
                "changed_dependency_refs",
                "decision_refs",
                "artifact_refs",
                "evidence_refs",
                "intervention_refs",
                "unfinished_work",
                "checkpoint_ref",
                "patch_hash",
                "proposer",
                "expires_at",
                "created_at",
            },
            "context_patch_acknowledgements": {
                "patch_id",
                "workspace_id",
                "patch_hash",
                "execution_id",
                "session_id",
                "operation_id",
                "accepted_sequence",
                "adapter_ref",
                "acknowledged_at",
                "created_at",
            },
        }
        for table_name, columns in expected_columns.items():
            actual = {item["name"] for item in inspector.get_columns(table_name)}
            assert columns <= actual

        request_columns = {
            item["name"] for item in inspector.get_columns("planning_requests")
        }
        materialization_columns = {
            item["name"]
            for item in inspector.get_columns("proposal_materializations")
        }
        input_columns = {
            item["name"] for item in inspector.get_columns("execution_inputs")
        }
        assert {
            "root_task_id",
            "base_plan_snapshot_id",
            "base_plan_generation",
            "base_plan_content_hash",
            "base_root_task_version",
            "base_captured_at",
            "conflict_reason",
        } <= request_columns
        assert {"kind", "diff_hash", "plan_transition_id"} <= materialization_columns
        assert "superseded_reason" in input_columns

        assert {
            "uq_plan_transitions_request_proposal",
            "uq_plan_entity_lineages_identity",
            "uq_context_patches_patch_hash",
        } <= _constraint_names(connection, "plan_transitions") | _constraint_names(
            connection, "plan_entity_lineages"
        ) | _constraint_names(connection, "context_patches") | _constraint_names(
            connection, "context_patch_acknowledgements"
        )
        assert inspector.get_pk_constraint("context_patch_acknowledgements")[
            "constrained_columns"
        ] == ["patch_id"]
        revision_indexes = _index_definitions(connection, "planning_requests")
        transition_indexes = _index_definitions(connection, "worker_transitions")
        assert "uq_planning_requests_active_revision" in revision_indexes
        revision_index = revision_indexes["uq_planning_requests_active_revision"]
        assert "status" in revision_index
        assert all(
            status in revision_index
            for status in (
                "submitted",
                "planning",
                "waiting_for_user",
                "proposal_ready",
                "failed",
            )
        )
        assert "kind" in revision_index and "revision" in revision_index
        assert "uq_worker_transitions_active_worker_item" in transition_indexes
        assert "blocked_unknown" in transition_indexes[
            "uq_worker_transitions_active_worker_item"
        ]


def test_r6_1_schema_exposes_expected_checks_and_compatibility_columns(
    database: Database,
) -> None:
    with database.engine.connect() as connection:
        request_checks = _constraint_names(connection, "planning_requests")
        input_checks = _constraint_names(connection, "execution_inputs")
        transition_checks = _constraint_names(connection, "worker_transitions")
        patch_checks = _constraint_names(connection, "context_patches")

    assert "ck_planning_requests_revision_base" in request_checks
    assert "ck_execution_inputs_status" in input_checks
    assert "ck_worker_transitions_status" in transition_checks
    assert "ck_worker_transitions_writer_state" in transition_checks
    assert "ck_context_patches_generation" in patch_checks
    assert "ck_context_patches_hashes" in patch_checks


def test_r6_1_active_revision_and_worker_transition_slots_are_released(
    database: Database,
    workspace_id: UUID,
) -> None:
    root_task_id = uuid4()
    first_snapshot_id = uuid4()
    second_snapshot_id = uuid4()
    worker_item_id = uuid4()
    with database.session() as session:
        session.add(_task(root_task_id, workspace_id, "Revision schema root"))
        session.flush()
        session.add_all(
            [
                _snapshot(
                    first_snapshot_id, workspace_id, root_task_id, generation=1
                ),
                _snapshot(
                    second_snapshot_id, workspace_id, root_task_id, generation=2
                ),
                WorkerItemModel(
                    id=worker_item_id,
                    workspace_id=workspace_id,
                    root_task_id=root_task_id,
                    stable_key="schema-worker",
                    title="Schema worker",
                    responsibility="Exercise the transition fence",
                    status="active",
                    context_version=1,
                    acknowledged_context_version=1,
                    derived_from_worker_item_ids=[],
                    version=1,
                ),
            ]
        )
        session.flush()
        first_request_id, _, _, _ = _add_planning_dependencies(
            session,
            workspace_id,
            request_kind="revision",
            root_task_id=root_task_id,
            base_snapshot_id=first_snapshot_id,
        )
        session.commit()

    with database.session() as session:
        with pytest.raises(IntegrityError):
            _add_planning_dependencies(
                session,
                workspace_id,
                request_kind="revision",
                root_task_id=root_task_id,
                base_snapshot_id=first_snapshot_id,
            )
            session.flush()
        session.rollback()
        session.execute(
            update(PlanningRequestModel)
            .where(PlanningRequestModel.id == first_request_id)
            .values(status="materialized")
        )
        session.commit()

    with database.session() as session:
        _add_planning_dependencies(
            session,
            workspace_id,
            request_kind="revision",
            root_task_id=root_task_id,
            base_snapshot_id=first_snapshot_id,
        )
        session.commit()

    with database.session() as session:
        plan_request_id, proposal_id, _, _ = _add_planning_dependencies(
            session, workspace_id, request_kind="create"
        )
        plan_transition_id = uuid4()
        session.add(
            PlanTransitionModel(
                id=plan_transition_id,
                workspace_id=workspace_id,
                root_task_id=root_task_id,
                planning_request_id=plan_request_id,
                proposal_revision_id=proposal_id,
                from_snapshot_id=first_snapshot_id,
                from_generation=1,
                to_snapshot_id=second_snapshot_id,
                to_generation=2,
                status="converging",
                started_at=NOW,
                completed_at=None,
                version=1,
            )
        )
        session.commit()

    def transition(transition_id: UUID) -> WorkerTransitionModel:
        return WorkerTransitionModel(
            id=transition_id,
            plan_transition_id=plan_transition_id,
            workspace_id=workspace_id,
            root_task_id=root_task_id,
            worker_item_id=worker_item_id,
            from_subtask_id=None,
            to_subtask_id=None,
            action="preserve",
            fallback_action=None,
            target_context_version=1,
            source_execution_id=None,
            source_session_id=None,
            target_execution_id=None,
            target_session_id=None,
            writer_state="fenced",
            status="pending",
            failure_code=None,
            lease_id=None,
            fencing_token=0,
            attempt_count=0,
            next_attempt_at=None,
            started_at=None,
            completed_at=None,
            version=1,
        )

    with database.session() as session:
        session.add(transition(uuid4()))
        session.commit()
    with database.session() as session:
        session.add(transition(uuid4()))
        with pytest.raises(IntegrityError):
            session.commit()
        session.rollback()
        session.execute(
            update(WorkerTransitionModel)
            .where(WorkerTransitionModel.worker_item_id == worker_item_id)
            .values(status="converged", writer_state="closed")
        )
        session.commit()
    with database.session() as session:
        session.add(transition(uuid4()))
        session.commit()
