"""Scenario registry; no arbitrary commands or database fixtures are accepted."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, cast
from uuid import UUID

import httpx

from .contracts import (
    InterventionSnapshot,
    NotificationSnapshot,
    RecoverySnapshot,
    RevisionSnapshot,
    ScenarioEvidence,
    SecondAdapterSnapshot,
    UserFlowSnapshot,
    WebNotificationSnapshot,
)
from .evidence import EvidenceBuilder
from .intervention import run_intervention
from .live_scenarios import observe_group
from .notification import run_notification, run_web_notification
from .observations import GroupRunObservation, LiveObservationError, load_observation
from .recovery import run_recovery
from .revision import run_revision
from .second_adapter import run_second_adapter
from .user_flow import run_user_flow

ResultBudgetState = Literal["completed", "reserved", "exhausted", "unknown"]
ResultSessionState = Literal["succeeded", "failed", "timed_out", "unknown"]


@dataclass(frozen=True, slots=True)
class ResultWorkerObservation:
    """The bounded, non-secret facts required for a real result-worker PASS."""

    selection_id: UUID | None = None
    selection_hash: str | None = None
    expected_selection_hash: str | None = None
    bundle_id: UUID | None = None
    context_package_id: UUID | None = None
    context_package_hash: str | None = None
    expected_context_package_hash: str | None = None
    selected_artifact_hashes: tuple[tuple[str, str], ...] = ()
    artifact_bodies: tuple[tuple[str, bytes], ...] = ()
    source_execution_ids: tuple[UUID, ...] = ()
    downstream_execution_id: UUID | None = None
    expected_downstream_execution_id: UUID | None = None
    downstream_body: bytes | None = None
    downstream_marker: bytes | None = None
    real_session_ids: tuple[UUID, ...] = ()
    budget_state: ResultBudgetState = "unknown"
    session_state: ResultSessionState = "unknown"
    acceptance_fact_required: bool = False
    root_task_id: UUID | None = None
    result_candidate_id: UUID | None = None
    result_candidate_hash: str | None = None
    expected_result_candidate_id: UUID | None = None
    expected_result_candidate_hash: str | None = None
    acceptance_id: UUID | None = None
    accepted_candidate_id: UUID | None = None
    accepted_candidate_hash: str | None = None
    expected_acceptance_id: UUID | None = None
    expected_accepted_candidate_id: UUID | None = None
    expected_accepted_candidate_hash: str | None = None


def build_result_worker_evidence(
    builder: EvidenceBuilder,
    observation: ResultWorkerObservation,
) -> ScenarioEvidence:
    """Classify result evidence without treating materialization as execution."""

    failures, incomplete, checks = _validate_result_worker_observation(observation)
    failure_code = (failures or incomplete or [None])[0]
    return builder.finish(
        real_tests=1 if not failures and not incomplete else 0,
        failed=1 if failures else 0,
        incomplete=bool(incomplete and not failures),
        check_codes=tuple(checks + failures + incomplete),
        failure_code_override=failure_code,
        execution_ids=tuple(
            dict.fromkeys(
                (*observation.source_execution_ids,
                 *((observation.downstream_execution_id,) if observation.downstream_execution_id else ()))
            )
        ),
        session_ids=observation.real_session_ids,
    )


def assert_result_worker_observation(
    observation: ResultWorkerObservation,
) -> tuple[str, ...]:
    """Fail a real entry immediately when its persisted facts are incomplete."""

    failures, incomplete, checks = _validate_result_worker_observation(observation)
    if failures:
        raise AssertionError(f"result evidence failed: {failures[0]}")
    if incomplete:
        raise AssertionError(f"result evidence incomplete: {incomplete[0]}")
    return tuple(checks)


def _validate_result_worker_observation(
    observation: ResultWorkerObservation,
) -> tuple[list[str], list[str], list[str]]:
    failures: list[str] = []
    incomplete: list[str] = []
    checks: list[str] = []

    if observation.selection_id is None:
        failures.append("selection_id_missing")
    else:
        checks.append("selection_id_observed")
    if not _is_sha256(observation.selection_hash):
        failures.append("selection_hash_missing")
    elif observation.expected_selection_hash != observation.selection_hash:
        failures.append("selection_hash_mismatch")
    else:
        checks.append("selection_hash_observed")

    if observation.bundle_id is None:
        failures.append("bundle_id_missing")
    else:
        checks.append("bundle_id_observed")
    if observation.context_package_id is None:
        failures.append("context_package_id_missing")
    if not _is_sha256(observation.context_package_hash):
        failures.append("context_package_hash_missing")
    elif observation.context_package_hash != observation.expected_context_package_hash:
        failures.append("context_package_hash_mismatch")
    else:
        checks.append("context_package_identity_observed")

    expected_artifacts = dict(observation.selected_artifact_hashes)
    actual_bodies = dict(observation.artifact_bodies)
    if not expected_artifacts:
        failures.append("selected_artifacts_missing")
    else:
        for artifact_ref, expected_hash in sorted(expected_artifacts.items()):
            body = actual_bodies.get(artifact_ref)
            if body is None:
                failures.append("artifact_body_missing")
                continue
            if not _is_sha256(expected_hash) or hashlib.sha256(body).hexdigest() != expected_hash:
                failures.append("artifact_hash_mismatch")
        if not any(code.startswith("artifact_") for code in failures):
            checks.append("selected_artifact_hashes_observed")

    if not observation.source_execution_ids:
        failures.append("source_execution_id_missing")
    else:
        checks.append("source_execution_ids_observed")
    if observation.downstream_execution_id is None:
        failures.append("downstream_execution_id_missing")
    elif (
        observation.expected_downstream_execution_id is None
        or observation.downstream_execution_id
        != observation.expected_downstream_execution_id
    ):
        failures.append("downstream_execution_mismatch")
    else:
        checks.append("downstream_execution_bound")

    if (
        observation.downstream_body is None
        or not observation.downstream_marker
        or observation.downstream_marker not in observation.downstream_body
    ):
        failures.append("downstream_marker_missing")
    else:
        checks.append("downstream_marker_observed")

    if not observation.real_session_ids:
        failures.append("real_session_id_missing")
    else:
        checks.append("real_session_observed")

    if observation.budget_state != "completed":
        incomplete.append(
            "budget_exhausted"
            if observation.budget_state == "exhausted"
            else "budget_not_completed"
        )
    else:
        checks.append("budget_completed")
    if observation.session_state == "failed":
        failures.append("real_session_failed")
    elif observation.session_state == "timed_out":
        incomplete.append("real_session_timed_out")
    elif observation.session_state == "unknown":
        incomplete.append("real_session_unknown")
    else:
        checks.append("real_session_succeeded")

    if observation.acceptance_fact_required:
        if observation.root_task_id is None or observation.result_candidate_id is None:
            failures.append("result_candidate_identity_missing")
        elif observation.expected_result_candidate_id != observation.result_candidate_id:
            failures.append("result_candidate_mismatch")
        elif observation.result_candidate_hash != observation.expected_result_candidate_hash:
            failures.append("result_candidate_hash_mismatch")
        else:
            checks.append("result_candidate_public_observed")
        if observation.acceptance_id is None:
            failures.append("result_acceptance_missing")
        elif observation.expected_acceptance_id != observation.acceptance_id:
            failures.append("result_acceptance_mismatch")
        elif observation.accepted_candidate_id != observation.result_candidate_id:
            failures.append("result_acceptance_candidate_mismatch")
        elif observation.accepted_candidate_hash != observation.result_candidate_hash:
            failures.append("result_acceptance_hash_mismatch")
        elif (
            observation.expected_accepted_candidate_id != observation.result_candidate_id
            or observation.expected_accepted_candidate_hash != observation.result_candidate_hash
        ):
            failures.append("result_acceptance_public_mismatch")
        else:
            checks.append("result_acceptance_public_observed")
    return failures, incomplete, checks


def _is_sha256(value: str | None) -> bool:
    if not isinstance(value, str) or len(value) != 64:
        return False
    return all(character in "0123456789abcdef" for character in value)

SCENARIOS = frozenset(
    {
        "profile",
        "result-inputs",
        "result-integration",
        "user-flow",
        "intervention",
        "revision",
        "recovery",
        "second-adapter",
        "notification",
        "web-notification",
    }
)


def run_scenario(
    name: str,
    manifest_path: Path,
    *,
    mode: Literal["check", "local", "real"] = "check",
) -> ScenarioEvidence:
    if name not in SCENARIOS:
        raise ValueError(f"unsupported scenario: {name}")
    raw = json.loads(manifest_path.read_text(encoding="utf-8"))
    run_id = UUID(str(raw["run_id"]))
    evidence_kind = (
        "real_worker"
        if name in {"result-inputs", "result-integration"}
        else "contract"
        if mode != "real"
        else "real_user_flow"
    )
    builder = EvidenceBuilder(
        run_id=run_id,
        scenario=name,
        evidence_kind=evidence_kind,
        code_commit=_code_commit(raw),
        runtime_fingerprint=str(raw.get("runtime_fingerprint", "0" * 64)),
    )
    if mode == "check":
        return builder.finish(
            real_tests=0,
            check_codes=("manifest_loaded", "check_mode_no_business_session"),
            failure_code_override="check_mode_no_business_session",
        )
    if name == "user-flow":
        snapshot = _optional_user_flow_snapshot(raw, mode=mode)
        user_flow_evidence_kind: Literal["contract", "real_user_flow"] = (
            "real_user_flow" if mode == "real" else "contract"
        )
        return run_user_flow(
            raw,
            snapshot=snapshot,
            evidence_kind=user_flow_evidence_kind,
            public_json_fetcher=(
                _fetch_public_json if mode == "real" else None
            ),
            content_fetcher=(
                _fetch_artifact_content if mode == "real" else None
            ),
        )
    if name == "intervention":
        snapshot = _optional_intervention_snapshot(raw, mode=mode)
        intervention_evidence_kind: Literal["contract", "real_user_flow"] = (
            "real_user_flow" if mode == "real" else "contract"
        )
        return run_intervention(
            raw,
            snapshot=snapshot,
            evidence_kind=intervention_evidence_kind,
        )
    if name == "revision":
        snapshot = _optional_revision_snapshot(raw, mode=mode)
        revision_evidence_kind: Literal["contract", "real_user_flow"] = (
            "real_user_flow" if mode == "real" else "contract"
        )
        return run_revision(
            raw,
            snapshot=snapshot,
            evidence_kind=revision_evidence_kind,
        )
    if name == "recovery":
        snapshot = _optional_recovery_snapshot(raw, mode=mode)
        recovery_evidence_kind: Literal["contract", "real_user_flow"] = (
            "real_user_flow" if mode == "real" else "contract"
        )
        return run_recovery(
            raw,
            snapshot=snapshot,
            evidence_kind=recovery_evidence_kind,
        )
    if name == "second-adapter":
        snapshot_value = (
            _real_observation_snapshot(raw, name)
            if mode == "real"
            else raw.get("second_adapter_snapshot")
        )
        snapshot = (
            SecondAdapterSnapshot.model_validate(snapshot_value)
            if isinstance(snapshot_value, dict)
            else None
        )
        second_adapter_evidence_kind: Literal["contract", "real_user_flow"] = (
            "real_user_flow" if mode == "real" else "contract"
        )
        return run_second_adapter(
            raw,
            snapshot=snapshot,
            evidence_kind=second_adapter_evidence_kind,
        )
    if name == "notification":
        snapshot_value = (
            _real_observation_snapshot(raw, name)
            if mode == "real"
            else raw.get("notification_snapshot")
        )
        snapshot = (
            NotificationSnapshot.model_validate(snapshot_value)
            if isinstance(snapshot_value, dict)
            else None
        )
        notification_evidence_kind: Literal["contract", "real_user_flow"] = (
            "real_user_flow" if mode == "real" else "contract"
        )
        return run_notification(
            raw,
            snapshot=snapshot,
            evidence_kind=notification_evidence_kind,
        )
    if name == "web-notification":
        snapshot_value = (
            _real_observation_snapshot(raw, name)
            if mode == "real"
            else raw.get("web_notification_snapshot")
        )
        snapshot = (
            WebNotificationSnapshot.model_validate(snapshot_value)
            if isinstance(snapshot_value, dict)
            else None
        )
        web_notification_evidence_kind: Literal["contract", "real_user_flow"] = (
            "real_user_flow" if mode == "real" else "contract"
        )
        return run_web_notification(
            raw,
            snapshot=snapshot,
            evidence_kind=web_notification_evidence_kind,
        )
    if name == "profile":
        try:
            observation = observe_group("A", raw)
        except LiveObservationError as exc:
            return builder.finish(
                real_tests=0,
                incomplete=True,
                check_codes=("profile_group_observation_missing", f"profile_{exc.code}"),
                failure_code_override=f"profile_{exc.code}",
            )
        if observation.status != "completed":
            return builder.finish(
                real_tests=0,
                incomplete=True,
                check_codes=(
                    "profile_group_observation_observed",
                    f"profile_group_status_{observation.status}",
                ),
                failure_code_override="profile_group_observation_not_completed",
            )
        if observation.group == "A":
            if len(observation.source_execution_ids) < 2 or not observation.terminal_execution_ids:
                return _incomplete_profile(
                    builder,
                    observation,
                    "profile_execution_terminal_missing",
                )
            if observation.integration_execution_id is None:
                return _incomplete_profile(
                    builder,
                    observation,
                    "profile_integration_missing",
                )
            expected_execution_ids = {
                *observation.source_execution_ids,
                observation.integration_execution_id,
            }
            if not expected_execution_ids.issubset(set(observation.terminal_execution_ids)):
                return _incomplete_profile(
                    builder,
                    observation,
                    "profile_execution_terminal_missing",
                )
            if not expected_execution_ids.issubset(
                set(observation.released_lease_execution_ids)
            ):
                return _incomplete_profile(
                    builder,
                    observation,
                    "profile_lease_release_missing",
                )
            if not observation.released_worker_ids:
                return _incomplete_profile(
                    builder,
                    observation,
                    "profile_worker_capacity_missing",
                )
            if observation.result_candidate_id is None:
                return _incomplete_profile(
                    builder,
                    observation,
                    "profile_result_candidate_missing",
                )
            if not observation.result_accepted:
                return _incomplete_profile(
                    builder,
                    observation,
                    "profile_result_acceptance_missing",
                )
            execution_ids = tuple(
                dict.fromkeys(
                    (*observation.source_execution_ids, observation.integration_execution_id)
                )
            )
            return builder.finish(
                real_tests=1,
                check_codes=(
                    "profile_group_observation_observed",
                    "profile_root_task_observed",
                    "profile_confirmation_command_observed",
                    "profile_execution_terminal_observed",
                    "profile_lease_release_observed",
                    "profile_worker_capacity_observed",
                    "profile_integration_observed",
                    "profile_result_candidate_observed",
                    "profile_result_acceptance_observed",
                ),
                command_ids=observation.command_ids,
                execution_ids=execution_ids,
                root_task_id=observation.root_task_id,
            )
        return builder.finish(
            real_tests=1,
            check_codes=(
                "profile_group_observation_observed",
                "profile_root_task_observed",
                "profile_confirmation_command_observed",
            ),
            command_ids=observation.command_ids,
            root_task_id=observation.root_task_id,
        )
    if name in {"result-inputs", "result-integration"}:
        observation = (
            _result_worker_observation(raw, name) if mode == "real" else None
        )
        if observation is not None:
            return build_result_worker_evidence(builder, observation)
        return builder.finish(
            real_tests=0,
            incomplete=True,
            check_codes=(f"{name.replace('-', '_')}_real_evidence_not_supplied",),
            failure_code_override=f"{name.replace('-', '_')}_real_evidence_not_supplied",
        )
    return builder.finish(
        real_tests=0,
        incomplete=True,
        check_codes=("scenario_handler_not_enabled",),
    )


def _incomplete_profile(
    builder: EvidenceBuilder,
    observation: GroupRunObservation,
    code: str,
) -> ScenarioEvidence:
    root_task_id = getattr(observation, "root_task_id", None)
    command_ids = getattr(observation, "command_ids", ())
    return builder.finish(
        real_tests=0,
        incomplete=True,
        check_codes=("profile_group_observation_observed", code),
        command_ids=command_ids,
        root_task_id=root_task_id,
        failure_code_override=code,
    )


def _code_commit(manifest: dict[str, object]) -> str:
    value = manifest.get("code_commit")
    if isinstance(value, str) and len(value) == 40:
        return value
    return "0" * 40


def _optional_user_flow_snapshot(
    manifest: dict[str, object],
    *,
    mode: Literal["check", "local", "real"],
) -> UserFlowSnapshot | None:
    value = (
        _real_observation_snapshot(manifest, "user-flow")
        if mode == "real"
        else manifest.get("user_flow_snapshot")
    )
    if not isinstance(value, dict):
        return None
    return UserFlowSnapshot.model_validate(value)


def _optional_intervention_snapshot(
    manifest: dict[str, object],
    *,
    mode: Literal["check", "local", "real"],
) -> InterventionSnapshot | None:
    value = (
        _real_observation_snapshot(manifest, "intervention")
        if mode == "real"
        else manifest.get("intervention_snapshot")
    )
    if not isinstance(value, dict):
        return None
    return InterventionSnapshot.model_validate(value)


def _optional_revision_snapshot(
    manifest: dict[str, object],
    *,
    mode: Literal["check", "local", "real"],
) -> RevisionSnapshot | None:
    value = (
        _real_observation_snapshot(manifest, "revision")
        if mode == "real"
        else manifest.get("revision_snapshot")
    )
    if not isinstance(value, dict):
        return None
    return RevisionSnapshot.model_validate(value)


def _optional_recovery_snapshot(
    manifest: dict[str, object],
    *,
    mode: Literal["check", "local", "real"],
) -> RecoverySnapshot | None:
    value = (
        _real_observation_snapshot(manifest, "recovery")
        if mode == "real"
        else manifest.get("recovery_snapshot")
    )
    if not isinstance(value, dict):
        return None
    return RecoverySnapshot.model_validate(value)


def _real_observation_snapshot(
    manifest: dict[str, object], scenario: str
) -> dict[str, object] | None:
    try:
        observation = load_observation(manifest, scenario)
    except LiveObservationError:
        return None
    value = observation.get("snapshot")
    return value if isinstance(value, dict) else None


def _result_worker_observation(
    manifest: dict[str, object],
    scenario: str,
    *,
    public_json_fetcher: Callable[
        [dict[str, object], str], dict[str, object] | None
    ] | None = None,
    content_fetcher: Callable[[dict[str, object], UUID], bytes | None] | None = None,
) -> ResultWorkerObservation | None:
    value = _real_observation_snapshot(manifest, scenario)
    if value is None:
        return None
    try:
        read_json = public_json_fetcher or _fetch_public_json
        read_content = content_fetcher or _fetch_artifact_content
        selection_id = _optional_uuid(value.get("selection_id"))
        context_package_id = _optional_uuid(value.get("context_package_id"))
        selection_body = (
            read_json(
                manifest,
                f"/api/v1/workspaces/{manifest['workspace_id']}"
                f"/collaboration/input-selections/{selection_id}",
            )
            if selection_id is not None
            else None
        )
        context_body = (
            read_json(
                manifest,
                f"/api/v1/workspaces/{manifest['workspace_id']}"
                f"/context-packages/{context_package_id}",
            )
            if context_package_id is not None
            else None
        )
        source_execution_ids = tuple(
            item
            for item in (
                _optional_uuid(candidate)
                for candidate in _items(value.get("source_execution_ids"))
            )
            if item is not None
            and _execution_observed(read_json, manifest, item)
        )
        downstream_execution_id = _optional_uuid(
            value.get("downstream_execution_id")
        )
        downstream_observed = (
            downstream_execution_id is not None
            and _execution_observed(read_json, manifest, downstream_execution_id)
        )
        artifact_hashes = tuple(
            (artifact_ref, str(item[1]))
            for item in _items(value.get("selected_artifact_hashes"))
            if isinstance(item, (list, tuple)) and len(item) == 2
            for artifact_ref in (_artifact_ref(item[0]),)
            if artifact_ref is not None
        )
        artifact_bodies = tuple(
            (artifact_ref, body)
            for artifact_ref, _expected_hash in artifact_hashes
            for artifact_id in (_artifact_id(artifact_ref),)
            if artifact_id is not None
            for body in (read_content(manifest, artifact_id),)
            if body is not None
        )
        downstream_artifact_id = _artifact_id(
            value.get("downstream_artifact_id")
            or value.get("downstream_artifact_ref")
        )
        downstream_body = (
            read_content(manifest, downstream_artifact_id)
            if downstream_artifact_id is not None and downstream_observed
            else None
        )
        marker = _marker_bytes(value.get("downstream_marker"))
        root_task_id = _optional_uuid(value.get("root_task_id"))
        result_candidate_id = _optional_uuid(value.get("candidate_id"))
        acceptance_id = _optional_uuid(value.get("acceptance_id"))
        result_body = (
            read_json(
                manifest,
                f"/api/v1/workspaces/{manifest['workspace_id']}"
                f"/tasks/{root_task_id}/results",
            )
            if root_task_id is not None
            else None
        )
        public_candidate = (
            result_body.get("current_candidate")
            if isinstance(result_body, Mapping)
            else None
        )
        public_acceptance = (
            result_body.get("acceptance")
            if isinstance(result_body, Mapping)
            else None
        )
        public_candidate_map = (
            public_candidate if isinstance(public_candidate, Mapping) else None
        )
        public_acceptance_map = (
            public_acceptance if isinstance(public_acceptance, Mapping) else None
        )
        return ResultWorkerObservation(
            selection_id=selection_id,
            selection_hash=_optional_text(value.get("selection_hash")),
            expected_selection_hash=_public_hash(selection_body),
            bundle_id=_optional_uuid(value.get("bundle_id")),
            context_package_id=context_package_id,
            context_package_hash=_optional_text(value.get("context_package_hash")),
            expected_context_package_hash=_public_hash(context_body),
            selected_artifact_hashes=artifact_hashes,
            artifact_bodies=artifact_bodies,
            source_execution_ids=source_execution_ids,
            downstream_execution_id=downstream_execution_id,
            expected_downstream_execution_id=(
                downstream_execution_id if downstream_observed else None
            ),
            downstream_body=downstream_body,
            downstream_marker=marker,
            real_session_ids=tuple(
                item
                for item in (
                    _optional_uuid(candidate)
                    for candidate in _items(value.get("real_session_ids"))
                )
                if item is not None
            ),
            budget_state=_budget_state(value.get("budget_state")),
            session_state=_session_state(value.get("session_state")),
            acceptance_fact_required=True,
            root_task_id=root_task_id,
            result_candidate_id=result_candidate_id,
            result_candidate_hash=_optional_text(value.get("candidate_hash")),
            expected_result_candidate_id=(
                _optional_uuid(public_candidate_map.get("candidate_id"))
                if public_candidate_map is not None
                else None
            ),
            expected_result_candidate_hash=(
                _optional_text(public_candidate_map.get("candidate_hash"))
                if public_candidate_map is not None
                else None
            ),
            acceptance_id=acceptance_id,
            accepted_candidate_id=_optional_uuid(value.get("accepted_candidate_id")),
            accepted_candidate_hash=_optional_text(value.get("accepted_candidate_hash")),
            expected_acceptance_id=(
                _optional_uuid(public_acceptance_map.get("acceptance_id"))
                if public_acceptance_map is not None
                else None
            ),
            expected_accepted_candidate_id=(
                _optional_uuid(public_acceptance_map.get("candidate_id"))
                if public_acceptance_map is not None
                else None
            ),
            expected_accepted_candidate_hash=(
                _optional_text(public_acceptance_map.get("candidate_hash"))
                if public_acceptance_map is not None
                else None
            ),
        )
    except (TypeError, ValueError):
        return None


def _optional_uuid(value: object) -> UUID | None:
    try:
        return UUID(str(value)) if value is not None else None
    except (TypeError, ValueError):
        return None


def _items(value: object) -> tuple[object, ...]:
    if isinstance(value, (list, tuple)):
        return tuple(value)
    return ()


def _budget_state(value: object) -> ResultBudgetState:
    if isinstance(value, str) and value in {
        "completed",
        "reserved",
        "exhausted",
        "unknown",
    }:
        return cast(ResultBudgetState, value)
    return "unknown"


def _session_state(value: object) -> ResultSessionState:
    if isinstance(value, str) and value in {
        "succeeded",
        "failed",
        "timed_out",
        "unknown",
    }:
        return cast(ResultSessionState, value)
    return "unknown"


def _optional_text(value: object) -> str | None:
    return value if isinstance(value, str) else None


def _public_hash(value: Mapping[str, object] | None) -> str | None:
    if value is None:
        return None
    return _optional_text(value.get("selection_hash")) or _optional_text(
        value.get("package_hash")
    )


def _artifact_id(value: object) -> UUID | None:
    if isinstance(value, Mapping):
        return _optional_uuid(value.get("artifact_id") or value.get("id"))
    return _optional_uuid(value)


def _artifact_ref(value: object) -> str | None:
    artifact_id = _artifact_id(value)
    if artifact_id is not None:
        return str(artifact_id)
    return value if isinstance(value, str) and value.strip() else None


def _marker_bytes(value: object) -> bytes | None:
    if isinstance(value, str) and value:
        return value.encode("utf-8")
    if isinstance(value, bytes) and value:
        return value
    return None


def _execution_observed(
    read_json: Callable[[dict[str, object], str], dict[str, object] | None],
    manifest: dict[str, object],
    execution_id: UUID,
) -> bool:
    workspace_id = _optional_uuid(manifest.get("workspace_id"))
    if workspace_id is None:
        return False
    body = read_json(
        manifest,
        f"/api/v1/executions/{execution_id}?workspace_id={workspace_id}",
    )
    return body is not None and _optional_uuid(body.get("id")) == execution_id


def _fetch_public_json(
    manifest: dict[str, object], path: str
) -> dict[str, object] | None:
    base_url = manifest.get("control_plane_url")
    if not isinstance(base_url, str) or not base_url.strip():
        return None
    try:
        with httpx.Client(
            base_url=base_url.rstrip("/"), timeout=5.0, trust_env=False
        ) as client:
            response = client.get(path)
        if response.status_code != 200:
            return None
        body = response.json()
    except (httpx.HTTPError, ValueError):
        return None
    return body if isinstance(body, dict) else None


def _fetch_artifact_content(
    manifest: dict[str, object], artifact_id: UUID
) -> bytes | None:
    base_url = manifest.get("control_plane_url")
    workspace_id = _optional_uuid(manifest.get("workspace_id"))
    if not isinstance(base_url, str) or workspace_id is None:
        return None
    prefix = f"/api/v1/workspaces/{workspace_id}/artifacts/{artifact_id}"
    try:
        with httpx.Client(
            base_url=base_url.rstrip("/"), timeout=5.0, trust_env=False
        ) as client:
            descriptor_response = client.get(f"{prefix}/descriptor")
            if descriptor_response.status_code != 200:
                return None
            descriptor = descriptor_response.json()
            if not isinstance(descriptor, dict) or not descriptor.get(
                "content_available"
            ):
                return None
            content_response = client.get(f"{prefix}/content")
        if content_response.status_code != 200:
            return None
        body = bytes(content_response.content)
    except (httpx.HTTPError, ValueError):
        return None
    expected_hash = _optional_text(descriptor.get("content_hash"))
    expected_size = descriptor.get("byte_size")
    if (
        expected_hash is None
        or hashlib.sha256(body).hexdigest() != expected_hash.lower()
        or not isinstance(expected_size, int)
        or len(body) != expected_size
    ):
        return None
    return body
