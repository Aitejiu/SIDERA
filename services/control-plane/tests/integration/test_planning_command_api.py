from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
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
from sidera_control_plane.domain.planning import (
    PlanningClarification,
    PlanningRequest,
    PlanningRunLease,
    PlanningRunLeaseStatus,
)
from sidera_control_plane.domain.planning_profiles import (
    InvalidOutputPolicy,
    PlanningBudgetPolicy,
    PlanningProfile,
    PlanningProfileStatus,
    PlanningRetryPolicy,
    UnknownOutcomePolicy,
)
from sidera_control_plane.domain.work_context import (
    WorkContext,
    WorkContextRevisionReason,
)
from sidera_control_plane.persistence.context_package_repository import (
    ContextAssemblyRepository,
)
from sidera_control_plane.persistence.database import Database
from sidera_control_plane.persistence.models import (
    ContextAssemblyRequestModel,
    ExecutionRequestModel,
    PlanningBriefRevisionModel,
    PlanningClarificationModel,
    PlanningRunModel,
    PlanSnapshotModel,
    TaskModel,
    WorkContextModel,
    WorkContextRevisionModel,
)
from sidera_control_plane.persistence.unit_of_work import SqlAlchemyUnitOfWork

pytestmark = pytest.mark.integration


@pytest.fixture
def client(database_url: str) -> Iterator[TestClient]:
    settings = Settings(database_url=database_url, local_actor_id="operator")
    with TestClient(create_app(settings)) as test_client:
        yield test_client


NOW = datetime(2026, 8, 23, 12, 0, tzinfo=UTC)


def _planner_capabilities() -> PlannerAdapterCapabilityProfile:
    return PlannerAdapterCapabilityProfile(
        structured_output=StructuredOutputCapability.JSON_SCHEMA,
        clarification=True,
        idempotency=IdempotencyCapability.OPERATION_ID,
        cancellation=CancellationCapability.RUN,
        usage_reporting=UsageReportingCapability.TOKENS,
        max_context_bytes=128_000,
        stability=StabilityLevel.STABLE,
        interface_version="sidera.planner.v1",
        proposal_schema_versions=(2,),
    )


def _planner_profile(workspace_id: UUID, instance_id: UUID) -> PlanningProfile:
    return PlanningProfile(
        id=uuid4(),
        workspace_id=workspace_id,
        profile_key="integration",
        version=1,
        display_name="Integration planner",
        planner_module_instance_id=instance_id,
        planner_module_config_version=1,
        planner_interface_version="sidera.planner.v1",
        prompt_policy_version="prompt.v1",
        system_policy={"approval": {"required": True}},
        proposal_schema_version=2,
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


def _seed_waiting_request(
    database: Database,
    workspace_id: UUID,
    *,
    source_type: str = "web",
    source_ref: str | None = None,
) -> tuple[UUID, UUID, int]:
    definition_id = uuid4()
    instance_id = uuid4()
    profile = _planner_profile(workspace_id, instance_id)
    request_id = uuid4()
    run_id = uuid4()
    with SqlAlchemyUnitOfWork(database) as uow:
        uow.modules.add_definition(
            ModuleDefinition(
                id=definition_id,
                workspace_id=workspace_id,
                module_type="planner_adapter",
                provider="fake",
                interface_version="sidera.planner.v1",
                config_schema_version=1,
                capabilities=_planner_capabilities(),
                lifecycle=ModuleLifecycle.ACTIVE,
            )
        )
        uow.modules.add_instance(
            ModuleInstance(
                id=instance_id,
                workspace_id=workspace_id,
                definition_id=definition_id,
                instance_name="integration-planner",
                config_version=1,
                config_namespace="planning",
                config={"planning.mode": "test"},
                secret_refs=(),
                lifecycle=ModuleLifecycle.ACTIVE,
            )
        )
        uow.planning_profiles.add(profile)
        request = PlanningRequest.create(
            request_id=request_id,
            workspace_id=workspace_id,
            profile_snapshot=profile.snapshot(_planner_capabilities()),
            objective="Implement planning",
            constraints=("Keep it reviewable",),
            acceptance_criteria=("The clarification is persisted",),
            context_refs=("context:test",),
            requested_outputs=("proposal",),
            source_type=source_type,
            source_ref=source_ref,
            actor="operator",
            correlation_id=uuid4(),
            run_id=run_id,
            now=NOW,
        )
        uow.planning_requests.add(request)
        context_revision = WorkContextRevisionFactory().create(
            work_context_id=uuid4(),
            workspace_id=workspace_id,
            brief=request.briefs[0],
            revision_number=1,
            reason=WorkContextRevisionReason.REQUEST_CREATED,
            actor="operator",
            created_at=NOW,
        )
        uow.work_contexts.add(
            WorkContext.create(
                work_context_id=context_revision.work_context_id,
                workspace_id=workspace_id,
                planning_request_id=request.id,
                first_revision=context_revision,
                created_at=NOW,
            ),
            context_revision,
        )

    with SqlAlchemyUnitOfWork(database) as uow:
        request = uow.planning_requests.get(request_id, workspace_id, for_update=True)
        assert request is not None
        run = request.runs[0]
        lease_id = uuid4()
        uow.planning_leases.add(
            PlanningRunLease(
                id=lease_id,
                workspace_id=workspace_id,
                planning_run_id=run.id,
                owner_id="planning-worker:test",
                status=PlanningRunLeaseStatus.ACTIVE,
                fencing_token=1,
                acquired_at=NOW,
                expires_at=NOW + timedelta(minutes=5),
                released_at=None,
                version=1,
            )
        )
        request.start_planning(
            run_id=run.id,
            lease_id=lease_id,
            fencing_token=1,
            now=NOW,
        )
        clarification = PlanningClarification.create(
            clarification_id=uuid4(),
            planning_request_id=request.id,
            planning_run_id=run.id,
            question="Which adapter should be used?",
            rationale="The request allows multiple adapters.",
            answer_type="choice",
            options=("fake", "opencode"),
            blocking=True,
            created_at=NOW,
        )
        request.mark_needs_clarification(
            run_id=run.id,
            clarifications=(clarification,),
            lease_id=lease_id,
            fencing_token=1,
            now=NOW,
        )
        uow.planning_requests.save(request, 1)
        uow.planning_runs.save(run, 1)
        uow.planning_records.add_clarification(clarification)
        return request.id, clarification.id, request.version


def test_command_response_keeps_legacy_fields_and_adds_data(
    client: TestClient, workspace_id: UUID
) -> None:
    response = client.post(
        "/api/v1/commands",
        json={
            "command_id": str(uuid4()),
            "workspace_id": str(workspace_id),
            "command_type": "RegisterPlannerModule",
            "target_type": "workspace",
            "actor": {"type": "local", "id": "operator"},
            "target_id": None,
            "expected_version": None,
            "idempotency_key": "planning-module-api-1",
            "correlation_id": str(uuid4()),
            "payload": {
                "provider": "fake",
                "interface_version": "sidera.planner.v1",
                "config_schema_version": 1,
                "capabilities": {
                    "structured_output": "json_schema",
                    "clarification": True,
                    "idempotency": "operation-id",
                    "cancellation": "run",
                    "usage_reporting": "tokens",
                    "max_context_bytes": 128000,
                    "stability": "stable",
                    "interface_version": "sidera.planner.v1",
                    "proposal_schema_versions": [2],
                },
                "instance_name": "api-planner",
                "config_namespace": "planning",
                "config": {"planning.mode": "test"},
                "secret_refs": [],
            },
        },
    )

    assert response.status_code == 201, response.text
    body = response.json()
    assert {"command_id", "aggregate_id", "aggregate_version", "event_ids", "data"} <= body.keys()
    assert set(body["data"]) == {"module_definition_id", "module_instance_id"}


def _command(
    workspace_id: UUID,
    command_type: str,
    target_type: str,
    payload: dict[str, object],
    *,
    idempotency_key: str,
    target_id: UUID | None = None,
    expected_version: int | None = None,
) -> dict[str, object]:
    return {
        "command_id": str(uuid4()),
        "workspace_id": str(workspace_id),
        "command_type": command_type,
        "target_type": target_type,
        "actor": {"type": "local", "id": "operator"},
        "target_id": str(target_id) if target_id is not None else None,
        "expected_version": expected_version,
        "idempotency_key": idempotency_key,
        "correlation_id": str(uuid4()),
        "payload": payload,
    }


def test_planning_commands_persist_one_request_lifecycle_without_task_materialization(
    client: TestClient,
    database: Database,
    workspace_id: UUID,
) -> None:
    module_request = _command(
        workspace_id,
        "RegisterPlannerModule",
        "workspace",
        {
            "provider": "fake",
            "interface_version": "sidera.planner.v1",
            "config_schema_version": 1,
            "capabilities": {
                "structured_output": "json_schema",
                "clarification": True,
                "idempotency": "operation-id",
                "cancellation": "run",
                "usage_reporting": "tokens",
                "max_context_bytes": 128000,
                "stability": "stable",
                "interface_version": "sidera.planner.v1",
                "proposal_schema_versions": [2],
            },
            "instance_name": "lifecycle-planner",
            "config_namespace": "planning",
            "config": {"planning.mode": "test"},
            "secret_refs": [],
        },
        idempotency_key="planning-lifecycle-module",
    )
    module_created = client.post("/api/v1/commands", json=module_request)
    assert module_created.status_code == 201, module_created.text
    module_data = module_created.json()["data"]

    module_replay = client.post(
        "/api/v1/commands",
        json={**module_request, "correlation_id": str(uuid4())},
    )
    assert module_replay.status_code == 201, module_replay.text
    assert module_replay.json() == module_created.json()

    profile_request = _command(
        workspace_id,
        "RegisterPlanningProfile",
        "workspace",
        {
            "profile_key": "default",
            "version": 1,
            "display_name": "Default planner",
            "planner_module_instance_id": module_data["module_instance_id"],
            "planner_module_config_version": 1,
            "planner_interface_version": "sidera.planner.v1",
            "prompt_policy_version": "prompt.v1",
            "system_policy": {"approval": {"required": True}},
            "proposal_schema_version": 2,
            "allowed_capabilities": ["code"],
            "adapter_preferences": ["opencode"],
            "adapter_forbidden": [],
            "budget": {
                "max_context_bytes": 64000,
                "max_tokens": 10000,
                "max_cost_microunits": 1000000,
                "max_runtime_seconds": 600,
                "max_revisions": 3,
                "max_nodes": 20,
                "max_concurrency": 2,
            },
            "retry_policy": {
                "max_retries": 1,
                "backoff_seconds": [5],
                "unknown_outcome_policy": "block",
                "invalid_output_policy": "retry",
            },
            "default_priority": 50,
            "execution_max_attempts": 2,
            "execution_budget_level": "standard",
            "user_selectable": True,
            "planner_secret_refs": [],
        },
        idempotency_key="planning-lifecycle-profile",
    )
    profile_created = client.post("/api/v1/commands", json=profile_request)
    assert profile_created.status_code == 201, profile_created.text
    profile_data = profile_created.json()["data"]

    submit_request = _command(
        workspace_id,
        "SubmitPlanningRequest",
        "workspace",
        {
            "profile_id": profile_data["planning_profile_id"],
            "profile_version": 1,
            "objective": "Implement planning",
            "constraints": ["Keep it reviewable"],
            "acceptance_criteria": ["The request is queued"],
            "context_refs": ["context:test"],
            "requested_outputs": ["proposal"],
            "source_type": "web",
            "source_ref": "conversation:test",
        },
        idempotency_key="planning-lifecycle-submit",
    )
    submitted = client.post("/api/v1/commands", json=submit_request)
    assert submitted.status_code == 201, submitted.text
    submit_data = submitted.json()["data"]
    assert submit_data["brief_revision"] == 1

    cancelled = client.post(
        "/api/v1/commands",
        json=_command(
            workspace_id,
            "CancelPlanningRequest",
            "planning_request",
            {},
            idempotency_key="planning-lifecycle-cancel",
            target_id=UUID(submit_data["planning_request_id"]),
            expected_version=1,
        ),
    )
    assert cancelled.status_code == 200, cancelled.text
    assert cancelled.json()["data"]["status"] == "cancelled"

    with database.session() as session:
        assert session.scalar(select(func.count()).select_from(TaskModel)) == 0
        assert session.scalar(select(func.count()).select_from(PlanSnapshotModel)) == 0
        assert session.scalar(select(func.count()).select_from(ExecutionRequestModel)) == 0
        assembly = session.scalar(select(ContextAssemblyRequestModel))
        assert assembly is not None
        assert assembly.purpose == "planning_run"
        assert assembly.status == "queued"
        assert assembly.idempotency_key.startswith("context:planning-run:")


def test_answer_planning_clarification_persists_new_brief_and_queued_run(
    client: TestClient,
    database: Database,
    workspace_id: UUID,
) -> None:
    request_id, clarification_id, expected_version = _seed_waiting_request(
        database, workspace_id
    )

    answer_command = _command(
        workspace_id,
        "AnswerPlanningClarification",
        "planning_request",
        {
            "answers": [
                {"clarification_id": str(clarification_id), "answer": "fake"}
            ]
        },
        idempotency_key="planning-answer-integration",
        target_id=request_id,
        expected_version=expected_version,
    )
    response = client.post(
        "/api/v1/commands",
        json=answer_command,
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["data"]["brief_revision"] == 2
    assert body["data"]["planning_request_id"] == str(request_id)

    replay = client.post(
        "/api/v1/commands",
        json={**answer_command, "correlation_id": str(uuid4())},
    )
    assert replay.status_code == 200, replay.text
    assert replay.json() == body

    with database.session() as session:
        briefs = session.scalars(
            select(PlanningBriefRevisionModel).where(
                PlanningBriefRevisionModel.planning_request_id == request_id
            )
        ).all()
        runs = session.scalars(
            select(PlanningRunModel).where(
                PlanningRunModel.planning_request_id == request_id
            )
        ).all()
        clarification = session.scalar(
            select(PlanningClarificationModel).where(
                PlanningClarificationModel.id == clarification_id
            )
        )
        context = session.scalar(
            select(WorkContextModel).where(
                WorkContextModel.planning_request_id == request_id
            )
        )
        revisions = session.scalars(
            select(WorkContextRevisionModel)
            .where(WorkContextRevisionModel.work_context_id == context.id)
            .order_by(WorkContextRevisionModel.revision_number.asc())
        ).all() if context is not None else []
        assert len(briefs) == 2
        assert len(runs) == 2
        assert clarification is not None
        assert clarification.answer == "fake"
        assert clarification.answer_brief_revision == 2
        assert any(run.status == "queued" and run.attempt == 2 for run in runs)
        assert context is not None
        assert context.current_revision_number == 2
        assert len(revisions) == 2
        assert revisions[0].reason == "request_created"
        assert revisions[1].reason == "clarification_accepted"
        assert revisions[0].content_hash != revisions[1].content_hash
        assert revisions[0].snapshot["clarification_decisions"] == []
        assert revisions[1].snapshot["clarification_decisions"][0]["answer"] == "fake"


def test_clarification_context_assembly_failure_rolls_back_new_run(
    client: TestClient,
    database: Database,
    workspace_id: UUID,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    request_id, clarification_id, expected_version = _seed_waiting_request(
        database, workspace_id
    )

    def fail_add(self, request) -> None:
        del self, request
        raise RuntimeError("context assembly insert failed")

    monkeypatch.setattr(ContextAssemblyRepository, "add", fail_add)

    with pytest.raises(RuntimeError, match="context assembly insert failed"):
        client.post(
            "/api/v1/commands",
            json=_command(
                workspace_id,
                "AnswerPlanningClarification",
                "planning_request",
                {
                    "answers": [
                        {"clarification_id": str(clarification_id), "answer": "fake"}
                    ]
                },
                idempotency_key="planning-answer-assembly-failure",
                target_id=request_id,
                expected_version=expected_version,
            ),
        )

    with database.session() as session:
        assert session.scalar(
            select(func.count()).select_from(PlanningBriefRevisionModel)
        ) == 1
        assert session.scalar(select(func.count()).select_from(PlanningRunModel)) == 1
        assert session.scalar(
            select(func.count()).select_from(WorkContextRevisionModel)
        ) == 1
        assert session.scalar(
            select(func.count()).select_from(ContextAssemblyRequestModel)
        ) == 0


def test_stale_answer_returns_actionable_planning_conflict(
    client: TestClient,
    database: Database,
    workspace_id: UUID,
) -> None:
    request_id, clarification_id, current_version = _seed_waiting_request(
        database, workspace_id
    )
    response = client.post(
        "/api/v1/commands",
        json=_command(
            workspace_id,
            "AnswerPlanningClarification",
            "planning_request",
            {
                "answers": [
                    {"clarification_id": str(clarification_id), "answer": "fake"}
                ]
            },
            idempotency_key="planning-answer-stale",
            target_id=request_id,
            expected_version=current_version - 1,
        ),
    )

    assert response.status_code == 409, response.text
    assert response.json()["error"] == {
        "code": "planning_conflict",
        "message": "planning request or proposal changed",
        "details": {
            "current_request_version": current_version,
            "current_proposal_revision": None,
            "current_proposal_content_hash": None,
        },
    }
    detail = client.get(
        f"/api/v1/workspaces/{workspace_id}/planning-requests/{request_id}"
    )
    assert detail.status_code == 200, detail.text
    assert detail.json()["version"] == current_version


@pytest.mark.parametrize(
    ("command_type", "target_type", "payload"),
    [
        (
            "AnswerPlanningClarification",
            "planning_request",
            {"answers": []},
        ),
        (
            "RequestProposalRevision",
            "planning_request",
            {
                "current_proposal_revision": 1,
                "current_proposal_content_hash": "a" * 64,
                "feedback": "narrow the scope",
                "unexpected": True,
            },
        ),
        (
            "RejectPlanningProposal",
            "planning_request",
            {
                "current_proposal_revision": 1,
                "current_proposal_content_hash": "a" * 64,
            },
        ),
        (
            "SubmitPlanRevision",
            "task",
            {
                "root_task_id": str(uuid4()),
                "expected_base_snapshot_id": str(uuid4()),
                "expected_base_generation": 1,
                "expected_base_content_hash": "a" * 64,
                "objective": "revise the plan",
                "constraints": [],
                "acceptance_criteria": ["the revision is reviewable"],
                "planning_profile_id": str(uuid4()),
                "planning_profile_version": 1,
            },
        ),
        (
            "ConfirmPlanningProposal",
            "planning_request",
            {
                "proposal_revision": 1,
                "proposal_content_hash": "a" * 64,
                "profile_id": str(uuid4()),
                "profile_version": 1,
                "unexpected": True,
            },
        ),
        ("RetryBlockedPlanningRun", "planning_run", {}),
    ],
)
def test_planning_mutation_payloads_are_strict(
    client: TestClient,
    workspace_id: UUID,
    command_type: str,
    target_type: str,
    payload: dict[str, object],
) -> None:
    response = client.post(
        "/api/v1/commands",
        json=_command(
            workspace_id,
            command_type,
            target_type,
            payload,
            idempotency_key=f"strict-{command_type}",
            target_id=uuid4(),
            expected_version=1,
        ),
    )

    assert response.status_code == 422, response.text
    assert response.json()["error"]["code"] == "validation_error"
