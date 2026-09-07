from __future__ import annotations

from dataclasses import replace
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest

from sidera_control_plane.application.code_result_delivery import resolve_code_inputs
from sidera_control_plane.application.result_ports import CodeInputReceipt
from sidera_control_plane.domain.code_results import (
    CodeInputManifestV1,
    CodeResultSnapshotV1,
)
from sidera_control_plane.domain.result_operations import ResultError

from .result_integration_fixtures import snapshot_mapping

pytest_plugins = ("tests.unit.git_result_fixtures",)


def test_dependency_input_is_not_applied_twice(code_input_case) -> None:
    receipt = code_input_case.materialize_order(("A", "B", "C"))

    assert isinstance(receipt, CodeInputReceipt)
    assert receipt.conflict_paths == ()
    assert code_input_case.read("values.txt") == b"A\nB\n"
    assert code_input_case.read("extra.txt") == b"C\n"
    assert receipt.actual_tree == code_input_case.store.inspect_revision(
        repository_ref="repo://fixture", revision=receipt.actual_commit
    )[1]


def test_dependency_order_is_reconstructed_from_git_ancestry(code_input_case) -> None:
    receipt = code_input_case.materialize_order(("C", "B", "A"))

    assert receipt.conflict_paths == ()
    assert code_input_case.read("values.txt") == b"A\nB\n"
    assert code_input_case.read("extra.txt") == b"C\n"


def test_same_materialization_operation_replays_the_persisted_receipt(
    code_input_case,
) -> None:
    first = code_input_case.materialize_order(("A", "B", "C"))
    replay = code_input_case.materialize_order(("A", "B", "C"))

    assert replay == first
    assert code_input_case.store.inspect_materialization(first.operation_id) == first


def test_replay_can_restore_the_same_receipt_into_a_clean_worktree(
    code_input_case, tmp_path
) -> None:
    first = code_input_case.materialize_order(("A", "B", "C"))
    replay_path = tmp_path / "integration-replay"
    code_input_case.add_worktree(
        replay_path, "integration-replay", code_input_case.base_commit
    )
    checkout = code_input_case.workspace_manifest.checkouts[0]
    replay_manifest = replace(
        code_input_case.workspace_manifest,
        checkouts=(replace(checkout, branch="integration-replay"),),
    )
    replay = code_input_case.store.materialize(
        workspace_manifest=replay_manifest,
        code_inputs=code_input_case.manifest_for(("A", "B", "C")),
        operation_id=first.operation_id,
    )

    assert replay == first
    assert (replay_path / "values.txt").read_bytes() == b"A\nB\n"
    assert (replay_path / "extra.txt").read_bytes() == b"C\n"


def test_materialization_rejects_a_source_tree_that_is_not_in_the_repository(
    code_input_case,
) -> None:
    source = replace(code_input_case.sources["A"], input_tree="f" * 40)
    manifest = code_input_case.manifest_for(("A",), sources=(source,))

    with pytest.raises(ResultError, match="code_input_source_tree_invalid"):
        code_input_case.store.materialize(
            workspace_manifest=code_input_case.workspace_manifest,
            code_inputs=manifest,
            operation_id=uuid4(),
        )


def test_materialization_rejects_a_cross_repository_manifest(code_input_case) -> None:
    manifest = code_input_case.manifest_for(("A",), repository_ref="repo://other")

    with pytest.raises(ResultError, match="code_repository_mismatch"):
        code_input_case.store.materialize(
            workspace_manifest=code_input_case.workspace_manifest,
            code_inputs=manifest,
            operation_id=uuid4(),
        )


def test_conflicting_source_delta_is_visible_and_does_not_publish_a_result(
    code_input_case,
) -> None:
    receipt = code_input_case.store.materialize(
        workspace_manifest=code_input_case.workspace_manifest,
        code_inputs=code_input_case.manifest_for(("A", "D")),
        operation_id=uuid4(),
    )

    assert receipt.conflict_paths == ("values.txt",)
    assert receipt.actual_commit == code_input_case.base_commit
    assert receipt.actual_tree == code_input_case.store.inspect_revision(
        repository_ref="repo://fixture", revision=code_input_case.base_commit
    )[1]
    assert not (code_input_case.workspace_path / "values.txt").exists()
    assert code_input_case.store.inspect_materialization(receipt.operation_id) == receipt


def test_resolve_code_inputs_rejects_a_result_not_in_the_fixed_selection() -> None:
    manifest = CodeInputManifestV1.from_mapping(
        {
            "schema_version": 1,
            "workspace_id": "20000000-0000-0000-0000-000000000001",
            "root_task_id": "30000000-0000-0000-0000-000000000001",
            "request_id": "50000000-0000-0000-0000-000000000002",
            "execution_id": "60000000-0000-0000-0000-000000000003",
            "selection_id": "b0000000-0000-0000-0000-000000000002",
            "selection_hash": "3" * 64,
            "repository_ref": "repo://integration-test",
            "base_commit": "a" * 40,
            "sources": [
                {
                    "result_id": "10000000-0000-0000-0000-000000000001",
                    "result_hash": "4" * 64,
                    "input_commit": "a" * 40,
                    "input_tree": "b" * 40,
                    "result_commit": "c" * 40,
                    "result_tree": "d" * 40,
                }
            ],
            "input_hash": "6a885c08e7d469482fb1e9e1b7d8719086063960fce87428aa120672e649ebec",
        }
    )
    snapshot = CodeResultSnapshotV1.from_mapping(snapshot_mapping())
    unselected_execution_id = UUID("60000000-0000-0000-0000-000000000099")
    selection = SimpleNamespace(
        selection_hash=manifest.selection_hash,
        workspace_id=manifest.workspace_id,
        root_task_id=manifest.root_task_id,
        target_execution_request_id=manifest.request_id,
        groups=(
            SimpleNamespace(
                sources=(SimpleNamespace(effective_execution_id=unselected_execution_id),)
            ),
        ),
        hard_inputs=(),
    )
    uow = SimpleNamespace(
        input_selections=SimpleNamespace(
            get=lambda workspace_id, selection_id: selection
        ),
        results=SimpleNamespace(
            get_record=lambda kind, record_id, workspace_id: snapshot
        ),
    )

    with pytest.raises(ResultError, match="code_input_result_not_selected"):
        resolve_code_inputs(uow, manifest)
