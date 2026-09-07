from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime
from uuid import UUID

import pytest

from sidera_control_plane.application.result_ports import ResultOperationAttemptV1
from sidera_control_plane.domain.errors import VersionConflict
from sidera_control_plane.domain.result_operations import (
    ResultError,
    ResultOperationStatus,
)

pytestmark = pytest.mark.integration

NOW = datetime(2026, 9, 7, 12, 0, tzinfo=UTC)


def test_snapshot_replay_is_idempotent(result_scenario) -> None:
    value = result_scenario.snapshot()

    with result_scenario.uow() as uow:
        first = uow.results.add_immutable("code_snapshot", value)
        second = uow.results.add_immutable("code_snapshot", value)

    assert first == second == value


def test_same_snapshot_id_with_different_content_is_rejected(result_scenario) -> None:
    value = result_scenario.snapshot()
    changed = result_scenario.snapshot(result_commit="e" * 40)
    changed = replace(changed, id=value.id, capture_operation_id=value.capture_operation_id)

    with result_scenario.uow() as uow:
        uow.results.add_immutable("code_snapshot", value)

    with pytest.raises(ResultError, match="result_record_conflict"), result_scenario.uow() as uow:
        uow.results.add_immutable("code_snapshot", changed)


def test_snapshot_lookup_is_workspace_scoped(result_scenario) -> None:
    value = result_scenario.snapshot()
    with result_scenario.uow() as uow:
        uow.results.add_immutable("code_snapshot", value)

    with result_scenario.uow() as uow:
        assert uow.results.get_record("code_snapshot", value.id, value.workspace_id) == value
        assert uow.results.get_record(
            "code_snapshot", value.id, UUID("00000000-0000-0000-0000-000000000099")
        ) is None


def test_result_head_save_uses_compare_and_swap(result_scenario) -> None:
    initial = result_scenario.head_fixture()
    with result_scenario.uow() as uow:
        uow.results.save_head(initial, expected_version=0)

    with result_scenario.uow() as stale:
        stale_head = stale.results.get_head(
            result_scenario.workspace_id, result_scenario.root_task_id
        )
        assert stale_head == initial
        with result_scenario.uow() as winner:
            winner_head = winner.results.get_head(
                result_scenario.workspace_id, result_scenario.root_task_id
            )
            assert winner_head == initial
            winner.results.save_head(
                replace(winner_head, result_version=2), expected_version=1
            )

        with pytest.raises(VersionConflict):
            stale.results.save_head(replace(stale_head, result_version=2), expected_version=1)


def test_operation_claim_is_unique(result_scenario) -> None:
    operation = ResultOperationAttemptV1(
        operation_id=result_scenario.operation_id,
        workspace_id=result_scenario.workspace_id,
        root_task_id=result_scenario.root_task_id,
        operation_kind="capture",
        attempt=1,
        status=ResultOperationStatus.PENDING,
        payload={"execution_id": str(result_scenario.execution_id)},
        lease_owner=None,
        lease_token=None,
        lease_expiry=None,
        receipt_ref=None,
        failure_code=None,
        version=1,
        created_at=NOW,
        updated_at=NOW,
    )
    with result_scenario.uow() as uow:
        uow.results.save_operation(operation, expected_version=0)

    with result_scenario.uow() as first:
        claimed = first.results.claim_operations("capture", NOW, 1)
        assert len(claimed) == 1
        assert claimed[0].status is ResultOperationStatus.RUNNING

    with result_scenario.uow() as second:
        assert second.results.claim_operations("capture", NOW, 1) == ()
