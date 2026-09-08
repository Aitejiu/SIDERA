"""Provider-neutral evidence for scoped WorkerItem intervention and rework."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Literal
from uuid import UUID

from .contracts import InterventionSnapshot, ScenarioEvidence
from .evidence import EvidenceBuilder


def build_intervention_evidence(
    builder: EvidenceBuilder,
    snapshot: InterventionSnapshot,
) -> ScenarioEvidence:
    failures: list[str] = []
    incomplete: list[str] = []
    checks: list[str] = []

    if snapshot.input_target_worker_item_id != snapshot.worker_item_id:
        failures.append("intervention_target_mismatch")
    else:
        checks.append("intervention_target_bound")
    if snapshot.plan_generation_before != snapshot.plan_generation_after:
        failures.append("local_intervention_changed_plan_generation")
    else:
        checks.append("plan_generation_unchanged")
    if set(snapshot.sibling_input_ids_before) != set(snapshot.sibling_input_ids_after):
        failures.append("sibling_inputs_changed")
    else:
        checks.append("sibling_inputs_unchanged")
    if set(snapshot.unaffected_result_ids_before) != set(snapshot.unaffected_result_ids_after):
        failures.append("unaffected_results_changed")
    else:
        checks.append("unaffected_results_preserved")

    if snapshot.input_status in {"failed", "unknown", "queued", "accepted"}:
        incomplete.append("worker_input_not_applied")
    elif snapshot.adapter_acknowledged:
        checks.append("worker_input_acknowledged")
    else:
        incomplete.append("adapter_ack_missing")
    if snapshot.observed_output_id is None or snapshot.observed_output_hash is None:
        incomplete.append("worker_output_not_observed")
    else:
        checks.append("worker_output_observed")

    if snapshot.failed_check_id is None:
        incomplete.append("rework_check_failure_not_observed")
    elif (
        snapshot.failed_check_candidate_id != snapshot.source_candidate_id
        or snapshot.failed_check_candidate_hash != snapshot.source_candidate_hash
    ):
        failures.append("rework_check_candidate_mismatch")
    else:
        checks.append("rework_check_bound_to_source_candidate")

    if snapshot.rework_record_id is None:
        incomplete.append("rework_record_not_observed")
    else:
        checks.append("rework_record_observed")
    if snapshot.rework_worker_item_id != snapshot.worker_item_id:
        failures.append("rework_worker_item_changed")
    elif snapshot.rework_worker_item_id is not None:
        checks.append("rework_kept_worker_item")
    if snapshot.rework_generation != snapshot.plan_generation_before:
        failures.append("rework_generation_changed")
    else:
        checks.append("rework_kept_generation")
    if snapshot.rework_execution_id is None or snapshot.rework_session_id is None:
        incomplete.append("rework_execution_session_not_observed")
    else:
        checks.append("rework_execution_session_observed")
    if snapshot.replacement_candidate_id is None or snapshot.replacement_candidate_hash is None:
        incomplete.append("replacement_candidate_not_observed")
    elif snapshot.replacement_candidate_id == snapshot.source_candidate_id:
        failures.append("replacement_candidate_reused")
    else:
        checks.append("replacement_candidate_is_new")

    if snapshot.rework_round > snapshot.rework_round_limit or snapshot.rework_elapsed_seconds > snapshot.rework_time_limit_seconds:
        incomplete.append("rework_budget_exhausted")
    else:
        checks.append("rework_budget_within_limit")

    failure_code = (failures or incomplete or [None])[0]
    execution_ids = tuple(
        item for item in (snapshot.rework_execution_id,) if item is not None
    )
    session_ids = tuple(item for item in (snapshot.rework_session_id,) if item is not None)
    return builder.finish(
        real_tests=1 if not failures and not incomplete else 0,
        failed=1 if failures else 0,
        incomplete=bool(incomplete and not failures),
        check_codes=tuple(checks + failures + incomplete),
        execution_ids=execution_ids,
        session_ids=session_ids,
        root_task_id=snapshot.root_task_id,
        failure_code_override=failure_code,
    )


def run_intervention(
    manifest: Mapping[str, object],
    *,
    snapshot: InterventionSnapshot | None = None,
    evidence_kind: Literal["contract", "real_user_flow"] = "contract",
) -> ScenarioEvidence:
    builder = EvidenceBuilder(
        run_id=_uuid(manifest.get("run_id")),
        scenario="intervention",
        evidence_kind=evidence_kind,
        code_commit=_text(manifest.get("code_commit"), "0" * 40),
        runtime_fingerprint=_text(
            manifest.get("runtime_fingerprint"), "0" * 64
        ),
    )
    if snapshot is None:
        return builder.finish(
            real_tests=0,
            incomplete=True,
            check_codes=("intervention_snapshot_not_supplied",),
        )
    return build_intervention_evidence(builder, snapshot)


def _uuid(value: object) -> UUID:
    try:
        return UUID(str(value))
    except (TypeError, ValueError) as exc:
        raise ValueError("manifest run_id is invalid") from exc


def _text(value: object, default: str) -> str:
    return value if isinstance(value, str) and value else default
