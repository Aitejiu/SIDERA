from __future__ import annotations

from uuid import uuid4

from tests.e2e.basic_acceptance.contracts import SecondAdapterSnapshot
from tests.e2e.basic_acceptance.evidence import EvidenceBuilder
from tests.e2e.basic_acceptance.second_adapter import (
    build_second_adapter_evidence,
    run_second_adapter,
)


def _manifest() -> dict[str, object]:
    return {
        "run_id": str(uuid4()),
        "code_commit": "a" * 40,
        "runtime_fingerprint": "b" * 64,
    }


def _snapshot(**overrides: object) -> SecondAdapterSnapshot:
    values: dict[str, object] = {
        "run_id": uuid4(),
        "root_task_id": uuid4(),
        "workspace_id": uuid4(),
        "source_worker_item_id": uuid4(),
        "target_worker_item_id": None,
        "source_adapter_type": "opencode",
        "target_adapter_type": "dsh-acp",
        "source_implementation": "OpenCodeACPAdapter",
        "target_implementation": "DshACPAdapter",
        "source_execution_id": uuid4(),
        "target_execution_id": uuid4(),
        "source_session_id": uuid4(),
        "target_session_id": uuid4(),
        "source_real_session": True,
        "target_real_session": True,
        "source_checkpoint_observed": True,
        "source_stop_observed": True,
        "source_unknown_reconciled": True,
        "target_started_held": True,
        "target_prompt_count_before_enable": 0,
        "target_enable_observed": True,
        "target_prompt_count_after_enable": 1,
        "target_single_writer_observed": True,
        "workspace_reused_exact": True,
        "context_binding_observed": True,
        "source_context_marker_observed": True,
        "target_context_marker_observed": True,
        "target_result_observed": True,
        "trace_usage_gap_explicit": True,
    }
    values["target_worker_item_id"] = values["source_worker_item_id"]
    values.update(overrides)
    return SecondAdapterSnapshot.model_validate(values)


def _builder(manifest: dict[str, object]):
    return EvidenceBuilder(
        run_id=uuid4(),
        scenario="second-adapter",
        evidence_kind="contract",
        code_commit=str(manifest["code_commit"]),
        runtime_fingerprint=str(manifest["runtime_fingerprint"]),
    )


def test_second_adapter_requires_a_real_target_and_all_handoff_facts() -> None:
    manifest = _manifest()
    evidence = build_second_adapter_evidence(_builder(manifest), _snapshot())
    assert evidence.status == "PASS"
    assert evidence.failure_code is None
    assert "target_held_without_prompt" in evidence.check_codes


def test_second_adapter_rejects_same_implementation_even_with_target_inventory() -> None:
    manifest = _manifest()
    evidence = build_second_adapter_evidence(
        _builder(manifest),
        _snapshot(target_implementation="OpenCodeACPAdapter"),
    )
    assert evidence.status == "FAIL"
    assert evidence.failure_code == "source_target_implementation_same"


def test_second_adapter_missing_snapshot_is_incomplete() -> None:
    evidence = run_second_adapter(_manifest())
    assert evidence.status == "INCOMPLETE"
    assert evidence.failure_code == "second_adapter_snapshot_not_supplied"


def test_second_adapter_cannot_pass_before_writer_enable() -> None:
    manifest = _manifest()
    evidence = build_second_adapter_evidence(
        _builder(manifest),
        _snapshot(target_started_held=False, target_prompt_count_before_enable=1),
    )
    assert evidence.status == "FAIL"
    assert "target_started_with_writer_open" in evidence.check_codes
