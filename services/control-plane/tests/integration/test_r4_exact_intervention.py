from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from threading import Barrier, Event
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient

from sidera_control_plane.api.app import create_app
from sidera_control_plane.application.code_result_capture import (
    CODE_RESULT_CAPTURE_OPERATION_KIND,
    CodeResultCaptureService,
    code_capture_operation_id,
)
from sidera_control_plane.application.result_ports import ResultOperationAttemptV1
from sidera_control_plane.application.worker_item_backfill import (
    WorkerItemBackfillService,
)
from sidera_control_plane.config import Settings
from sidera_control_plane.domain.result_operations import (
    ResultError,
    ResultOperationStatus,
)
from sidera_control_plane.persistence.database import Database
from sidera_control_plane.persistence.models import (
    EventModel,
    ExecutionInputModel,
    ResultOperationAttemptModel,
)
from sidera_control_plane.persistence.unit_of_work import SqlAlchemyUnitOfWork
from tests.integration.test_r4_task_first_api import (
    BACKEND_EXECUTION_ID,
    GENERATION_TWO_ID,
    NOW,
    ROOT_TASK_ID,
    SESSION_ID,
    WORKER_ID,
    _seed,
    _snapshot,
)

pytestmark = pytest.mark.integration

GENERATION_THREE_ID = UUID("10000000-0000-0000-0000-000000000003")


@pytest.fixture
def client(database_url: str) -> Iterator[TestClient]:
    with TestClient(create_app(Settings(database_url=database_url))) as test_client:
        yield test_client


def _publish_generation_three(client: TestClient, workspace_id: UUID) -> None:
    snapshot = _snapshot(
        generation=3,
        snapshot_id=GENERATION_THREE_ID,
        parent=GENERATION_TWO_ID,
    )
    response = client.post(
        "/api/v1/commands",
        json={
            "command_id": str(uuid4()),
            "workspace_id": str(workspace_id),
            "command_type": "PublishPlanSnapshot",
            "target_type": "task",
            "actor": {"type": "local", "id": "aitejiu"},
            "target_id": str(ROOT_TASK_ID),
            "expected_version": 1,
            "idempotency_key": "exact-intervention:publish:g3",
            "correlation_id": str(uuid4()),
            "payload": {
                "expected_current_generation": 2,
                "snapshot": snapshot.to_payload(),
            },
        },
    )
    assert response.status_code == 200, response.text


def _bind_historical_worker_item(database: Database, workspace_id: UUID) -> UUID:
    with SqlAlchemyUnitOfWork(database) as uow:
        WorkerItemBackfillService().backfill_root(
            uow, workspace_id, ROOT_TASK_ID
        )
        item = uow.worker_items.get_by_stable_key(
            ROOT_TASK_ID, "legacy:backend:backend"
        )
        assert item is not None
        execution = uow.executions.get(
            BACKEND_EXECUTION_ID, workspace_id, for_update=True
        )
        assert execution is not None
        previous_version = execution.version
        execution.worker_item_id = item.id
        uow.executions.save(execution, previous_version)
        return item.id


def _send_request(
    workspace_id: UUID,
    *,
    worker_item_id: UUID,
    plan_generation: int,
    client_key: str,
) -> dict[str, object]:
    return {
        "command_id": str(uuid4()),
        "workspace_id": str(workspace_id),
        "command_type": "SendExecutionInput",
        "target_type": "execution_session",
        "actor": {"type": "local", "id": "aitejiu"},
        "target_id": str(SESSION_ID),
        "expected_version": 2,
        "idempotency_key": f"exact-intervention:send:{client_key}",
        "correlation_id": str(uuid4()),
        "payload": {
            "context": {
                "worker_item_id": str(worker_item_id),
                "plan_generation": plan_generation,
                "context_version": 1,
                "worker_id": str(WORKER_ID),
                "execution_id": str(BACKEND_EXECUTION_ID),
                "session_id": str(SESSION_ID),
                "session_version": 2,
            },
            "client_idempotency_key": client_key,
            "text": "Continue the exact backend task",
            "attachment_refs": [],
        },
    }


def test_intervention_rejects_stale_plan_address_and_accepts_refreshed_address(
    client: TestClient,
    database: Database,
    workspace_id: UUID,
) -> None:
    _seed(database)
    _publish_generation_three(client, workspace_id)
    worker_item_id = _bind_historical_worker_item(database, workspace_id)

    stale = client.post(
        "/api/v1/commands",
        json=_send_request(
            workspace_id,
            worker_item_id=worker_item_id,
            plan_generation=2,
            client_key="stale-context",
        ),
    )

    assert stale.status_code == 409, stale.text
    assert stale.json()["error"]["code"] == "execution_context_changed"
    assert stale.json()["error"]["details"]["changed_fields"] == ["plan_generation"]
    assert stale.json()["error"]["details"]["current"] == {
        "worker_item_id": str(worker_item_id),
        "plan_generation": 3,
        "context_version": 1,
        "execution_id": str(BACKEND_EXECUTION_ID),
        "session_id": str(SESSION_ID),
        "session_version": 2,
        "worker_id": str(WORKER_ID),
    }

    with database.session() as session:
        assert session.query(ExecutionInputModel).count() == 0

    refreshed = client.post(
        "/api/v1/commands",
        json=_send_request(
            workspace_id,
            worker_item_id=worker_item_id,
            plan_generation=3,
            client_key="refreshed-context",
        ),
    )
    assert refreshed.status_code == 200, refreshed.text

    with database.session() as session:
        assert session.query(ExecutionInputModel).count() == 1
        accepted = (
            session.query(EventModel)
            .filter(EventModel.event_type == "ExecutionInputAccepted")
            .one()
        )
        assert accepted.payload["execution_context"] == {
            "worker_item_id": str(worker_item_id),
            "plan_generation": "3",
            "context_version": "1",
            "execution_id": str(BACKEND_EXECUTION_ID),
            "session_id": str(SESSION_ID),
            "session_version": "2",
            "worker_id": str(WORKER_ID),
        }
        assert "adapter_session_id" not in accepted.payload["execution_context"]


def test_intervention_returns_conflict_after_terminal_observation(
    client: TestClient,
    database: Database,
    workspace_id: UUID,
) -> None:
    _seed(database)
    _publish_generation_three(client, workspace_id)
    worker_item_id = _bind_historical_worker_item(database, workspace_id)
    now = NOW
    operation = ResultOperationAttemptV1(
        operation_id=code_capture_operation_id(BACKEND_EXECUTION_ID),
        workspace_id=workspace_id,
        root_task_id=ROOT_TASK_ID,
        operation_kind=CODE_RESULT_CAPTURE_OPERATION_KIND,
        attempt=1,
        status=ResultOperationStatus.RUNNING,
        payload={"terminal_observed": True, "terminal_outcome": "succeeded"},
        lease_owner=None,
        lease_token=None,
        lease_expiry=None,
        receipt_ref=None,
        failure_code=None,
        version=1,
        created_at=now,
        updated_at=now,
    )
    with SqlAlchemyUnitOfWork(database) as uow:
        uow.results.save_operation(operation, expected_version=0)

    response = client.post(
        "/api/v1/commands",
        json=_send_request(
            workspace_id,
            worker_item_id=worker_item_id,
            plan_generation=3,
            client_key="terminal-observed",
        ),
    )

    assert response.status_code == 409, response.text
    assert response.json()["error"]["code"] == "execution_context_changed"
    assert response.json()["error"]["details"]["changed_fields"] == [
        "execution_finalizing"
    ]
    with database.session() as session:
        assert session.query(ExecutionInputModel).count() == 0


def test_intervention_client_key_replays_accepted_input_after_terminal_observation(
    client: TestClient,
    database: Database,
    workspace_id: UUID,
) -> None:
    _seed(database)
    _publish_generation_three(client, workspace_id)
    worker_item_id = _bind_historical_worker_item(database, workspace_id)
    accepted = client.post(
        "/api/v1/commands",
        json=_send_request(
            workspace_id,
            worker_item_id=worker_item_id,
            plan_generation=3,
            client_key="accepted-before-finalization",
        ),
    )
    assert accepted.status_code == 200, accepted.text
    accepted_input_id = UUID(accepted.json()["aggregate_id"])

    now = datetime.now(UTC)
    with SqlAlchemyUnitOfWork(database) as uow:
        uow.results.save_operation(
            ResultOperationAttemptV1(
                operation_id=code_capture_operation_id(BACKEND_EXECUTION_ID),
                workspace_id=workspace_id,
                root_task_id=ROOT_TASK_ID,
                operation_kind=CODE_RESULT_CAPTURE_OPERATION_KIND,
                attempt=1,
                status=ResultOperationStatus.RUNNING,
                payload={
                    "execution_id": str(BACKEND_EXECUTION_ID),
                    "terminal_observed": True,
                    "terminal_outcome": "succeeded",
                },
                lease_owner=None,
                lease_token=None,
                lease_expiry=None,
                receipt_ref=None,
                failure_code=None,
                version=1,
                created_at=now,
                updated_at=now,
            ),
            expected_version=0,
        )

    replay_request = _send_request(
        workspace_id,
        worker_item_id=worker_item_id,
        plan_generation=3,
        client_key="accepted-before-finalization",
    )
    replay_request["idempotency_key"] = "exact-intervention:send:client-replay"
    replay = client.post("/api/v1/commands", json=replay_request)

    assert replay.status_code == 200, replay.text
    assert UUID(replay.json()["aggregate_id"]) == accepted_input_id
    with database.session() as session:
        inputs = session.query(ExecutionInputModel).all()
        assert len(inputs) == 1
        assert inputs[0].id == accepted_input_id


@pytest.mark.parametrize("first", ("input", "terminal", "race"))
def test_input_acceptance_and_terminal_observation_have_one_winner(
    client: TestClient,
    database: Database,
    workspace_id: UUID,
    first: str,
) -> None:
    _seed(database)
    _publish_generation_three(client, workspace_id)
    worker_item_id = _bind_historical_worker_item(database, workspace_id)
    now = NOW
    operation_id = code_capture_operation_id(BACKEND_EXECUTION_ID)
    with SqlAlchemyUnitOfWork(database) as uow:
        uow.results.save_operation(
            ResultOperationAttemptV1(
                operation_id=operation_id,
                workspace_id=workspace_id,
                root_task_id=ROOT_TASK_ID,
                operation_kind=CODE_RESULT_CAPTURE_OPERATION_KIND,
                attempt=1,
                status=ResultOperationStatus.RUNNING,
                payload={
                    "execution_id": str(BACKEND_EXECUTION_ID),
                    "session_id": str(SESSION_ID),
                    "fencing_token": 1,
                },
                lease_owner=None,
                lease_token=None,
                lease_expiry=None,
                receipt_ref=None,
                failure_code=None,
                version=1,
                created_at=now,
                updated_at=now,
            ),
            expected_version=0,
        )

    capture = CodeResultCaptureService(
        lambda: SqlAlchemyUnitOfWork(database), object(), clock=lambda: now
    )
    ready = Barrier(2)
    input_committed = Event()
    terminal_committed = Event()
    input_request = _send_request(
        workspace_id,
        worker_item_id=worker_item_id,
        plan_generation=3,
        client_key=f"execution-fence-{first}",
    )

    def send_input():
        ready.wait(timeout=5)
        if first == "terminal" and not terminal_committed.wait(timeout=5):
            raise TimeoutError("terminal observation did not commit first")
        try:
            return client.post("/api/v1/commands", json=input_request)
        finally:
            input_committed.set()

    def observe_terminal():
        ready.wait(timeout=5)
        if first == "input" and not input_committed.wait(timeout=5):
            raise TimeoutError("input acceptance did not commit first")
        try:
            return capture.observe_terminal(
                workspace_id=workspace_id,
                execution_id=BACKEND_EXECUTION_ID,
                operation_id=operation_id,
                outcome="succeeded",
                fencing_token=1,
            )
        except ResultError as exc:
            return exc.code
        finally:
            terminal_committed.set()

    with ThreadPoolExecutor(max_workers=2) as executor:
        input_future = executor.submit(send_input)
        terminal_future = executor.submit(observe_terminal)
        response = input_future.result(timeout=10)
        terminal_result = terminal_future.result(timeout=10)

    with database.session() as session:
        inputs = session.query(ExecutionInputModel).all()
        operation = session.get(ResultOperationAttemptModel, operation_id)
        assert operation is not None
        if response.status_code == 200:
            assert len(inputs) == 1
            assert terminal_result == "result_capture_input_pending"
            assert operation.payload.get("terminal_observed") is not True
        else:
            assert response.status_code == 409, response.text
            assert response.json()["error"]["code"] == "execution_context_changed"
            assert response.json()["error"]["details"]["changed_fields"] == [
                "execution_finalizing"
            ]
            assert inputs == []
            assert not isinstance(terminal_result, str)
            assert terminal_result.payload.get("terminal_observed") is True
