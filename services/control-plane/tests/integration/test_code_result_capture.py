from __future__ import annotations

import pytest

from sidera_control_plane.application.code_result_capture import (
    CodeResultCaptureService,
)
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
