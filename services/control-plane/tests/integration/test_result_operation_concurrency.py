from __future__ import annotations

import pytest

from sidera_control_plane.application.handoff_gate import (
    HandoffGateOperation,
    require_handoff_operation,
)
from sidera_control_plane.application.result_gate import require_result_gate
from sidera_control_plane.domain.result_operations import ResultError

pytestmark = pytest.mark.integration


def _accepted_unknown(result_scenario):
    result_scenario.seed_plan(apply=True)
    candidate = result_scenario.candidate()
    result_scenario.command(
        "CompleteTask", result_scenario.accept_payload(candidate)
    )
    result_scenario.prepare_unknown_application()
    return candidate


def test_revision_cannot_activate_during_unknown_application(result_scenario) -> None:
    _accepted_unknown(result_scenario)

    with result_scenario.uow() as uow, pytest.raises(
        ResultError, match="result_operation_in_progress"
    ):
        require_result_gate(
            uow,
            result_scenario.workspace_id,
            result_scenario.root_id,
            "activate_revision",
        )


def test_cancel_cannot_release_an_unknown_application(result_scenario) -> None:
    _accepted_unknown(result_scenario)
    root = result_scenario.root()

    with pytest.raises(ResultError, match="result_operation_in_progress"):
        result_scenario.command(
            "CancelTask",
            {},
            expected_version=root.version,
            idempotency_key="cancel-unknown-result-application",
        )


def test_rework_cannot_start_during_unknown_application(result_scenario) -> None:
    candidate = _accepted_unknown(result_scenario)

    with pytest.raises(ResultError, match="result_operation_in_progress"):
        result_scenario.command(
            "RequestTaskRework",
            result_scenario.rework_payload(candidate),
            idempotency_key="rework-unknown-result-application",
        )


def test_inspection_and_recovery_keep_unknown_application_blocked(result_scenario) -> None:
    _accepted_unknown(result_scenario)

    with result_scenario.uow() as uow:
        for action in ("inspect", "recover", "record_receipt"):
            require_result_gate(
                uow,
                result_scenario.workspace_id,
                result_scenario.root_id,
                action,
            )

    assert result_scenario.head().active_operation_id is not None


def test_handoff_cannot_capture_during_unknown_application(result_scenario) -> None:
    _accepted_unknown(result_scenario)

    with result_scenario.uow() as uow, pytest.raises(
        ResultError, match="result_operation_in_progress"
    ):
        require_handoff_operation(
            uow,
            worker_item_id=result_scenario.worker_item_ids["a"],
            workspace_id=result_scenario.workspace_id,
            operation=HandoffGateOperation.USER_INPUT,
        )


def test_gate_allows_legacy_root_without_result_head(result_scenario) -> None:
    with result_scenario.uow() as uow:
        require_result_gate(
            uow,
            result_scenario.workspace_id,
            result_scenario.root_id,
            "activate_revision",
        )
