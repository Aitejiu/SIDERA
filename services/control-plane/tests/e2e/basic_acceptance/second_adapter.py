"""Evidence classifier for a distinct real target WorkerAdapter handoff."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Literal
from uuid import UUID

from sidera_control_plane.gateway_worker import worker_adapter_inventory

from .contracts import ScenarioEvidence, SecondAdapterSnapshot
from .evidence import EvidenceBuilder


def build_second_adapter_evidence(
    builder: EvidenceBuilder,
    snapshot: SecondAdapterSnapshot,
) -> ScenarioEvidence:
    failures: list[str] = []
    incomplete: list[str] = []
    checks: list[str] = []
    registrations = {item.adapter_type: item for item in worker_adapter_inventory()}
    target = registrations.get(snapshot.target_adapter_type)
    source = registrations.get(snapshot.source_adapter_type)

    if source is None:
        failures.append("source_adapter_not_registered")
    elif source.is_fake or source.is_local_verification:
        failures.append("source_adapter_is_not_a_real_source")
    else:
        checks.append("source_adapter_registered")
    if target is None:
        incomplete.append("target_adapter_not_registered")
    elif target.is_fake or target.is_local_verification or target.delegates_to_opencode:
        failures.append("target_adapter_is_not_distinct_real_agent")
    elif "handoff-target:v1" not in target.capability_versions:
        incomplete.append("target_handoff_capability_not_declared")
    else:
        checks.append("target_adapter_registered_and_qualified")

    if snapshot.source_adapter_type == snapshot.target_adapter_type:
        failures.append("source_target_adapter_type_same")
    elif snapshot.source_implementation == snapshot.target_implementation:
        failures.append("source_target_implementation_same")
    else:
        checks.append("distinct_adapter_implementation_observed")
    if snapshot.source_worker_item_id != snapshot.target_worker_item_id:
        failures.append("worker_item_changed_during_handoff")
    else:
        checks.append("worker_item_identity_preserved")
    if snapshot.source_execution_id == snapshot.target_execution_id:
        failures.append("source_target_execution_same")
    else:
        checks.append("source_target_execution_distinct")
    if snapshot.source_session_id == snapshot.target_session_id:
        failures.append("source_target_session_same")
    else:
        checks.append("source_target_session_distinct")

    if not snapshot.source_real_session:
        incomplete.append("source_real_session_not_observed")
    else:
        checks.append("source_real_session_observed")
    if not snapshot.target_real_session:
        incomplete.append("target_real_session_not_observed")
    else:
        checks.append("target_real_session_observed")
    if not snapshot.source_checkpoint_observed:
        incomplete.append("source_checkpoint_not_observed")
    else:
        checks.append("source_checkpoint_observed")
    if not snapshot.source_stop_observed:
        incomplete.append("source_stop_not_observed")
    else:
        checks.append("source_stop_observed")
    if not snapshot.source_unknown_reconciled:
        incomplete.append("source_unknown_reconciliation_not_observed")
    else:
        checks.append("source_unknown_reconciled")

    if not snapshot.target_started_held:
        failures.append("target_started_with_writer_open")
    elif snapshot.target_prompt_count_before_enable != 0:
        failures.append("target_prompted_before_writer_enable")
    else:
        checks.append("target_held_without_prompt")
    if not snapshot.target_enable_observed:
        incomplete.append("target_writer_enable_not_observed")
    else:
        checks.append("target_writer_enable_observed")
    if snapshot.target_prompt_count_after_enable < 1:
        incomplete.append("target_enable_prompt_not_observed")
    else:
        checks.append("target_enable_prompt_observed")
    if not snapshot.target_single_writer_observed:
        failures.append("target_single_writer_not_proven")
    else:
        checks.append("target_single_writer_observed")
    if not snapshot.workspace_reused_exact:
        failures.append("workspace_reuse_exact_not_proven")
    else:
        checks.append("workspace_reuse_exact_observed")
    if not snapshot.context_binding_observed:
        failures.append("target_context_binding_missing")
    else:
        checks.append("target_context_binding_observed")
    if not snapshot.source_context_marker_observed or not snapshot.target_context_marker_observed:
        incomplete.append("handoff_context_marker_not_observed")
    else:
        checks.append("handoff_context_marker_observed")
    if not snapshot.target_result_observed:
        incomplete.append("target_result_not_observed")
    else:
        checks.append("target_result_observed")
    if not snapshot.trace_usage_gap_explicit:
        incomplete.append("trace_usage_gap_not_explicit")
    else:
        checks.append("trace_usage_boundary_explicit")

    failure_code = (failures or incomplete or [None])[0]
    return builder.finish(
        real_tests=1 if not failures and not incomplete else 0,
        failed=1 if failures else 0,
        incomplete=bool(incomplete and not failures),
        check_codes=tuple(checks + failures + incomplete),
        execution_ids=(snapshot.source_execution_id, snapshot.target_execution_id),
        session_ids=(snapshot.source_session_id, snapshot.target_session_id),
        root_task_id=snapshot.root_task_id,
        failure_code_override=failure_code,
    )


def run_second_adapter(
    manifest: Mapping[str, object],
    *,
    snapshot: SecondAdapterSnapshot | None = None,
    evidence_kind: Literal["contract", "real_user_flow"] = "contract",
) -> ScenarioEvidence:
    run_id = _uuid(manifest.get("run_id"))
    builder = EvidenceBuilder(
        run_id=run_id,
        scenario="second-adapter",
        evidence_kind=evidence_kind,
        code_commit=_text(manifest.get("code_commit"), "0" * 40),
        runtime_fingerprint=_text(manifest.get("runtime_fingerprint"), "0" * 64),
    )
    if snapshot is None:
        return builder.finish(
            real_tests=0,
            incomplete=True,
            check_codes=("second_adapter_snapshot_not_supplied",),
            failure_code_override="second_adapter_snapshot_not_supplied",
        )
    if snapshot.run_id != run_id:
        return builder.finish(
            real_tests=0,
            failed=1,
            check_codes=("second_adapter_snapshot_run_id_mismatch",),
            failure_code_override="second_adapter_snapshot_run_id_mismatch",
        )
    return build_second_adapter_evidence(builder, snapshot)


def _uuid(value: object) -> UUID:
    try:
        return UUID(str(value))
    except (TypeError, ValueError) as exc:
        raise ValueError("manifest run_id is invalid") from exc


def _text(value: object, default: str) -> str:
    return value if isinstance(value, str) and value else default
