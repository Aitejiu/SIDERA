"""Deterministic recovery and limit evidence for the R6 acceptance path."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Literal
from uuid import UUID

from .contracts import RecoverySnapshot, RuntimeLimitEvidence, ScenarioEvidence
from .evidence import EvidenceBuilder


def build_recovery_evidence(
    builder: EvidenceBuilder,
    snapshot: RecoverySnapshot,
) -> ScenarioEvidence:
    failures: list[str] = []
    incomplete: list[str] = []
    checks: list[str] = []

    if not snapshot.same_run_identity:
        failures.append("recovery_run_identity_changed")
    else:
        checks.append("recovery_run_identity_preserved")
    if snapshot.operation_id != snapshot.operation_id_after_restart:
        failures.append("recovery_operation_identity_changed")
    else:
        checks.append("recovery_operation_identity_preserved")
    if snapshot.duplicate_execution_count > 1:
        failures.append("recovery_duplicate_execution")
    else:
        checks.append("recovery_no_duplicate_execution")
    if snapshot.candidate_hash_before != snapshot.candidate_hash_after:
        failures.append("recovery_candidate_changed")
    elif snapshot.candidate_hash_before is None:
        incomplete.append("recovery_candidate_unavailable")
    else:
        checks.append("recovery_candidate_preserved")
    if snapshot.pending_acceptance_before != snapshot.pending_acceptance_after:
        failures.append("recovery_acceptance_state_changed")
    else:
        checks.append("recovery_acceptance_state_preserved")

    if snapshot.unknown_outcome_observed:
        if snapshot.replacement_started:
            failures.append("unknown_outcome_relaunched")
        elif snapshot.unknown_reconciled:
            checks.append("unknown_outcome_reconciled")
        else:
            incomplete.append("unknown_outcome_requires_reconciliation")
    else:
        checks.append("no_unknown_outcome")
    if snapshot.replacement_started and not snapshot.provider_identity_proven:
        failures.append("unverified_provider_replacement")
    if snapshot.execution_count > snapshot.execution_limit:
        incomplete.append("execution_limit_exceeded")
    else:
        checks.append("execution_limit_respected")
    if snapshot.elapsed_seconds > snapshot.deadline_seconds:
        incomplete.append("recovery_deadline_exceeded")
    else:
        checks.append("recovery_deadline_respected")
    if snapshot.state_after == "failed":
        failures.append("recovery_state_failed")
    elif snapshot.state_after == "unknown":
        incomplete.append("recovery_state_unknown")
    else:
        checks.append("recovery_state_observed")

    failure_code = (failures or incomplete or [None])[0]
    return builder.finish(
        real_tests=1 if not failures and not incomplete else 0,
        failed=1 if failures else 0,
        incomplete=bool(incomplete and not failures),
        check_codes=tuple(checks + failures + incomplete),
        command_ids=(snapshot.command_id,),
        root_task_id=snapshot.root_task_id,
        failure_code_override=failure_code,
    )


def build_runtime_limit_evidence(
    builder: EvidenceBuilder,
    evidence: RuntimeLimitEvidence,
) -> ScenarioEvidence:
    failures: list[str] = []
    incomplete: list[str] = []
    checks: list[str] = []
    if evidence.observed_peak_worker_capacity > evidence.configured_worker_capacity:
        failures.append("worker_capacity_exceeded")
    else:
        checks.append("worker_capacity_respected")
    if evidence.observed_retry_count > evidence.configured_retry_limit:
        incomplete.append("retry_limit_exceeded")
    else:
        checks.append("retry_limit_respected")
    if evidence.observed_elapsed_seconds > evidence.session_deadline_seconds:
        incomplete.append("session_deadline_exceeded")
    else:
        checks.append("session_deadline_respected")
    if not evidence.deadline_preserved_across_restart:
        failures.append("session_deadline_reset_on_restart")
    else:
        checks.append("session_deadline_preserved_on_restart")
    if not evidence.unknown_relaunch_blocked:
        failures.append("unknown_operation_relaunched")
    else:
        checks.append("unknown_operation_relaunch_blocked")
    if evidence.usage_status == "available":
        checks.append("usage_observed")
    else:
        incomplete.append("usage_unavailable")

    failure_code = (failures or incomplete or [None])[0]
    return builder.finish(
        real_tests=1 if not failures and not incomplete else 0,
        failed=1 if failures else 0,
        incomplete=bool(incomplete and not failures),
        check_codes=tuple(checks + failures + incomplete),
        failure_code_override=failure_code,
    )


def run_recovery(
    manifest: Mapping[str, object],
    *,
    snapshot: RecoverySnapshot | None = None,
    evidence_kind: Literal["contract", "real_user_flow"] = "contract",
) -> ScenarioEvidence:
    builder = EvidenceBuilder(
        run_id=_uuid(manifest.get("run_id")),
        scenario="recovery",
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
            check_codes=("recovery_snapshot_not_supplied",),
        )
    return build_recovery_evidence(builder, snapshot)


def _uuid(value: object) -> UUID:
    try:
        return UUID(str(value))
    except (TypeError, ValueError) as exc:
        raise ValueError("manifest run_id is invalid") from exc


def _text(value: object, default: str) -> str:
    return value if isinstance(value, str) and value else default
