"""Bounded, explicitly authorized R6.3.4 OpenCode result-input acceptance."""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import time
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit
from uuid import UUID, uuid4

import pytest
from sqlalchemy import select
from sqlalchemy.engine import make_url

from sidera_control_plane.domain.modules import WorkerAdapterCapabilityProfile

pytestmark = pytest.mark.integration

REAL_RESULT_MAX_SECONDS = 600
REAL_RESULT_MAX_ATTEMPTS = 1
REAL_RESULT_CONCURRENCY = 1
REAL_RESULT_MAX_REPORT_BYTES = 4_096


class RealResultInputGateError(ValueError):
    """A real result-input gate configuration is not safe to execute."""

    def __init__(self, message: str, *, code: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True, slots=True)
class RealResultInputGateConfig:
    executable: str
    model: str
    provider: str
    endpoint_host: str
    adapter_config_version: int
    expected_version: str | None
    fallbacks: tuple[str, ...]
    approved_fallbacks: tuple[str, ...]
    budget_microunits: int | None
    max_attempts: int = REAL_RESULT_MAX_ATTEMPTS
    concurrency: int = REAL_RESULT_CONCURRENCY
    max_seconds: int = REAL_RESULT_MAX_SECONDS
    max_report_bytes: int = REAL_RESULT_MAX_REPORT_BYTES


def real_result_gate_requested(
    environ: Mapping[str, str] | None = None,
) -> bool:
    """Return true only when both opt-in switches have their exact value ``1``."""

    values = os.environ if environ is None else environ
    return (
        values.get("SIDERA_RUN_REAL_RESULT_INPUTS") == "1"
        and values.get("SIDERA_RESULT_INPUTS_REAL_APPROVED") == "1"
    )


def load_real_result_gate_config(
    environ: Mapping[str, str] | None = None,
) -> RealResultInputGateConfig:
    """Load non-secret real-gate settings after explicit authorization."""

    values = os.environ if environ is None else environ
    if not real_result_gate_requested(values):
        raise RealResultInputGateError(
            "both real result-input authorization flags are required",
            code="authorization_required",
        )

    executable = _required(values, "SIDERA_OPENCODE_EXECUTABLE")
    model = _required(values, "SIDERA_OPENCODE_MODEL")
    provider = _required(values, "SIDERA_REAL_RESULT_INPUT_PROVIDER")
    endpoint_host = _endpoint_host(
        _required(values, "SIDERA_REAL_RESULT_INPUT_ENDPOINT_HOST")
    )
    adapter_config_version = _config_version(
        _required(values, "SIDERA_REAL_RESULT_INPUT_ADAPTER_CONFIG_VERSION")
    )
    if _contains_deepseek(provider) or _contains_deepseek(model):
        raise RealResultInputGateError(
            "direct DeepSeek provider/model is not allowed for the real gate",
            code="direct_deepseek_rejected",
        )

    fallbacks = _csv(values.get("SIDERA_REAL_RESULT_INPUT_FALLBACKS"))
    approved_fallbacks = _csv(
        values.get("SIDERA_REAL_RESULT_INPUT_APPROVED_FALLBACKS")
    )
    if fallbacks and not set(fallbacks).issubset(approved_fallbacks):
        raise RealResultInputGateError(
            "every configured fallback must be explicitly approved",
            code="fallback_not_approved",
        )
    if any(_contains_deepseek(item) for item in (*fallbacks, *approved_fallbacks)):
        raise RealResultInputGateError(
            "DeepSeek fallback is not allowed for the real gate",
            code="deepseek_fallback_rejected",
        )

    budget_microunits = _optional_nonnegative_int(
        values.get("SIDERA_REAL_RESULT_INPUT_BUDGET_MICROUNITS")
    )
    expected_version = _optional_text(values.get("SIDERA_OPENCODE_EXPECTED_VERSION"))
    return RealResultInputGateConfig(
        executable=executable,
        model=model,
        provider=provider,
        endpoint_host=endpoint_host,
        adapter_config_version=adapter_config_version,
        expected_version=expected_version,
        fallbacks=fallbacks,
        approved_fallbacks=approved_fallbacks,
        budget_microunits=budget_microunits,
    )


def validate_real_result_gate_profile(
    profile: WorkerAdapterCapabilityProfile,
) -> None:
    """Require the v2 context contract used by the result-input path."""

    if not isinstance(profile, WorkerAdapterCapabilityProfile):
        raise RealResultInputGateError(
            "OpenCode profile is invalid",
            code="profile_invalid",
        )
    if 2 not in profile.context_package_versions:
        raise RealResultInputGateError(
            "OpenCode profile does not support context package v2",
            code="context_v2_unsupported",
        )


def safe_real_result_gate_summary(
    config: RealResultInputGateConfig,
) -> dict[str, object]:
    """Return the only configuration fields allowed in gate logs."""

    return {
        "provider": config.provider,
        "model": config.model,
        "endpoint_host": config.endpoint_host,
        "adapter_config_version": config.adapter_config_version,
        "max_attempts": config.max_attempts,
        "concurrency": config.concurrency,
        "max_seconds": config.max_seconds,
        "max_report_bytes": config.max_report_bytes,
        "budget_microunits": config.budget_microunits,
    }


def run_real_result_preflight(config: RealResultInputGateConfig) -> Any:
    """Run the existing OpenCode ACP preflight only after the gate is open."""

    from sidera_control_plane.opencode_preflight import run_preflight

    report = run_preflight(
        executable=config.executable,
        expected_version=config.expected_version,
        timeout_seconds=min(30.0, float(config.max_seconds)),
    )
    if report.status != "ok" or report.capability_profile is None:
        raise RealResultInputGateError(
            "OpenCode preflight did not prove an executable ACP profile",
            code="opencode_preflight_failed",
        )
    validate_real_result_gate_profile(report.capability_profile)
    return report


def test_real_opencode_result_input_path(tmp_path: Path) -> None:
    """Run at most two sources and one v2 consumer when explicitly authorized."""

    if not real_result_gate_requested():
        pytest.skip(
            "set SIDERA_RUN_REAL_RESULT_INPUTS=1 and "
            "SIDERA_RESULT_INPUTS_REAL_APPROVED=1 to run the real gate"
        )

    started_at = time.monotonic()
    try:
        config = load_real_result_gate_config()
        database_url = _real_database_url()
        preflight = run_real_result_preflight(config)
    except RealResultInputGateError as exc:
        pytest.fail(f"real result-input preflight blocked: {exc.code}")

    print(
        json.dumps(
            {
                "real_result_input_preflight": {
                    **safe_real_result_gate_summary(config),
                    "opencode_version": preflight.version,
                    "status": "ready",
                }
            },
            sort_keys=True,
        ),
        flush=True,
    )

    if preflight.resolved_executable is None:
        pytest.fail("real result-input preflight did not resolve OpenCode")
    if preflight.capability_profile is None:
        pytest.fail("real result-input preflight did not return a capability profile")

    deadline = started_at + config.max_seconds
    adapters: list[Any] = []
    database: Any = None
    try:
        from sidera_control_plane.adapters.git_workspace import (
            GitWorktreeWorkspaceManager,
        )
        from sidera_control_plane.adapters.opencode_acp import OpenCodeACPAdapter
        from sidera_control_plane.domain.modules import (
            SandboxCapability,
            StabilityLevel,
            WorkspaceProviderCapabilityProfile,
        )
        from sidera_control_plane.persistence.database import Database
        from tests.e2e.seed_r6_3_4_result_inputs import seed_result_input_plan
        from tests.integration.result_input_scenario import ResultInputWorkerRuntime

        _ensure_real_time(deadline)
        repository, revision = _create_real_repository(tmp_path / "repository")
        workspace_root = tmp_path / "workspaces"
        workspace_root.mkdir(mode=0o700)
        workspace_manager = GitWorktreeWorkspaceManager(workspace_root)

        def adapter_factory(instance: Any) -> OpenCodeACPAdapter:
            if instance.config.get("opencode.model") != config.model:
                raise AssertionError("real gate adapter model changed during binding")
            if instance.config.get("opencode.provider") != config.provider:
                raise AssertionError(
                    "real gate adapter provider changed during binding"
                )
            adapter = OpenCodeACPAdapter(
                executable=preflight.resolved_executable,
                model=config.model,
                workspace_root=workspace_root,
                request_timeout_seconds=min(60.0, float(config.max_seconds)),
                stop_grace_seconds=2.0,
            )
            adapters.append(adapter)
            return adapter

        runtime = ResultInputWorkerRuntime(
            adapter_provider="opencode",
            adapter_interface_version="sidera.worker-adapter.v1",
            adapter_capabilities=preflight.capability_profile,
            adapter_factory=adapter_factory,
            adapter_config_namespace="opencode",
            adapter_config={
                "opencode.executable": preflight.resolved_executable,
                "opencode.model": config.model,
                "opencode.provider": config.provider,
                "opencode.endpoint_host": config.endpoint_host,
                "opencode.fallbacks": list(config.fallbacks),
                "opencode.approved_fallbacks": list(config.approved_fallbacks),
            },
            workspace_provider="git-worktree",
            workspace_interface_version="sidera.workspace-provider.v1",
            workspace_capabilities=WorkspaceProviderCapabilityProfile(
                isolation=SandboxCapability.FILESYSTEM,
                writable=True,
                git_worktree=True,
                stability=StabilityLevel.STABLE,
            ),
            workspace_factory=lambda _instance: workspace_manager,
            workspace_config_namespace="workspace",
            workspace_config={
                "workspace.repository_ref": str(repository),
                "workspace.base_revision": revision,
                "workspace.expected_outputs": ["diff"],
            },
        )
        database = Database(database_url)
        workspace_id = uuid4()
        groups = _real_result_groups()
        _seed, scenario = seed_result_input_plan(
            database,
            workspace_id,
            tmp_path / "sources",
            now=datetime.now(UTC),
            groups=groups,
            source_worker_keys=("a", "b"),
            max_attempts=1,
            worker_runtime=runtime,
        )
        scenario.confirm_prepared_plan()

        source_markers = {
            "a": f"SIDERA_REAL_R6_3_4_A_{uuid4().hex}".encode(),
            "b": f"SIDERA_REAL_R6_3_4_B_{uuid4().hex}".encode(),
        }
        source_artifact_ids: dict[str, UUID] = {}
        for key in ("a", "b"):
            _ensure_real_time(deadline)
            artifacts = scenario.complete_injected_worker(
                key,
                prompt=(
                    "In the current Git worktree, create or replace exactly one file "
                    f"named `r6-real-{key}.txt`. Its only line must be the following "
                    f"marker: {source_markers[key].decode()}. Do not change any "
                    "other file and finish after saving it."
                ),
                marker=source_markers[key],
                max_report_bytes=config.max_report_bytes,
            )
            if len(artifacts) != 1:
                raise AssertionError(f"real source {key} returned an unexpected artifact count")
            source_artifact_ids[key] = artifacts[0]

        _ensure_real_time(deadline)
        scenario.reconcile()
        selection = scenario.selection("summary")
        selected_sources = {
            source.worker_item_key
            for group in selection.groups
            for source in group.sources
        }
        if selected_sources != {"a", "b"}:
            raise AssertionError(f"real selection chose {sorted(selected_sources)}")

        _ensure_real_time(deadline)
        summary_marker = f"SIDERA_REAL_R6_3_4_SUMMARY_{uuid4().hex}".encode()
        summary_artifacts = scenario.complete_injected_worker(
            "summary",
            prompt=(
                "Read the two selected source reports from the typed Sidera result "
                "inputs in your context. Create or replace exactly one file named "
                "`r6-real-summary.txt` containing the two source facts and the exact "
                f"summary marker `{summary_marker.decode()}`. Keep it short, "
                "do not invent facts, and do not change any other file."
            ),
            marker=summary_marker,
            max_report_bytes=config.max_report_bytes,
        )
        if len(summary_artifacts) != 1:
            raise AssertionError("real summary returned an unexpected artifact count")

        for key, artifact_id in source_artifact_ids.items():
            body, content_hash = _artifact_body(scenario, artifact_id)
            if len(body) > config.max_report_bytes:
                raise AssertionError(f"real source {key} exceeded the report byte limit")
            if source_markers[key] not in body:
                raise AssertionError(f"real source {key} marker is missing from its Artifact")
            if hashlib.sha256(body).hexdigest() != content_hash:
                raise AssertionError(f"real source {key} Artifact hash does not match bytes")

        summary_body, summary_hash = _artifact_body(scenario, summary_artifacts[0])
        if summary_markers_missing(summary_body, source_markers, summary_marker):
            raise AssertionError("real summary did not retain both source markers")
        if hashlib.sha256(summary_body).hexdigest() != summary_hash:
            raise AssertionError("real summary Artifact hash does not match bytes")

        summary_execution_id = scenario.latest_execution_id("summary")
        package = scenario.context_package_for_execution(summary_execution_id)
        from sidera_control_plane.domain.context_packages import (
            ContextPackageManifestV2,
        )

        if not isinstance(package, ContextPackageManifestV2):
            raise TypeError("real summary did not receive Context Package v2")
        package_artifact_ids = {
            artifact.artifact_id
            for artifact in package.result_input_manifest.artifacts
        }
        selected_artifact_ids = {
            artifact.artifact_id
            for group in selection.groups
            for source in group.sources
            for artifact in source.artifacts
        }
        if package_artifact_ids != selected_artifact_ids:
            raise AssertionError("summary Context v2 does not match persisted Selection")

        with database.session() as session:
            from sidera_control_plane.persistence.models import (
                ArtifactModel,
                CollaborationInputSelectionModel,
                ContextPackageAcknowledgementModel,
                ContextPackageModel,
                EvidenceClaimModel,
                ExecutionModel,
                GatewayOperationModel,
            )
            selection_row = session.get(CollaborationInputSelectionModel, selection.id)
            package_row = session.get(ContextPackageModel, package.package_id)
            acknowledgement = session.scalar(
                select(ContextPackageAcknowledgementModel).where(
                    ContextPackageAcknowledgementModel.workspace_id == workspace_id,
                    ContextPackageAcknowledgementModel.package_id == package.package_id,
                    ContextPackageAcknowledgementModel.consumer_type == "worker",
                    ContextPackageAcknowledgementModel.consumer_id
                    == summary_execution_id,
                )
            )
            claims = session.scalars(
                select(EvidenceClaimModel).where(
                    EvidenceClaimModel.workspace_id == workspace_id,
                    EvidenceClaimModel.execution_id.in_(
                        [scenario.latest_execution_id("a"), scenario.latest_execution_id("b")]
                    ),
                )
            ).all()
            assert selection_row is not None
            assert selection_row.selection_hash == selection.selection_hash
            assert package_row is not None
            assert package_row.package_hash == package.package_hash
            assert acknowledgement is not None
            assert acknowledgement.acknowledged_package_hash == package.package_hash
            assert acknowledgement.context_version == package.context_version
            assert acknowledgement.adapter_config_version == config.adapter_config_version
            assert acknowledgement.operation_id is not None
            if len(claims) != 2 or {
                claim.acceptance_criterion_ref for claim in claims
            } != {"acceptance:build"}:
                raise AssertionError("real sources did not register build Evidence claims")

            execution_ids = [
                scenario.latest_execution_id(key) for key in ("a", "b", "summary")
            ]
            executions = session.scalars(
                select(ExecutionModel).where(
                    ExecutionModel.workspace_id == workspace_id,
                    ExecutionModel.id.in_(execution_ids),
                )
            ).all()
            if {execution.max_attempts for execution in executions} != {1}:
                raise AssertionError("real gate did not pin max_attempts=1")
            operations = session.scalars(
                select(GatewayOperationModel).where(
                    GatewayOperationModel.workspace_id == workspace_id,
                    GatewayOperationModel.execution_id.in_(execution_ids),
                )
            ).all()
            for execution_id in execution_ids:
                starts = [
                    operation
                    for operation in operations
                    if operation.execution_id == execution_id
                    and operation.operation_type == "start_session"
                ]
                if len(starts) != 1 or starts[0].status != "succeeded":
                    raise AssertionError(
                        f"execution {execution_id} does not have one successful start"
                    )
                if starts[0].attempt_count != 1:
                    raise AssertionError("real start operation was retried")

            actual_artifacts = session.scalars(
                select(ArtifactModel).where(
                    ArtifactModel.workspace_id == workspace_id,
                    ArtifactModel.id.in_(
                        (*source_artifact_ids.values(), *summary_artifacts)
                    ),
                )
            ).all()
            if len(actual_artifacts) != 3:
                raise AssertionError("real gate did not persist exactly three result Artifacts")

        _ensure_real_time(deadline)
        print(
            json.dumps(
                {
                    "real_result_input_acceptance": {
                        **safe_real_result_gate_summary(config),
                        "status": "passed",
                        "executions": 3,
                        "selected_sources": 2,
                        "artifacts": 3,
                        "usage": "not_reported_by_adapter",
                    }
                },
                sort_keys=True,
            ),
            flush=True,
        )
    finally:
        _terminate_real_adapters(adapters)
        if database is not None:
            database.dispose()


def _required(environ: Mapping[str, str], name: str) -> str:
    value = environ.get(name)
    if value is None or not value.strip():
        raise RealResultInputGateError(
            f"{name} is required",
            code="configuration_missing",
        )
    return value.strip()


def _optional_text(value: str | None) -> str | None:
    if value is None or not value.strip():
        return None
    return value.strip()


def _config_version(value: str) -> int:
    try:
        parsed = int(value)
    except ValueError as exc:
        raise RealResultInputGateError(
            "adapter config version must be 1",
            code="adapter_config_version_invalid",
        ) from exc
    if parsed != 1:
        raise RealResultInputGateError(
            "adapter config version must be 1",
            code="adapter_config_version_invalid",
        )
    return parsed


def _optional_nonnegative_int(value: str | None) -> int | None:
    if value is None or not value.strip():
        return None
    try:
        parsed = int(value)
    except ValueError as exc:
        raise RealResultInputGateError(
            "real result budget must be a non-negative integer",
            code="budget_invalid",
        ) from exc
    if parsed < 0:
        raise RealResultInputGateError(
            "real result budget must be a non-negative integer",
            code="budget_invalid",
        )
    return parsed


def _csv(value: str | None) -> tuple[str, ...]:
    if value is None or not value.strip():
        return ()
    return tuple(item.strip() for item in value.split(",") if item.strip())


def _endpoint_host(value: str) -> str:
    if any(character.isspace() for character in value):
        raise RealResultInputGateError(
            "endpoint host must not contain whitespace",
            code="endpoint_host_invalid",
        )
    if "://" in value:
        raise RealResultInputGateError(
            "endpoint host must not contain a URL scheme",
            code="endpoint_host_invalid",
        )
    try:
        parsed = urlsplit(f"//{value}")
        _ = parsed.port
    except ValueError as exc:
        raise RealResultInputGateError(
            "endpoint host is invalid",
            code="endpoint_host_invalid",
        ) from exc
    if (
        parsed.hostname is None
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path
        or parsed.query
        or parsed.fragment
        or any(character in value for character in "/?#@")
    ):
        raise RealResultInputGateError(
            "endpoint host must not contain path, query, fragment, or userinfo",
            code="endpoint_host_invalid",
        )
    return value.lower()


def _contains_deepseek(value: str) -> bool:
    return "deepseek" in value.casefold()


def _real_database_url(environ: Mapping[str, str] | None = None) -> str:
    values = os.environ if environ is None else environ
    raw = values.get("SIDERA_TEST_DATABASE_URL") or values.get("SIDERA_DATABASE_URL")
    if raw is None or not raw.strip():
        raise RealResultInputGateError(
            "a disposable test database URL is required",
            code="database_url_missing",
        )
    try:
        parsed = make_url(raw)
    except Exception as exc:
        raise RealResultInputGateError(
            "test database URL is invalid",
            code="database_url_invalid",
        ) from exc
    if not parsed.drivername.startswith("postgresql"):
        raise RealResultInputGateError(
            "real gate requires PostgreSQL",
            code="database_not_postgresql",
        )
    if parsed.host not in {"127.0.0.1", "localhost", "::1"}:
        raise RealResultInputGateError(
            "real gate requires a loopback test database",
            code="database_not_loopback",
        )
    database_name = (parsed.database or "").lower()
    if database_name != "sidera_test" and not database_name.startswith("sidera_test_"):
        raise RealResultInputGateError(
            "real gate requires a database named sidera_test or sidera_test_*",
            code="database_not_disposable",
        )
    return raw


def _create_real_repository(root: Path) -> tuple[Path, str]:
    root.mkdir(mode=0o700, parents=True)
    _run_git(root, ["init", "--initial-branch=main"])
    _run_git(root, ["config", "user.name", "SIDERA real acceptance"])
    _run_git(root, ["config", "user.email", "real-acceptance@sidera.local"])
    (root / "README.md").write_text("SIDERA bounded real result-input acceptance\n")
    _run_git(root, ["add", "README.md"])
    _run_git(root, ["commit", "-m", "real result-input base"])
    revision = _run_git(root, ["rev-parse", "HEAD"]).strip()
    if len(revision) != 40:
        raise AssertionError("real acceptance repository revision is not a SHA-1")
    return root, revision


def _run_git(root: Path, arguments: list[str]) -> str:
    result = subprocess.run(
        ["git", *arguments],
        cwd=root,
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout


def _real_result_groups() -> tuple[dict[str, object], ...]:
    return (
        {
            "group_key": "research",
            "title": "Real reports",
            "consumer_worker_item_key": "summary",
            "candidates": [
                {"worker_item_key": "a", "priority": 1},
                {"worker_item_key": "b", "priority": 2},
            ],
            "requirements": {
                "execution_status": "succeeded",
                "required_artifact_refs": ["artifact_type:diff"],
                "required_evidence_refs": ["acceptance:build"],
                "evidence_status": "candidate",
            },
            "mode": "at_least",
            "minimum_count": 2,
            "selection_strategy": "plan_order",
        },
    )


def _artifact_body(scenario: Any, artifact_id: UUID) -> tuple[bytes, str]:
    from sidera_control_plane.persistence.models import ArtifactModel

    with scenario.database.session() as session:
        artifact = session.get(ArtifactModel, artifact_id)
        if artifact is None:
            raise AssertionError(f"Artifact {artifact_id} was not persisted")
        return scenario.artifact_store.read(artifact.storage_ref), artifact.content_hash


def summary_markers_missing(
    body: bytes,
    source_markers: Mapping[str, bytes],
    summary_marker: bytes,
) -> bool:
    return summary_marker not in body or any(marker not in body for marker in source_markers.values())


def _ensure_real_time(deadline: float) -> None:
    if time.monotonic() > deadline:
        raise AssertionError("real result-input gate exceeded its 600 second wall-clock limit")


def _terminate_real_adapters(adapters: list[Any]) -> None:
    for adapter in adapters:
        for process in adapter.active_processes():
            if process.poll() is not None:
                continue
            process.terminate()
            try:
                process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=2)
