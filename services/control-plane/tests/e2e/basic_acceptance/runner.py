"""Scenario registry; no arbitrary commands or database fixtures are accepted."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Literal
from uuid import UUID

from .contracts import (
    InterventionSnapshot,
    RevisionSnapshot,
    ScenarioEvidence,
    UserFlowSnapshot,
)
from .evidence import EvidenceBuilder
from .intervention import run_intervention
from .revision import run_revision
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
        snapshot = _optional_user_flow_snapshot(raw)
        user_flow_evidence_kind: Literal["contract", "real_user_flow"] = (
            "real_user_flow" if mode == "real" else "contract"
        )
        return run_user_flow(
            raw,
            snapshot=snapshot,
            evidence_kind=user_flow_evidence_kind,
        )
    if name == "intervention":
        snapshot = _optional_intervention_snapshot(raw)
        intervention_evidence_kind: Literal["contract", "real_user_flow"] = (
            "real_user_flow" if mode == "real" else "contract"
        )
        return run_intervention(
            raw,
            snapshot=snapshot,
            evidence_kind=intervention_evidence_kind,
        )
    if name == "revision":
        snapshot = _optional_revision_snapshot(raw)
        revision_evidence_kind: Literal["contract", "real_user_flow"] = (
            "real_user_flow" if mode == "real" else "contract"
        )
        return run_revision(
            raw,
            snapshot=snapshot,
            evidence_kind=revision_evidence_kind,
        )
    return builder.finish(
        real_tests=0,
        incomplete=True,
        check_codes=("scenario_handler_not_enabled",),
    )


def _code_commit(manifest: dict[str, object]) -> str:
    value = manifest.get("code_commit")
    if isinstance(value, str) and len(value) == 40:
        return value
    return "0" * 40


def _optional_user_flow_snapshot(
    manifest: dict[str, object],
) -> UserFlowSnapshot | None:
    value = manifest.get("user_flow_snapshot")
    if not isinstance(value, dict):
        return None
    return UserFlowSnapshot.model_validate(value)


def _optional_intervention_snapshot(
    manifest: dict[str, object],
) -> InterventionSnapshot | None:
    value = manifest.get("intervention_snapshot")
    if not isinstance(value, dict):
        return None
    return InterventionSnapshot.model_validate(value)


def _optional_revision_snapshot(
    manifest: dict[str, object],
) -> RevisionSnapshot | None:
    value = manifest.get("revision_snapshot")
    if not isinstance(value, dict):
        return None
    return RevisionSnapshot.model_validate(value)
