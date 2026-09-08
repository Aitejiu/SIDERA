"""Provider-neutral evidence for a Main Agent initiated plan revision."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Literal
from uuid import UUID

from .contracts import RevisionSnapshot, ScenarioEvidence
from .evidence import EvidenceBuilder


def build_revision_evidence(
    builder: EvidenceBuilder,
    snapshot: RevisionSnapshot,
) -> ScenarioEvidence:
    failures: list[str] = []
    incomplete: list[str] = []
    checks: list[str] = []

    if not snapshot.main_agent_tool_call_observed:
        incomplete.append("main_agent_revision_tool_not_observed")
    elif not snapshot.tool_call_id:
        failures.append("main_agent_revision_tool_call_id_missing")
    else:
        checks.append("main_agent_revision_tool_observed")
    if snapshot.main_agent_thread_id_before != snapshot.main_agent_thread_id_after:
        failures.append("main_agent_thread_changed")
    else:
        checks.append("main_agent_thread_preserved")

    if not snapshot.old_plan_still_queryable:
        failures.append("old_plan_not_queryable")
    else:
        checks.append("old_plan_preserved")
    if snapshot.new_plan_id is None or snapshot.new_plan_hash is None:
        incomplete.append("new_plan_not_observed")
    elif snapshot.new_plan_id == snapshot.old_plan_id:
        failures.append("revision_reused_old_plan_id")
    else:
        checks.append("new_plan_observed")
    if snapshot.new_generation != snapshot.old_generation + 1:
        failures.append("revision_generation_not_incremented")
    else:
        checks.append("revision_generation_incremented")
    if snapshot.revision_id is None or snapshot.revision_hash is None:
        incomplete.append("revision_identity_not_observed")
    else:
        checks.append("revision_identity_observed")
    if snapshot.confirmation_command_id is None:
        incomplete.append("revision_confirmation_not_observed")
    else:
        checks.append("revision_confirmation_observed")
    if snapshot.transition_id is None:
        incomplete.append("revision_transition_not_observed")
    else:
        checks.append("revision_transition_observed")

    old_workers = set(snapshot.old_worker_item_ids)
    reused = set(snapshot.reused_worker_item_ids)
    changed = set(snapshot.changed_worker_item_ids)
    added = set(snapshot.added_worker_item_ids)
    transitioned = set(snapshot.transition_worker_item_ids)
    if not reused.issubset(old_workers):
        failures.append("reused_worker_not_in_old_plan")
    elif reused & changed:
        failures.append("worker_marked_reused_and_changed")
    elif not (changed | added).issubset(transitioned):
        failures.append("affected_worker_transition_missing")
    else:
        checks.append("worker_reuse_scope_observed")
    if changed & added:
        failures.append("worker_marked_changed_and_added")

    before = set(snapshot.source_result_ids_before)
    after = set(snapshot.source_result_ids_after)
    unimpacted = set(snapshot.unimpacted_result_ids)
    if not before.issubset(after):
        failures.append("source_result_lost_during_revision")
    elif not unimpacted.issubset(before) or not unimpacted.issubset(after):
        failures.append("unimpacted_result_not_reused")
    else:
        checks.append("source_result_reuse_observed")
    if not snapshot.old_inputs_rejected_for_new_plan:
        incomplete.append("old_input_scope_not_rejected")
    else:
        checks.append("old_input_scope_rejected")

    if snapshot.transition_status == "failed":
        failures.append("revision_transition_failed")
    elif snapshot.transition_status == "unknown":
        incomplete.append("revision_transition_unknown")
    else:
        checks.append("revision_transition_converged")

    failure_code = (failures or incomplete or [None])[0]
    return builder.finish(
        real_tests=1 if not failures and not incomplete else 0,
        failed=1 if failures else 0,
        incomplete=bool(incomplete and not failures),
        check_codes=tuple(checks + failures + incomplete),
        command_ids=tuple(
            item
            for item in (snapshot.confirmation_command_id,)
            if item is not None
        ),
        root_task_id=snapshot.root_task_id,
        failure_code_override=failure_code,
    )


def run_revision(
    manifest: Mapping[str, object],
    *,
    snapshot: RevisionSnapshot | None = None,
    evidence_kind: Literal["contract", "real_user_flow"] = "contract",
) -> ScenarioEvidence:
    builder = EvidenceBuilder(
        run_id=_uuid(manifest.get("run_id")),
        scenario="revision",
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
            check_codes=("revision_snapshot_not_supplied",),
        )
    return build_revision_evidence(builder, snapshot)


def _uuid(value: object) -> UUID:
    try:
        return UUID(str(value))
    except (TypeError, ValueError) as exc:
        raise ValueError("manifest run_id is invalid") from exc


def _text(value: object, default: str) -> str:
    return value if isinstance(value, str) and value else default
