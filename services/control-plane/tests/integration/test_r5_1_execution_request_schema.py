from datetime import UTC, datetime
from uuid import UUID, uuid4

import pytest
from sqlalchemy import inspect, select
from sqlalchemy.exc import IntegrityError

from sidera_control_plane.persistence.database import Database
from sidera_control_plane.persistence.models import (
    ExecutionModel,
    ExecutionRequestDependencyModel,
    ExecutionRequestModel,
    ModuleDefinitionModel,
    ModuleInstanceModel,
    PlanningBriefRevisionModel,
    PlanningProfileModel,
    PlanningProposalRevisionModel,
    PlanningRequestModel,
    PlanningRunModel,
    PlanSnapshotModel,
    ProposalMaterializationModel,
    TaskModel,
    WorkerItemModel,
    WorkspaceModel,
)

pytestmark = pytest.mark.integration

NOW = datetime(2026, 8, 23, 12, 0, tzinfo=UTC)
WORKSPACE_A = UUID("00000000-0000-0000-0000-000000000001")
WORKSPACE_B = UUID("00000000-0000-0000-0000-000000000002")


def seed_execution_request_facts(
    database: Database, workspace_id: UUID
) -> dict[str, UUID]:
    assert workspace_id == WORKSPACE_A
    ids = {
        "module_definition": uuid4(),
        "module_instance": uuid4(),
        "profile": uuid4(),
        "request": uuid4(),
        "brief": uuid4(),
        "run": uuid4(),
        "proposal": uuid4(),
        "root_task": uuid4(),
        "subtask": uuid4(),
        "plan": uuid4(),
        "worker_item": uuid4(),
        "execution": uuid4(),
        "workspace_b_task": uuid4(),
    }
    with database.session() as session:
        session.add(
            WorkspaceModel(
                id=WORKSPACE_B,
                name="R5.1 Secondary Workspace",
                status="active",
                version=1,
            )
        )
        session.add(
            ModuleDefinitionModel(
                id=ids["module_definition"],
                workspace_id=WORKSPACE_A,
                module_type="planner_adapter",
                provider="test",
                interface_version="sidera.planner.v1",
                config_schema_version=1,
                capabilities={},
                lifecycle="active",
            )
        )
        session.flush()
        session.add(
            ModuleInstanceModel(
                id=ids["module_instance"],
                workspace_id=WORKSPACE_A,
                definition_id=ids["module_definition"],
                instance_name="r5-planner",
                config_version=1,
                config_namespace="planning",
                config={},
                secret_refs=[],
                lifecycle="active",
            )
        )
        session.flush()
        session.add(
            PlanningProfileModel(
                id=ids["profile"],
                workspace_id=WORKSPACE_A,
                profile_key="default",
                version=1,
                display_name="Default planner",
                planner_module_instance_id=ids["module_instance"],
                planner_module_config_version=1,
                planner_interface_version="sidera.planner.v1",
                status="active",
                user_selectable=True,
                policy={"proposal_schema_version": 2},
            )
        )
        session.flush()
        session.add(
            PlanningRequestModel(
                id=ids["request"],
                workspace_id=WORKSPACE_A,
                kind="create",
                status="proposal_ready",
                planning_profile_id=ids["profile"],
                planning_profile_version=1,
                profile_snapshot={"profile_version": 1},
                source_type="test",
                source_ref="test:r5.1",
                actor="integration",
                correlation_id=uuid4(),
                version=1,
            )
        )
        session.flush()
        session.add(
            PlanningBriefRevisionModel(
                planning_request_id=ids["request"],
                revision=1,
                workspace_id=WORKSPACE_A,
                payload={"objective": "test"},
                created_by="integration",
            )
        )
        session.flush()
        session.add(
            PlanningRunModel(
                id=ids["run"],
                workspace_id=WORKSPACE_A,
                planning_request_id=ids["request"],
                brief_revision=1,
                planning_profile_id=ids["profile"],
                planning_profile_version=1,
                planner_module_instance_id=ids["module_instance"],
                planner_module_config_version=1,
                attempt=1,
                operation_id=uuid4(),
                status="succeeded",
                result_kind="proposal",
                capability_snapshot={},
                usage={},
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
                id=ids["proposal"],
                planning_request_id=ids["request"],
                revision=1,
                workspace_id=WORKSPACE_A,
                brief_revision=1,
                planning_run_id=ids["run"],
                proposal_schema_version=2,
                kind="create",
                profile_id=ids["profile"],
                profile_version=1,
                planner_module_instance_id=ids["module_instance"],
                planner_module_config_version=1,
                decision_summary={},
                plan_template={},
                execution_intents=[],
                impact_policy={},
                derived_diff={},
                canonical_payload={},
                content_hash="a" * 64,
            )
        )
        session.flush()
        for task_id, title in (
            (ids["root_task"], "Root task"),
            (ids["subtask"], "Subtask"),
            (ids["workspace_b_task"], "Workspace B task"),
        ):
            session.add(
                TaskModel(
                    id=task_id,
                    workspace_id=(
                        WORKSPACE_B
                        if task_id == ids["workspace_b_task"]
                        else WORKSPACE_A
                    ),
                    title=title,
                    description="test",
                    acceptance_criteria=["acceptance:test"],
                    status="pending",
                    priority=50,
                    requested_by="integration",
                    version=1,
                )
            )
        session.flush()
        session.add(
            PlanSnapshotModel(
                id=ids["plan"],
                schema_version=2,
                workspace_id=WORKSPACE_A,
                root_task_id=ids["root_task"],
                generation=1,
                parent_snapshot_id=None,
                source_type="test",
                source_ref="test:r5.1",
                title="Test plan",
                goal_summary="Test activation facts",
                payload={},
                published_by="integration",
                published_at=NOW,
            )
        )
        session.flush()
        session.add(
            WorkerItemModel(
                id=ids["worker_item"],
                workspace_id=WORKSPACE_A,
                root_task_id=ids["root_task"],
                stable_key="backend",
                title="Backend",
                responsibility="Implement backend",
                status="active",
                context_version=1,
                acknowledged_context_version=0,
                derived_from_worker_item_ids=[],
                version=1,
            )
        )
        session.flush()
        session.add(
            ExecutionModel(
                id=ids["execution"],
                workspace_id=WORKSPACE_A,
                task_id=ids["subtask"],
                lineage_id=ids["execution"],
                attempt=1,
                max_attempts=1,
                purpose="test",
                status="succeeded",
                capability_requirements=["python"],
                last_fencing_token=0,
                not_before=NOW,
                started_at=NOW,
                finished_at=NOW,
                version=1,
                worker_item_id=ids["worker_item"],
            )
        )
        session.commit()
    return ids


def request_row(
    ids: dict[str, UUID],
    *,
    request_id: UUID | None = None,
    worker_key: str = "backend",
) -> ExecutionRequestModel:
    return ExecutionRequestModel(
        id=request_id or uuid4(),
        workspace_id=WORKSPACE_A,
        planning_request_id=ids["request"],
        proposal_revision_id=ids["proposal"],
        plan_snapshot_id=ids["plan"],
        plan_generation=1,
        root_task_id=ids["root_task"],
        subtask_task_id=ids["subtask"],
        worker_item_id=ids["worker_item"],
        worker_item_key=worker_key,
        intent_key=f"{worker_key}-intent",
        purpose="run worker",
        capability_requirements=["python"],
        adapter_preferences=["opencode"],
        budget_class="standard",
        not_before=NOW,
        max_attempts=1,
        expected_outputs=["diff"],
        expected_artifact_refs=["artifact_type:diff"],
        expected_evidence_refs=["acceptance:test"],
        status="blocked",
        gate_reason="waiting_dependency",
        gate_details={"waiting": True},
        gate_evaluated_at=NOW,
        status_changed_at=NOW,
        version=1,
    )


def test_r5_1_execution_request_schema_has_idempotency_and_lineage_constraints(
    database: Database,
) -> None:
    with database.engine.connect() as connection:
        inspector = inspect(connection)
        assert {
            "proposal_materializations",
            "execution_requests",
            "execution_request_dependencies",
        } <= set(inspector.get_table_names())
        request_uniques = {
            item["name"] for item in inspector.get_unique_constraints("execution_requests")
        }
        request_indexes = {
            item["name"] for item in inspector.get_indexes("execution_requests")
        }
        request_checks = {
            item["name"] for item in inspector.get_check_constraints("execution_requests")
        }
        dependency_checks = {
            item["name"]
            for item in inspector.get_check_constraints("execution_request_dependencies")
        }

    assert "uq_execution_requests_proposal_worker_item" in request_uniques
    assert "uq_execution_requests_identity" in request_uniques
    assert "uq_execution_requests_lineage" in request_indexes
    assert "ck_execution_requests_materialized_lineage" in request_checks
    assert "ck_execution_request_dependencies_not_self" in dependency_checks


def test_r5_1_materialization_and_request_uniqueness_are_database_enforced(
    database: Database,
    workspace_id: UUID,
) -> None:
    ids = seed_execution_request_facts(database, workspace_id)
    materialization = ProposalMaterializationModel(
        id=uuid4(),
        workspace_id=WORKSPACE_A,
        planning_request_id=ids["request"],
        proposal_revision_id=ids["proposal"],
        root_task_id=ids["root_task"],
        plan_snapshot_id=ids["plan"],
        plan_generation=1,
        subtask_task_mapping={"subtask": str(ids["subtask"])},
        worker_item_mapping={"backend": str(ids["worker_item"])},
        execution_request_mapping={},
        deep_link="/workspaces/test/tasks/root",
        confirmed_by="integration",
    )
    with database.session() as session:
        session.add(materialization)
        session.commit()

    with pytest.raises(IntegrityError), database.session() as session:
        session.add(
            ProposalMaterializationModel(
                id=uuid4(),
                workspace_id=WORKSPACE_A,
                planning_request_id=ids["request"],
                proposal_revision_id=ids["proposal"],
                root_task_id=ids["root_task"],
                plan_snapshot_id=ids["plan"],
                plan_generation=1,
                subtask_task_mapping={},
                worker_item_mapping={},
                execution_request_mapping={},
                deep_link="/duplicate",
                confirmed_by="integration",
            )
        )
        session.commit()

    with database.session() as session:
        session.add(request_row(ids))
        session.commit()

    with pytest.raises(IntegrityError), database.session() as session:
        session.add(request_row(ids, worker_key="backend"))
        session.commit()

    with pytest.raises(IntegrityError), database.session() as session:
        invalid = request_row(ids, worker_key="materialized-without-lineage")
        invalid.status = "materialized"
        session.add(invalid)
        session.commit()


def test_r5_1_dependency_edges_are_unique_non_self_and_workspace_scoped(
    database: Database,
    workspace_id: UUID,
) -> None:
    ids = seed_execution_request_facts(database, workspace_id)
    downstream = request_row(ids)
    with database.session() as session:
        session.add(downstream)
        session.add(request_row(ids, worker_key="upstream"))
        session.commit()

    with database.session() as session:
        upstream_id = session.scalar(
            select(ExecutionRequestModel.id).where(
                ExecutionRequestModel.worker_item_key == "upstream"
            )
        )
    assert upstream_id is not None

    edge = ExecutionRequestDependencyModel(
        workspace_id=WORKSPACE_A,
        downstream_request_id=downstream.id,
        upstream_request_id=upstream_id,
        enforcement="hard",
        required_artifact_refs=["artifact_type:diff"],
        required_evidence_refs=["acceptance:test"],
    )
    with database.session() as session:
        session.add(edge)
        session.commit()

    with pytest.raises(IntegrityError), database.session() as session:
        session.add(
            ExecutionRequestDependencyModel(
                workspace_id=WORKSPACE_A,
                downstream_request_id=downstream.id,
                upstream_request_id=upstream_id,
                enforcement="hard",
                required_artifact_refs=[],
                required_evidence_refs=[],
            )
        )
        session.commit()

    with pytest.raises(IntegrityError), database.session() as session:
        session.add(
            ExecutionRequestDependencyModel(
                workspace_id=WORKSPACE_A,
                downstream_request_id=downstream.id,
                upstream_request_id=downstream.id,
                enforcement="hard",
                required_artifact_refs=[],
                required_evidence_refs=[],
            )
        )
        session.commit()


def test_r5_1_execution_request_rejects_cross_workspace_task_reference(
    database: Database,
    workspace_id: UUID,
) -> None:
    ids = seed_execution_request_facts(database, workspace_id)
    with pytest.raises(IntegrityError), database.session() as session:
        invalid = request_row(ids, worker_key="cross-workspace-task")
        invalid.root_task_id = ids["workspace_b_task"]
        session.add(invalid)
        session.commit()
