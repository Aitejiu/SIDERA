from __future__ import annotations

from collections.abc import Callable
from dataclasses import replace
from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from sidera_control_plane.api.app import create_app
from sidera_control_plane.application.plan_transition_coordinator import (
    PlanTransitionCoordinator,
)
from sidera_control_plane.config import Settings
from sidera_control_plane.domain.gateway import (
    GatewayOperation,
    GatewayOperationType,
    gateway_operation_id,
)
from sidera_control_plane.persistence.models import (
    EventModel,
    GatewayOperationModel,
    WorkerTransitionModel,
)
from tests.integration.test_revision_confirmation import (
    _base_plan,
    _confirm_command,
    _retire_payload,
    _seed_revision_confirmation,
)
from tests.integration.test_worker_transition_lifecycle import _seed_active_runtime

pytestmark = pytest.mark.integration

NOW = datetime(2026, 8, 28, 12, 0, tzinfo=UTC)


def _transition_command(
    *,
    workspace_id: UUID,
    transition_id: UUID,
    expected_version: int,
    command_type: str,
    payload: dict[str, Any],
    idempotency_key: str,
    command_id: UUID | None = None,
) -> dict[str, Any]:
    return {
        "command_id": str(command_id or uuid4()),
        "workspace_id": str(workspace_id),
        "command_type": command_type,
        "target_type": "worker_transition",
        "actor": {"type": "local", "id": "operator"},
        "target_id": str(transition_id),
        "expected_version": expected_version,
        "idempotency_key": idempotency_key,
        "correlation_id": str(uuid4()),
        "payload": payload,
    }


def _retire_unknown(
    database,
    database_url: str,
    workspace_id: UUID,
    uow_factory: Callable[[], Any],
) -> dict[str, Any]:
    snapshot = _base_plan(workspace_id)
    payload = replace(
        _retire_payload(snapshot),
        execution_intents=tuple(
            replace(
                intent,
                expected_artifact_refs=("artifact_type:diff",),
                required_evidence_refs=("acceptance:implementation",),
            )
            for intent in _retire_payload(snapshot).execution_intents
        ),
    )
    seed = _seed_revision_confirmation(
        database,
        workspace_id,
        snapshot=snapshot,
        payload=payload,
    )
    runtime = _seed_active_runtime(database, seed["worker_item"].id, workspace_id)
    with TestClient(
        create_app(Settings(database_url=database_url, local_actor_id="operator"))
    ) as client:
        response = client.post(
            "/api/v1/commands",
            json=_confirm_command(seed, idempotency_key="r6-1-2-recovery-seed"),
        )
    assert response.status_code == 201, response.text

    coordinator = PlanTransitionCoordinator(
        uow_factory=uow_factory,
        clock=lambda: NOW,
        owner_id="recovery-test",
    )
    assert coordinator.run_once(limit=10).failed == 0

    with uow_factory() as uow:
        execution = uow.executions.get(runtime["execution_id"], workspace_id)
        lease = uow.leases.get(runtime["lease_id"], workspace_id)
        assert execution is not None and lease is not None
        operation = GatewayOperation.create(
            id=uuid4(),
            operation_id=gateway_operation_id(
                execution.id, GatewayOperationType.STOP_SESSION
            ),
            workspace_id=workspace_id,
            task_id=execution.task_id,
            execution_id=execution.id,
            session_id=runtime["session_id"],
            operation_type=GatewayOperationType.STOP_SESSION,
            module_instance_id=None,
            config_version=None,
            lease_id=lease.id,
            fencing_token=lease.fencing_token,
            now=NOW,
        )
        uow.gateway_operations.add(operation)
        uow.session.flush()
        previous_version = operation.version
        operation.start(now=NOW)
        operation.mark_unknown(failure_code="gateway_outcome_unknown", now=NOW)
        uow.gateway_operations.save(operation, previous_version)

    result = coordinator.run_once(limit=10)
    assert result.failed == 1
    with database.session() as session:
        transition = session.scalar(
            select(WorkerTransitionModel).where(
                WorkerTransitionModel.worker_item_id == seed["worker_item"].id
            )
        )
        assert transition is not None
        assert transition.status == "blocked_unknown"
        return {
            "seed": seed,
            "runtime": runtime,
            "transition_id": transition.id,
            "plan_transition_id": transition.plan_transition_id,
            "version": transition.version,
            "operation_id": gateway_operation_id(
                runtime["execution_id"], GatewayOperationType.STOP_SESSION
            ),
        }


def _close_source(database, workspace_id: UUID, runtime: dict[str, UUID], uow_factory) -> None:
    with uow_factory() as uow:
        session = uow.sessions.get_any(runtime["session_id"], workspace_id, for_update=True)
        lease = uow.leases.get(runtime["lease_id"], workspace_id, for_update=True)
        execution = uow.executions.get(runtime["execution_id"], workspace_id, for_update=True)
        worker = uow.workers.get(runtime["worker_id"], workspace_id, for_update=True)
        assert session is not None and lease is not None and execution is not None and worker is not None
        session_version = session.version
        session.close(lost=False, now=NOW)
        uow.sessions.save(session, session_version)
        lease_version = lease.version
        lease.revoke(now=NOW)
        uow.leases.save(lease, lease_version)
        worker_version = worker.version
        worker.release()
        uow.workers.save(worker, worker_version)
        execution_version = execution.version
        execution.cancel(now=NOW)
        uow.executions.save(execution, execution_version)


def _transition_version(database, transition_id: UUID, workspace_id: UUID) -> int:
    with database.session() as session:
        version = session.scalar(
            select(WorkerTransitionModel.version).where(
                WorkerTransitionModel.id == transition_id,
                WorkerTransitionModel.workspace_id == workspace_id,
            )
        )
        assert version is not None
        return version


def test_reconcile_is_audit_only_and_idempotent(
    database, database_url: str, workspace_id: UUID, uow_factory
) -> None:
    blocked = _retire_unknown(database, database_url, workspace_id, uow_factory)
    payload = {"reason": "Inspect the recorded stop operation", "evidence_refs": ["ops://stop-check"]}
    request = _transition_command(
        workspace_id=workspace_id,
        transition_id=blocked["transition_id"],
        expected_version=blocked["version"],
        command_type="ReconcileWorkerTransition",
        payload=payload,
        idempotency_key="reconcile-transition",
    )

    with TestClient(
        create_app(Settings(database_url=database_url, local_actor_id="operator"))
    ) as client:
        first = client.post("/api/v1/commands", json=request)
        second = client.post("/api/v1/commands", json=request)

    assert first.status_code == 200, first.text
    assert second.status_code == 200, second.text
    assert first.json() == second.json()
    with database.session() as session:
        assert session.scalar(
            select(func.count()).select_from(EventModel).where(
                EventModel.event_type == "WorkerTransitionReconciliationRequested"
            )
        ) == 1
        transition = session.get(WorkerTransitionModel, blocked["transition_id"])
        assert transition is not None
        assert transition.status == "blocked_unknown"
        assert transition.version == blocked["version"]


def test_reconcile_rejects_mismatched_operation_or_patch_hash(
    database, database_url: str, workspace_id: UUID, uow_factory
) -> None:
    blocked = _retire_unknown(database, database_url, workspace_id, uow_factory)
    with TestClient(
        create_app(Settings(database_url=database_url, local_actor_id="operator"))
    ) as client:
        mismatched_operation = client.post(
            "/api/v1/commands",
            json=_transition_command(
                workspace_id=workspace_id,
                transition_id=blocked["transition_id"],
                expected_version=blocked["version"],
                command_type="ReconcileWorkerTransition",
                payload={
                    "operation_id": str(uuid4()),
                    "reason": "The supplied operation must be exact",
                    "evidence_refs": ["ops://wrong-operation"],
                },
                idempotency_key="reconcile-wrong-operation",
            ),
        )
        mismatched_hash = client.post(
            "/api/v1/commands",
            json=_transition_command(
                workspace_id=workspace_id,
                transition_id=blocked["transition_id"],
                expected_version=blocked["version"],
                command_type="ReconcileWorkerTransition",
                payload={
                    "patch_hash": "0" * 64,
                    "reason": "The supplied patch hash must be exact",
                    "evidence_refs": ["ops://wrong-hash"],
                },
                idempotency_key="reconcile-wrong-hash",
            ),
        )

    assert mismatched_operation.status_code == 422, mismatched_operation.text
    assert mismatched_hash.status_code == 422, mismatched_hash.text


def test_confirm_stopped_requires_live_stop_proof_and_records_evidence(
    database, database_url: str, workspace_id: UUID, uow_factory
) -> None:
    blocked = _retire_unknown(database, database_url, workspace_id, uow_factory)
    payload = {
        "reason": "Verify the recorded source writer state",
        "source_execution_id": str(blocked["runtime"]["execution_id"]),
        "source_session_id": str(blocked["runtime"]["session_id"]),
        "evidence_refs": ["operator://session-stopped"],
    }
    with TestClient(
        create_app(Settings(database_url=database_url, local_actor_id="operator"))
    ) as client:
        active = client.post(
            "/api/v1/commands",
            json=_transition_command(
                workspace_id=workspace_id,
                transition_id=blocked["transition_id"],
                expected_version=blocked["version"],
                command_type="ConfirmWorkerTransitionStopped",
                payload=payload,
                idempotency_key="confirm-stopped-active",
            ),
        )
    assert active.status_code == 409, active.text

    _close_source(database, workspace_id, blocked["runtime"], uow_factory)
    version = _transition_version(database, blocked["transition_id"], workspace_id)
    with TestClient(
        create_app(Settings(database_url=database_url, local_actor_id="operator"))
    ) as client:
        confirmed = client.post(
            "/api/v1/commands",
            json=_transition_command(
                workspace_id=workspace_id,
                transition_id=blocked["transition_id"],
                expected_version=version,
                command_type="ConfirmWorkerTransitionStopped",
                payload=payload,
                idempotency_key="confirm-stopped-after-proof",
            ),
        )
    assert confirmed.status_code == 200, confirmed.text
    assert confirmed.json()["data"]["source_session_stopped"] is True
    with database.session() as session:
        assert session.scalar(
            select(func.count()).select_from(EventModel).where(
                EventModel.event_type == "WorkerTransitionStoppedConfirmed"
            )
        ) == 1


def test_recovery_requires_evidence_and_can_continue_or_fail_closed(
    database, database_url: str, workspace_id: UUID, uow_factory
) -> None:
    blocked = _retire_unknown(database, database_url, workspace_id, uow_factory)
    with TestClient(
        create_app(Settings(database_url=database_url, local_actor_id="operator"))
    ) as client:
        invalid = client.post(
            "/api/v1/commands",
            json=_transition_command(
                workspace_id=workspace_id,
                transition_id=blocked["transition_id"],
                expected_version=blocked["version"],
                command_type="RecoverWorkerTransition",
                payload={"strategy": "continue_after_confirmed_stop"},
                idempotency_key="recover-invalid",
            ),
        )
    assert invalid.status_code == 422, invalid.text

    _close_source(database, workspace_id, blocked["runtime"], uow_factory)
    version = _transition_version(database, blocked["transition_id"], workspace_id)
    with TestClient(
        create_app(Settings(database_url=database_url, local_actor_id="operator"))
    ) as client:
        recovered = client.post(
            "/api/v1/commands",
            json=_transition_command(
                workspace_id=workspace_id,
                transition_id=blocked["transition_id"],
                expected_version=version,
                command_type="RecoverWorkerTransition",
                payload={
                    "strategy": "continue_after_confirmed_stop",
                    "reason": "The old session is terminal and has no active lease",
                    "evidence_refs": ["operator://session-stopped"],
                },
                idempotency_key="recover-continue",
            ),
        )
    assert recovered.status_code == 200, recovered.text
    with database.session() as session:
        transition = session.get(WorkerTransitionModel, blocked["transition_id"])
        assert transition is not None
        assert transition.status == "pending"
        assert transition.writer_state == "fenced"

def test_recovery_can_fail_closed_after_stop_proof(
    database, database_url: str, workspace_id: UUID, uow_factory
) -> None:
    blocked = _retire_unknown(database, database_url, workspace_id, uow_factory)
    _close_source(database, workspace_id, blocked["runtime"], uow_factory)
    version = _transition_version(database, blocked["transition_id"], workspace_id)
    with TestClient(
        create_app(Settings(database_url=database_url, local_actor_id="operator"))
    ) as client:
        failed_closed = client.post(
            "/api/v1/commands",
            json=_transition_command(
                workspace_id=workspace_id,
                transition_id=blocked["transition_id"],
                expected_version=version,
                command_type="RecoverWorkerTransition",
                payload={
                    "strategy": "fail_closed_branch",
                    "reason": "Retire this branch after operator review",
                    "evidence_refs": ["operator://branch-failed-closed"],
                },
                idempotency_key="recover-fail-closed",
            ),
        )
    assert failed_closed.status_code == 200, failed_closed.text
    with database.session() as session:
        transition = session.get(WorkerTransitionModel, blocked["transition_id"])
        assert transition is not None
        assert transition.status == "failed"
        assert transition.writer_state == "closed"


def test_recovery_rejects_cross_workspace_transition(
    database, database_url: str, workspace_id: UUID, uow_factory
) -> None:
    blocked = _retire_unknown(database, database_url, workspace_id, uow_factory)
    other_workspace = uuid4()
    request = _transition_command(
        workspace_id=other_workspace,
        transition_id=blocked["transition_id"],
        expected_version=blocked["version"],
        command_type="RecoverWorkerTransition",
        payload={
            "strategy": "fail_closed_branch",
            "reason": "Cross workspace access must fail closed",
            "evidence_refs": ["operator://wrong-workspace"],
        },
        idempotency_key="recover-cross-workspace",
    )
    with TestClient(
        create_app(Settings(database_url=database_url, local_actor_id="operator"))
    ) as client:
        response = client.post("/api/v1/commands", json=request)
    assert response.status_code == 404, response.text


def test_reconcile_adapter_never_replays_unknown_operation(
    database, database_url: str, workspace_id: UUID, uow_factory
) -> None:
    blocked = _retire_unknown(database, database_url, workspace_id, uow_factory)
    with database.session() as session:
        before = session.scalar(
            select(GatewayOperationModel.attempt_count).where(
                GatewayOperationModel.operation_id == blocked["operation_id"]
            )
        )
    assert before == 1
    with TestClient(
        create_app(Settings(database_url=database_url, local_actor_id="operator"))
    ) as client:
        response = client.post(
            "/api/v1/commands",
            json=_transition_command(
                workspace_id=workspace_id,
                transition_id=blocked["transition_id"],
                expected_version=blocked["version"],
                command_type="RecoverWorkerTransition",
                payload={
                    "strategy": "reconcile_adapter",
                    "operation_id": str(blocked["operation_id"]),
                    "reason": "Ask the adapter for the durable outcome",
                    "evidence_refs": ["adapter://reconcile-request"],
                },
                idempotency_key="recover-reconcile-adapter",
            ),
        )
    assert response.status_code == 200, response.text
    with database.session() as session:
        after = session.scalar(
            select(GatewayOperationModel.attempt_count).where(
                GatewayOperationModel.operation_id == blocked["operation_id"]
            )
        )
        transition = session.get(WorkerTransitionModel, blocked["transition_id"])
    assert after == before
    assert transition is not None and transition.status == "blocked_unknown"


def test_transition_recovery_routes_delegate_and_validate_path_identity(
    database, database_url: str, workspace_id: UUID, uow_factory
) -> None:
    blocked = _retire_unknown(database, database_url, workspace_id, uow_factory)
    base_url = (
        f"/api/v1/workspaces/{workspace_id}/worker-transitions/"
        f"{blocked['transition_id']}"
    )
    with TestClient(
        create_app(Settings(database_url=database_url, local_actor_id="operator"))
    ) as client:
        reconciled = client.post(
            f"{base_url}/reconcile",
            json=_transition_command(
                workspace_id=workspace_id,
                transition_id=blocked["transition_id"],
                expected_version=blocked["version"],
                command_type="ReconcileWorkerTransition",
                payload={
                    "reason": "Inspect this transition through its dedicated route",
                    "evidence_refs": ["operator://route-reconcile"],
                },
                idempotency_key="route-reconcile",
            ),
        )
        stopped = client.post(
            f"{base_url}/confirm-stopped",
            json=_transition_command(
                workspace_id=workspace_id,
                transition_id=blocked["transition_id"],
                expected_version=blocked["version"],
                command_type="ConfirmWorkerTransitionStopped",
                payload={
                    "reason": "The active source must still block confirmation",
                    "evidence_refs": ["operator://route-confirm"],
                },
                idempotency_key="route-confirm",
            ),
        )
        recovered = client.post(
            f"{base_url}/recover",
            json=_transition_command(
                workspace_id=workspace_id,
                transition_id=blocked["transition_id"],
                expected_version=blocked["version"],
                command_type="RecoverWorkerTransition",
                payload={
                    "strategy": "reconcile_adapter",
                    "reason": "Request adapter reconciliation through its dedicated route",
                    "evidence_refs": ["operator://route-recover"],
                },
                idempotency_key="route-recover",
            ),
        )
        mismatched = client.post(
            f"/api/v1/workspaces/{uuid4()}/worker-transitions/"
            f"{blocked['transition_id']}/recover",
            json=_transition_command(
                workspace_id=workspace_id,
                transition_id=blocked["transition_id"],
                expected_version=blocked["version"],
                command_type="RecoverWorkerTransition",
                payload={
                    "strategy": "reconcile_adapter",
                    "reason": "The path workspace must match the envelope",
                    "evidence_refs": ["operator://route-mismatch"],
                },
                idempotency_key="route-mismatch",
            ),
        )

    assert reconciled.status_code == 200, reconciled.text
    assert stopped.status_code == 409, stopped.text
    assert recovered.status_code == 200, recovered.text
    assert mismatched.status_code == 422, mismatched.text
