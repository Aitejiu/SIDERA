from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from threading import Barrier
from uuid import uuid4

import pytest

from sidera_control_plane.application.code_result_capture import (
    CodeResultCaptureService,
    code_capture_operation_id,
)
from sidera_control_plane.domain.events import EventData
from sidera_control_plane.domain.execution import ExecutionStatus
from sidera_control_plane.domain.inputs import ExecutionInput
from sidera_control_plane.domain.result_operations import ResultError


def test_capture_survives_provider_recreation(result_scenario) -> None:
    result_scenario.seed_plan()
    first = result_scenario.source(
        "login",
        files={"login.py": b"enabled = True\n"},
        commit=True,
    )

    result_scenario.reopen_services()
    replay = result_scenario.capture_service.capture_execution(
        workspace_id=first.workspace_id,
        execution_id=first.execution_id,
        operation_id=first.capture_operation_id,
    )

    assert replay.id == first.id
    assert replay.content_hash == first.content_hash


def test_capture_does_not_publish_while_workspace_is_still_writing(
    result_scenario,
) -> None:
    result_scenario.seed_plan()
    result_scenario.source(
        "login",
        files={"login.py": b"enabled = True\n"},
        commit=False,
    )

    with pytest.raises(
        ResultError, match="result_terminal_observation_required"
    ):
        result_scenario.capture_service.capture_execution(
            workspace_id=result_scenario.workspace_id,
            execution_id=result_scenario.execution_id,
            operation_id=result_scenario.capture_operation_id,
        )


def test_repeated_capture_is_one_immutable_result(result_scenario) -> None:
    result_scenario.seed_plan()
    first = result_scenario.source(
        "login",
        files={"login.py": b"enabled = True\n"},
        commit=True,
    )

    second = result_scenario.capture_service.capture_execution(
        workspace_id=first.workspace_id,
        execution_id=first.execution_id,
        operation_id=first.capture_operation_id,
    )

    assert second == first
    assert len(result_scenario.retained_code_results()) == 1


def test_code_result_capture_uses_next_execution_aggregate_version(
    result_scenario, monkeypatch
) -> None:
    result_scenario.seed_plan()
    service = result_scenario.capture_service
    capture_execution = service.capture_execution
    gateway_start_versions: list[int] = []

    def capture_after_gateway_start(**kwargs):
        execution_events = tuple(
            event
            for event in result_scenario.events()
            if event.aggregate_id == kwargs["execution_id"]
        )
        assert execution_events
        with result_scenario.uow() as uow:
            execution = uow.executions.get(
                kwargs["execution_id"],
                kwargs["workspace_id"],
                for_update=True,
            )
            assert execution is not None
            gateway_start_versions.append(execution.version)
            uow.events.append_with_outbox(
                EventData(
                    id=uuid4(),
                    workspace_id=execution.workspace_id,
                    aggregate_type="execution",
                    aggregate_id=execution.id,
                    aggregate_version=execution.version,
                    event_type="ExecutionStarted",
                    payload={
                        "execution_id": str(execution.id),
                        "status": execution.status.value,
                    },
                    actor_type="worker-gateway",
                    actor_id="test-worker-gateway",
                    causation_id=execution_events[-1].causation_id,
                    correlation_id=result_scenario.root_task_id,
                )
            )
        return capture_execution(**kwargs)

    monkeypatch.setattr(service, "capture_execution", capture_after_gateway_start)
    captured = result_scenario.source(
        "login",
        files={"login.py": b"enabled = True\n"},
        commit=True,
    )
    assert captured is not None

    capture_events = tuple(
        event
        for event in result_scenario.events("CodeResultCaptured")
        if event.aggregate_id == captured.execution_id
    )

    assert len(capture_events) == 1
    assert len(gateway_start_versions) == 1
    capture_event = capture_events[0]
    assert capture_event.aggregate_type == "execution"
    assert capture_event.aggregate_version == gateway_start_versions[0] + 1
    with result_scenario.uow() as uow:
        execution = uow.executions.get(
            captured.execution_id,
            result_scenario.workspace_id,
        )

    assert execution is not None
    assert execution.status is ExecutionStatus.SUCCEEDED
    assert execution.version == capture_event.aggregate_version + 1


def test_capture_failure_does_not_mark_execution_consumable(
    result_scenario, monkeypatch
) -> None:
    result_scenario.seed_plan()
    result_scenario.source(
        "login",
        files={"login.py": b"enabled = True\n"},
        commit=True,
        capture=False,
    )
    service = result_scenario.capture_service

    def fail_persistence(*args, **kwargs):
        raise RuntimeError("injected result persistence failure")

    monkeypatch.setattr(service, "_persist_snapshot", fail_persistence)

    with pytest.raises(RuntimeError, match="injected result persistence failure"):
        service.capture_execution(
            workspace_id=result_scenario.workspace_id,
            execution_id=result_scenario.execution_id,
            operation_id=result_scenario.capture_operation_id,
        )

    result_scenario.reopen_services()
    replay = result_scenario.capture_service.capture_execution(
        workspace_id=result_scenario.workspace_id,
        execution_id=result_scenario.execution_id,
        operation_id=result_scenario.capture_operation_id,
    )
    assert replay.content_hash


def test_capture_service_is_constructible_from_real_dependencies(result_scenario) -> None:
    assert isinstance(result_scenario.capture_service, CodeResultCaptureService)


@pytest.mark.parametrize(
    "pending_status", ("accepted", "queued", "dispatching", "blocked")
)
def test_terminal_observation_waits_for_pending_session_input(
    result_scenario,
    pending_status: str,
) -> None:
    result_scenario.source(
        "login",
        files={"login.py": b"enabled = True\n"},
        commit=False,
    )
    execution_id = result_scenario.execution_id
    now = datetime.now(UTC)

    with result_scenario.uow() as uow:
        session = uow.sessions.get_active_for_execution(
            execution_id, for_update=True
        )
        assert session is not None
        item = ExecutionInput.accept(
            id=uuid4(),
            workspace_id=result_scenario.workspace_id,
            execution_id=execution_id,
            session_id=session.id,
            client_idempotency_key=f"pending-{pending_status}",
            text="Include the missing edge case",
            attachment_refs=(),
            now=now,
        )
        if pending_status != "accepted":
            item.queue()
        if pending_status in {"dispatching", "blocked"}:
            item.begin_dispatch(operation_id=uuid4())
        if pending_status == "blocked":
            item.block_unknown(failure_code="prompt_result_unknown")
        uow.execution_inputs.add(item)

    with pytest.raises(ResultError, match="result_capture_input_pending"):
        result_scenario.capture_service.observe_terminal(
            workspace_id=result_scenario.workspace_id,
            execution_id=execution_id,
            operation_id=result_scenario.capture_operation_id,
            outcome="succeeded",
            fencing_token=1,
        )

    with result_scenario.uow() as uow:
        operation = uow.results.get_operation(
            result_scenario.capture_operation_id,
            result_scenario.workspace_id,
        )
        assert operation is not None
        assert operation.payload.get("terminal_observed") is not True


@pytest.mark.parametrize("settled_status", ("delivered", "cancelled"))
def test_terminal_observation_ignores_settled_session_input(
    result_scenario,
    settled_status: str,
) -> None:
    result_scenario.source(
        "login",
        files={"login.py": b"enabled = True\n"},
        commit=False,
    )
    execution_id = result_scenario.execution_id
    now = datetime.now(UTC)

    with result_scenario.uow() as uow:
        session = uow.sessions.get_active_for_execution(
            execution_id, for_update=True
        )
        assert session is not None
        item = ExecutionInput.accept(
            id=uuid4(),
            workspace_id=result_scenario.workspace_id,
            execution_id=execution_id,
            session_id=session.id,
            client_idempotency_key=f"settled-{settled_status}",
            text="Keep the settled intervention",
            attachment_refs=(),
            now=now,
        )
        item.queue()
        if settled_status == "delivered":
            item.begin_dispatch(operation_id=uuid4())
            item.deliver()
        else:
            item.cancel(now=now)
        uow.execution_inputs.add(item)

    operation = result_scenario.capture_service.observe_terminal(
        workspace_id=result_scenario.workspace_id,
        execution_id=execution_id,
        operation_id=result_scenario.capture_operation_id,
        outcome="succeeded",
        fencing_token=1,
    )

    assert operation.payload.get("terminal_observed") is True


def test_capture_and_terminal_observation_race_without_deadlock(
    result_scenario,
) -> None:
    result_scenario.source(
        "login",
        files={"login.py": b"enabled = True\n"},
        commit=False,
    )
    barrier = Barrier(2)

    def observe_terminal():
        barrier.wait(timeout=5)
        return result_scenario.capture_service.observe_terminal(
            workspace_id=result_scenario.workspace_id,
            execution_id=result_scenario.execution_id,
            operation_id=result_scenario.capture_operation_id,
            outcome="succeeded",
            fencing_token=1,
        )

    def capture_result():
        barrier.wait(timeout=5)
        try:
            return result_scenario.capture_service.capture_execution(
                workspace_id=result_scenario.workspace_id,
                execution_id=result_scenario.execution_id,
                operation_id=result_scenario.capture_operation_id,
            )
        except ResultError as exc:
            return exc.code

    with ThreadPoolExecutor(max_workers=2) as executor:
        terminal_future = executor.submit(observe_terminal)
        capture_future = executor.submit(capture_result)
        terminal = terminal_future.result(timeout=10)
        concurrent_capture = capture_future.result(timeout=10)

    assert terminal.payload.get("terminal_observed") is True
    if isinstance(concurrent_capture, str):
        assert concurrent_capture == "result_terminal_observation_required"
    else:
        assert concurrent_capture.content_hash

    final_capture = result_scenario.capture_service.capture_execution(
        workspace_id=result_scenario.workspace_id,
        execution_id=result_scenario.execution_id,
        operation_id=result_scenario.capture_operation_id,
    )
    assert final_capture.content_hash


def _start_continuation_capture(result_scenario):
    original = result_scenario.source(
        "login",
        files={"login.py": b"enabled = True\n"},
        commit=True,
    )
    assert original is not None
    child_id = result_scenario._planning.continue_worker("a")
    _, _, child_session_id, _ = result_scenario._planning._start_source(
        child_id,
        key="a",
    )
    worktree, manifest = result_scenario._create_worktree(child_id)
    with result_scenario.uow() as uow:
        uow.workspaces.add(manifest)
        child = uow.executions.get(child_id, result_scenario.workspace_id)
        assert child is not None
        assert (
            uow.execution_requests.get_by_execution_lineage(
                child.lineage_id,
                child.workspace_id,
            )
            is None
        )
    return original, child_id, child_session_id, worktree, manifest


def test_capture_resolves_continuation_authority_through_source_session(
    result_scenario,
) -> None:
    original, child_id, _, worktree, manifest = _start_continuation_capture(
        result_scenario
    )
    operation_id = code_capture_operation_id(child_id)
    service = result_scenario.capture_service
    service.record_execution_start(
        manifest=manifest,
        writable_paths=(".",),
        operation_id=operation_id,
    )
    (worktree / "login.py").write_text(
        "enabled = True\ncontinued = True\n",
        encoding="utf-8",
    )
    service.observe_terminal(
        workspace_id=result_scenario.workspace_id,
        execution_id=child_id,
        operation_id=operation_id,
        outcome="succeeded",
        fencing_token=1,
    )
    result_scenario.reopen_services()
    captured = result_scenario.capture_service.capture_execution(
        workspace_id=result_scenario.workspace_id,
        execution_id=child_id,
        operation_id=operation_id,
    )

    assert captured.request_id == original.request_id
    assert captured.execution_id == child_id
    assert captured.worker_item_id == original.worker_item_id
    assert captured.root_task_id == original.root_task_id
    assert captured.result_tree != captured.input_tree


@pytest.mark.parametrize("link_corruption", ("missing_links", "wrong_session"))
def test_capture_rejects_unproven_continuation_authority(
    result_scenario,
    link_corruption: str,
) -> None:
    _, child_id, child_session_id, _, manifest = _start_continuation_capture(
        result_scenario
    )
    with result_scenario.uow() as uow:
        child = uow.executions.get(child_id, result_scenario.workspace_id)
        assert child is not None
        previous_version = child.version
        if link_corruption == "missing_links":
            child.source_execution_id = None
            child.source_session_id = None
        else:
            child.source_session_id = child_session_id
        child.version += 1
        uow.executions.save(child, previous_version)

    operation_id = code_capture_operation_id(child_id)
    with pytest.raises(ResultError) as raised:
        result_scenario.capture_service.record_execution_start(
            manifest=manifest,
            writable_paths=(".",),
            operation_id=operation_id,
        )
    assert raised.value.code == "result_capture_context_mismatch"

    with result_scenario.uow() as uow:
        assert uow.results.get_operation(operation_id, result_scenario.workspace_id) is None
        assert (
            uow.results.get_code_result_by_capture_operation(
                operation_id,
                result_scenario.workspace_id,
            )
            is None
        )
