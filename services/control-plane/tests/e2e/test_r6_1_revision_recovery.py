from __future__ import annotations

import sys
from collections.abc import Mapping
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select

_INTEGRATION_TESTS = str(Path(__file__).resolve().parents[1] / "integration")
if _INTEGRATION_TESTS not in sys.path:
    sys.path.insert(0, _INTEGRATION_TESTS)

from tests.integration.test_gateway_runner import (
    FakeWorkspaceProvider,
    RecordingFakeAdapter,
    _message,
    _patch_materialization,
    _seed_assignment,
)
from tests.integration.test_plan_transition_commands import _retire_unknown
from tests.integration.test_plan_transition_failures import _gateway
from tests.integration.test_plan_transition_failures import (
    test_lost_stop_response_is_unknown_and_not_replayed as _lost_stop_response,
)
from tests.integration.test_plan_transition_failures import (
    test_new_session_survives_coordinator_crash_and_reconciles_on_restart as _new_session_survives,
)
from tests.integration.test_plan_transition_failures import (
    test_patch_delivered_before_db_failure_is_unknown_and_not_replayed as _patch_delivered,
)
from tests.integration.test_plan_transition_failures import (
    test_session_start_persistence_failure_is_unknown_and_not_replayed as _session_start_persistence,
)
from tests.integration.test_revision_confirmation import (
    _base_plan,
    _confirm_command,
    _preserve_payload,
    _revision_payload,
    _seed_revision_confirmation,
)

pytestmark = [
    pytest.mark.integration,
    pytest.mark.usefixtures("clean_database"),
]

NOW = datetime(2026, 8, 29, 12, 0, tzinfo=UTC)


from sidera_control_plane.api.app import create_app
from sidera_control_plane.application.worker_item_materializer import (
    WorkerItemMaterializer,
)
from sidera_control_plane.config import Settings
from sidera_control_plane.domain.context_patches import (
    ContextPatchDeliveryResult,
    ContextPatchMaterializationV1,
)
from sidera_control_plane.domain.gateway import (
    AdapterSessionHandle,
    DeliveryOutcome,
    GatewayOperationType,
)
from sidera_control_plane.domain.plan_revisions import (
    ContextDisposition,
    IdentityStrategy,
    RevisionBaseDescriptorV1,
    SubtaskIdentityDecision,
    WorkerItemIdentityDecision,
    plan_snapshot_content_hash,
)
from sidera_control_plane.domain.plan_transitions import (
    PlanTransitionStatus,
)
from sidera_control_plane.domain.planning import (
    PlanningRequest,
    PlanningRequestKind,
    PlanningUsage,
)
from sidera_control_plane.domain.planning_proposals import (
    PlanningProfileDescriptor,
    ProposalRevision,
)
from sidera_control_plane.domain.revision_diff import RevisionDiffNormalizer
from sidera_control_plane.persistence.database import Database
from sidera_control_plane.persistence.models import (
    EventModel,
    ExecutionModel,
    ExecutionSessionModel,
    GatewayOperationModel,
    PlanEntityLineageModel,
    PlanningRequestModel,
    PlanSnapshotModel,
    PlanTransitionModel,
    ProposalMaterializationModel,
    RootTaskPlanHeadModel,
    WorkerTransitionModel,
)
from sidera_control_plane.persistence.unit_of_work import SqlAlchemyUnitOfWork


class _UnknownPatchAdapter(RecordingFakeAdapter):
    def __init__(self) -> None:
        super().__init__()
        self.patch_count = 0

    def apply_context_patch(
        self,
        handle: AdapterSessionHandle,
        materialization: ContextPatchMaterializationV1,
        *,
        operation_id: UUID,
    ) -> ContextPatchDeliveryResult:
        del handle
        self.patch_count += 1
        return ContextPatchDeliveryResult(
            outcome="unknown",
            operation_id=operation_id,
            patch_hash=materialization.patch.patch_hash,
            accepted_sequence=None,
            error_code="patch_response_lost",
        )


@pytest.fixture
def client(database_url: str) -> TestClient:
    with TestClient(
        create_app(Settings(database_url=database_url, local_actor_id="operator"))
    ) as test_client:
        yield test_client


def _assert_patch_response_lost_is_not_replayed(
    database: Database, workspace_id: UUID
) -> None:
    ids = _seed_assignment(database, workspace_id)
    adapter = _UnknownPatchAdapter()
    gateway = _gateway(database, adapter, FakeWorkspaceProvider())
    started = gateway.handle(_message(workspace_id, ids["execution_id"], ids["lease_id"]))
    adapter.release_boundary(adapter.active_sessions()[0])
    materialization = _patch_materialization(workspace_id, uuid4())
    kwargs = {
        "workspace_id": workspace_id,
        "execution_id": ids["execution_id"],
        "session_id": started.session_id,
        "lease_id": ids["lease_id"],
        "fencing_token": 1,
        "worker_transition_id": materialization.patch.worker_transition_id,
        "action_step": "apply_context_patch",
        "logical_attempt": 1,
        "materialization": materialization,
    }

    first = gateway.apply_context_patch(**kwargs)
    second = gateway.apply_context_patch(**kwargs)

    assert first.outcome == DeliveryOutcome.UNKNOWN
    assert second.outcome == DeliveryOutcome.UNKNOWN
    assert adapter.patch_count == 1
    with database.session() as session:
        operation = session.scalar(
            select(GatewayOperationModel).where(
                GatewayOperationModel.operation_type
                == GatewayOperationType.APPLY_CONTEXT_PATCH.value
            )
        )
        assert operation is not None
        assert operation.status == "unknown"
        assert operation.attempt_count == 1
        assert operation.failure_code == "patch_response_lost"


@pytest.mark.parametrize(
    "fault",
    (
        "patch_response_lost",
        "stop_response_lost",
        "adapter_delivered_db_failure",
        "session_start_db_failure",
        "continuation_session_started_coordinator_crash",
    ),
)
def test_r6_1_unknown_recovery_fault_matrix(
    fault: str,
    database: Database,
    database_url: str,
    workspace_id: UUID,
    uow_factory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    if fault == "patch_response_lost":
        _assert_patch_response_lost_is_not_replayed(database, workspace_id)
    elif fault == "stop_response_lost":
        _lost_stop_response(database, workspace_id)
    elif fault == "adapter_delivered_db_failure":
        _patch_delivered(database, workspace_id, monkeypatch)
    elif fault == "session_start_db_failure":
        _session_start_persistence(database, workspace_id, monkeypatch)
    else:
        _new_session_survives(
            database, database_url, workspace_id, uow_factory, monkeypatch
        )
        with database.session() as session:
            assert (
                session.scalar(select(func.count()).select_from(ExecutionSessionModel))
                == 2
            )
            assert set(session.scalars(select(ExecutionModel.status)).all()) == {
                "running",
                "succeeded",
            }
            assert session.scalar(select(WorkerTransitionModel.status)) == "converged"


def _generic_force_action_present(value: object) -> bool:
    generic_actions = {
        "force",
        "force_apply",
        "force_confirm",
        "force_converge",
        "force_retry",
    }
    if isinstance(value, Mapping):
        return any(
            str(key).lower() in generic_actions
            or _generic_force_action_present(child)
            for key, child in value.items()
        )
    if isinstance(value, (list, tuple)):
        return any(_generic_force_action_present(child) for child in value)
    return isinstance(value, str) and value.lower() in generic_actions


def _same_shape_revision_payload(snapshot):
    template = _revision_payload(snapshot)
    subtask = snapshot.subtasks[0]
    worker = snapshot.worker_item_plans[0]
    assert subtask.task_id is not None
    assert worker.worker_item_id is not None
    return replace(
        template,
        root_task=replace(
            template.root_task,
            title=snapshot.title,
            goal_summary=snapshot.goal_summary,
            acceptance_refs=snapshot.acceptance_refs,
        ),
        subtasks=(
            replace(
                template.subtasks[0],
                key=subtask.key,
                kind=subtask.kind,
                title=subtask.title,
                goal_summary=subtask.goal_summary,
                acceptance_refs=subtask.acceptance_refs,
                expected_outputs=subtask.expected_outputs,
                milestone_key=subtask.milestone_key,
                source_refs=subtask.source_refs,
            ),
        ),
        worker_items=(
            replace(
                template.worker_items[0],
                key=worker.key,
                subtask_key=worker.subtask_key,
                title=worker.title,
                role=worker.role,
                responsibility=worker.responsibility,
                capability_requirements=worker.capability_requirements,
                worker_policy_ref=worker.worker_policy_ref,
                configuration_ref=worker.configuration_ref,
                concurrency_limit=worker.concurrency_limit,
                source_refs=worker.source_refs,
            ),
        ),
        subtask_milestones=tuple(snapshot.subtask_milestones),
        subtask_dependencies=tuple(snapshot.subtask_dependencies),
        worker_item_dependencies=tuple(snapshot.worker_item_dependencies),
        execution_intents=(
            replace(
                template.execution_intents[0],
                worker_item_key=worker.key,
                purpose=worker.responsibility,
                capability_requirements=worker.capability_requirements,
                required_evidence_refs=(
                    subtask.acceptance_refs[0]
                    if subtask.acceptance_refs
                    else "acceptance:revision",
                ),
            ),
        ),
        subtask_identity_decisions=(
            SubtaskIdentityDecision(
                target_subtask_key=subtask.key,
                strategy=IdentityStrategy.REUSE,
                existing_subtask_task_id=subtask.task_id,
                derived_from_subtask_task_ids=(),
                reason="keep the current subtask identity while recovery is reviewed",
            ),
        ),
        worker_item_identity_decisions=(
            WorkerItemIdentityDecision(
                target_worker_item_key=worker.key,
                strategy=IdentityStrategy.REUSE,
                existing_worker_item_id=worker.worker_item_id,
                derived_from_worker_item_ids=(),
                target_subtask_key=worker.subtask_key,
                context_disposition=ContextDisposition.UNCHANGED,
                change_reasons=(),
            ),
        ),
        retired_subtask_ids=(),
        retired_worker_item_ids=(),
    )


def _seed_ready_revision(
    database: Database,
    workspace_id: UUID,
    base_snapshot,
    profile_snapshot,
    *,
    payload=None,
) -> dict[str, Any]:
    with SqlAlchemyUnitOfWork(database) as uow:
        root_task = uow.tasks.get(base_snapshot.root_task_id, workspace_id)
        assert root_task is not None
        root_task_version = root_task.version
        profile = uow.planning_profiles.get(
            profile_snapshot.profile_id,
            workspace_id,
            version=profile_snapshot.profile_version,
        )
        assert profile is not None

    payload = payload or _revision_payload(base_snapshot)
    payload = replace(
        payload,
        base_descriptor=RevisionBaseDescriptorV1(
            schema_version=1,
            workspace_id=workspace_id,
            root_task_id=base_snapshot.root_task_id,
            plan_snapshot_id=base_snapshot.id,
            plan_generation=base_snapshot.generation,
            plan_content_hash=plan_snapshot_content_hash(base_snapshot),
            root_task_version=root_task_version,
            captured_at=NOW,
        ),
    )
    request = PlanningRequest.create_revision(
        request_id=uuid4(),
        workspace_id=workspace_id,
        profile_snapshot=profile_snapshot,
        objective="Revise the active control-room plan",
        constraints=("Keep completed work and stable identities auditable.",),
        acceptance_criteria=("The next revision remains reviewable.",),
        context_refs=(),
        requested_outputs=("proposal",),
        source_type="r6.1-e2e",
        source_ref="revision-recovery",
        actor="operator",
        correlation_id=uuid4(),
        run_id=uuid4(),
        now=NOW,
        base=payload.base_descriptor,
    )
    run = request.runs[0]
    request.start_planning(
        run_id=run.id,
        lease_id=uuid4(),
        fencing_token=1,
        now=NOW,
    )
    assert run.lease_id is not None
    request.succeed_run(
        run_id=run.id,
        lease_id=run.lease_id,
        fencing_token=run.fencing_token,
        provider_ref="fake:r6.1-revision-recovery",
        structured_result_type="planning.proposal.v3",
        usage=PlanningUsage.zero(),
        now=NOW,
    )
    proposal = ProposalRevision.create(
        planning_request_id=request.id,
        revision=1,
        brief_revision=1,
        planning_run_id=run.id,
        kind=PlanningRequestKind.REVISION,
        payload=payload,
        profile_descriptor=PlanningProfileDescriptor.from_snapshot(profile_snapshot),
        profile_snapshot=profile_snapshot,
        created_at=NOW,
        derived_diff=RevisionDiffNormalizer().normalize(base_snapshot, payload),
    )
    with SqlAlchemyUnitOfWork(database) as uow:
        uow.planning_requests.add(request)
        uow.session.flush()
        uow.planning_records.add_proposal(proposal, workspace_id)
        uow.session.flush()
        previous_version = request.version
        request.mark_proposal_ready(
            run_id=run.id,
            proposal_revision=proposal.revision,
            content_hash=proposal.content_hash,
            now=NOW,
        )
        uow.planning_requests.save(request, previous_version)
    return {
        "request": request,
        "proposal": proposal,
        "profile": profile,
        "snapshot": base_snapshot,
        "payload": payload,
        "root_task": root_task,
    }


def _converge_transition(
    database: Database, workspace_id: UUID, transition_id: UUID
) -> None:
    with SqlAlchemyUnitOfWork(database) as uow:
        transition = uow.plan_transitions.get(
            transition_id, workspace_id, for_update=True
        )
        workers = list(
            uow.worker_transitions.list_for_plan_transition(
                transition_id, workspace_id
            )
        )
        assert transition is not None
        assert workers
        for worker in workers:
            previous_version = worker.version
            worker.converge(now=NOW)
            uow.worker_transitions.save(worker, previous_version)
        previous_version = transition.version
        transition.reconcile(workers, now=NOW)
        uow.plan_transitions.save(transition, previous_version)


def _materialize_first_revision(
    client: TestClient,
    database: Database,
    workspace_id: UUID,
) -> tuple[dict[str, Any], Any, UUID]:
    bootstrap_snapshot = _base_plan(
        workspace_id,
        snapshot_id=uuid4(),
        parent_snapshot_id=None,
    )
    bootstrap_seed = _seed_revision_confirmation(
        database,
        workspace_id,
        snapshot=bootstrap_snapshot,
        payload=_revision_payload(bootstrap_snapshot),
    )
    response = client.post(
        "/api/v1/commands",
        json=_confirm_command(
            bootstrap_seed, idempotency_key="r6-1-recovery-bootstrap"
        ),
    )
    assert response.status_code == 201, response.text
    bootstrap_data = response.json()["data"]
    bootstrap_transition_id = UUID(bootstrap_data["plan_transition_id"])
    _converge_transition(database, workspace_id, bootstrap_transition_id)
    with SqlAlchemyUnitOfWork(database) as uow:
        generation_two = uow.plans.get(
            UUID(bootstrap_data["plan_snapshot_id"]), workspace_id
        )
        assert generation_two is not None
        head = uow.plans.get_head(generation_two.root_task_id, workspace_id)
        assert head is not None
        generation_three = replace(
            generation_two,
            id=uuid4(),
            generation=3,
            parent_snapshot_id=generation_two.id,
            source_ref="integration:g3-running-head",
        )
        uow.plans.add_snapshot_and_advance_head(
            generation_three,
            expected_head=head,
        )
        WorkerItemMaterializer().materialize(
            uow,
            generation_three,
            now=NOW,
        )

    seed = _seed_ready_revision(
        database,
        workspace_id,
        generation_three,
        bootstrap_seed["request"].profile_snapshot,
    )
    response = client.post(
        "/api/v1/commands",
        json=_confirm_command(seed, idempotency_key="r6-1-recovery-first-revision"),
    )
    assert response.status_code == 201, response.text
    data = response.json()["data"]
    transition_id = UUID(data["plan_transition_id"])
    with SqlAlchemyUnitOfWork(database) as uow:
        current = uow.plans.get(UUID(data["plan_snapshot_id"]), workspace_id)
        assert current is not None
    return seed, current, transition_id


def test_r6_1_unknown_revision_gate_is_recovery_only(
    client: TestClient,
    database: Database,
    database_url: str,
    workspace_id: UUID,
    uow_factory,
) -> None:
    blocked = _retire_unknown(database, database_url, workspace_id, uow_factory)
    with SqlAlchemyUnitOfWork(database) as uow:
        head = uow.plans.get_head(blocked["seed"]["root_task"].id, workspace_id)
        assert head is not None
        current_snapshot = uow.plans.get(head.active_snapshot_id, workspace_id)
        assert current_snapshot is not None
    next_seed = _seed_ready_revision(
        database,
        workspace_id,
        current_snapshot,
        blocked["seed"]["request"].profile_snapshot,
        payload=_same_shape_revision_payload(current_snapshot),
    )

    response = client.post(
        "/api/v1/commands",
        json=_confirm_command(next_seed, idempotency_key="r6-1-unknown-confirm"),
    )
    assert response.status_code == 409, response.text
    assert response.json()["error"]["code"] == "revision_confirmation_blocked"

    detail = client.get(
        f"/api/v1/workspaces/{workspace_id}/plan-transitions/"
        f"{blocked['plan_transition_id']}"
    )
    worker_detail = client.get(
        f"/api/v1/workspaces/{workspace_id}/worker-transitions/"
        f"{blocked['transition_id']}"
    )
    assert detail.status_code == 200, detail.text
    assert worker_detail.status_code == 200, worker_detail.text
    body = detail.json()
    worker = worker_detail.json()
    assert body["status"] == "degraded"
    assert body["workers"]
    workers_by_transition_id = {
        item["worker_transition_id"]: item for item in body["workers"]
    }
    blocked_worker = workers_by_transition_id[str(blocked["transition_id"])]
    assert blocked_worker["status"] == "blocked_unknown"
    assert blocked_worker["writer_state"] == "unknown"
    assert blocked_worker["operations"][0]["status"] == "unknown"
    assert worker["queued_input_disposition"]["policy"] == "explicit_resend_only"
    assert not _generic_force_action_present(body)
    assert not _generic_force_action_present(worker)


def test_r6_1_continuous_revision_blocks_in_flight_then_accepts_same_head(
    client: TestClient,
    database: Database,
    workspace_id: UUID,
) -> None:
    first_seed, g4, first_transition_id = _materialize_first_revision(
        client, database, workspace_id
    )
    next_seed = _seed_ready_revision(
        database,
        workspace_id,
        g4,
        first_seed["request"].profile_snapshot,
    )

    blocked = client.post(
        "/api/v1/commands",
        json=_confirm_command(next_seed, idempotency_key="r6-1-in-flight-confirm"),
    )
    assert blocked.status_code == 409, blocked.text
    assert blocked.json()["error"]["code"] == "revision_confirmation_blocked"

    with database.session() as session:
        head = session.get(RootTaskPlanHeadModel, (workspace_id, g4.root_task_id))
        request = session.get(PlanningRequestModel, next_seed["request"].id)
        assert head is not None and request is not None
        assert head.active_generation == 4
        assert request.status == "proposal_ready"

    _converge_transition(database, workspace_id, first_transition_id)
    confirmed = client.post(
        "/api/v1/commands",
        json=_confirm_command(next_seed, idempotency_key="r6-1-after-recovery-confirm"),
    )
    assert confirmed.status_code == 201, confirmed.text
    assert confirmed.json()["data"]["plan_generation"] == 5

    stale_seed = _seed_ready_revision(
        database,
        workspace_id,
        g4,
        first_seed["request"].profile_snapshot,
    )
    stale = client.post(
        "/api/v1/commands",
        json=_confirm_command(stale_seed, idempotency_key="r6-1-stale-base-confirm"),
    )
    assert stale.status_code == 409, stale.text
    assert stale.json()["data"]["status"] == "conflicted"
    assert stale.json()["data"]["reason"] == "stale_base"
    with database.session() as session:
        request = session.get(PlanningRequestModel, stale_seed["request"].id)
        assert request is not None
        assert request.status == "conflicted"
        assert request.conflict_reason == "stale_base"


def test_r6_1_revert_creates_forward_generation_and_preserves_g4_history(
    client: TestClient,
    database: Database,
    workspace_id: UUID,
) -> None:
    first_seed, g4, first_transition_id = _materialize_first_revision(
        client, database, workspace_id
    )
    _converge_transition(database, workspace_id, first_transition_id)
    g3 = first_seed["snapshot"]
    revert_payload = replace(
        _preserve_payload(g3),
        base_descriptor=RevisionBaseDescriptorV1(
            schema_version=1,
            workspace_id=workspace_id,
            root_task_id=g4.root_task_id,
            plan_snapshot_id=g4.id,
            plan_generation=g4.generation,
            plan_content_hash=plan_snapshot_content_hash(g4),
            root_task_version=first_seed["root_task"].version,
            captured_at=NOW,
        ),
        impact_policy=replace(
            _preserve_payload(g3).impact_policy,
            revision_operation="revert",
        ),
    )
    revert_seed = _seed_ready_revision(
        database,
        workspace_id,
        g4,
        first_seed["request"].profile_snapshot,
        payload=revert_payload,
    )
    assert revert_seed["payload"].impact_policy.revision_operation == "revert"
    assert {
        item.target_worker_item_key
        for item in revert_seed["payload"].worker_item_identity_decisions
    } == {item.key for item in g3.worker_item_plans}

    confirmed = client.post(
        "/api/v1/commands",
        json=_confirm_command(revert_seed, idempotency_key="r6-1-revert-confirm"),
    )
    assert confirmed.status_code == 201, confirmed.text
    data = confirmed.json()["data"]
    g5_id = UUID(data["plan_snapshot_id"])
    g5_transition_id = UUID(data["plan_transition_id"])
    assert data["plan_generation"] == 5

    with database.session() as session:
        head = session.get(RootTaskPlanHeadModel, (workspace_id, g3.root_task_id))
        g3_row = session.get(PlanSnapshotModel, g3.id)
        g4_row = session.get(PlanSnapshotModel, g4.id)
        g5_row = session.get(PlanSnapshotModel, g5_id)
        transitions = session.scalars(
            select(PlanTransitionModel)
            .where(PlanTransitionModel.workspace_id == workspace_id)
            .order_by(PlanTransitionModel.to_generation)
        ).all()
        materializations = session.scalars(
            select(ProposalMaterializationModel)
            .where(ProposalMaterializationModel.workspace_id == workspace_id)
            .order_by(ProposalMaterializationModel.plan_generation)
        ).all()
        lineages = session.scalars(
            select(PlanEntityLineageModel).where(
                PlanEntityLineageModel.workspace_id == workspace_id
            )
        ).all()
        events = session.scalars(
            select(EventModel).where(EventModel.workspace_id == workspace_id)
        ).all()
        assert head is not None
        assert g3_row is not None and g4_row is not None and g5_row is not None
        assert head.active_generation == 5
        assert head.active_snapshot_id == g5_id
        assert head.active_snapshot_id != g3.id
        assert g5_row.parent_snapshot_id == g4.id
        assert [row.generation for row in (g3_row, g4_row, g5_row)] == [3, 4, 5]
        assert {
            item["key"] for item in g5_row.payload["worker_items"]
        } == {item["key"] for item in g3_row.payload["worker_items"]}
        assert [row.to_generation for row in transitions] == [2, 4, 5]
        g4_transition = transitions[1]
        assert g4_transition.id == first_transition_id
        assert g4_transition.status == PlanTransitionStatus.CONVERGED.value
        assert transitions[2].id == g5_transition_id
        assert transitions[2].from_snapshot_id == g4.id
        assert transitions[2].to_snapshot_id == g5_id
        assert [row.plan_generation for row in materializations] == [2, 4, 5]
        assert any(item.plan_transition_id == first_transition_id for item in lineages)
        assert any(item.plan_transition_id == g5_transition_id for item in lineages)
        assert any(
            item.event_type == "task.plan_snapshot_published"
            and item.payload.get("generation") == 4
            for item in events
        )
        assert any(
            item.event_type == "task.plan_snapshot_published"
            and item.payload.get("generation") == 5
            for item in events
        )

    for transition_id in (first_transition_id, g5_transition_id):
        response = client.get(
            f"/api/v1/workspaces/{workspace_id}/plan-transitions/{transition_id}"
        )
        assert response.status_code == 200, response.text
    assert client.get(
        f"/api/v1/workspaces/{workspace_id}/plan-transitions/{g5_transition_id}"
    ).json()["from_generation"] == 4
