from __future__ import annotations

import hashlib
import json
from copy import deepcopy
from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import UUID

from sidera_control_plane.domain.code_results import CodeResultSnapshotV1
from sidera_control_plane.domain.result_operations import TaskResultHead


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


def proposal_v6_mapping() -> dict[str, object]:
    """Return a V6 proposal with an explicit result integration contract."""

    from .result_input_fixtures import proposal_v5_mapping

    value = deepcopy(proposal_v5_mapping())
    value["schema_version"] = 6
    spec = integration_mapping()
    spec["integration_worker_item_key"] = "review-worker"
    spec["rework_policy"] = {
        "max_auto_rework_rounds": 2,
        "max_auto_rework_seconds": 1800,
        "allowed_worker_item_keys": ["build-worker", "review-worker"],
    }
    value["integration_spec"] = spec
    return value


def revision_v6_mapping() -> dict[str, object]:
    """Return a V6 revision with an explicit result integration contract."""

    from .result_input_fixtures import revision_v5_mapping

    value = deepcopy(revision_v5_mapping())
    value["schema_version"] = 6
    spec = integration_mapping()
    spec["integration_worker_item_key"] = "summary"
    spec["input_group_keys"] = ["research"]
    spec["rework_policy"] = {
        "max_auto_rework_rounds": 2,
        "max_auto_rework_seconds": 1800,
        "allowed_worker_item_keys": ["a", "b", "c", "summary"],
    }
    value["integration_spec"] = spec
    return value


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


def _snapshot_content_hash(value: dict[str, object]) -> str:
    content = {
        "schema_version": value["schema_version"],
        "workspace_id": value["workspace_id"],
        "root_task_id": value["root_task_id"],
        "worker_item_id": value["worker_item_id"],
        "request_id": value["request_id"],
        "execution_id": value["execution_id"],
        "repository_ref": value["repository_ref"],
        "input_commit": value["input_commit"],
        "input_tree": value["input_tree"],
        "result_commit": value["result_commit"],
        "result_tree": value["result_tree"],
        "consumed_result_ids": value["consumed_result_ids"],
        "artifact_ids": value["artifact_ids"],
        "capture_operation_id": value["capture_operation_id"],
    }
    return hashlib.sha256(
        json.dumps(
            content,
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
    ).hexdigest()


def _impact_snapshot(**overrides: object) -> CodeResultSnapshotV1:
    value = snapshot_mapping(**overrides)
    value["content_hash"] = _snapshot_content_hash(value)
    return CodeResultSnapshotV1.from_mapping(value)


@dataclass(frozen=True, slots=True)
class ReworkImpactCase:
    a1: CodeResultSnapshotV1
    a2: CodeResultSnapshotV1
    b: CodeResultSnapshotV1
    c: CodeResultSnapshotV1
    hard_edges: tuple[tuple[UUID, UUID], ...]

    @property
    def a_worker_id(self) -> UUID:
        return self.a1.worker_item_id

    @property
    def b_worker_id(self) -> UUID:
        return self.b.worker_item_id

    @property
    def c_worker_id(self) -> UUID:
        return self.c.worker_item_id

    @property
    def provenance(self) -> dict[UUID, CodeResultSnapshotV1]:
        return {item.id: item for item in (self.a1, self.b, self.c)}


def rework_impact_case(
    *, replacement_tree: str = "e" * 40, b_consumes_a1: bool = True
) -> ReworkImpactCase:
    a1 = _impact_snapshot(
        id="10000000-0000-0000-0000-000000000101",
        worker_item_id="40000000-0000-0000-0000-000000000101",
        request_id="50000000-0000-0000-0000-000000000101",
        execution_id="60000000-0000-0000-0000-000000000101",
        capture_operation_id="80000000-0000-0000-0000-000000000101",
        result_commit="c" * 40,
        result_tree="d" * 40,
    )
    a2 = _impact_snapshot(
        id="10000000-0000-0000-0000-000000000104",
        worker_item_id="40000000-0000-0000-0000-000000000101",
        request_id="50000000-0000-0000-0000-000000000104",
        execution_id="60000000-0000-0000-0000-000000000104",
        capture_operation_id="80000000-0000-0000-0000-000000000104",
        result_commit="f" * 40,
        result_tree=replacement_tree,
    )
    b = _impact_snapshot(
        id="10000000-0000-0000-0000-000000000102",
        worker_item_id="40000000-0000-0000-0000-000000000102",
        request_id="50000000-0000-0000-0000-000000000102",
        execution_id="60000000-0000-0000-0000-000000000102",
        capture_operation_id="80000000-0000-0000-0000-000000000102",
        result_commit="c" * 40,
        result_tree="e" * 40,
        consumed_result_ids=[str(a1.id)] if b_consumes_a1 else [],
    )
    c = _impact_snapshot(
        id="10000000-0000-0000-0000-000000000103",
        worker_item_id="40000000-0000-0000-0000-000000000103",
        request_id="50000000-0000-0000-0000-000000000103",
        execution_id="60000000-0000-0000-0000-000000000103",
        capture_operation_id="80000000-0000-0000-0000-000000000103",
        result_commit="c" * 40,
        result_tree="e" * 40,
    )
    return ReworkImpactCase(
        a1=a1,
        a2=a2,
        b=b,
        c=c,
        hard_edges=((a1.worker_item_id, b.worker_item_id),),
    )


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


def context_v3_mapping(*, mode: str = "code", **overrides: object) -> dict[str, object]:
    """Return a complete ContextPackage manifest for one exact V3 purpose."""

    if mode not in {"code", "verification"}:
        raise ValueError("context V3 mode must be code or verification")
    value: dict[str, object] = {
        "manifest_schema_version": 3,
        "package_id": "71000000-0000-0000-0000-000000000001",
        "workspace_id": "20000000-0000-0000-0000-000000000001",
        "purpose": "execution",
        "consumer_type": "worker",
        "consumer_id": "40000000-0000-0000-0000-000000000010",
        "worker_item_id": "40000000-0000-0000-0000-000000000001",
        "work_context_revision_id": "41000000-0000-0000-0000-000000000001",
        "context_version": 1,
        "control_plane_fact_snapshot": {
            "execution_request_id": "50000000-0000-0000-0000-000000000002",
            "execution_id": "60000000-0000-0000-0000-000000000003",
        },
        "knowledge_refs": [],
        "artifact_refs": [],
        "evidence_refs": [],
        "budget": {
            "profile_id": "72000000-0000-0000-0000-000000000001",
            "profile_version": 1,
            "max_sections": 8,
            "max_chars": 20_000,
            "max_input_tokens": 4_000,
            "actual_sections": 0,
            "actual_chars": 0,
            "actual_input_tokens": 0,
        },
        "expires_at": "2026-09-07T13:00:00+00:00",
        "idempotency_key": f"context-v3-{mode}",
        "status": "active",
        "created_at": "2026-09-07T12:00:00+00:00",
        "materialized_at": None,
        "revoked_at": None,
        "revocation_reason": None,
        "revocation_scope": None,
        "result_input_manifest": None,
        "code_input_manifest": code_input_manifest_mapping()
        if mode == "code"
        else None,
        "verification_manifest": verification_manifest_mapping()
        if mode == "verification"
        else None,
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


def head_fixture(**overrides: object) -> TaskResultHead:
    value: dict[str, object] = {
        "workspace_id": fixed_uuid("20000000-0000-0000-0000-000000000001"),
        "root_task_id": fixed_uuid("30000000-0000-0000-0000-000000000001"),
        "current_candidate_id": None,
        "current_acceptance_id": None,
        "result_version": 1,
        "active_operation_id": None,
        "auto_rounds_used": 0,
        "auto_rounds_limit": 2,
        "auto_seconds_limit": 1800,
        "auto_started_at": None,
        "auto_deadline": None,
    }
    value.update(overrides)
    return TaskResultHead(**value)  # type: ignore[arg-type]


def apply_operation_mapping(**overrides: object) -> dict[str, object]:
    value: dict[str, object] = {
        "id": "b1000000-0000-0000-0000-000000000001",
        "workspace_id": "20000000-0000-0000-0000-000000000001",
        "root_task_id": "30000000-0000-0000-0000-000000000001",
        "acceptance_id": "a2000000-0000-0000-0000-000000000001",
        "candidate_id": "90000000-0000-0000-0000-000000000001",
        "repository_ref": "repo://integration-test",
        "target_ref": "refs/heads/main",
        "expected_old_commit": "a" * 40,
        "accepted_commit": "c" * 40,
        "status": "pending",
        "attempt": 1,
        "lease_owner": None,
        "lease_token": None,
        "lease_expiry": None,
        "receipt_ref": None,
        "failure_code": None,
        "version": 1,
    }
    value.update(overrides)
    return value
