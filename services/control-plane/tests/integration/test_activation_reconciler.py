from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from sidera_control_plane.api.app import create_app
from sidera_control_plane.application.activation_reconciler import ActivationReconciler
from sidera_control_plane.config import Settings
from sidera_control_plane.context_package_worker import ContextPackageWorker
from sidera_control_plane.domain.execution_requests import ExecutionRequestStatus
from sidera_control_plane.domain.task import TaskStatus
from sidera_control_plane.persistence.context_package_repository import (
    ContextAssemblyRepository,
)
from sidera_control_plane.persistence.models import (
    ArtifactModel,
    ContextAssemblyRequestModel,
    EvidenceClaimModel,
    ExecutionModel,
    ExecutionRequestDependencyModel,
    ExecutionRequestModel,
    GatewayOperationModel,
    TaskModel,
)
from tests.integration.test_collaboration_evaluation_recovery import _prepare_fixture
from tests.integration.test_context_package_worker import EmptyCatalog
from tests.integration.test_proposal_confirmation import command, seed_proposal

pytestmark = pytest.mark.integration

NOW = datetime.now(UTC) + timedelta(minutes=5)


@pytest.fixture
def client(database_url: str) -> Iterator[TestClient]:
    settings = Settings(database_url=database_url, local_actor_id="operator")
    with TestClient(create_app(settings)) as test_client:
        yield test_client


def _reconciler(database, uow_factory) -> ActivationReconciler:
    return ActivationReconciler(
        uow_factory=uow_factory,
        owner_id="activation-test",
        clock=lambda: NOW,
    )


def _materialize_plan(client: TestClient, uow_factory, workspace_id: UUID) -> UUID:
    request_id, profile_id, content_hash, _proposal_id = seed_proposal(
        uow_factory, workspace_id
    )
    response = client.post(
        "/api/v1/commands",
        json=command(workspace_id, request_id, profile_id, content_hash),
    )
    assert response.status_code == 201, response.text
    return request_id


def test_release_required_request_stays_blocked_before_run_exists(
    database,
    workspace_id: UUID,
    uow_factory,
) -> None:
    ids, _snapshot, _collaboration_reconciler = _prepare_fixture(
        database, workspace_id, uow_factory
    )

    reconciler = _reconciler(database, uow_factory)
    with uow_factory() as uow:
        request = uow.execution_requests.get(ids["request"], workspace_id)
        assert request is not None
        decision = reconciler._decision(
            uow,
            request,
            SimpleNamespace(task=SimpleNamespace(status=TaskStatus.READY)),
            now=NOW,
        )

    assert request.status is ExecutionRequestStatus.BLOCKED
    assert not decision.ready
    assert decision.reason == "collaboration_run_pending"


def test_root_ready_request_creates_execution_and_marks_subtask_ready(
    client: TestClient,
    database,
    workspace_id: UUID,
    uow_factory,
) -> None:
    _materialize_plan(client, uow_factory, workspace_id)

    result = _reconciler(database, uow_factory).run_once(limit=10)

    assert result.examined == 1
    assert result.readied == 0
    assert result.materialized == 1
    assert result.deferred == 0
    assert result.failed == 0
    execution_id = result.execution_ids[0]
    with database.session() as session:
        execution = session.get(ExecutionModel, execution_id)
        assert execution is not None
        assert execution.task_id is not None
        assert execution.worker_item_id is not None
        assert execution.status == "queued"
        assert execution.adapter_preferences == ["fake"]
        assert execution.adapter_forbidden == ["unsafe"]
        assert session.scalar(select(func.count()).select_from(GatewayOperationModel)) == 0
        request = session.scalar(select(ExecutionRequestModel))
        assert request is not None
        assert request.status == "materialized"
        assert request.execution_lineage_id == execution.lineage_id
        context_request = session.scalar(
            select(ContextAssemblyRequestModel).where(
                ContextAssemblyRequestModel.consumer_id == execution.id
            )
        )
        assert context_request is not None
        assert context_request.purpose == "execution"
        assert context_request.status == "queued"
        assert context_request.context_version == execution.version
        task = session.get(TaskModel, request.subtask_task_id)
        assert task is not None
        assert task.status == "ready"

    package_result = ContextPackageWorker(
        uow_factory=uow_factory,
        catalog=EmptyCatalog(),
        owner_id="context-worker:activation-test",
        clock=lambda: NOW,
    ).run_once(limit=1)
    assert package_result.claimed == 1
    assert package_result.ready == 1
    assert package_result.failed == 0

    repeated = _reconciler(database, uow_factory).run_once(limit=10)
    assert repeated == type(result)(0, 0, 0, 0, 0, ())


def test_long_execution_request_purpose_is_preserved_on_materialization(
    client: TestClient,
    database,
    workspace_id: UUID,
    uow_factory,
) -> None:
    _materialize_plan(client, uow_factory, workspace_id)
    sentence = "Preserve the complete worker instructions during activation."
    purpose = " ".join([sentence] * 7)
    assert len(purpose) > 255

    with database.session() as session:
        request = session.scalar(select(ExecutionRequestModel))
        assert request is not None
        request.purpose = purpose
        session.commit()

    result = _reconciler(database, uow_factory).run_once(limit=10)

    assert result.materialized == 1
    assert result.failed == 0
    with database.session() as session:
        execution = session.get(ExecutionModel, result.execution_ids[0])
        assert execution is not None
        assert execution.purpose == purpose


def test_context_assembly_creation_rolls_back_execution_materialization(
    client: TestClient,
    database,
    workspace_id: UUID,
    uow_factory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _materialize_plan(client, uow_factory, workspace_id)

    def fail_add(self, request) -> None:
        del self, request
        raise RuntimeError("context assembly insert failed")

    monkeypatch.setattr(ContextAssemblyRepository, "add", fail_add)

    result = _reconciler(database, uow_factory).run_once(limit=10)

    assert result.examined == 1
    assert result.materialized == 0
    assert result.failed == 1
    with database.session() as session:
        assert session.scalar(select(func.count()).select_from(ExecutionModel)) == 0
        assert session.scalar(
            select(func.count()).select_from(ContextAssemblyRequestModel)
        ) == 0
        request = session.scalar(select(ExecutionRequestModel))
        assert request is not None
        assert request.status == "ready"


def test_hard_dependency_waits_for_lineage_artifact_and_candidate_evidence(
    client: TestClient,
    database,
    workspace_id: UUID,
    uow_factory,
) -> None:
    request_id = _materialize_plan(client, uow_factory, workspace_id)
    upstream_request_id = uuid4()
    upstream_execution_id = uuid4()

    with database.session() as session:
        downstream = session.scalar(
            select(ExecutionRequestModel).where(
                ExecutionRequestModel.planning_request_id == request_id
            )
        )
        assert downstream is not None
        downstream.status = "blocked"
        downstream.gate_reason = "not_evaluated"
        downstream.gate_details = {}
        downstream.gate_evaluated_at = None
        downstream.status_changed_at = NOW
        session.add(
            ExecutionModel(
                id=upstream_execution_id,
                workspace_id=workspace_id,
                task_id=downstream.subtask_task_id,
                lineage_id=upstream_execution_id,
                attempt=1,
                max_attempts=1,
                purpose="upstream work",
                status="succeeded",
                capability_requirements=["code"],
                pinned_worker_id=None,
                assigned_worker_id=None,
                active_lease_id=None,
                worker_item_id=downstream.worker_item_id,
                last_fencing_token=0,
                not_before=NOW,
                failure_code=None,
                started_at=NOW,
                finished_at=NOW,
                version=1,
                stop_requested=False,
                source_execution_id=None,
                source_session_id=None,
            )
        )
        session.flush()
        session.add(
            ExecutionRequestModel(
                id=upstream_request_id,
                workspace_id=workspace_id,
                planning_request_id=downstream.planning_request_id,
                proposal_revision_id=downstream.proposal_revision_id,
                plan_snapshot_id=downstream.plan_snapshot_id,
                plan_generation=downstream.plan_generation,
                root_task_id=downstream.root_task_id,
                subtask_task_id=downstream.subtask_task_id,
                worker_item_id=downstream.worker_item_id,
                worker_item_key="upstream-worker",
                intent_key="upstream-intent",
                purpose="upstream work",
                capability_requirements=["code"],
                adapter_preferences=["opencode"],
                budget_class="standard",
                not_before=NOW,
                max_attempts=1,
                expected_outputs=["diff"],
                expected_artifact_refs=[],
                expected_evidence_refs=[],
                status="materialized",
                gate_reason="ready",
                gate_details={"reason": "ready"},
                gate_evaluated_at=NOW,
                status_changed_at=NOW,
                execution_lineage_id=upstream_execution_id,
                first_execution_id=upstream_execution_id,
                version=2,
            )
        )
        session.flush()
        session.add(
            ExecutionRequestDependencyModel(
                workspace_id=workspace_id,
                downstream_request_id=downstream.id,
                upstream_request_id=upstream_request_id,
                enforcement="hard",
                required_artifact_refs=["artifact_type:diff"],
                required_evidence_refs=["acceptance:done"],
            )
        )
        session.commit()

    blocked_without_outputs = _reconciler(database, uow_factory).run_once(limit=10)
    assert blocked_without_outputs.materialized == 0
    assert blocked_without_outputs.deferred == 1

    artifact_id = uuid4()
    with database.session() as session:
        session.add(
            ArtifactModel(
                id=artifact_id,
                workspace_id=workspace_id,
                task_id=downstream.subtask_task_id,
                execution_id=upstream_execution_id,
                session_id=None,
                artifact_type="diff",
                storage_ref="memory://activation-diff",
                content_hash="b" * 64,
                size_bytes=1,
                media_type="text/plain",
                provenance={},
            )
        )
        session.add(
            EvidenceClaimModel(
                id=uuid4(),
                workspace_id=workspace_id,
                task_id=downstream.subtask_task_id,
                execution_id=upstream_execution_id,
                acceptance_criterion_ref="orphan",
                conclusion="passed",
                environment_ref="test",
                artifact_ids=[str(uuid4())],
                producer="upstream-worker",
                status="candidate",
            )
        )
        session.commit()

    blocked_with_orphan_evidence = _reconciler(database, uow_factory).run_once(
        limit=10
    )
    assert blocked_with_orphan_evidence.materialized == 0
    assert blocked_with_orphan_evidence.deferred == 1

    with database.session() as session:
        session.add(
            EvidenceClaimModel(
                id=uuid4(),
                workspace_id=workspace_id,
                task_id=downstream.subtask_task_id,
                execution_id=upstream_execution_id,
                acceptance_criterion_ref="done",
                conclusion="passed",
                environment_ref="test",
                artifact_ids=[str(artifact_id)],
                producer="upstream-worker",
                status="candidate",
            )
        )
        session.commit()

    ready_with_outputs = _reconciler(database, uow_factory).run_once(limit=10)
    assert ready_with_outputs.materialized == 1
    assert ready_with_outputs.failed == 0
    with database.session() as session:
        stored_downstream = session.get(ExecutionRequestModel, downstream.id)
        assert stored_downstream is not None
        assert stored_downstream.status == "materialized"
        assert session.scalar(select(func.count()).select_from(ExecutionModel)) == 2
