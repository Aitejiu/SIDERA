from __future__ import annotations

from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import UTC, datetime
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select, update

from sidera_control_plane.adapters.fake_planner import _proposal_payload
from sidera_control_plane.api.app import create_app
from sidera_control_plane.application.work_context_commands import (
    WorkContextRevisionFactory,
)
from sidera_control_plane.application.worker_item_materializer import (
    WorkerItemMaterializer,
)
from sidera_control_plane.config import Settings
from sidera_control_plane.domain.errors import VersionConflict
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
from sidera_control_plane.domain.planning import PlanningRequest
from sidera_control_plane.domain.planning_profiles import (
    InvalidOutputPolicy,
    PlanningBudgetPolicy,
    PlanningProfile,
    PlanningProfileStatus,
    PlanningRetryPolicy,
    UnknownOutcomePolicy,
)
from sidera_control_plane.domain.planning_proposals import (
    PlanningProfileDescriptor,
    ProposalRevision,
)
from sidera_control_plane.domain.plans import PlanValidationError, TaskPlanSnapshot
from sidera_control_plane.domain.work_context import (
    WorkContext,
    WorkContextRevisionReason,
)
from sidera_control_plane.persistence.models import (
    EventModel,
    ExecutionModel,
    ExecutionRequestModel,
    OutboxMessageModel,
    PlanningProfileModel,
    PlanSnapshotModel,
    ProposalMaterializationModel,
    TaskModel,
    WorkContextModel,
    WorkContextRevisionModel,
    WorkerItemModel,
    WorkerItemPlacementModel,
)
from sidera_control_plane.persistence.work_context_repository import (
    WorkContextRepository,
)

pytestmark = pytest.mark.integration

NOW = datetime(2026, 8, 23, 12, 0, tzinfo=UTC)


@pytest.fixture
def client(database_url: str) -> Iterator[TestClient]:
    settings = Settings(database_url=database_url, local_actor_id="operator")
    with TestClient(create_app(settings)) as test_client:
        yield test_client


def planner_capabilities() -> PlannerAdapterCapabilityProfile:
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


def profile(workspace_id: UUID, instance_id: UUID) -> PlanningProfile:
    return PlanningProfile(
        id=uuid4(),
        workspace_id=workspace_id,
        profile_key="default",
        version=1,
        display_name="Default planner",
        planner_module_instance_id=instance_id,
        planner_module_config_version=1,
        planner_interface_version="sidera.planner.v1",
        prompt_policy_version="prompt.v1",
        system_policy={},
        proposal_schema_version=2,
        allowed_capabilities=("code",),
        adapter_preferences=("fake",),
        adapter_forbidden=("unsafe",),
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
            backoff_seconds=(1,),
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


def seed_proposal(
    uow_factory,
    workspace_id: UUID,
    *,
    proposal_payload=None,
) -> tuple[UUID, UUID, str, UUID]:
    definition_id = uuid4()
    instance_id = uuid4()
    definition = ModuleDefinition(
        id=definition_id,
        workspace_id=workspace_id,
        module_type="planner_adapter",
        provider="fake",
        interface_version="sidera.planner.v1",
        config_schema_version=1,
        capabilities=planner_capabilities(),
        lifecycle=ModuleLifecycle.ACTIVE,
    )
    instance = ModuleInstance(
        id=instance_id,
        workspace_id=workspace_id,
        definition_id=definition_id,
        instance_name="confirmation-planner",
        config_version=1,
        config_namespace="planning",
        config={},
        secret_refs=(),
        lifecycle=ModuleLifecycle.ACTIVE,
    )
    stored_profile = profile(workspace_id, instance_id)
    request = PlanningRequest.create(
        request_id=uuid4(),
        workspace_id=workspace_id,
        profile_snapshot=stored_profile.snapshot(planner_capabilities()),
        objective="Confirm a generated plan",
        constraints=("Keep materialization atomic",),
        acceptance_criteria=("The plan is executable",),
        context_refs=("context:test",),
        requested_outputs=("proposal",),
        source_type="integration",
        source_ref="test:confirmation",
        actor="operator",
        correlation_id=uuid4(),
        run_id=uuid4(),
        now=NOW,
    )
    with uow_factory() as uow:
        uow.modules.add_definition(definition)
        uow.modules.add_instance(instance)
        uow.planning_profiles.add(stored_profile)
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

    with uow_factory() as uow:
        request = uow.planning_requests.get(request.id, workspace_id, for_update=True)
        assert request is not None
        run = request.runs[0]
        previous_request_version = request.version
        previous_run_version = run.version
        lease_id = uuid4()
        request.start_planning(
            run_id=run.id,
            lease_id=lease_id,
            fencing_token=1,
            now=NOW,
        )
        request.succeed_run(
            run_id=run.id,
            lease_id=lease_id,
            fencing_token=1,
            provider_ref="fake:planner",
            structured_result_type="planning.proposal.v2",
            usage=run.usage,
            now=NOW,
        )
        proposal = ProposalRevision.create(
            planning_request_id=request.id,
            revision=1,
            brief_revision=1,
            planning_run_id=run.id,
            kind=request.kind,
            payload=proposal_payload or _proposal_payload(),
            profile_descriptor=PlanningProfileDescriptor.from_snapshot(
                request.profile_snapshot
            ),
            profile_snapshot=request.profile_snapshot,
            created_at=NOW,
        )
        request.mark_proposal_ready(
            run_id=run.id,
            proposal_revision=proposal.revision,
            content_hash=proposal.content_hash,
            now=NOW,
        )
        uow.planning_records.add_proposal(proposal, workspace_id)
        uow.planning_runs.save(run, previous_run_version)
        uow.planning_requests.save(request, previous_request_version)
    return request.id, stored_profile.id, proposal.content_hash, proposal.id


def _two_worker_proposal_payload():
    payload = _proposal_payload()
    first_worker = payload.worker_items[0]
    second_worker = replace(
        first_worker,
        key="verification-worker",
        title="Verification worker",
        responsibility="Verify the requested change.",
    )
    second_intent = replace(
        payload.execution_intents[0],
        worker_item_key=second_worker.key,
        purpose=second_worker.responsibility,
    )
    return replace(
        payload,
        worker_items=(first_worker, second_worker),
        execution_intents=(payload.execution_intents[0], second_intent),
    )


def command(
    workspace_id: UUID,
    request_id: UUID,
    profile_id: UUID,
    content_hash: str,
    *,
    expected_version: int = 4,
    proposal_revision: int = 1,
    idempotency_key: str = "confirm-proposal",
) -> dict[str, object]:
    return {
        "command_id": str(uuid4()),
        "workspace_id": str(workspace_id),
        "command_type": "ConfirmPlanningProposal",
        "target_type": "planning_request",
        "actor": {"type": "local", "id": "operator"},
        "target_id": str(request_id),
        "expected_version": expected_version,
        "idempotency_key": idempotency_key,
        "correlation_id": str(uuid4()),
        "payload": {
            "proposal_revision": proposal_revision,
            "proposal_content_hash": content_hash,
            "profile_id": str(profile_id),
            "profile_version": 1,
        },
    }


def test_confirm_planning_proposal_materializes_atomic_task_first_graph(
    client: TestClient,
    database,
    workspace_id: UUID,
    uow_factory,
) -> None:
    request_id, profile_id, content_hash, proposal_id = seed_proposal(
        uow_factory, workspace_id
    )

    response = client.post(
        "/api/v1/commands",
        json=command(workspace_id, request_id, profile_id, content_hash),
    )

    assert response.status_code == 201, response.text
    body = response.json()
    data = body["data"]
    root_task_id = UUID(data["root_task_id"])
    assert body["aggregate_id"] == str(request_id)
    assert data["proposal_revision"] == 1
    assert data["proposal_content_hash"] == content_hash
    assert data["deep_link"] == f"/workspaces/{workspace_id}/tasks/{root_task_id}"
    assert len(data["subtask_task_mapping"]) == 1
    assert len(data["worker_item_mapping"]) == 1
    assert len(data["execution_request_mapping"]) == 1

    with database.session() as session:
        assert session.scalar(select(func.count()).select_from(TaskModel)) == 2
        assert session.scalar(select(func.count()).select_from(PlanSnapshotModel)) == 1
        assert session.scalar(select(func.count()).select_from(WorkerItemModel)) == 1
        assert session.scalar(select(func.count()).select_from(WorkerItemPlacementModel)) == 1
        assert session.scalar(select(func.count()).select_from(ExecutionRequestModel)) == 1
        assert session.scalar(select(func.count()).select_from(ExecutionModel)) == 0
        materialization = session.scalar(
            select(ProposalMaterializationModel).where(
                ProposalMaterializationModel.planning_request_id == request_id
            )
        )
        assert materialization is not None
        assert materialization.proposal_revision_id == proposal_id
        context = session.scalar(
            select(WorkContextModel).where(
                WorkContextModel.planning_request_id == request_id
            )
        )
        assert context is not None
        assert context.root_task_id == root_task_id
        assert context.current_revision_number == 2
        revisions = session.scalars(
            select(WorkContextRevisionModel)
            .where(WorkContextRevisionModel.work_context_id == context.id)
            .order_by(WorkContextRevisionModel.revision_number.asc())
        ).all()
        assert len(revisions) == 2
        assert revisions[1].reason == "proposal_confirmed"
        assert revisions[1].brief_planning_request_id == request_id
        assert revisions[1].brief_revision == 1
        assert revisions[1].proposal_revision_id == proposal_id
        assert revisions[1].plan_snapshot_id == materialization.plan_snapshot_id
        assert "decision_summary" not in revisions[1].snapshot
        assert "worker_items" not in revisions[1].snapshot
        assert any(
            item["key"] == "proposal:understanding"
            for item in revisions[1].snapshot["accepted_decisions"]
        )
        context_events = session.scalars(
            select(EventModel).where(
                EventModel.aggregate_type == "work_context",
                EventModel.aggregate_id == context.id,
            )
        ).all()
        binding_events = session.scalars(
            select(EventModel).where(
                EventModel.aggregate_type == "work_context_binding",
                EventModel.aggregate_id == context.id,
            )
        ).all()
        context_outbox = session.scalars(select(OutboxMessageModel)).all()
        assert [event.event_type for event in context_events] == ["WorkContextRevised"]
        assert [event.event_type for event in binding_events] == [
            "WorkContextBoundToTask"
        ]
        assert {
            row.topic
            for row in context_outbox
            if row.payload["aggregate_id"] == str(context.id)
        } == {"work_context.revised", "work_context.bound_to_task"}

    with uow_factory() as uow:
        planning_request = uow.planning_requests.get(request_id, workspace_id)
        assert planning_request is not None
        assert planning_request.status.value == "materialized"
        execution_requests = [
            uow.execution_requests.get(UUID(data["execution_request_mapping"]["implementation-worker"]), workspace_id)
        ]
        assert execution_requests[0] is not None
        assert execution_requests[0].status.value == "ready"


def test_confirm_planning_proposal_flushes_snapshot_before_multiple_placements(
    client: TestClient,
    database,
    workspace_id: UUID,
    uow_factory,
) -> None:
    request_id, profile_id, content_hash, _proposal_id = seed_proposal(
        uow_factory,
        workspace_id,
        proposal_payload=_two_worker_proposal_payload(),
    )

    response = client.post(
        "/api/v1/commands",
        json=command(
            workspace_id,
            request_id,
            profile_id,
            content_hash,
            idempotency_key="confirm-multiple-placements",
        ),
    )

    assert response.status_code == 201, response.text
    with database.session() as session:
        assert session.scalar(select(func.count()).select_from(PlanSnapshotModel)) == 1
        assert session.scalar(select(func.count()).select_from(WorkerItemPlacementModel)) == 2


def test_confirm_replay_returns_same_mapping_without_second_root_task(
    client: TestClient,
    database,
    workspace_id: UUID,
    uow_factory,
) -> None:
    request_id, profile_id, content_hash, _proposal_id = seed_proposal(
        uow_factory, workspace_id
    )
    first = command(
        workspace_id,
        request_id,
        profile_id,
        content_hash,
        idempotency_key="confirm-replay",
    )
    created = client.post("/api/v1/commands", json=first)
    assert created.status_code == 201, created.text

    replay = client.post(
        "/api/v1/commands",
        json={**first, "correlation_id": str(uuid4())},
    )
    assert replay.status_code == 201, replay.text
    assert replay.json() == created.json()

    with database.session() as session:
        assert session.scalar(select(func.count()).select_from(TaskModel)) == 2
        assert session.scalar(select(func.count()).select_from(ProposalMaterializationModel)) == 1


def test_invalid_confirmation_rolls_back_without_creating_task_rows(
    client: TestClient,
    database,
    workspace_id: UUID,
    uow_factory,
) -> None:
    request_id, profile_id, content_hash, _proposal_id = seed_proposal(
        uow_factory, workspace_id
    )

    response = client.post(
        "/api/v1/commands",
        json=command(
            workspace_id,
            request_id,
            profile_id,
            "0" * 64,
            idempotency_key="confirm-invalid-hash",
        ),
    )

    assert response.status_code == 409, response.text
    assert response.json()["error"] == {
        "code": "planning_conflict",
        "message": "planning request or proposal changed",
        "details": {
            "current_request_version": 4,
            "current_proposal_revision": 1,
            "current_proposal_content_hash": content_hash,
        },
    }
    assert content_hash != "0" * 64
    with database.session() as session:
        _assert_no_materialization_rows(session)


def test_stale_request_version_rejects_before_materialization(
    client: TestClient,
    database,
    workspace_id: UUID,
    uow_factory,
) -> None:
    request_id, profile_id, content_hash, _proposal_id = seed_proposal(
        uow_factory, workspace_id
    )

    response = client.post(
        "/api/v1/commands",
        json=command(
            workspace_id,
            request_id,
            profile_id,
            content_hash,
            expected_version=3,
            idempotency_key="confirm-stale-version",
        ),
    )

    assert response.status_code == 409, response.text
    assert response.json()["error"] == {
        "code": "planning_conflict",
        "message": "planning request or proposal changed",
        "details": {
            "current_request_version": 4,
            "current_proposal_revision": 1,
            "current_proposal_content_hash": content_hash,
        },
    }
    with database.session() as session:
        _assert_no_materialization_rows(session)


def test_non_current_proposal_revision_rejects_before_materialization(
    client: TestClient,
    database,
    workspace_id: UUID,
    uow_factory,
) -> None:
    request_id, profile_id, content_hash, _proposal_id = seed_proposal(
        uow_factory, workspace_id
    )

    response = client.post(
        "/api/v1/commands",
        json=command(
            workspace_id,
            request_id,
            profile_id,
            content_hash,
            proposal_revision=2,
            idempotency_key="confirm-non-current-revision",
        ),
    )

    assert response.status_code == 409, response.text
    assert response.json()["error"] == {
        "code": "planning_conflict",
        "message": "planning request or proposal changed",
        "details": {
            "current_request_version": 4,
            "current_proposal_revision": 1,
            "current_proposal_content_hash": content_hash,
        },
    }
    with database.session() as session:
        _assert_no_materialization_rows(session)


@pytest.mark.parametrize(
    ("command_type", "proposal_payload"),
    [
        (
            "RequestProposalRevision",
            {
                "current_proposal_revision": 2,
                "current_proposal_content_hash": "0" * 64,
                "feedback": "narrow the scope",
            },
        ),
        (
            "RejectPlanningProposal",
            {
                "current_proposal_revision": 2,
                "current_proposal_content_hash": "0" * 64,
                "reason": "proposal is out of scope",
            },
        ),
    ],
)
def test_stale_proposal_reference_returns_actionable_conflict(
    client: TestClient,
    database,
    workspace_id: UUID,
    uow_factory,
    command_type: str,
    proposal_payload: dict[str, object],
) -> None:
    request_id, _profile_id, content_hash, _proposal_id = seed_proposal(
        uow_factory, workspace_id
    )
    response = client.post(
        "/api/v1/commands",
        json={
            "command_id": str(uuid4()),
            "workspace_id": str(workspace_id),
            "command_type": command_type,
            "target_type": "planning_request",
            "actor": {"type": "local", "id": "operator"},
            "target_id": str(request_id),
            "expected_version": 4,
            "idempotency_key": f"stale-{command_type}",
            "correlation_id": str(uuid4()),
            "payload": {**proposal_payload, "current_proposal_content_hash": "0" * 64},
        },
    )

    assert response.status_code == 409, response.text
    assert response.json()["error"] == {
        "code": "planning_conflict",
        "message": "planning request or proposal changed",
        "details": {
            "current_request_version": 4,
            "current_proposal_revision": 1,
            "current_proposal_content_hash": content_hash,
        },
    }
    with database.session() as session:
        _assert_no_materialization_rows(session)


def test_disabled_profile_rejects_before_materialization(
    client: TestClient,
    database,
    workspace_id: UUID,
    uow_factory,
) -> None:
    request_id, profile_id, content_hash, _proposal_id = seed_proposal(
        uow_factory, workspace_id
    )
    with database.session() as session:
        session.execute(
            update(PlanningProfileModel)
            .where(PlanningProfileModel.id == profile_id)
            .values(status=PlanningProfileStatus.DISABLED.value)
        )
        session.commit()

    response = client.post(
        "/api/v1/commands",
        json=command(
            workspace_id,
            request_id,
            profile_id,
            content_hash,
            idempotency_key="confirm-disabled-profile",
        ),
    )

    assert response.status_code == 422, response.text
    assert response.json()["error"]["code"] == "validation_error"
    with database.session() as session:
        _assert_no_materialization_rows(session)


def test_plan_validation_failure_rolls_back_partial_materialization(
    client: TestClient,
    database,
    workspace_id: UUID,
    uow_factory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    request_id, profile_id, content_hash, _proposal_id = seed_proposal(
        uow_factory, workspace_id
    )

    def fail_plan_validation(cls, **kwargs):
        del cls, kwargs
        raise PlanValidationError("cross_subtask_worker_dependency", "injected failure")

    monkeypatch.setattr(TaskPlanSnapshot, "create", classmethod(fail_plan_validation))

    response = client.post(
        "/api/v1/commands",
        json=command(
            workspace_id,
            request_id,
            profile_id,
            content_hash,
            idempotency_key="confirm-plan-validation-failure",
        ),
    )

    assert response.status_code == 422, response.text
    assert response.json()["error"]["code"] == "validation_error"
    with database.session() as session:
        _assert_no_materialization_rows(session)


def test_second_worker_item_failure_rolls_back_partial_materialization(
    client: TestClient,
    database,
    workspace_id: UUID,
    uow_factory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    request_id, profile_id, content_hash, _proposal_id = seed_proposal(
        uow_factory, workspace_id
    )
    original_materialize = WorkerItemMaterializer.materialize

    def fail_after_worker_items(materializer, uow, snapshot, *, now):
        original_materialize(materializer, uow, snapshot, now=now)
        raise PlanValidationError("worker_item_key_conflict", "injected second item failure")

    monkeypatch.setattr(WorkerItemMaterializer, "materialize", fail_after_worker_items)

    response = client.post(
        "/api/v1/commands",
        json=command(
            workspace_id,
            request_id,
            profile_id,
            content_hash,
            idempotency_key="confirm-worker-item-failure",
        ),
    )

    assert response.status_code == 422, response.text
    assert response.json()["error"]["code"] == "validation_error"
    with database.session() as session:
        _assert_no_materialization_rows(session)


def test_work_context_binding_conflict_rolls_back_partial_materialization(
    client: TestClient,
    database,
    workspace_id: UUID,
    uow_factory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    request_id, profile_id, content_hash, _proposal_id = seed_proposal(
        uow_factory, workspace_id
    )

    def fail_binding(
        self,
        context,
        root_task_id,
        revision,
        *,
        expected_version: int,
    ) -> None:
        del self, context, root_task_id, revision
        raise VersionConflict(expected_version, expected_version + 1)

    monkeypatch.setattr(WorkContextRepository, "bind_root_task", fail_binding)

    response = client.post(
        "/api/v1/commands",
        json=command(
            workspace_id,
            request_id,
            profile_id,
            content_hash,
            idempotency_key="confirm-context-conflict",
        ),
    )

    assert response.status_code == 409, response.text
    assert response.json()["error"]["code"] == "version_conflict"
    with database.session() as session:
        _assert_no_materialization_rows(session)
        context = session.scalar(
            select(WorkContextModel).where(
                WorkContextModel.planning_request_id == request_id
            )
        )
        assert context is not None
        assert context.root_task_id is None
        assert context.current_revision_number == 1
        assert context.version == 1
        assert (
            session.scalar(
                select(func.count()).select_from(WorkContextRevisionModel).where(
                    WorkContextRevisionModel.work_context_id == context.id
                )
            )
            == 1
        )


def test_concurrent_distinct_confirmations_create_one_materialization(
    database_url: str,
    database,
    workspace_id: UUID,
    uow_factory,
) -> None:
    request_id, profile_id, content_hash, _proposal_id = seed_proposal(
        uow_factory, workspace_id
    )
    payloads = [
        command(
            workspace_id,
            request_id,
            profile_id,
            content_hash,
            idempotency_key="confirm-concurrent-a",
        ),
        command(
            workspace_id,
            request_id,
            profile_id,
            content_hash,
            idempotency_key="confirm-concurrent-b",
        ),
    ]

    def post(payload: dict[str, object]):
        settings = Settings(database_url=database_url, local_actor_id="operator")
        with TestClient(create_app(settings)) as local_client:
            return local_client.post("/api/v1/commands", json=payload)

    with ThreadPoolExecutor(max_workers=2) as executor:
        responses = list(executor.map(post, payloads))

    assert sorted(response.status_code for response in responses) == [201, 409]
    with database.session() as session:
        assert session.scalar(select(func.count()).select_from(TaskModel)) == 2
        assert session.scalar(select(func.count()).select_from(ProposalMaterializationModel)) == 1


def _assert_no_materialization_rows(session) -> None:
    assert session.scalar(select(func.count()).select_from(TaskModel)) == 0
    assert session.scalar(select(func.count()).select_from(PlanSnapshotModel)) == 0
    assert session.scalar(select(func.count()).select_from(WorkerItemModel)) == 0
    assert session.scalar(select(func.count()).select_from(ExecutionRequestModel)) == 0
    assert session.scalar(select(func.count()).select_from(ProposalMaterializationModel)) == 0
