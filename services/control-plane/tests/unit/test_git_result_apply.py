from __future__ import annotations

import subprocess
from typing import TYPE_CHECKING

import pytest

from sidera_control_plane.application.result_ports import ApplyOutcome, ApplyReceipt
from sidera_control_plane.domain.result_operations import ResultError

if TYPE_CHECKING:
    from .git_result_fixtures import GitApplyCase

pytest_plugins = ("tests.unit.git_result_fixtures",)


def test_apply_fast_forwards_target_and_persists_receipt(
    apply_case: GitApplyCase,
) -> None:
    receipt = apply_case.store.apply(apply_case.operation)

    assert isinstance(receipt, ApplyReceipt)
    assert receipt.operation_id == apply_case.operation.id
    assert receipt.candidate_id == apply_case.operation.candidate_id
    assert receipt.acceptance_id == apply_case.operation.acceptance_id
    assert receipt.repository_ref == apply_case.operation.repository_ref
    assert receipt.target_ref == apply_case.operation.target_ref
    assert receipt.old_commit == apply_case.base_commit
    assert receipt.new_commit == apply_case.accepted_commit
    assert receipt.outcome is ApplyOutcome.SUCCEEDED
    assert receipt.receipt_ref == f"refs/sidera/applications/{apply_case.operation.id}"
    assert receipt.old_tree != receipt.new_tree
    assert apply_case.target_commit() == apply_case.accepted_commit
    assert apply_case.store.inspect_apply(apply_case.operation) == receipt


def test_apply_replay_uses_receipt_after_target_moves(
    apply_case: GitApplyCase,
) -> None:
    receipt = apply_case.store.apply(apply_case.operation)
    advanced_target = apply_case.advance_target_after_acceptance()

    replay = apply_case.store.apply(apply_case.operation)

    assert replay == receipt
    assert apply_case.store.inspect_apply(apply_case.operation) == receipt
    assert advanced_target != receipt.new_commit
    assert apply_case.target_commit() == advanced_target


def test_target_drift_is_rejected_without_creating_receipt(
    apply_case: GitApplyCase,
) -> None:
    advanced_target = apply_case.advance_target_after_acceptance()

    with pytest.raises(ResultError, match="result_apply_target_changed"):
        apply_case.store.apply(apply_case.operation)

    assert apply_case.target_commit() == advanced_target
    assert apply_case.store.inspect_apply(apply_case.operation) is None


def test_target_checked_out_is_rejected_without_mutating_ref(
    apply_case: GitApplyCase,
) -> None:
    worktree = apply_case.checked_out_target()
    before = apply_case.target_commit()
    try:
        with pytest.raises(ResultError, match="result_apply_target_checked_out"):
            apply_case.store.apply(apply_case.operation)
    finally:
        subprocess.run(
            ["git", "-C", str(apply_case.repository), "worktree", "remove", str(worktree)],
            check=True,
            capture_output=True,
        )

    assert apply_case.target_commit() == before
    assert apply_case.store.inspect_apply(apply_case.operation) is None


def test_non_fast_forward_is_rejected_without_mutating_ref(
    apply_case: GitApplyCase,
) -> None:
    operation = apply_case.operation_with_unrelated_commit()
    before = apply_case.target_commit()

    with pytest.raises(ResultError, match="result_apply_not_fast_forward"):
        apply_case.store.apply(operation)

    assert apply_case.target_commit() == before
    assert apply_case.store.inspect_apply(operation) is None


def test_corrupt_receipt_is_not_replayed(
    apply_case: GitApplyCase,
) -> None:
    apply_case.store.apply(apply_case.operation)
    apply_case.corrupt_receipt()

    with pytest.raises(ResultError, match="result_apply_receipt_corrupt"):
        apply_case.store.inspect_apply(apply_case.operation)
    with pytest.raises(ResultError, match="result_apply_receipt_corrupt"):
        apply_case.store.apply(apply_case.operation)

    assert apply_case.target_commit() == apply_case.accepted_commit


def test_matching_head_without_receipt_is_not_success(
    apply_case: GitApplyCase,
) -> None:
    apply_case.set_target_to_accepted_without_receipt()

    with pytest.raises(ResultError, match="result_apply_target_changed"):
        apply_case.store.apply(apply_case.operation)

    assert apply_case.target_commit() == apply_case.accepted_commit
    assert apply_case.store.inspect_apply(apply_case.operation) is None
