from __future__ import annotations

from copy import deepcopy
from datetime import UTC, datetime
from uuid import UUID


def integration_mapping() -> dict[str, object]:
    return {
        "schema_version": 1,
        "repository_ref": "repo://integration-test",
        "base_commit": "a" * 40,
        "integration_worker_item_key": "integrate",
        "input_group_keys": ["implementation"],
        "criteria": [
            {
                "key": "login",
                "text": "Expired credentials are rejected",
                "mode": "automatic",
                "check_keys": ["unit"],
            }
        ],
        "checks": [
            {
                "key": "unit",
                "version": 1,
                "argv": ["python3", "-m", "unittest"],
                "cwd": ".",
                "environment_ref": "env://python-test",
                "prepare_argvs": [],
                "timeout_seconds": 60,
                "success_exit_codes": [0],
                "report_paths": [],
                "required": True,
            }
        ],
        "verification_binding": {
            "adapter_module_ref": "00000000-0000-0000-0000-000000000010",
            "adapter_config_version": 1,
            "workspace_module_ref": "00000000-0000-0000-0000-000000000011",
            "workspace_config_version": 1,
        },
        "rework_policy": {
            "max_auto_rework_rounds": 2,
            "max_auto_rework_seconds": 1800,
            "allowed_worker_item_keys": ["login", "integrate"],
        },
        "apply_policy": {
            "apply_on_accept": False,
            "mode": "fast_forward_only",
            "target_ref": None,
        },
        "finalization_policy": "stop_unused",
    }


def independent_mapping() -> dict[str, object]:
    """Return a fresh mapping so tests cannot mutate shared fixture state."""

    return deepcopy(integration_mapping())


def snapshot_mapping(**overrides: object) -> dict[str, object]:
    value: dict[str, object] = {
        "id": "10000000-0000-0000-0000-000000000001",
        "schema_version": 1,
        "workspace_id": "20000000-0000-0000-0000-000000000001",
        "root_task_id": "30000000-0000-0000-0000-000000000001",
        "worker_item_id": "40000000-0000-0000-0000-000000000001",
        "request_id": "50000000-0000-0000-0000-000000000001",
        "execution_id": "60000000-0000-0000-0000-000000000001",
        "repository_ref": "repo://integration-test",
        "input_commit": "a" * 40,
        "input_tree": "b" * 40,
        "result_commit": "c" * 40,
        "result_tree": "d" * 40,
        "consumed_result_ids": [],
        "artifact_ids": ["70000000-0000-0000-0000-000000000001"],
        "capture_operation_id": "80000000-0000-0000-0000-000000000001",
        "content_hash": "7f6a661f24859b1d83d7e9b616da99b79e01a8f231cf09c02456f24a26720193",
        "created_at": datetime(2026, 9, 7, 12, 0, tzinfo=UTC).isoformat(),
    }
    value.update(overrides)
    return value


def candidate_mapping(**overrides: object) -> dict[str, object]:
    value: dict[str, object] = {
        "id": "90000000-0000-0000-0000-000000000001",
        "schema_version": 1,
        "workspace_id": "20000000-0000-0000-0000-000000000001",
        "root_task_id": "30000000-0000-0000-0000-000000000001",
        "plan_snapshot_id": "a0000000-0000-0000-0000-000000000001",
        "plan_content_hash": "1" * 64,
        "integration_execution_id": "60000000-0000-0000-0000-000000000002",
        "base_commit": "a" * 40,
        "source_result_ids": ["10000000-0000-0000-0000-000000000001"],
        "input_selection_id": "b0000000-0000-0000-0000-000000000001",
        "input_selection_hash": "2" * 64,
        "code_result_id": "10000000-0000-0000-0000-000000000001",
        "previous_candidate_id": None,
        "content_hash": "a4a7b7a5c37e1c6231fb37dc17cafcdb8ff6ad5394e2925613e54447baf73cf4",
        "created_at": datetime(2026, 9, 7, 12, 1, tzinfo=UTC).isoformat(),
    }
    value.update(overrides)
    return value


def code_input_manifest_mapping(**overrides: object) -> dict[str, object]:
    value: dict[str, object] = {
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
    value.update(overrides)
    return value


def fixed_uuid(value: str) -> UUID:
    return UUID(value)


def verification_manifest_mapping(**overrides: object) -> dict[str, object]:
    value: dict[str, object] = {
        "schema_version": 1,
        "candidate_id": "90000000-0000-0000-0000-000000000001",
        "candidate_hash": "a4a7b7a5c37e1c6231fb37dc17cafcdb8ff6ad5394e2925613e54447baf73cf4",
        "result_commit": "c" * 40,
        "result_tree": "d" * 40,
        "checks": [
            {
                "key": "unit",
                "version": 1,
                "spec_hash": "5" * 64,
            }
        ],
        "environment_binding": {"ref": "env://python-test", "version": 1},
        "verification_basis_hash": "03cf6c66dec81fbcccab9e239b63fbcacb749589a69ed38222eb186fa2a00f06",
    }
    value.update(overrides)
    return value


def verification_mapping(**overrides: object) -> dict[str, object]:
    value: dict[str, object] = {
        "id": "a1000000-0000-0000-0000-000000000001",
        "schema_version": 1,
        "workspace_id": "20000000-0000-0000-0000-000000000001",
        "root_task_id": "30000000-0000-0000-0000-000000000001",
        "candidate_id": "90000000-0000-0000-0000-000000000001",
        "execution_id": "60000000-0000-0000-0000-000000000004",
        "check_key": "unit",
        "check_version": 1,
        "check_hash": "5" * 64,
        "environment_ref": "env://python-test",
        "environment_hash": "6" * 64,
        "actual_commit": "c" * 40,
        "actual_tree": "d" * 40,
        "operation_id": "80000000-0000-0000-0000-000000000002",
        "attempt": 1,
        "status": "failed",
        "exit_code": 1,
        "started_at": "2026-09-07T12:02:00+00:00",
        "finished_at": "2026-09-07T12:02:01+00:00",
        "artifact_ids": ["70000000-0000-0000-0000-000000000002"],
        "failure_code": "check_exit_nonzero",
    }
    value.update(overrides)
    return value


def acceptance_mapping(**overrides: object) -> dict[str, object]:
    value: dict[str, object] = {
        "id": "a2000000-0000-0000-0000-000000000001",
        "workspace_id": "20000000-0000-0000-0000-000000000001",
        "root_task_id": "30000000-0000-0000-0000-000000000001",
        "candidate_id": "90000000-0000-0000-0000-000000000001",
        "candidate_hash": "a4a7b7a5c37e1c6231fb37dc17cafcdb8ff6ad5394e2925613e54447baf73cf4",
        "plan_content_hash": "1" * 64,
        "acceptance_basis_hash": "7" * 64,
        "result_version": 1,
        "criteria": [
            {
                "criterion_key": "login",
                "decision": "exception_accepted",
                "reason": "Known fixture limitation",
            }
        ],
        "actor_type": "local",
        "actor_id": "aitejiu",
        "command_id": "a3000000-0000-0000-0000-000000000001",
        "created_at": "2026-09-07T12:03:00+00:00",
    }
    value.update(overrides)
    return value
