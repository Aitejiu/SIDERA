from collections.abc import Iterator
from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from sidera_control_plane.api.app import create_app
from sidera_control_plane.application.work_context_commands import (
    WorkContextRevisionFactory,
)
from sidera_control_plane.config import Settings
from sidera_control_plane.domain.modules import (
    CancellationCapability,
    IdempotencyCapability,
    ModuleDefinition,
    ModuleInstance,
    ModuleLifecycle,
    PlannerAdapterCapabilityProfile,
    StabilityLevel,
    StructuredOutputCapability,
    UsageReportingCapability,
)
from sidera_control_plane.domain.plan_revisions import plan_snapshot_content_hash
from sidera_control_plane.domain.planning import PlanningRequest
from sidera_control_plane.domain.planning_profiles import (
    InvalidOutputPolicy,
    PlanningBudgetPolicy,
    PlanningProfile,
    PlanningProfileStatus,
    PlanningRetryPolicy,
    UnknownOutcomePolicy,
)
from sidera_control_plane.domain.plans import TaskPlanSnapshot
from sidera_control_plane.domain.task import Task
from sidera_control_plane.domain.work_context import (
    WorkContext,
    WorkContextRevisionReason,
)
from sidera_control_plane.persistence.database import Database
from sidera_control_plane.persistence.models import (
    ContextAssemblyRequestModel,
    PlanningRequestModel,
    PlanSnapshotModel,
    RootTaskPlanHeadModel,
    TaskModel,
    WorkContextModel,
    WorkContextRevisionModel,
)
from sidera_control_plane.persistence.unit_of_work import SqlAlchemyUnitOfWork

pytestmark = pytest.mark.integration

NOW = datetime(2026, 8, 28, 12, 0, tzinfo=UTC)


@pytest.fixture
def client(database_url: str) -> Iterator[TestClient]:
    settings = Settings(database_url=database_url, local_actor_id="operator")
    with TestClient(create_app(settings)) as test_client:
        yield test_client


def _capabilities() -> PlannerAdapterCapabilityProfile:
    return PlannerAdapterCapabilityProfile(
        structured_output=StructuredOutputCapability.JSON_SCHEMA,
        clarification=True,
        idempotency=IdempotencyCapability.OPERATION_ID,
        cancellation=CancellationCapability.RUN,
        usage_reporting=UsageReportingCapability.TOKENS,
        max_context_bytes=128_000,
        stability=StabilityLevel.STABLE,
        interface_version="sidera.planner.v1",
        proposal_schema_versions=(2, 3),
    )


def _profile(workspace_id: UUID, instance_id: UUID) -> PlanningProfile:
    return PlanningProfile(
        id=uuid4(),
        workspace_id=workspace_id,
        profile_key="revision-integration",
        version=1,
        display_name="Revision integration planner",
        planner_module_instance_id=instance_id,
        planner_module_config_version=1,
        planner_interface_version="sidera.planner.v1",
        prompt_policy_version="prompt.v1",
        system_policy={"approval": {"required": True}},
        proposal_schema_version=3,
        allowed_capabilities=("code",),
        adapter_preferences=("fake",),
        adapter_forbidden=(),
        budget=PlanningBudgetPolicy(
            max_context_bytes=64_000,
            max_tokens=10_000,
            max_cost_microunits=1_000_000,
            max_runtime_seconds=600,
            max_revisions=3,
            max_nodes=20,
            max_concurrency=2,
        ),
        retry_policy=PlanningRetryPolicy(
            max_retries=1,
            backoff_seconds=(5,),
            unknown_outcome_policy=UnknownOutcomePolicy.BLOCK,
            invalid_output_policy=InvalidOutputPolicy.RETRY,
        ),
        default_priority=50,
        execution_max_attempts=2,
        execution_budget_level="standard",
        user_selectable=True,
        status=PlanningProfileStatus.ACTIVE,
        planner_secret_refs=(),
    )


def _seed_revision_base(
    database: Database, workspace_id: UUID
) -> tuple[Task, TaskPlanSnapshot, PlanningProfile]:
    definition_id = uuid4()
    instance_id = uuid4()
    profile = _profile(workspace_id, instance_id)
    root_task = Task.create(
        task_id=uuid4(),
        workspace_id=workspace_id,
        title="Revision root",
        description="Root task for plan revision integration",
        acceptance_criteria=("The revision remains reviewable",),
        priority=50,
        requested_by="integration",
    )
    snapshot = TaskPlanSnapshot.create(
        snapshot_id=uuid4(),
        schema_version=1,
        workspace_id=workspace_id,
        root_task_id=root_task.id,
        generation=1,
        parent_snapshot_id=None,
        source_type="integration",
        source_ref="revision-base",
        title="Initial plan",
        goal_summary="Initial plan used as an exact revision base",
        acceptance_refs=("plan:reviewable",),
        published_by="integration",
        published_at=NOW,
    )
    capabilities = _capabilities()
    original_request = PlanningRequest.create(
        request_id=uuid4(),
        workspace_id=workspace_id,
        profile_snapshot=profile.snapshot(capabilities),
        objective="Create the initial plan",
        constraints=(),
        acceptance_criteria=("The initial plan is reviewable",),
        context_refs=(),
        requested_outputs=("plan",),
        source_type="integration",
        source_ref="revision-base",
        actor="integration",
        correlation_id=uuid4(),
        run_id=uuid4(),
        now=NOW,
    )

    with SqlAlchemyUnitOfWork(database) as uow:
        uow.modules.add_definition(
            ModuleDefinition(
                id=definition_id,
                workspace_id=workspace_id,
                module_type="planner_adapter",
                provider="fake",
                interface_version="sidera.planner.v1",
                config_schema_version=1,
                capabilities=capabilities,
                lifecycle=ModuleLifecycle.ACTIVE,
            )
        )
        uow.modules.add_instance(
            ModuleInstance(
                id=instance_id,
                workspace_id=workspace_id,
                definition_id=definition_id,
                instance_name="revision-integration-planner",
                config_version=1,
                config_namespace="planning",
                config={"planning.mode": "test"},
                secret_refs=(),
                lifecycle=ModuleLifecycle.ACTIVE,
            )
        )
        uow.planning_profiles.add(profile)
        uow.tasks.add(root_task)
        uow.plans.add_snapshot_and_advance_head(snapshot, expected_head=None)
        uow.planning_requests.add(original_request)
        context_revision = WorkContextRevisionFactory().create(
            work_context_id=uuid4(),
            workspace_id=workspace_id,
            brief=original_request.briefs[0],
            revision_number=1,
            reason=WorkContextRevisionReason.REQUEST_CREATED,
            actor="integration",
            created_at=NOW,
        )
        context = WorkContext.create(
            work_context_id=context_revision.work_context_id,
            workspace_id=workspace_id,
            planning_request_id=original_request.id,
            first_revision=context_revision,
            created_at=NOW,
        )
        context.bind_root_task(root_task.id, expected_version=1, now=NOW)
        uow.work_contexts.add(context, context_revision)
    return root_task, snapshot, profile


def _command(
    workspace_id: UUID,
    root_task: Task,
    snapshot: TaskPlanSnapshot,
    profile: PlanningProfile,
    *,
    command_id: UUID | None = None,
    idempotency_key: str = "revision-integration",
    expected_generation: int | None = None,
) -> dict[str, Any]:
    return {
        "command_id": str(command_id or uuid4()),
        "workspace_id": str(workspace_id),
        "command_type": "SubmitPlanRevision",
        "target_type": "task",
        "actor": {"type": "local", "id": "operator"},
        "target_id": str(root_task.id),
        "expected_version": root_task.version,
        "idempotency_key": idempotency_key,
        "correlation_id": str(uuid4()),
        "payload": {
            "root_task_id": str(root_task.id),
            "expected_base_snapshot_id": str(snapshot.id),
            "expected_base_generation": (
                snapshot.generation
                if expected_generation is None
                else expected_generation
            ),
            "expected_base_content_hash": plan_snapshot_content_hash(snapshot),
            "objective": "Revise the running plan",
            "constraints": ["Keep completed work reusable"],
            "acceptance_criteria": ["The revised plan is reviewable"],
            "planning_profile_id": str(profile.id),
            "planning_profile_version": profile.version,
        },
    }


def test_submit_plan_revision_is_idempotent_and_preserves_base(
    client: TestClient,
    database: Database,
    workspace_id: UUID,
) -> None:
    root_task, snapshot, profile = _seed_revision_base(database, workspace_id)
    request = _command(workspace_id, root_task, snapshot, profile)

    first = client.post("/api/v1/commands", json=request)
    assert first.status_code == 201, first.text
    replay = client.post(
        "/api/v1/commands",
        json={**request, "correlation_id": str(uuid4())},
    )
    assert replay.status_code == 201, replay.text
    assert replay.json() == first.json()

    with database.session() as session:
        assert session.scalar(select(func.count()).select_from(TaskModel)) == 1
        assert session.scalar(select(func.count()).select_from(PlanSnapshotModel)) == 1
        assert session.scalar(select(func.count()).select_from(RootTaskPlanHeadModel)) == 1
        assert session.scalar(select(func.count()).select_from(PlanningRequestModel)) == 2
        assert session.scalar(select(func.count()).select_from(ContextAssemblyRequestModel)) == 1
        task_row = session.scalar(select(TaskModel).where(TaskModel.id == root_task.id))
        head_row = session.scalar(
            select(RootTaskPlanHeadModel).where(
                RootTaskPlanHeadModel.root_task_id == root_task.id
            )
        )
        context_row = session.scalar(
            select(WorkContextModel).where(WorkContextModel.root_task_id == root_task.id)
        )
        assert task_row is not None
        assert task_row.version == root_task.version
        assert task_row.status == "pending"
        assert head_row is not None
        assert head_row.active_snapshot_id == snapshot.id
        assert head_row.active_generation == snapshot.generation
        assert head_row.active_snapshot_hash == plan_snapshot_content_hash(snapshot)
        assert context_row is not None
        assert context_row.current_revision_number == 2
        revisions = session.scalars(
            select(WorkContextRevisionModel)
            .where(WorkContextRevisionModel.work_context_id == context_row.id)
            .order_by(WorkContextRevisionModel.revision_number)
        ).all()
        assert len(revisions) == 2
        assert revisions[-1].reason == "replanned"


def test_submit_plan_revision_rejects_stale_generation_without_new_revision(
    client: TestClient,
    database: Database,
    workspace_id: UUID,
) -> None:
    root_task, snapshot, profile = _seed_revision_base(database, workspace_id)
    response = client.post(
        "/api/v1/commands",
        json=_command(
            workspace_id,
            root_task,
            snapshot,
            profile,
            idempotency_key="revision-stale-generation",
            expected_generation=2,
        ),
    )

    assert response.status_code == 409, response.text
    assert response.json()["error"]["code"] == "plan_generation_conflict"
    with database.session() as session:
        assert session.scalar(select(func.count()).select_from(PlanningRequestModel)) == 1
        assert session.scalar(select(func.count()).select_from(WorkContextRevisionModel)) == 1


def test_submit_plan_revision_rejects_root_from_another_workspace(
    client: TestClient,
    database: Database,
    workspace_id: UUID,
) -> None:
    root_task, snapshot, profile = _seed_revision_base(database, workspace_id)
    other_workspace = UUID("00000000-0000-0000-0000-000000000099")
    with database.session() as session:
        from sidera_control_plane.persistence.models import WorkspaceModel

        session.add(
            WorkspaceModel(
                id=other_workspace,
                name="Other workspace",
                status="active",
                version=1,
            )
        )
        session.commit()

    body = _command(other_workspace, root_task, snapshot, profile)
    response = client.post("/api/v1/commands", json=body)
    assert response.status_code == 404, response.text
    assert response.json()["error"]["code"] == "not_found"
