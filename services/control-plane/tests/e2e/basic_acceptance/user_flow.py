"""Provider-neutral facts and evidence for the confirmed multi-worker path."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Literal
from urllib.parse import urlencode
from uuid import UUID

from .contracts import ExecutionWindow, ScenarioEvidence, UserFlowSnapshot
from .evidence import EvidenceBuilder
from .user_flow_restore import (
    RestoreChecker,
    restore_accepted_tree,
)

PublicJsonFetcher = Callable[
    [dict[str, object], str], dict[str, object] | None
]
ContentFetcher = Callable[[dict[str, object], UUID], bytes | None]


@dataclass(frozen=True, slots=True)
class UserFlowPublicObservation:
    snapshot: UserFlowSnapshot
    check_codes: tuple[str, ...]


_PUBLIC_REQUIRED_CHECKS = (
    "selection_public_observed",
    "selection_identity_bound",
    "selected_inputs_bound",
    "context_package_public_observed",
    "selected_artifacts_observed",
    "source_execution_facts_observed",
    "source_sessions_observed",
    "source_events_observed",
    "integration_execution_fact_observed",
    "integration_session_observed",
    "integration_events_observed",
    "candidate_public_observed",
    "verification_public_observed",
    "acceptance_public_observed",
)


def collect_user_flow_observation(
    manifest: Mapping[str, object],
    snapshot: UserFlowSnapshot,
    *,
    public_json_fetcher: PublicJsonFetcher | None = None,
    content_fetcher: ContentFetcher | None = None,
    restore_checker: RestoreChecker | None = None,
) -> UserFlowPublicObservation:
    """Re-read user-flow facts from public APIs and derive timing facts.

    The observation file only provides IDs.  In particular, its execution
    windows, dependency flag, and artifact bodies are never used as evidence.
    """

    snapshot_tree_claim = snapshot.accepted_tree_hash
    empty = snapshot.model_copy(
        update={
            "source_execution_ids": (),
            "source_session_ids": (),
            "source_windows": (),
            "dependency_satisfied": False,
            "verification_id": None,
            "verification_candidate_id": None,
            "verification_candidate_hash": None,
            "verification_status": "unknown",
            "acceptance_id": None,
            "accepted_candidate_id": None,
            "accepted_candidate_hash": None,
            "accepted": False,
            "accepted_tree_hash": None,
            "restored_tree_hash": None,
            "restore_check_passed": False,
        }
    )
    if not _manifest_identity_matches(manifest, snapshot):
        return UserFlowPublicObservation(empty, ("public_identity_mismatch",))

    read_json = public_json_fetcher or _missing_public_json
    read_content = content_fetcher
    manifest_dict = dict(manifest)
    selection_path = (
        f"/api/v1/workspaces/{snapshot.workspace_id}"
        f"/collaboration/input-selections/{snapshot.input_selection_id}"
    )
    selection = read_json(manifest_dict, selection_path)
    if selection is None:
        return UserFlowPublicObservation(empty, ("selection_public_missing",))
    checks: list[str] = ["selection_public_observed"]
    if not _selection_identity_matches(selection, snapshot):
        return UserFlowPublicObservation(
            empty,
            (*checks, "selection_identity_mismatch"),
        )
    checks.append("selection_identity_bound")

    selected_ids, selected_artifact_ids = _selected_source_facts(selection)
    expected_source_ids = tuple(snapshot.source_execution_ids)
    if set(selected_ids) != set(expected_source_ids) or len(selected_ids) < 2:
        return UserFlowPublicObservation(
            empty,
            (*checks, "selected_inputs_mismatch"),
        )
    checks.append("selected_inputs_bound")

    package_ids = _selection_package_ids(selection)
    package_id = package_ids[0] if package_ids else None
    package = None
    if package_id is not None:
        package = read_json(
            manifest_dict,
            f"/api/v1/workspaces/{snapshot.workspace_id}"
            f"/context-packages/{package_id}",
        )
    else:
        for schema_version in (3, 2, 1):
            package = read_json(
                manifest_dict,
                f"/api/v1/workspaces/{snapshot.workspace_id}"
                f"/executions/{snapshot.integration_execution_id}/context-package"
                f"?manifest_schema_version={schema_version}",
            )
            if isinstance(package, Mapping):
                package_id = _optional_uuid(package.get("package_id"))
                if package_id is not None:
                    break
    if _context_package_is_ready(
        package,
        snapshot.workspace_id,
        package_id,
    ):
        checks.append("context_package_public_observed")
    else:
        checks.append("context_package_missing")

    if not selected_artifact_ids:
        selected_artifact_ids = _public_artifact_ids(
            manifest_dict,
            snapshot.workspace_id,
            expected_source_ids,
            read_json,
        )
    if read_content is not None and selected_artifact_ids:
        artifact_bodies = tuple(
            read_content(manifest_dict, artifact_id)
            for artifact_id in selected_artifact_ids
        )
        if all(body is not None for body in artifact_bodies):
            checks.append("selected_artifacts_observed")
        else:
            checks.append("selected_artifacts_missing")
    else:
        checks.append("selected_artifacts_missing")

    source_bodies: dict[UUID, Mapping[str, object]] = {}
    for execution_id in expected_source_ids:
        body = read_json(
            manifest_dict,
            f"/api/v1/executions/{execution_id}?workspace_id={snapshot.workspace_id}",
        )
        if isinstance(body, Mapping) and _execution_identity_matches(
            body, execution_id, snapshot.workspace_id
        ):
            source_bodies[execution_id] = body
    source_windows = tuple(
        window
        for execution_id in expected_source_ids
        if (window := _execution_window(source_bodies.get(execution_id), execution_id))
        is not None
    )
    if len(source_windows) == len(expected_source_ids) and all(
        body.get("status") == "succeeded" for body in source_bodies.values()
    ):
        checks.append("source_execution_facts_observed")
    else:
        checks.append("source_execution_facts_missing")

    integration_body = read_json(
        manifest_dict,
        f"/api/v1/executions/{snapshot.integration_execution_id}"
        f"?workspace_id={snapshot.workspace_id}",
    )
    integration_window = _execution_window(
        integration_body, snapshot.integration_execution_id
    )
    if (
        isinstance(integration_body, Mapping)
        and _execution_identity_matches(
            integration_body,
            snapshot.integration_execution_id,
            snapshot.workspace_id,
        )
        and integration_window is not None
        and integration_body.get("status") == "succeeded"
    ):
        checks.append("integration_execution_fact_observed")
    else:
        checks.append("integration_execution_fact_missing")

    source_session_ids, source_events_ok = _observe_sessions_and_events(
        manifest_dict,
        snapshot.workspace_id,
        expected_source_ids,
        snapshot.source_session_ids,
        read_json,
    )
    if len(source_session_ids) == len(expected_source_ids):
        checks.append("source_sessions_observed")
    else:
        checks.append("source_sessions_missing")
    if source_events_ok:
        checks.append("source_events_observed")
    else:
        checks.append("source_events_missing")

    integration_session_ids, integration_events_ok = _observe_sessions_and_events(
        manifest_dict,
        snapshot.workspace_id,
        (snapshot.integration_execution_id,),
        (snapshot.integration_session_id,),
        read_json,
    )
    if integration_session_ids:
        checks.append("integration_session_observed")
    else:
        checks.append("integration_session_missing")
    if integration_events_ok:
        checks.append("integration_events_observed")
    else:
        checks.append("integration_events_missing")

    results_path = (
        f"/api/v1/workspaces/{snapshot.workspace_id}"
        f"/tasks/{snapshot.root_task_id}/results"
    )
    result_body = read_json(manifest_dict, results_path)
    candidate_body = None
    acceptance_body = None
    criteria: object = None
    if isinstance(result_body, Mapping):
        if (
            _optional_uuid(result_body.get("workspace_id")) != snapshot.workspace_id
            or _optional_uuid(result_body.get("root_task_id")) != snapshot.root_task_id
        ):
            checks.append("result_identity_mismatch")
        else:
            candidate_body = result_body.get("current_candidate")
            acceptance_body = result_body.get("acceptance")
            criteria = result_body.get("criteria")

    candidate_id = _optional_uuid(
        candidate_body.get("candidate_id")
        if isinstance(candidate_body, Mapping)
        else None
    )
    candidate_hash = (
        candidate_body.get("candidate_hash")
        if isinstance(candidate_body, Mapping)
        else None
    )
    result_commit = (
        candidate_body.get("result_commit")
        if isinstance(candidate_body, Mapping)
        else None
    )
    result_tree = (
        candidate_body.get("result_tree")
        if isinstance(candidate_body, Mapping)
        else None
    )
    candidate_public_ok = False
    if not isinstance(candidate_body, Mapping):
        checks.append("candidate_public_missing")
    elif candidate_id != snapshot.candidate_id or candidate_hash != snapshot.candidate_hash:
        checks.append("candidate_public_mismatch")
    elif candidate_body.get("is_current") is not True:
        checks.append("candidate_not_current")
    elif not _is_git_oid(result_commit) or not _is_git_oid(result_tree):
        checks.append("candidate_result_identity_missing")
    else:
        candidate_public_ok = True
        checks.append("candidate_public_observed")

    accepted = False
    acceptance_id = None
    if not isinstance(acceptance_body, Mapping):
        checks.append("acceptance_public_missing")
    elif (
        _optional_uuid(acceptance_body.get("candidate_id")) != candidate_id
        or acceptance_body.get("candidate_hash") != candidate_hash
    ):
        checks.append("acceptance_public_mismatch")
    else:
        acceptance_id = _optional_uuid(acceptance_body.get("acceptance_id"))
        if acceptance_id is None:
            checks.append("acceptance_public_missing")
        else:
            accepted = True
            checks.append("acceptance_public_observed")

    verification_id = None
    verification_status: Literal["passed", "failed", "unknown"] = "unknown"
    verification_codes: tuple[str, ...] = ()
    if candidate_public_ok and candidate_id is not None:
        verification_id, verification_status, verification_codes = (
            _observe_candidate_verifications(
                manifest_dict,
                snapshot.workspace_id,
                snapshot.root_task_id,
                candidate_id,
                result_commit,
                result_tree,
                criteria,
                read_json,
            )
        )
    else:
        verification_codes = ("verification_candidate_unavailable",)
    checks.extend(verification_codes)

    restored_tree_hash = None
    restore_check_passed = False
    should_restore = bool(
        candidate_public_ok
        and accepted
        and verification_status == "passed"
        and isinstance(result_tree, str)
        and isinstance(result_commit, str)
    )
    if should_restore and snapshot_tree_claim is not None and snapshot_tree_claim != result_tree:
        checks.append("accepted_tree_identity_mismatch")
        should_restore = False
    if should_restore:
        checker = restore_checker or restore_accepted_tree
        restoration = checker(manifest, result_commit, result_tree)
        if restoration.failure_code:
            checks.append(restoration.failure_code)
        restored_tree_hash = restoration.restored_tree_hash
        restore_check_passed = restoration.status == "passed" and (
            restoration.restored_tree_hash == result_tree
        )
        if restoration.status == "failed" and not restoration.failure_code:
            checks.append("accepted_tree_restore_check_failed")
        elif restoration.status == "passed" and not restore_check_passed:
            checks.append("accepted_tree_restore_mismatch")

        rechecked_body = read_json(manifest_dict, results_path)
        if rechecked_body is None:
            checks.append("public_result_recheck_missing")
            accepted = False
            acceptance_id = None
            restored_tree_hash = None
            restore_check_passed = False
        elif not _same_public_result_identity(result_body, rechecked_body):
            checks.append("public_result_identity_changed")
            accepted = False
            acceptance_id = None
            restored_tree_hash = None
            restore_check_passed = False

    dependency_satisfied = bool(
        "context_package_public_observed" in checks
        and "selected_artifacts_observed" in checks
        and "source_execution_facts_observed" in checks
        and "integration_execution_fact_observed" in checks
        and integration_window is not None
        and all(window.finished_at <= integration_window.started_at for window in source_windows)
    )
    if dependency_satisfied:
        checks.append("integration_after_selected_inputs")
    else:
        checks.append("integration_before_selected_inputs")

    observed = empty.model_copy(
        update={
            "source_execution_ids": tuple(source_bodies),
            "source_session_ids": source_session_ids,
            "source_windows": source_windows,
            "dependency_satisfied": dependency_satisfied,
            "verification_id": verification_id,
            "verification_candidate_id": candidate_id if verification_id else None,
            "verification_candidate_hash": candidate_hash if verification_id else None,
            "verification_status": verification_status,
            "acceptance_id": acceptance_id,
            "accepted_candidate_id": candidate_id if accepted else None,
            "accepted_candidate_hash": candidate_hash if accepted else None,
            "accepted": accepted,
            "accepted_tree_hash": result_tree if accepted and candidate_public_ok else None,
            "restored_tree_hash": restored_tree_hash,
            "restore_check_passed": restore_check_passed,
        }
    )
    return UserFlowPublicObservation(observed, tuple(checks))


def build_user_flow_evidence(
    builder: EvidenceBuilder,
    snapshot: UserFlowSnapshot,
    *,
    public_check_codes: tuple[str, ...] | None = None,
) -> ScenarioEvidence:
    failures: list[str] = []
    incomplete: list[str] = []
    checks: list[str] = []

    if public_check_codes is not None:
        checks.extend(public_check_codes)
        if "public_identity_mismatch" in public_check_codes:
            failures.append("public_identity_mismatch")
        if "selection_identity_mismatch" in public_check_codes:
            failures.append("selection_identity_mismatch")
        if "candidate_public_mismatch" in public_check_codes:
            failures.append("candidate_public_mismatch")
        if "acceptance_public_mismatch" in public_check_codes:
            failures.append("acceptance_public_mismatch")
        if "result_identity_mismatch" in public_check_codes:
            failures.append("result_identity_mismatch")
        if "candidate_not_current" in public_check_codes:
            failures.append("candidate_not_current")
        if "verification_candidate_mismatch" in public_check_codes:
            failures.append("verification_candidate_mismatch")
        if "verification_commit_mismatch" in public_check_codes:
            failures.append("verification_commit_mismatch")
        if "verification_tree_mismatch" in public_check_codes:
            failures.append("verification_tree_mismatch")
        if "verification_failed" in public_check_codes:
            failures.append("verification_failed")
        if "accepted_tree_identity_mismatch" in public_check_codes:
            failures.append("accepted_tree_identity_mismatch")
        if "accepted_tree_restore_mismatch" in public_check_codes:
            failures.append("accepted_tree_restore_mismatch")
        if "accepted_tree_restore_check_failed" in public_check_codes:
            failures.append("accepted_tree_restore_check_failed")
        if "public_result_identity_changed" in public_check_codes:
            failures.append("public_result_identity_changed")
        if "public_result_recheck_missing" in public_check_codes:
            incomplete.append("public_result_recheck_missing")
        for required in _PUBLIC_REQUIRED_CHECKS:
            if required not in public_check_codes:
                incomplete.append(required.replace("_observed", "_missing"))

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

    if snapshot.verification_status == "failed":
        failures.append("verification_failed")
    elif snapshot.verification_id is None:
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

    if snapshot.accepted_tree_hash is None or snapshot.restored_tree_hash is None:
        incomplete.append("accepted_tree_restore_not_observed")
    elif snapshot.restored_tree_hash != snapshot.accepted_tree_hash:
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
    public_json_fetcher: PublicJsonFetcher | None = None,
    content_fetcher: ContentFetcher | None = None,
    restore_checker: RestoreChecker | None = None,
) -> ScenarioEvidence:
    """Return evidence only from a supplied public snapshot; never invent facts."""

    run_id = _uuid(manifest.get("run_id"))
    builder = EvidenceBuilder(
        run_id=run_id,
        scenario="user-flow",
        evidence_kind=evidence_kind,
        code_commit=_text_or_default(manifest.get("code_commit"), "0" * 40),
        runtime_fingerprint=_text_or_default(
            manifest.get("runtime_fingerprint"), "0" * 64
        ),
    )
    if snapshot is not None:
        if snapshot.run_id != run_id:
            return builder.finish(
                real_tests=0,
                failed=1,
                check_codes=("user_flow_snapshot_run_id_mismatch",),
                failure_code_override="user_flow_snapshot_run_id_mismatch",
            )
        public_check_codes = None
        if evidence_kind == "real_user_flow":
            observed = collect_user_flow_observation(
                manifest,
                snapshot,
                public_json_fetcher=public_json_fetcher,
                content_fetcher=content_fetcher,
                restore_checker=restore_checker,
            )
            snapshot = observed.snapshot
            public_check_codes = observed.check_codes
        return build_user_flow_evidence(
            builder,
            snapshot,
            public_check_codes=public_check_codes,
        )
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


def _observe_candidate_verifications(
    manifest: dict[str, object],
    workspace_id: UUID,
    root_task_id: UUID,
    candidate_id: UUID,
    result_commit: object,
    result_tree: object,
    criteria: object,
    read_json: PublicJsonFetcher,
) -> tuple[UUID | None, Literal["passed", "failed", "unknown"], tuple[str, ...]]:
    if not isinstance(criteria, list | tuple):
        return None, "unknown", ("verification_criteria_missing",)

    automatic: list[tuple[set[str], tuple[UUID, ...], str, str]] = []
    codes: list[str] = []
    failed = False
    incomplete = False
    for criterion in criteria:
        if not isinstance(criterion, Mapping) or criterion.get("mode") != "automatic":
            continue
        check_keys_raw = criterion.get("check_keys")
        check_keys = (
            {key for key in check_keys_raw if isinstance(key, str) and key}
            if isinstance(check_keys_raw, list | tuple)
            else set()
        )
        result_ids_raw = criterion.get("verification_result_ids")
        result_ids = (
            tuple(
                parsed
                for value in result_ids_raw
                if (parsed := _optional_uuid(value)) is not None
            )
            if isinstance(result_ids_raw, list | tuple)
            else ()
        )
        status = str(criterion.get("verification_status", "unknown"))
        observed_status = str(criterion.get("observed_status", "pending"))
        if not check_keys or not result_ids or len(result_ids) != len(result_ids_raw):
            incomplete = True
        if status == "failed":
            failed = True
        elif status in {"unknown", "interrupted", "blocked", "not_run"}:
            incomplete = True
        elif status != "passed" or observed_status != "passed":
            failed = True
        automatic.append((check_keys, result_ids, status, observed_status))

    if not automatic:
        return None, "unknown", ("verification_criteria_missing",)

    rows: list[Mapping[str, object]] = []
    cursor: str | None = None
    seen_cursors: set[str] = set()
    seen_ids: set[UUID] = set()
    path_base = (
        f"/api/v1/workspaces/{workspace_id}/tasks/{root_task_id}"
        f"/results/{candidate_id}/verifications"
    )
    for _ in range(100):
        query = {"limit": "100"}
        if cursor is not None:
            query["cursor"] = cursor
        page = read_json(manifest, f"{path_base}?{urlencode(query)}")
        if not isinstance(page, Mapping):
            return None, "unknown", ("verification_public_missing",)
        if _optional_uuid(page.get("candidate_id")) != candidate_id:
            return None, "unknown", ("verification_candidate_mismatch",)
        items = page.get("items")
        if not isinstance(items, list | tuple):
            return None, "unknown", ("verification_public_invalid",)
        for item in items:
            if not isinstance(item, Mapping):
                return None, "unknown", ("verification_public_invalid",)
            verification_id = _optional_uuid(item.get("verification_id"))
            if verification_id is None or verification_id in seen_ids:
                return None, "unknown", ("verification_public_invalid",)
            seen_ids.add(verification_id)
            rows.append(item)
        next_cursor = page.get("next_cursor")
        if next_cursor is None:
            break
        if not isinstance(next_cursor, str) or not next_cursor or next_cursor in seen_cursors:
            return None, "unknown", ("verification_pagination_invalid",)
        seen_cursors.add(next_cursor)
        cursor = next_cursor
    else:
        return None, "unknown", ("verification_pagination_invalid",)

    by_id = {
        _optional_uuid(row.get("verification_id")): row
        for row in rows
    }
    referenced_ids = {verification_id for _, ids, _, _ in automatic for verification_id in ids}
    if not referenced_ids or any(verification_id not in by_id for verification_id in referenced_ids):
        incomplete = True

    selected_rows = [by_id[item] for item in sorted(referenced_ids, key=str) if item in by_id]
    passed_ids: set[UUID] = set()
    for check_keys, result_ids, _, _ in automatic:
        criterion_rows = [by_id[item] for item in result_ids if item in by_id]
        actual_keys = {
            str(row.get("check_key"))
            for row in criterion_rows
            if isinstance(row.get("check_key"), str)
        }
        if actual_keys != check_keys:
            incomplete = True

    for row in selected_rows:
        row_id = _optional_uuid(row.get("verification_id"))
        row_matches_candidate = True
        if _optional_uuid(row.get("candidate_id")) != candidate_id:
            codes.append("verification_candidate_mismatch")
            failed = True
            row_matches_candidate = False
        if row.get("actual_commit") != result_commit:
            codes.append("verification_commit_mismatch")
            failed = True
            row_matches_candidate = False
        if row.get("actual_tree") != result_tree:
            codes.append("verification_tree_mismatch")
            failed = True
            row_matches_candidate = False
        status = row.get("status")
        if status == "failed":
            failed = True
        elif status != "passed":
            incomplete = True
        elif row_matches_candidate and row_id is not None:
            passed_ids.add(row_id)

    representative_id = min(passed_ids, key=str) if passed_ids else None

    if failed:
        codes.append("verification_failed")
    if incomplete:
        codes.append("verification_records_incomplete")
    if failed or incomplete:
        return representative_id, "failed" if failed else "unknown", tuple(dict.fromkeys(codes))

    codes.append("verification_public_observed")
    return representative_id, "passed", tuple(codes)


def _manifest_identity_matches(
    manifest: Mapping[str, object], snapshot: UserFlowSnapshot
) -> bool:
    for name, expected in (
        ("run_id", snapshot.run_id),
        ("workspace_id", snapshot.workspace_id),
        ("root_task_id", snapshot.root_task_id),
    ):
        value = manifest.get(name)
        if value is not None and _optional_uuid(value) != expected:
            return False
    return True


def _selection_identity_matches(
    selection: Mapping[str, object], snapshot: UserFlowSnapshot
) -> bool:
    # The selection's run_id identifies a control-plane run, not the S2A batch.
    return all(
        _optional_uuid(selection.get(name)) == expected
        for name, expected in (
            ("selection_id", snapshot.input_selection_id),
            ("workspace_id", snapshot.workspace_id),
            ("root_task_id", snapshot.root_task_id),
        )
    ) and selection.get("selection_hash") == snapshot.input_selection_hash


def _selected_source_facts(
    selection: Mapping[str, object],
) -> tuple[tuple[UUID, ...], tuple[UUID, ...]]:
    source_ids: list[UUID] = []
    artifact_ids: list[UUID] = []
    values = [selection.get("groups"), selection.get("hard_inputs")]
    for value in values:
        if not isinstance(value, list | tuple):
            continue
        for group in value:
            if not isinstance(group, Mapping):
                continue
            source_values = group.get("sources")
            if source_values is None:
                source_values = [group.get("source")]
            if not isinstance(source_values, list | tuple):
                continue
            for source in source_values:
                if not isinstance(source, Mapping):
                    continue
                execution_id = _optional_uuid(source.get("effective_execution_id"))
                if execution_id is not None:
                    source_ids.append(execution_id)
                artifacts = source.get("artifacts")
                if isinstance(artifacts, list | tuple):
                    for artifact in artifacts:
                        if isinstance(artifact, Mapping):
                            artifact_id = _optional_uuid(
                                artifact.get("artifact_id") or artifact.get("id")
                            )
                            if artifact_id is not None:
                                artifact_ids.append(artifact_id)
    return _unique(source_ids), _unique(artifact_ids)


def _selection_package_ids(selection: Mapping[str, object]) -> tuple[UUID, ...]:
    result: list[UUID] = []
    consumers = selection.get("consumers")
    if isinstance(consumers, list | tuple):
        for consumer in consumers:
            if isinstance(consumer, Mapping):
                package_id = _optional_uuid(consumer.get("package_id"))
                if package_id is not None:
                    result.append(package_id)
    return _unique(result)


def _public_artifact_ids(
    manifest: dict[str, object],
    workspace_id: UUID,
    execution_ids: tuple[UUID, ...],
    read_json: PublicJsonFetcher,
) -> tuple[UUID, ...]:
    """Discover source-bound artifact identities through the public gateway list."""

    result: list[UUID] = []
    for execution_id in execution_ids:
        body = read_json(
            manifest,
            f"/api/v1/gateway/artifacts?workspace_id={workspace_id}"
            f"&execution_id={execution_id}&limit=100",
        )
        items = body.get("items") if body is not None else None
        if not isinstance(items, list | tuple):
            continue
        for item in items:
            if not isinstance(item, Mapping):
                continue
            if (
                _optional_uuid(item.get("workspace_id")) != workspace_id
                or _optional_uuid(item.get("execution_id")) != execution_id
            ):
                continue
            artifact_id = _optional_uuid(item.get("id") or item.get("artifact_id"))
            if artifact_id is not None:
                result.append(artifact_id)
    return _unique(result)


def _context_package_is_ready(
    package: Mapping[str, object] | None,
    workspace_id: UUID,
    package_id: UUID | None,
) -> bool:
    if package is None:
        return False
    assembly = package.get("assembly")
    return (
        package_id is not None
        and _optional_uuid(package.get("package_id")) == package_id
        and _optional_uuid(package.get("workspace_id")) == workspace_id
        and package.get("status") == "active"
        and package.get("materialized_at") is not None
        and isinstance(assembly, Mapping)
        and assembly.get("status") == "ready"
    )


def _execution_identity_matches(
    body: Mapping[str, object] | None,
    execution_id: UUID,
    workspace_id: UUID,
) -> bool:
    return (
        body is not None
        and _optional_uuid(body.get("id")) == execution_id
        and _optional_uuid(body.get("workspace_id")) == workspace_id
    )


def _execution_window(
    body: Mapping[str, object] | None,
    execution_id: UUID,
) -> ExecutionWindow | None:
    if body is None or _optional_uuid(body.get("id")) != execution_id:
        return None
    started_at = _parse_datetime(body.get("started_at"))
    finished_at = _parse_datetime(body.get("finished_at"))
    if started_at is None or finished_at is None or finished_at < started_at:
        return None
    return ExecutionWindow(
        execution_id=execution_id,
        started_at=started_at,
        finished_at=finished_at,
    )


def _observe_sessions_and_events(
    manifest: dict[str, object],
    workspace_id: UUID,
    execution_ids: tuple[UUID, ...],
    session_ids: tuple[UUID, ...],
    read_json: PublicJsonFetcher,
) -> tuple[tuple[UUID, ...], bool]:
    observed: list[UUID] = []
    events_ok = True
    if len(execution_ids) != len(session_ids):
        return (), False
    for execution_id, session_id in zip(execution_ids, session_ids, strict=True):
        session_body = read_json(
            manifest,
            f"/api/v1/gateway/sessions?workspace_id={workspace_id}"
            f"&session_id={session_id}&limit=1",
        )
        items = session_body.get("items") if session_body is not None else None
        item = items[0] if isinstance(items, list | tuple) and items else None
        if not isinstance(item, Mapping) or not (
            _optional_uuid(item.get("id")) == session_id
            and _optional_uuid(item.get("workspace_id")) == workspace_id
            and _optional_uuid(item.get("execution_id")) == execution_id
        ):
            events_ok = False
            continue
        observed.append(session_id)
        events_body = read_json(
            manifest,
            f"/api/v1/gateway/events?workspace_id={workspace_id}"
            f"&session_id={session_id}&limit=100",
        )
        events = events_body.get("items") if events_body is not None else None
        if not isinstance(events, list | tuple) or not any(
            isinstance(event, Mapping)
            and _optional_uuid(event.get("session_id")) == session_id
            and _optional_uuid(event.get("execution_id")) == execution_id
            and _optional_uuid(event.get("workspace_id")) == workspace_id
            for event in events
        ):
            events_ok = False
    return tuple(observed), events_ok and len(observed) == len(session_ids)


def _parse_datetime(value: object) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        return None
    return parsed.astimezone(UTC)


def _optional_uuid(value: object) -> UUID | None:
    try:
        return UUID(str(value)) if value is not None else None
    except (TypeError, ValueError):
        return None


def _is_git_oid(value: object) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 40
        and all(char in "0123456789abcdef" for char in value)
    )


def _same_public_result_identity(
    before: Mapping[str, object] | None,
    after: Mapping[str, object] | None,
) -> bool:
    if before is None or after is None:
        return False
    for field in ("workspace_id", "root_task_id"):
        if before.get(field) != after.get(field):
            return False
    before_candidate = before.get("current_candidate")
    after_candidate = after.get("current_candidate")
    before_acceptance = before.get("acceptance")
    after_acceptance = after.get("acceptance")
    if not all(
        isinstance(value, Mapping)
        for value in (
            before_candidate,
            after_candidate,
            before_acceptance,
            after_acceptance,
        )
    ):
        return False
    assert isinstance(before_candidate, Mapping)
    assert isinstance(after_candidate, Mapping)
    assert isinstance(before_acceptance, Mapping)
    assert isinstance(after_acceptance, Mapping)
    return all(
        before_candidate.get(field) == after_candidate.get(field)
        for field in ("candidate_id", "candidate_hash", "result_commit", "result_tree")
    ) and all(
        before_acceptance.get(field) == after_acceptance.get(field)
        for field in ("acceptance_id", "candidate_id", "candidate_hash")
    )


def _unique(values: list[UUID]) -> tuple[UUID, ...]:
    return tuple(dict.fromkeys(values))


def _missing_public_json(
    _manifest: dict[str, object], _path: str
) -> dict[str, object] | None:
    return None


def _uuid(value: object) -> UUID:
    try:
        return UUID(str(value))
    except (TypeError, ValueError) as exc:
        raise ValueError("manifest run_id is invalid") from exc


def _text_or_default(value: object, default: str) -> str:
    return value if isinstance(value, str) and value else default
