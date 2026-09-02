from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from sidera_control_plane.adapters.fake_planner import (
    FakePlannerAdapter,
    FakePlannerConfig,
)
from sidera_control_plane.api.app import create_app
from sidera_control_plane.application.activation_reconciler import ActivationReconciler
from sidera_control_plane.application.context_materialization import ContextMaterializer
from sidera_control_plane.application.plan_transition_coordinator import (
    PlanTransitionCoordinator,
)
from sidera_control_plane.application.planning_runner import PlanningRunner
from sidera_control_plane.application.ports import (
    ExactRevisionManifest,
    ExactSectionBody,
    KnowledgeCatalogPort,
    SearchHit,
)
from sidera_control_plane.config import Settings
from sidera_control_plane.context_package_worker import ContextPackageWorker
from sidera_control_plane.domain.context_patches import (
    ContextPatchDeliveryResult,
    ContextPatchMaterializationV1,
)
from sidera_control_plane.domain.gateway import context_patch_operation_id
from sidera_control_plane.domain.plan_revisions import (
    WorkerTransitionAction,
    plan_snapshot_content_hash,
)
from sidera_control_plane.domain.plan_transitions import (
    PlanTransitionStatus,
    WorkerTransitionStatus,
)
from sidera_control_plane.domain.planning_revision_proposals import (
    PlanningRevisionProposalPayloadV3,
)
from sidera_control_plane.persistence.database import Database
from sidera_control_plane.persistence.models import (
    ContextPackageAcknowledgementModel,
    ExecutionInputModel,
    ExecutionModel,
    ExecutionSessionModel,
    PlanSnapshotModel,
    RootTaskPlanHeadModel,
    WorkerItemModel,
)
from tests.e2e.r6_1_fake_runtime import activate_fake_target
from tests.e2e.seed_r6_1_revision import seed_revision_fixture

pytestmark = [
    pytest.mark.integration,
    pytest.mark.usefixtures("clean_database"),
]

NOW = datetime(2026, 8, 29, 12, 0, tzinfo=UTC)


@pytest.fixture
def client(database_url: str) -> Iterator[TestClient]:
    settings = Settings(database_url=database_url, local_actor_id="operator")
    with TestClient(create_app(settings)) as test_client:
        yield test_client


class _StaticPlannerFactory:
    def __init__(self, adapter: FakePlannerAdapter) -> None:
        self._adapter = adapter

    def create(
        self, definition: Any, instance: Any, profile_snapshot: Any
    ) -> FakePlannerAdapter:
        del definition, instance, profile_snapshot
        return self._adapter


class _EmptyCatalog(KnowledgeCatalogPort):
    def search(
        self,
        workspace_id: UUID,
        query: str,
        *,
        tags=(),
        source_types=(),
        status="active",
        limit=20,
        correlation_id: UUID,
        caller_request_id: UUID,
    ) -> tuple[SearchHit, ...]:
        del workspace_id, query, tags, source_types, status, limit
        del correlation_id, caller_request_id
        return ()

    def get_revision_manifest(
        self,
        workspace_id: UUID,
        page_id: UUID,
        revision_id: UUID,
        *,
        correlation_id: UUID,
        caller_request_id: UUID,
    ) -> ExactRevisionManifest:
        del workspace_id, page_id, revision_id, correlation_id, caller_request_id
        raise AssertionError("test context has no exact Knowledge refs")

    def materialize(
        self,
        workspace_id: UUID,
        sections,
        *,
        correlation_id: UUID,
        caller_request_id: UUID,
    ) -> tuple[ExactSectionBody, ...]:
        del workspace_id, sections, correlation_id, caller_request_id
        raise AssertionError("test context has no exact Knowledge refs")


class _RecordingPatchGateway:
    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []

    def apply_context_patch(
        self,
        *,
        workspace_id: UUID,
        execution_id: UUID,
        session_id: UUID,
        lease_id: UUID,
        fencing_token: int,
        worker_transition_id: UUID,
        action_step: str,
        logical_attempt: int,
        materialization: ContextPatchMaterializationV1,
    ) -> ContextPatchDeliveryResult:
        del workspace_id, lease_id, fencing_token
        patch = materialization.patch
        operation_id = context_patch_operation_id(
            worker_transition_id,
            action_step,
            logical_attempt=logical_attempt,
        )
        self.calls.append(
            {
                "execution_id": execution_id,
                "session_id": session_id,
                "worker_transition_id": worker_transition_id,
                "operation_id": operation_id,
                "patch_id": patch.id,
                "patch_hash": patch.patch_hash,
            }
        )
        return ContextPatchDeliveryResult(
            outcome="delivered",
            operation_id=operation_id,
            patch_hash=patch.patch_hash,
            accepted_sequence=1,
            error_code=None,
        )


def test_fake_revision_fixture_contract(database, workspace_id: UUID) -> None:
    fixture = seed_revision_fixture(database, workspace_id, now=NOW)

    with database.session() as session:
        head = session.get(
            RootTaskPlanHeadModel,
            (workspace_id, fixture.root_task_id),
        )
        assert head is not None
        assert head.active_generation == 3
        assert head.active_snapshot_id == fixture.base_snapshot_id

        snapshot = session.get(PlanSnapshotModel, fixture.base_snapshot_id)
        assert snapshot is not None
        assert {item["key"] for item in snapshot.payload["worker_items"]} == {
            "worker-a",
            "worker-b",
            "worker-c",
            "worker-d",
        }
        dependencies = {
            (item["upstream_subtask_key"], item["downstream_subtask_key"])
            for item in snapshot.payload["subtask_dependencies"]
        }
        assert {
            ("subtask-a", "subtask-c"),
            ("subtask-b", "subtask-c"),
        } <= dependencies

        workers = session.scalars(
            select(WorkerItemModel).where(
                WorkerItemModel.workspace_id == workspace_id,
                WorkerItemModel.id.in_(tuple(fixture.worker_item_ids.values())),
            )
        ).all()
        assert {item.stable_key for item in workers} == {
            "worker-a",
            "worker-b",
            "worker-c",
            "worker-d",
        }
        assert {item.stable_key for item in workers if item.status == "active"} >= {
            "worker-a",
            "worker-b",
        }

        execution = session.get(ExecutionModel, fixture.worker_b_execution_id)
        assert execution is not None
        assert execution.status == "running"
        active_session = session.get(ExecutionSessionModel, fixture.worker_b_session_id)
        assert active_session is not None
        assert active_session.status == "active"

        acknowledgement = session.get(
            ContextPackageAcknowledgementModel,
            (
                fixture.worker_b_package_id,
                "worker",
                fixture.worker_b_execution_id,
            ),
        )
        assert acknowledgement is not None
        assert (
            acknowledgement.acknowledged_package_hash == fixture.worker_b_package_hash
        )
        assert acknowledgement.context_version == 1

        queued_input = session.get(ExecutionInputModel, fixture.worker_b_input_id)
        assert queued_input is not None
        assert queued_input.session_id == fixture.worker_b_session_id
        assert queued_input.status == "queued"

    assert fixture.revision_actions == {
        "worker-a": "preserve",
        "worker-b": "patch",
        "worker-e": "create",
        "worker-d": "retire",
    }


def test_fake_revision_control_room_runs_to_convergence(
    client: TestClient,
    database: Database,
    workspace_id: UUID,
    uow_factory,
) -> None:
    now = datetime.now(UTC)
    fixture = seed_revision_fixture(database, workspace_id, now=now)

    with uow_factory() as uow:
        root_task = uow.tasks.get(fixture.root_task_id, workspace_id)
        base_snapshot = uow.plans.get(fixture.base_snapshot_id, workspace_id)
        assert root_task is not None
        assert base_snapshot is not None

    submit = client.post(
        "/api/v1/commands",
        json={
            "command_id": str(uuid4()),
            "workspace_id": str(workspace_id),
            "command_type": "SubmitPlanRevision",
            "target_type": "task",
            "actor": {"type": "local", "id": "operator"},
            "target_id": str(fixture.root_task_id),
            "expected_version": root_task.version,
            "idempotency_key": "r6-1-fake-control-room-submit",
            "correlation_id": str(uuid4()),
            "payload": {
                "root_task_id": str(fixture.root_task_id),
                "expected_base_snapshot_id": str(base_snapshot.id),
                "expected_base_generation": base_snapshot.generation,
                "expected_base_content_hash": plan_snapshot_content_hash(base_snapshot),
                "objective": "Revise the running control room plan",
                "constraints": ["Keep stable WorkerItem identities reusable"],
                "acceptance_criteria": ["The revised plan is reviewable"],
                "planning_profile_id": str(fixture.planning_profile_id),
                "planning_profile_version": 1,
            },
        },
    )
    assert submit.status_code == 201, submit.text
    planning_request_id = UUID(submit.json()["data"]["planning_request_id"])

    context_worker = ContextPackageWorker(
        uow_factory=uow_factory,
        catalog=_EmptyCatalog(),
        owner_id="r6-1-fake-context-worker",
        clock=lambda: datetime.now(UTC),
    )
    context_result = context_worker.run_once(10)
    assert context_result.claimed == 1
    assert context_result.ready == 1
    assert context_result.failed == 0

    with TemporaryDirectory(prefix="sidera-r6-1-fake-planning-") as temp_root:
        planning_result = PlanningRunner(
            uow_factory=uow_factory,
            adapter_factory=_StaticPlannerFactory(
                FakePlannerAdapter(FakePlannerConfig(scenario="revision_control_room"))
            ),
            owner_id="r6-1-fake-planning-worker",
            lease_ttl_seconds=60,
            lease_renew_interval_seconds=1,
            clock=lambda: datetime.now(UTC),
            context_materializer=ContextMaterializer(_EmptyCatalog()),
            planner_temp_root=Path(temp_root),
        ).run_once(1)
    assert planning_result.claimed == 1
    assert planning_result.failed == 0

    with uow_factory() as uow:
        request = uow.planning_requests.get(planning_request_id, workspace_id)
        assert request is not None
        latest_run = request.runs[-1]
        assert planning_result.succeeded == 1, (
            f"outcome={planning_result.outcomes}; "
            f"run_error={latest_run.error_code}:{latest_run.error_message}"
        )
        proposals = uow.planning_records.list_proposals(
            planning_request_id,
            workspace_id,
        )
        assert len(proposals) == 1
        proposal = proposals[0]
        assert isinstance(proposal.payload, PlanningRevisionProposalPayloadV3)
        assert proposal.payload.base_descriptor.plan_snapshot_id == base_snapshot.id
        assert proposal.payload.base_descriptor.plan_generation == 3
        assert {
            (item.upstream_subtask_key, item.downstream_subtask_key)
            for item in proposal.payload.subtask_dependencies
        } == {
            ("subtask-a", "subtask-c"),
            ("subtask-b", "subtask-c"),
        }
        worker_decisions = {
            item.target_worker_item_key: item
            for item in proposal.payload.worker_item_identity_decisions
        }
        assert worker_decisions["worker-a"].context_disposition.value == "unchanged"
        assert (
            worker_decisions["worker-b"].context_disposition.value == "patch_required"
        )
        assert worker_decisions["worker-e"].existing_worker_item_id is None
        assert proposal.derived_diff.impact_counts.patch == 1
        assert proposal.derived_diff.impact_counts.create == 1
        assert proposal.derived_diff.impact_counts.retire == 1

        confirm = client.post(
            "/api/v1/commands",
            json={
                "command_id": str(uuid4()),
                "workspace_id": str(workspace_id),
                "command_type": "ConfirmPlanRevision",
                "target_type": "planning_request",
                "actor": {"type": "local", "id": "operator"},
                "target_id": str(request.id),
                "expected_version": request.version,
                "idempotency_key": "r6-1-fake-control-room-confirm",
                "correlation_id": str(uuid4()),
                "payload": {
                    "expected_proposal_revision": proposal.revision,
                    "expected_proposal_content_hash": proposal.content_hash,
                    "expected_diff_hash": proposal.derived_diff.diff_hash,
                    "expected_base_snapshot_id": str(base_snapshot.id),
                    "expected_base_content_hash": plan_snapshot_content_hash(
                        base_snapshot
                    ),
                    "profile_id": str(fixture.planning_profile_id),
                    "profile_version": 1,
                },
            },
        )
    assert confirm.status_code == 201, confirm.text
    confirmation_data = confirm.json()["data"]
    plan_transition_id = UUID(confirmation_data["plan_transition_id"])
    assert confirmation_data["plan_generation"] == 4

    with uow_factory() as uow:
        plan_transition = uow.plan_transitions.get(
            plan_transition_id,
            workspace_id,
        )
        transitions = uow.worker_transitions.list_for_plan_transition(
            plan_transition_id,
            workspace_id,
        )
        assert plan_transition is not None
        assert plan_transition.status is PlanTransitionStatus.CONVERGING
        by_worker = {item.worker_item_id: item for item in transitions}
        assert (
            by_worker[fixture.worker_item_ids["worker-a"]].status
            is WorkerTransitionStatus.CONVERGED
        )
        assert (
            by_worker[fixture.worker_item_ids["worker-c"]].status
            is WorkerTransitionStatus.CONVERGED
        )
        assert (
            by_worker[fixture.worker_item_ids["worker-b"]].action
            is WorkerTransitionAction.PATCH
        )
        assert (
            by_worker[fixture.worker_item_ids["worker-d"]].action
            is WorkerTransitionAction.RETIRE
        )
        assert (
            sum(item.action is WorkerTransitionAction.CREATE for item in transitions)
            == 1
        )
        create_transition = next(
            item for item in transitions if item.action is WorkerTransitionAction.CREATE
        )
        assert create_transition.status is WorkerTransitionStatus.PENDING

    gateway = _RecordingPatchGateway()
    coordinator = PlanTransitionCoordinator(
        uow_factory=uow_factory,
        owner_id="r6-1-fake-transition-coordinator",
        clock=lambda: datetime.now(UTC),
        gateway=gateway,
    )
    first_transition_result = coordinator.run_once(20)
    assert first_transition_result.failed == 0
    assert len(gateway.calls) == 1

    with uow_factory() as uow:
        transitions = uow.worker_transitions.list_for_plan_transition(
            plan_transition_id,
            workspace_id,
        )
        by_worker = {item.worker_item_id: item for item in transitions}
        patch_transition = by_worker[fixture.worker_item_ids["worker-b"]]
        retire_transition = by_worker[fixture.worker_item_ids["worker-d"]]
        create_transition = next(
            item for item in transitions if item.action is WorkerTransitionAction.CREATE
        )
        assert patch_transition.status is WorkerTransitionStatus.CONVERGED
        assert retire_transition.status is WorkerTransitionStatus.CONVERGED
        assert create_transition.status is WorkerTransitionStatus.WAITING_ACTIVATION
        queued_input = uow.execution_inputs.get(
            fixture.worker_b_input_id,
            workspace_id,
        )
        assert queued_input is not None
        assert queued_input.status.value == "queued"
        patches = uow.context_patches.list_for_worker_item(
            fixture.worker_item_ids["worker-b"],
            workspace_id,
        )
        assert len(patches) == 1
        patch_ack = uow.context_patches.get_acknowledgement(
            patches[0].id,
            workspace_id,
        )
        assert patch_ack is not None
        assert patch_ack.patch_hash == patches[0].patch_hash
        assert patch_ack.accepted_sequence == 1
        assert gateway.calls[0]["patch_hash"] == patches[0].patch_hash

        target_snapshot = uow.plans.get(
            plan_transition.to_snapshot_id,
            workspace_id,
        )
        assert target_snapshot is not None
        target_worker = next(
            item for item in target_snapshot.worker_item_plans if item.key == "worker-e"
        )
        assert target_worker.worker_item_id is not None
        target_worker_item_id = target_worker.worker_item_id

    activation_result = ActivationReconciler(
        uow_factory=uow_factory,
        owner_id="r6-1-fake-activation-reconciler",
        clock=lambda: datetime.now(UTC),
    ).run_once(10)
    assert activation_result.failed == 0
    with uow_factory() as uow:
        debug_request = uow.execution_requests.get_by_proposal_worker_item(
            UUID(confirmation_data["proposal_revision_id"]),
            "worker-e",
            workspace_id,
        )
        assert debug_request is not None
        assert activation_result.materialized == 1, (
            f"activation={activation_result}; "
            f"status={debug_request.status}; "
            f"gate={debug_request.gate_reason}:{dict(debug_request.gate_details)}"
        )

    target_execution_id: UUID
    with uow_factory() as uow:
        target_request = uow.execution_requests.get_by_proposal_worker_item(
            UUID(confirmation_data["proposal_revision_id"]),
            "worker-e",
            workspace_id,
        )
        assert target_request is not None
        assert target_request.first_execution_id is not None
        target_execution_id = target_request.first_execution_id

    package_result = ContextPackageWorker(
        uow_factory=uow_factory,
        catalog=_EmptyCatalog(),
        owner_id="r6-1-fake-target-context-worker",
        clock=lambda: datetime.now(UTC),
    ).run_once(10)
    assert package_result.claimed == 1
    assert package_result.ready == 1
    assert package_result.failed == 0

    target_session_id = activate_fake_target(
        uow_factory,
        target_execution_id,
        target_worker_item_id,
        workspace_id,
        now=datetime.now(UTC),
    )
    assert target_session_id

    second_transition_result = coordinator.run_once(20)
    assert second_transition_result.failed == 0
    assert len(gateway.calls) == 1

    with uow_factory() as uow:
        final_plan = uow.plan_transitions.get(plan_transition_id, workspace_id)
        final_transitions = uow.worker_transitions.list_for_plan_transition(
            plan_transition_id,
            workspace_id,
        )
        assert final_plan is not None
        assert final_plan.status is PlanTransitionStatus.CONVERGED
        assert final_transitions
        assert all(
            item.status is WorkerTransitionStatus.CONVERGED
            for item in final_transitions
        )
