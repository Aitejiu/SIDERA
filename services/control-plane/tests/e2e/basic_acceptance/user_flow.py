"""Provider-neutral facts and evidence for the confirmed multi-worker path."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Literal
from uuid import UUID

from .contracts import ScenarioEvidence, UserFlowSnapshot
from .evidence import EvidenceBuilder


def build_user_flow_evidence(
    builder: EvidenceBuilder,
    snapshot: UserFlowSnapshot,
) -> ScenarioEvidence:
    failures: list[str] = []
    incomplete: list[str] = []
    checks: list[str] = []

    if len(snapshot.source_execution_ids) < 2:
        failures.append("source_execution_ids_incomplete")
    else:
        checks.append("source_executions_observed")
    if len(snapshot.source_session_ids) != len(snapshot.source_execution_ids):
        failures.append("source_session_binding_missing")
    else:
        checks.append("source_sessions_bound")
    if snapshot.integration_session_id is None:
        failures.append("integration_session_missing")
    else:
        checks.append("integration_session_bound")

    if snapshot.candidate_selection_hash != snapshot.input_selection_hash:
        failures.append("candidate_input_selection_mismatch")
    else:
        checks.append("candidate_selection_observed")
    if not snapshot.dependency_satisfied:
        incomplete.append("dependency_fact_missing")
    else:
        checks.append("integration_dependency_satisfied")

    if snapshot.verification_id is None:
        incomplete.append("verification_not_observed")
    elif snapshot.verification_status != "passed":
        incomplete.append("verification_not_passed")
    elif (
        snapshot.verification_candidate_id != snapshot.candidate_id
        or snapshot.verification_candidate_hash != snapshot.candidate_hash
    ):
        failures.append("verification_candidate_mismatch")
    else:
        checks.append("verification_bound_to_candidate")

    if not snapshot.accepted or snapshot.acceptance_id is None:
        incomplete.append("acceptance_not_observed")
    elif (
        snapshot.accepted_candidate_id != snapshot.candidate_id
        or snapshot.accepted_candidate_hash != snapshot.candidate_hash
    ):
        failures.append("acceptance_candidate_mismatch")
    else:
        checks.append("candidate_accepted")

    if snapshot.restored_tree_hash != snapshot.accepted_tree_hash:
        failures.append("accepted_tree_restore_mismatch")
    elif not snapshot.restore_check_passed:
        failures.append("accepted_tree_restore_check_failed")
    else:
        checks.append("accepted_tree_restored")

    overlap = _source_overlap(snapshot)
    if overlap is True:
        checks.append("parallel_overlap_observed")
    elif overlap is False:
        incomplete.append("parallel_overlap_not_observed")
    else:
        incomplete.append("parallel_window_missing")

    execution_ids = (*snapshot.source_execution_ids, snapshot.integration_execution_id)
    session_ids = (*snapshot.source_session_ids, snapshot.integration_session_id)
    failure_code = (failures or incomplete or [None])[0]
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


def run_user_flow(
    manifest: Mapping[str, object],
    *,
    snapshot: UserFlowSnapshot | None = None,
    evidence_kind: Literal["contract", "real_user_flow"] = "contract",
) -> ScenarioEvidence:
    """Return evidence only from a supplied public snapshot; never invent facts."""

    builder = EvidenceBuilder(
        run_id=_uuid(manifest.get("run_id")),
        scenario="user-flow",
        evidence_kind=evidence_kind,
        code_commit=_text_or_default(manifest.get("code_commit"), "0" * 40),
        runtime_fingerprint=_text_or_default(
            manifest.get("runtime_fingerprint"), "0" * 64
        ),
    )
    if snapshot is not None:
        return build_user_flow_evidence(builder, snapshot)
    return builder.finish(
        real_tests=0,
        incomplete=True,
        check_codes=("user_flow_snapshot_not_supplied",),
    )


def _source_overlap(snapshot: UserFlowSnapshot) -> bool | None:
    windows = tuple(
        item
        for item in snapshot.source_windows
        if item.execution_id in snapshot.source_execution_ids
    )
    if len(windows) < 2:
        return None
    latest_start = max(item.started_at for item in windows)
    earliest_finish = min(item.finished_at for item in windows)
    return latest_start < earliest_finish


def _uuid(value: object) -> UUID:
    try:
        return UUID(str(value))
    except (TypeError, ValueError) as exc:
        raise ValueError("manifest run_id is invalid") from exc


def _text_or_default(value: object, default: str) -> str:
    return value if isinstance(value, str) and value else default
