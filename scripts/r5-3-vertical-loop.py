#!/usr/bin/env python3
"""Run the bounded R5.3 deterministic cross-service vertical gate.

The runner deliberately keeps Knowledge content in the Knowledge Service.  It
only records IDs, statuses, hashes, and safe error codes in its result.  The
worker runtime facts are deterministic local fixtures because the current
public CP API does not register worker module definitions yet.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, cast
from urllib.parse import urlencode
from uuid import NAMESPACE_URL, UUID, uuid4, uuid5

import httpx
from sidera_control_plane.application.event_ingestor import ArtifactRegistry
from sidera_control_plane.application.knowledge_candidate_submission import (
    FileCandidateProposalSpool,
    KnowledgeCandidateProposalV1,
    KnowledgeCandidateSectionV1,
    KnowledgeCandidateSourceSuggestionV1,
    KnowledgeCandidateSubmissionService,
    KnowledgeCandidateSubmissionStatus,
)
from sidera_control_plane.domain.artifacts import ArtifactCandidate, ArtifactType
from sidera_control_plane.domain.context_packages import (
    ContextBudgetV1,
    ContextConsumerType,
    ContextPackageKnowledgeRef,
    ContextPackageManifestV1,
    ContextPackageStatus,
    ContextPurpose,
    ContextSelectionReason,
    ContextTrustLevel,
)
from sidera_control_plane.domain.modules import (
    CapabilityInput,
    IdempotencyCapability,
    ModuleDefinition,
    ModuleInstance,
    ModuleLifecycle,
    ResumeCapability,
    SandboxCapability,
    StabilityLevel,
    StopCapability,
    WorkerAdapterCapabilityProfile,
    WorkspaceProviderCapabilityProfile,
)
from sidera_control_plane.domain.planning_profiles import (
    InvalidOutputPolicy,
    PlanningBudgetPolicy,
    PlanningProfile,
    PlanningProfileStatus,
    PlanningRetryPolicy,
    UnknownOutcomePolicy,
)
from sidera_control_plane.domain.worker import Worker
from sidera_control_plane.domain.worker_runtime import WorkerRuntimeConfiguration
from sidera_control_plane.persistence.artifact_content_store import (
    LocalArtifactContentStore,
)
from sidera_control_plane.persistence.database import Database
from sidera_control_plane.persistence.models import (
    CommandModel,
    ModuleDefinitionModel,
    ModuleInstanceModel,
    OutboxMessageModel,
    PlanningProfileModel,
    WorkerModel,
    WorkerRuntimeConfigurationModel,
    WorkspaceModel,
)
from sidera_control_plane.persistence.unit_of_work import SqlAlchemyUnitOfWork
from sqlalchemy import delete, select
from sqlalchemy.exc import IntegrityError

ALLOWED_ENV_KEYS = frozenset(
    {
        "SIDERA_DATABASE_URL",
        "SIDERA_R5_3_TEST_DATABASE_URL",
        "SIDERA_KNOWLEDGE_DATABASE_URL",
        "SIDERA_R5_3_KNOWLEDGE_TEST_DATABASE_URL",
        "SIDERA_CONTROL_PLANE_URL",
        "SIDERA_KNOWLEDGE_BASE_URL",
        "SIDERA_WORKSPACE_ID",
        "SIDERA_KNOWLEDGE_LOCAL_OWNER_TOKEN",
        "SIDERA_KNOWLEDGE_CONTROL_PLANE_TOKEN",
        "SIDERA_KNOWLEDGE_MAIN_AGENT_TOKEN",
        "SIDERA_MAIN_AGENT_GATEWAY_URL",
        "SIDERA_WEB_URL",
        "SIDERA_CONTEXT_PACKAGE_CUTOVER_AT",
        "SIDERA_KNOWLEDGE_CANDIDATE_SPOOL_DIR",
        "SIDERA_ASSIGNMENT_LEASE_SECONDS",
        "SIDERA_ASSIGNMENT_LEASE_RENEW_INTERVAL_SECONDS",
        "SIDERA_AKASHIC_ENDPOINT",
        "SIDERA_AKASHIC_WORKSPACE_TOKEN",
        "SIDERA_AKASHIC_WORKSPACE",
        "SIDERA_AKASHIC_CONFIG",
        "SIDERA_OPENCODE_EXECUTABLE",
        "SIDERA_OPENCODE_EXPECTED_VERSION",
        "SIDERA_OPENCODE_MODEL",
        "SIDERA_PLANNING_OPENCODE_REQUEST_TIMEOUT_SECONDS",
        "SIDERA_PLANNING_PROFILE_ID",
        "SIDERA_PLANNING_PROFILE_VERSION",
        "SIDERA_R5_3_RUNTIME_DIR",
        "SIDERA_R5_3_ENV_FILE",
    }
)
_ENV_KEY = re.compile(r"^[A-Z][A-Z0-9_]*$")
_SAFE_CODE = re.compile(r"[^a-z0-9_.:-]+")
_FAULT_RECOVERY_RELATIVE_PATHS = (
    "services/control-plane/tests/integration/test_r5_3_fault_recovery.py",
    "services/control-plane/tests/integration/test_context_package_failures.py",
    "services/control-plane/tests/integration/test_context_package_worker.py",
    "services/control-plane/tests/integration/test_execution_context_gate.py",
    "services/control-plane/tests/integration/test_gateway_runner.py",
    "services/control-plane/tests/integration/test_knowledge_candidate_submission.py",
    "services/control-plane/tests/integration/test_outbox_dispatcher.py",
)


@dataclass(frozen=True, slots=True)
class RunIdentity:
    """Stable UUID5 and idempotency key namespace for one gate run."""

    run_id: UUID

    @property
    def namespace(self) -> UUID:
        return uuid5(NAMESPACE_URL, f"sidera:r5-3:vertical-loop:{self.run_id}")

    def entity(self, name: str) -> UUID:
        clean_name = _required_text(name, "identity name")
        return uuid5(self.namespace, clean_name)

    def idempotency(self, name: str) -> str:
        clean_name = _required_text(name, "idempotency name")
        return f"sidera-r5-3:{self.run_id}:{clean_name}"

    def marker(self, name: str) -> str:
        clean_name = _required_text(name, "marker name")
        return f"sidera-r5-3-marker:{self.run_id}:{clean_name}"


class GateFailure(RuntimeError):
    """A bounded failure that is safe to print in the release log."""

    def __init__(self, stage: str, error_code: str) -> None:
        self.stage = _safe_code(stage, fallback="unknown_stage")
        self.error_code = _safe_code(error_code, fallback="gate_failed")
        super().__init__(self.error_code)

    @classmethod
    def from_http(
        cls, *, stage: str, status_code: int, body: str | None = None
    ) -> GateFailure:
        del body
        return cls(stage, f"http_{status_code}")

    def as_dict(self) -> dict[str, str]:
        return {"stage": self.stage, "error_code": self.error_code}


class Deadline:
    def __init__(self, *, seconds: float) -> None:
        if seconds < 0:
            raise ValueError("deadline seconds cannot be negative")
        self._expires_at = time.monotonic() + seconds

    def require(self, stage: str) -> None:
        if self.remaining <= 0:
            raise GateFailure(stage, "deadline_exceeded")

    @property
    def remaining(self) -> float:
        return max(0.0, self._expires_at - time.monotonic())

    def sleep(self, seconds: float) -> None:
        delay = min(max(0.0, seconds), self.remaining)
        if delay > 0:
            time.sleep(delay)


@dataclass(frozen=True, slots=True)
class RunnerConfig:
    control_plane_url: str
    knowledge_base_url: str
    workspace_id: UUID
    database_url: str
    test_database_url: str
    candidate_spool_dir: Path
    owner_token: str
    control_plane_token: str
    deadline_seconds: float
    poll_interval_seconds: float
    run_id: UUID
    opencode_executable: str
    opencode_expected_version: str | None
    planning_profile_id: UUID | None
    planning_profile_version: int | None


class JsonHttpClient:
    """HTTP boundary that never exposes response bodies in GateFailure."""

    def __init__(self, base_url: str, *, token: str | None = None) -> None:
        self._base_url = base_url.rstrip("/")
        self._token = token
        self._client = httpx.Client(
            base_url=self._base_url,
            timeout=httpx.Timeout(10.0),
            trust_env=False,
            headers={"Accept": "application/json"},
        )

    def close(self) -> None:
        self._client.close()

    def request(
        self,
        method: str,
        path: str,
        *,
        stage: str,
        payload: Mapping[str, object] | None = None,
        allow_status: frozenset[int] = frozenset(),
        allow_json_array: bool = False,
    ) -> tuple[int, dict[str, Any] | list[Any] | None]:
        headers: dict[str, str] = {}
        if self._token:
            headers["Authorization"] = f"Bearer {self._token}"
        try:
            response = self._client.request(
                method,
                path,
                json=dict(payload) if payload is not None else None,
                headers=headers,
            )
        except httpx.HTTPError as exc:
            raise GateFailure(stage, f"transport_{type(exc).__name__}") from exc
        if response.status_code not in allow_status and not 200 <= response.status_code < 300:
            raise GateFailure.from_http(
                stage=stage,
                status_code=response.status_code,
                body=response.text,
            )
        if response.status_code in allow_status:
            return response.status_code, None
        try:
            document = response.json()
        except ValueError as exc:
            raise GateFailure(stage, "invalid_json") from exc
        if not isinstance(document, dict) and not (
            allow_json_array and isinstance(document, list)
        ):
            raise GateFailure(stage, "invalid_json_object")
        return response.status_code, cast(dict[str, Any] | list[Any], document)


def build_real_evidence_report(
    *,
    run_marker: str,
    workspace_id: UUID,
    task_id: UUID,
    execution_id: UUID,
    session_id: UUID,
    worker_item_id: UUID,
    worker_id: UUID,
    lease_id: UUID,
    fencing_token: int,
    execution_status: str,
    session_status: str,
    trace_event_counts: Mapping[str, int],
    trace_last_sequence: int,
    workspace_ref: str,
    repository_revision: str,
    provider: str,
    model: str,
    interface_version: str,
    config_version: int,
    planner_provider: str,
    planner_model: str,
    planner_interface_version: str,
    planner_config_version: int,
    workspace_provider: str,
    workspace_interface_version: str,
    workspace_config_version: int,
    opencode_version: str,
    planning_profile_id: UUID | None,
    planning_profile_version: int | None,
) -> bytes:
    """Build an artifact from allowlisted facts, never from raw runtime payloads."""

    marker = _required_text(run_marker, "run_marker")
    revision = _required_text(repository_revision, "repository_revision")
    if not re.fullmatch(r"[0-9a-f]{40}", revision):
        raise ValueError("repository_revision must be a lowercase Git SHA")
    counts: dict[str, int] = {}
    for event_type, count in sorted(trace_event_counts.items()):
        clean_type = _required_text(event_type, "trace event type")
        if not isinstance(count, int) or isinstance(count, bool) or count < 0:
            raise ValueError("trace event counts must be non-negative integers")
        counts[clean_type] = count
    if not isinstance(trace_last_sequence, int) or isinstance(trace_last_sequence, bool):
        raise TypeError("trace_last_sequence must be an integer")
    if trace_last_sequence < 0 or sum(counts.values()) != trace_last_sequence:
        raise ValueError("trace sequence count is inconsistent")

    document = {
        "schema_version": 1,
        "run_marker": marker,
        "scope": {
            "workspace_id": str(workspace_id),
            "task_id": str(task_id),
            "worker_item_id": str(worker_item_id),
            "execution_id": str(execution_id),
            "session_id": str(session_id),
        },
        "binding": {
            "worker_id": str(worker_id),
            "lease_id": str(lease_id),
            "fencing_token": _positive_int(fencing_token, "fencing_token"),
        },
        "execution": {
            "status": _required_text(execution_status, "execution_status"),
            "session_status": _required_text(session_status, "session_status"),
        },
        "trace": {
            "event_count": trace_last_sequence,
            "event_type_counts": counts,
            "last_sequence": trace_last_sequence,
        },
        "workspace": {
            "ref": _required_text(workspace_ref, "workspace_ref"),
            "repository_clean": True,
            "repository_revision": revision,
        },
        "runtime": {
            "opencode_version": _required_text(opencode_version, "opencode_version"),
            "planner": {
                "config_version": _positive_int(
                    planner_config_version, "planner_config_version"
                ),
                "interface_version": _required_text(
                    planner_interface_version, "planner_interface_version"
                ),
                "model": _required_text(planner_model, "planner_model"),
                "provider": _required_text(planner_provider, "planner_provider"),
            },
            "worker": {
                "config_version": _positive_int(config_version, "config_version"),
                "interface_version": _required_text(
                    interface_version, "interface_version"
                ),
                "model": _required_text(model, "model"),
                "provider": _required_text(provider, "provider"),
            },
            "workspace_provider": {
                "config_version": _positive_int(
                    workspace_config_version, "workspace_config_version"
                ),
                "interface_version": _required_text(
                    workspace_interface_version, "workspace_interface_version"
                ),
                "provider": _required_text(
                    workspace_provider, "workspace_provider"
                ),
            },
            "planning_profile": {
                "id": str(planning_profile_id) if planning_profile_id else None,
                "version": planning_profile_version,
            },
        },
    }
    return json.dumps(
        document, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")


class VerticalLoopRunner:
    def __init__(self, config: RunnerConfig) -> None:
        self.config = config
        self.identity = RunIdentity(config.run_id)
        self.cp = JsonHttpClient(config.control_plane_url)
        self.knowledge_owner = JsonHttpClient(
            config.knowledge_base_url, token=config.owner_token
        )
        self.knowledge_control_plane = JsonHttpClient(
            config.knowledge_base_url, token=config.control_plane_token
        )
        self.database = Database(config.database_url)

    def close(self) -> None:
        self.cp.close()
        self.knowledge_owner.close()
        self.knowledge_control_plane.close()
        self.database.dispose()

    def cleanup_test_run(self) -> dict[str, Any]:
        """Remove only deterministic fixture rows that are not audit-linked."""

        removed = self._cleanup_owned_spool_files()
        removed_rows: list[str] = []
        blocked_rows: list[str] = []
        fixture_ids = {
            "planner_profile": self.identity.entity("planning-profile"),
            "planner_module_instance": self.identity.entity("planner-module-instance"),
            "planner_module_definition": self.identity.entity("planner-module-definition"),
            "worker": self.identity.entity("worker"),
            "worker_adapter_instance": self.identity.entity("worker-adapter-instance"),
            "worker_workspace_instance": self.identity.entity("worker-workspace-instance"),
            "worker_adapter_definition": self.identity.entity("worker-adapter-definition"),
            "worker_workspace_definition": self.identity.entity("worker-workspace-definition"),
        }
        command_ids = tuple(
            self.identity.entity(f"cp-command:{label}")
            for label in (
                "planning-initial-submit",
                "planning-initial-attach",
                "planning-confirm-proposal",
                "execution-report-succeeded",
                "planning-replan-submit",
                "planning-replan-attach",
            )
        )
        with self.database.session() as session:
            deletions = (
                (
                    "worker_runtime_configuration",
                    WorkerRuntimeConfigurationModel,
                    (
                        WorkerRuntimeConfigurationModel.worker_id
                        == fixture_ids["worker"],
                        WorkerRuntimeConfigurationModel.workspace_id
                        == self.config.workspace_id,
                    ),
                ),
                (
                    "planning_profile",
                    PlanningProfileModel,
                    (
                        PlanningProfileModel.id == fixture_ids["planner_profile"],
                        PlanningProfileModel.workspace_id == self.config.workspace_id,
                    ),
                ),
                (
                    "worker",
                    WorkerModel,
                    (
                        WorkerModel.id == fixture_ids["worker"],
                        WorkerModel.workspace_id == self.config.workspace_id,
                    ),
                ),
                (
                    "planner_module_instance",
                    ModuleInstanceModel,
                    (
                        ModuleInstanceModel.id
                        == fixture_ids["planner_module_instance"],
                        ModuleInstanceModel.workspace_id == self.config.workspace_id,
                    ),
                ),
                (
                    "worker_adapter_instance",
                    ModuleInstanceModel,
                    (
                        ModuleInstanceModel.id
                        == fixture_ids["worker_adapter_instance"],
                        ModuleInstanceModel.workspace_id == self.config.workspace_id,
                    ),
                ),
                (
                    "worker_workspace_instance",
                    ModuleInstanceModel,
                    (
                        ModuleInstanceModel.id
                        == fixture_ids["worker_workspace_instance"],
                        ModuleInstanceModel.workspace_id == self.config.workspace_id,
                    ),
                ),
                (
                    "planner_module_definition",
                    ModuleDefinitionModel,
                    (
                        ModuleDefinitionModel.id
                        == fixture_ids["planner_module_definition"],
                        ModuleDefinitionModel.workspace_id == self.config.workspace_id,
                    ),
                ),
                (
                    "worker_adapter_definition",
                    ModuleDefinitionModel,
                    (
                        ModuleDefinitionModel.id
                        == fixture_ids["worker_adapter_definition"],
                        ModuleDefinitionModel.workspace_id == self.config.workspace_id,
                    ),
                ),
                (
                    "worker_workspace_definition",
                    ModuleDefinitionModel,
                    (
                        ModuleDefinitionModel.id
                        == fixture_ids["worker_workspace_definition"],
                        ModuleDefinitionModel.workspace_id == self.config.workspace_id,
                    ),
                ),
            )
            for label, model, conditions in deletions:
                try:
                    with session.begin_nested():
                        result = session.execute(delete(model).where(*conditions))
                    if result.rowcount:
                        removed_rows.append(label)
                except IntegrityError:
                    blocked_rows.append(label)
            for command_id in command_ids:
                try:
                    with session.begin_nested():
                        result = session.execute(
                            delete(CommandModel).where(
                                CommandModel.id == command_id,
                                CommandModel.workspace_id == self.config.workspace_id,
                            )
                        )
                    if result.rowcount:
                        removed_rows.append("command")
                except IntegrityError:
                    blocked_rows.append("command")
            session.commit()
        return {
            "status": (
                "cleaned"
                if not blocked_rows
                else "cleaned_with_audit_dependencies"
            ),
            "run_id": str(self.config.run_id),
            "removed_spool_files": removed,
            "removed_rows": sorted(removed_rows),
            "blocked_rows": sorted(set(blocked_rows)),
        }

    def _cleanup_owned_spool_files(self) -> int:
        root = self.config.candidate_spool_dir
        if not root.exists():
            return 0
        if root.is_symlink() or not root.is_dir():
            raise GateFailure("cleanup", "candidate_spool_root_invalid")
        slug = self._knowledge_slug()
        removed = 0
        try:
            paths = tuple(root.iterdir())
        except OSError as exc:
            raise GateFailure("cleanup", "candidate_spool_scan_failed") from exc
        for path in paths:
            if path.is_symlink() or not path.is_file() or path.suffix != ".json":
                continue
            try:
                document = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, UnicodeDecodeError, json.JSONDecodeError):
                continue
            if not isinstance(document, Mapping) or document.get("slug") != slug:
                continue
            try:
                path.unlink()
            except OSError as exc:
                raise GateFailure("cleanup", "candidate_spool_delete_failed") from exc
            removed += 1
        return removed

    def _knowledge_slug(self) -> str:
        return f"r5-3-deterministic-{self.config.run_id.hex[:12]}"

    @staticmethod
    def _gate_repository_path(candidate_spool_dir: Path, run_id: UUID) -> Path:
        return candidate_spool_dir.parent / "gate-repositories" / str(run_id)

    def _prepare_gate_repository(self) -> tuple[Path, str]:
        source = Path(__file__).resolve().parents[1]
        target = self._gate_repository_path(
            self.config.candidate_spool_dir, self.config.run_id
        )
        if target.is_symlink():
            raise GateFailure("worker_fixture", "gate_repository_symlink")
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise GateFailure("worker_fixture", "gate_repository_unavailable") from exc

        if not target.exists():
            try:
                subprocess.run(
                    ["git", "clone", "--no-hardlinks", "--quiet", str(source), str(target)],
                    check=True,
                    capture_output=True,
                    text=True,
                )
            except (OSError, subprocess.CalledProcessError) as exc:
                raise GateFailure("worker_fixture", "gate_repository_clone_failed") from exc
        if target.is_symlink() or not target.is_dir():
            raise GateFailure("worker_fixture", "gate_repository_unavailable")

        source_revision = self._git_repository_revision(source)
        target_revision = self._git_repository_revision(target)
        try:
            clean_status = subprocess.run(
                [
                    "git",
                    "-C",
                    str(target),
                    "status",
                    "--porcelain",
                    "--untracked-files=all",
                ],
                check=True,
                capture_output=True,
                text=True,
            ).stdout.strip()
        except (OSError, subprocess.CalledProcessError) as exc:
            raise GateFailure("worker_fixture", "gate_repository_unavailable") from exc
        if target_revision != source_revision:
            raise GateFailure("worker_fixture", "gate_repository_revision_mismatch")
        if clean_status:
            raise GateFailure("worker_fixture", "gate_repository_not_clean")
        return target, target_revision

    @staticmethod
    def _git_repository_revision(repository: Path) -> str:
        try:
            revision = subprocess.run(
                ["git", "-C", str(repository), "rev-parse", "HEAD"],
                check=True,
                capture_output=True,
                text=True,
            ).stdout.strip()
        except (OSError, subprocess.CalledProcessError) as exc:
            raise GateFailure("worker_fixture", "repository_revision_unavailable") from exc
        if not revision:
            raise GateFailure("worker_fixture", "repository_revision_unavailable")
        return revision

    @staticmethod
    def _first_mapping_uuid(value: object, field_name: str) -> UUID:
        mapping = _mapping(value, field_name)
        if not mapping:
            raise ValueError(f"{field_name} must not be empty")
        if any(not isinstance(key, str) or not key.strip() for key in mapping):
            raise ValueError(f"{field_name} keys must be non-empty strings")
        key = min(mapping)
        return _as_uuid(mapping[key], f"{field_name}.{key}")

    @staticmethod
    def _task_overview_is_complete(value: object) -> bool:
        if not isinstance(value, Mapping):
            return False
        subtask_count = value.get("subtask_count")
        planned_count = value.get("planned_subtask_count")
        running_count = value.get("running_subtask_count")
        completed_count = value.get("completed_subtask_count")
        failed_count = value.get("failed_subtask_count")
        return (
            isinstance(subtask_count, int)
            and not isinstance(subtask_count, bool)
            and subtask_count > 0
            and planned_count == 0
            and running_count == 0
            and completed_count == subtask_count
            and failed_count == 0
        )

    def run(self) -> dict[str, Any]:
        self._ensure_workspace()
        worker_facts = self._seed_worker_runtime()
        profile_id = self._seed_planning_profile()
        initial_knowledge = self._create_and_accept_initial_knowledge()
        planning = self._run_planning_path(profile_id, initial_knowledge)
        execution = self._run_execution_path(planning, worker_facts, initial_knowledge)
        candidate = self._run_worker_candidate_path(
            planning, execution, initial_knowledge
        )
        replan = self._run_explicit_replan(profile_id, initial_knowledge, candidate)
        self._assert_no_marker_in_control_plane()
        return {
            "status": "passed",
            "mode": "deterministic",
            "run_id": str(self.config.run_id),
            "knowledge": {
                **initial_knowledge,
                "worker_revision_number": candidate["revision_number"],
                "worker_revision_id": candidate["revision_id"],
                "worker_revision_hash": candidate["revision_hash"],
            },
            "context": {
                "planning_run_id": planning["run_id"],
                "planning_package_id": planning["package_id"],
                "planning_package_hash": planning["package_hash"],
                "planning_revision_number": planning["context_revision_number"],
                "proposal_confirmed_revision_number": planning[
                    "proposal_confirmed_revision_number"
                ],
                "post_review_revision_number": candidate[
                    "post_review_revision_number"
                ],
                "replan_request_id": replan["request_id"],
                "replan_run_id": replan["run_id"],
                "replan_package_id": replan["package_id"],
                "replan_package_hash": replan["package_hash"],
                "replan_revision_number": replan["context_revision_number"],
                "package_hashes_distinct": len(
                    {
                        planning["package_hash"],
                        execution["package_hash"],
                        replan["package_hash"],
                    }
                )
                == 3,
            },
            "execution": execution,
            "candidate_submission": {
                "submission_id": candidate["submission_id"],
                "candidate_id": candidate["candidate_id"],
                "status": candidate["status"],
            },
        }

    def record_real_execution_evidence(
        self,
        *,
        task_id: UUID,
        execution_id: UUID,
        session_id: UUID | None,
        acceptance_ref: str,
        run_marker: str,
        repository_path: Path | None = None,
    ) -> dict[str, Any]:
        """Register a redacted report after rechecking a real execution."""

        task = self._cp_get(
            f"/api/v1/tasks/{task_id}",
            params={"workspace_id": str(self.config.workspace_id)},
            stage="real_evidence_task",
        )
        criteria = tuple(
            _required_text(item, "task.acceptance_criteria.item")
            for item in _sequence(task.get("acceptance_criteria"), "task.acceptance_criteria")
        )
        acceptance = _required_text(acceptance_ref, "acceptance_ref")
        if acceptance not in criteria:
            raise GateFailure("real_evidence_task", "acceptance_ref_not_declared")

        execution = self._cp_get(
            f"/api/v1/executions/{execution_id}",
            params={"workspace_id": str(self.config.workspace_id)},
            stage="real_evidence_execution",
        )
        if execution.get("task_id") != str(task_id):
            raise GateFailure("real_evidence_execution", "execution_task_mismatch")
        if execution.get("status") != "succeeded":
            raise GateFailure("real_evidence_execution", "execution_not_succeeded")

        sessions_doc = self._cp_get(
            "/api/v1/gateway/sessions",
            params={
                "workspace_id": str(self.config.workspace_id),
                "execution_id": str(execution_id),
                "limit": "50",
            },
            stage="real_evidence_sessions",
        )
        session_items = [
            _mapping(item, "real_evidence.session")
            for item in _sequence(sessions_doc.get("items"), "real_evidence.sessions")
        ]
        if session_id is None:
            if len(session_items) != 1:
                raise GateFailure("real_evidence_sessions", "session_not_unique")
            selected_session = session_items[0]
        else:
            selected_session = next(
                (
                    item
                    for item in session_items
                    if item.get("id") == str(session_id)
                ),
                None,
            )
            if selected_session is None:
                raise GateFailure("real_evidence_sessions", "session_not_found")
        selected_session_id = _as_uuid(
            selected_session.get("id"), "real_evidence.session.id"
        )
        if selected_session.get("status") != "closed":
            raise GateFailure("real_evidence_sessions", "session_not_closed")
        if selected_session.get("execution_id") != str(execution_id):
            raise GateFailure("real_evidence_sessions", "session_execution_mismatch")

        trace = self._real_trace_summary(execution_id, selected_session_id)
        binding = self._cp_get(
            f"/api/v1/gateway/executions/{execution_id}/binding",
            params={"workspace_id": str(self.config.workspace_id)},
            stage="real_evidence_binding",
        )
        worker_id = _as_uuid(binding.get("worker_id"), "real_evidence.worker_id")
        worker_item_id = _as_uuid(
            execution.get("worker_item_id"), "real_evidence.worker_item_id"
        )
        lease_id = _as_uuid(binding.get("lease_id"), "real_evidence.lease_id")
        fencing_token = _positive_int(
            binding.get("fencing_token"), "real_evidence.fencing_token"
        )
        if selected_session.get("worker_id") != str(worker_id):
            raise GateFailure("real_evidence_binding", "session_worker_mismatch")

        runtime = self._real_runtime_facts(binding)
        repository = repository_path or (
            Path(__file__).resolve().parents[1]
            / ".sidera"
            / "workspaces"
            / str(execution_id)
        )
        repository_revision = self._real_repository_revision(repository)
        opencode_version = self._real_opencode_version(runtime["worker"]["executable"])
        workspace_ref = _required_text(
            selected_session.get("workspace_ref"), "real_evidence.workspace_ref"
        )
        report = build_real_evidence_report(
            run_marker=run_marker,
            workspace_id=self.config.workspace_id,
            task_id=task_id,
            execution_id=execution_id,
            session_id=selected_session_id,
            worker_item_id=worker_item_id,
            worker_id=worker_id,
            lease_id=lease_id,
            fencing_token=fencing_token,
            execution_status=_required_text(
                execution.get("status"), "real_evidence.execution_status"
            ),
            session_status=_required_text(
                selected_session.get("status"), "real_evidence.session_status"
            ),
            trace_event_counts=trace["event_type_counts"],
            trace_last_sequence=trace["last_sequence"],
            workspace_ref=workspace_ref,
            repository_revision=repository_revision,
            provider=runtime["worker"]["provider"],
            model=runtime["worker"]["model"],
            interface_version=runtime["worker"]["interface_version"],
            config_version=runtime["worker"]["config_version"],
            planner_provider=runtime["planner"]["provider"],
            planner_model=runtime["planner"]["model"],
            planner_interface_version=runtime["planner"]["interface_version"],
            planner_config_version=runtime["planner"]["config_version"],
            workspace_provider=runtime["workspace"]["provider"],
            workspace_interface_version=runtime["workspace"]["interface_version"],
            workspace_config_version=runtime["workspace"]["config_version"],
            opencode_version=opencode_version,
            planning_profile_id=runtime["planning_profile"]["id"],
            planning_profile_version=runtime["planning_profile"]["version"],
        )
        content_hash = hashlib.sha256(report).hexdigest()
        artifact_store = LocalArtifactContentStore(
            Path(__file__).resolve().parents[1] / ".sidera" / "artifacts"
        )
        storage_ref = artifact_store.put(
            self.config.workspace_id,
            execution_id,
            content_hash,
            report,
        )
        artifact_candidate = ArtifactCandidate(
            artifact_type=ArtifactType.REPORT,
            storage_ref=storage_ref,
            content_hash=content_hash,
            size_bytes=len(report),
            media_type="application/json",
            provenance={
                "producer": "r5-3-5-gate-verifier",
                "acceptance_criterion_refs": [acceptance],
                "conclusion": (
                    "The real OpenCode execution succeeded with a closed session, "
                    "a contiguous trace, and a clean Git worktree."
                ),
                "environment_ref": workspace_ref,
                "run_marker": _required_text(run_marker, "run_marker"),
            },
        )
        registration = ArtifactRegistry(
            lambda: SqlAlchemyUnitOfWork(self.database)
        ).register(
            workspace_id=self.config.workspace_id,
            task_id=task_id,
            execution_id=execution_id,
            session_id=selected_session_id,
            candidates=(artifact_candidate,),
            acceptance_criteria=criteria,
            producer="r5-3-5-gate-verifier",
            environment_ref=workspace_ref,
        )
        if len(registration.artifacts) != 1 or len(registration.evidence_claims) != 1:
            raise GateFailure("real_evidence_register", "registration_incomplete")
        artifact = registration.artifacts[0]
        claim = registration.evidence_claims[0]
        return {
            "status": "recorded",
            "task_id": str(task_id),
            "execution_id": str(execution_id),
            "session_id": str(selected_session_id),
            "artifact_id": str(artifact.id),
            "evidence_id": str(claim.id),
            "content_hash": content_hash,
            "size_bytes": len(report),
            "acceptance_ref": acceptance,
            "trace_event_count": trace["event_count"],
            "trace_last_sequence": trace["last_sequence"],
            "provider": runtime["worker"]["provider"],
            "model": runtime["worker"]["model"],
            "opencode_version": opencode_version,
            "planning_profile_id": str(runtime["planning_profile"]["id"]),
            "planning_profile_version": runtime["planning_profile"]["version"],
        }

    def _real_trace_summary(
        self, execution_id: UUID, session_id: UUID
    ) -> dict[str, Any]:
        cursor = 0
        expected_sequence = 1
        counts: dict[str, int] = {}
        while True:
            document = self._cp_get(
                "/api/v1/gateway/events",
                params={
                    "workspace_id": str(self.config.workspace_id),
                    "execution_id": str(execution_id),
                    "session_id": str(session_id),
                    "cursor": str(cursor),
                    "limit": "100",
                },
                stage="real_evidence_trace",
            )
            items = [
                _mapping(item, "real_evidence.event")
                for item in _sequence(document.get("items"), "real_evidence.events")
            ]
            for item in items:
                sequence = _positive_int(
                    item.get("sequence"), "real_evidence.event.sequence"
                )
                if sequence != expected_sequence:
                    raise GateFailure("real_evidence_trace", "trace_sequence_gap")
                event_type = _required_text(
                    item.get("event_type"), "real_evidence.event.event_type"
                )
                counts[event_type] = counts.get(event_type, 0) + 1
                expected_sequence += 1
            next_cursor = document.get("next_cursor")
            if next_cursor is None:
                break
            try:
                next_value = int(next_cursor)
            except (TypeError, ValueError) as exc:
                raise GateFailure("real_evidence_trace", "trace_cursor_invalid") from exc
            next_value = _positive_or_zero(next_value, "real_evidence.next_cursor")
            if next_value <= cursor:
                raise GateFailure("real_evidence_trace", "trace_cursor_stalled")
            cursor = next_value
        if expected_sequence == 1:
            raise GateFailure("real_evidence_trace", "trace_empty")
        return {
            "event_count": expected_sequence - 1,
            "event_type_counts": counts,
            "last_sequence": expected_sequence - 1,
        }

    def _real_runtime_facts(self, binding: Mapping[str, Any]) -> dict[str, Any]:
        adapter_id = _as_uuid(
            binding.get("adapter_module_id"), "real_evidence.adapter_module_id"
        )
        workspace_id = _as_uuid(
            binding.get("workspace_module_id"), "real_evidence.workspace_module_id"
        )
        profile_id = self.config.planning_profile_id
        profile_version = self.config.planning_profile_version
        if profile_id is None or profile_version is None:
            raise GateFailure("real_evidence_runtime", "planning_profile_not_configured")
        with self.database.session() as session:
            profile = session.get(PlanningProfileModel, profile_id)
            if profile is None or profile.workspace_id != self.config.workspace_id:
                raise GateFailure("real_evidence_runtime", "planning_profile_not_found")
            if profile.version != profile_version:
                raise GateFailure("real_evidence_runtime", "planning_profile_version_mismatch")

            def module_facts(module_id: UUID, label: str) -> dict[str, Any]:
                instance = session.get(ModuleInstanceModel, module_id)
                if instance is None or instance.workspace_id != self.config.workspace_id:
                    raise GateFailure("real_evidence_runtime", f"{label}_module_not_found")
                definition = session.get(ModuleDefinitionModel, instance.definition_id)
                if definition is None or definition.workspace_id != self.config.workspace_id:
                    raise GateFailure("real_evidence_runtime", f"{label}_definition_not_found")
                return {
                    "provider": definition.provider,
                    "interface_version": definition.interface_version,
                    "config_version": instance.config_version,
                    "config": dict(instance.config),
                }

            worker = module_facts(adapter_id, "worker")
            planner = module_facts(profile.planner_module_instance_id, "planner")
            workspace = module_facts(workspace_id, "workspace")
        worker_config = worker["config"]
        planner_config = planner["config"]
        worker_model = worker_config.get("opencode.model")
        planner_model = planner_config.get("planning.model")
        executable = worker_config.get("opencode.executable")
        return {
            "worker": {
                "provider": _required_text(worker["provider"], "worker.provider"),
                "model": _required_text(worker_model, "worker.model"),
                "interface_version": _required_text(
                    worker["interface_version"], "worker.interface_version"
                ),
                "config_version": _positive_int(
                    worker["config_version"], "worker.config_version"
                ),
                "executable": _required_text(executable, "worker.executable"),
            },
            "planner": {
                "provider": _required_text(planner["provider"], "planner.provider"),
                "model": _required_text(planner_model, "planner.model"),
                "interface_version": _required_text(
                    planner["interface_version"], "planner.interface_version"
                ),
                "config_version": _positive_int(
                    planner["config_version"], "planner.config_version"
                ),
            },
            "workspace": {
                "provider": _required_text(
                    workspace["provider"], "workspace.provider"
                ),
                "interface_version": _required_text(
                    workspace["interface_version"], "workspace.interface_version"
                ),
                "config_version": _positive_int(
                    workspace["config_version"], "workspace.config_version"
                ),
            },
            "planning_profile": {"id": profile_id, "version": profile_version},
        }

    @staticmethod
    def _real_repository_revision(repository: Path) -> str:
        if repository.is_symlink() or not repository.is_dir():
            raise GateFailure("real_evidence_worktree", "repository_unavailable")
        try:
            status = subprocess.run(
                ["git", "-C", str(repository), "status", "--porcelain", "--untracked-files=all"],
                check=True,
                capture_output=True,
                text=True,
            ).stdout.strip()
            revision = subprocess.run(
                ["git", "-C", str(repository), "rev-parse", "HEAD"],
                check=True,
                capture_output=True,
                text=True,
            ).stdout.strip()
        except (OSError, subprocess.CalledProcessError) as exc:
            raise GateFailure("real_evidence_worktree", "repository_unavailable") from exc
        if status:
            raise GateFailure("real_evidence_worktree", "repository_not_clean")
        if not re.fullmatch(r"[0-9a-f]{40}", revision):
            raise GateFailure("real_evidence_worktree", "repository_revision_invalid")
        return revision

    def _real_opencode_version(self, executable: str) -> str:
        try:
            completed = subprocess.run(
                [executable, "--version"],
                check=False,
                capture_output=True,
                text=True,
                timeout=10,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise GateFailure("real_evidence_runtime", "opencode_version_unavailable") from exc
        if completed.returncode != 0:
            raise GateFailure("real_evidence_runtime", "opencode_version_unavailable")
        match = re.search(r"(?<!\d)(\d+\.\d+\.\d+)(?!\d)", completed.stdout)
        if match is None:
            raise GateFailure("real_evidence_runtime", "opencode_version_invalid")
        version = match.group(1)
        if (
            self.config.opencode_expected_version is not None
            and version != self.config.opencode_expected_version
        ):
            raise GateFailure("real_evidence_runtime", "opencode_version_mismatch")
        return version

    def _create_and_accept_initial_knowledge(self) -> dict[str, Any]:
        candidate_id = self.identity.entity("knowledge-initial-candidate")
        source_id = self.identity.entity("knowledge-initial-source")
        payload = {
            "candidate_schema_version": 1,
            "candidate_id": str(candidate_id),
            "operation": "create_page",
            "target_page_id": None,
            "expected_revision_id": None,
            "expected_revision_hash": None,
            "title": "R5.3 deterministic knowledge fixture",
            "slug": self._knowledge_slug(),
            "summary": "A deterministic knowledge fixture for the vertical gate.",
            "tags": ["r5-3", "deterministic"],
            "sections": [
                {
                    "section_key": "gate-context",
                    "heading": "Gate context",
                    "summary": "Immutable input for the planning context.",
                    "body_markdown": self.identity.marker("knowledge-initial"),
                    "ordinal": 0,
                }
            ],
            "relations": [],
            "sources": [
                {
                    "source_id": str(source_id),
                    "source_type": "manual",
                    "label": "r5-3 deterministic fixture",
                    "uri": None,
                    "workspace_relative_path": None,
                    "git_revision": None,
                    "digest": None,
                    "metadata": {},
                }
            ],
            "provenance": {},
            "supersedes_candidate_id": None,
        }
        submitted = self._knowledge_command(
            "knowledge-initial-submit",
            actor_type="local_owner",
            payload=payload,
            target_id=candidate_id,
        )
        if submitted.get("status") != "applied":
            raise GateFailure("knowledge_initial_submit", "command_not_applied")

        candidate = self._knowledge_get(
            f"/api/v1/workspaces/{self.config.workspace_id}/knowledge/candidates/{candidate_id}",
            stage="knowledge_initial_candidate",
        )
        summary = _mapping(candidate.get("summary"), "candidate.summary")
        candidate_hash = _required_text(summary.get("candidate_hash"), "candidate_hash")
        candidate_version = _positive_int(summary.get("version"), "candidate.version")
        reviewed = self._knowledge_command(
            "knowledge-initial-review",
            actor_type="local_owner",
            payload={
                "candidate_id": str(candidate_id),
                "decision": "accept",
                "reason": "Accepted by the deterministic owner gate.",
                "observed_candidate_hash": candidate_hash,
                "observed_target_revision_id": None,
                "observed_target_revision_hash": None,
                "expected_candidate_version": candidate_version,
            },
            target_id=candidate_id,
        )
        reviewed_result = _mapping(reviewed.get("result"), "review.result")
        page_id = _as_uuid(reviewed_result.get("page_id"), "review.page_id")
        revision_id = _as_uuid(reviewed_result.get("revision_id"), "review.revision_id")
        revision = self._knowledge_get(
            f"/api/v1/workspaces/{self.config.workspace_id}/knowledge/pages/{page_id}/revisions/{revision_id}",
            stage="knowledge_initial_revision",
        )
        sections = _sequence(revision.get("sections"), "revision.sections")
        section = _mapping(sections[0], "revision.section")
        return {
            "page_id": str(page_id),
            "initial_revision_id": str(revision_id),
            "initial_revision_hash": _required_text(
                revision.get("content_hash"), "revision.content_hash"
            ),
            "initial_revision_number": _positive_int(
                revision.get("revision_number"), "revision.revision_number"
            ),
            "section_key": _required_text(section.get("section_key"), "section_key"),
            "section_hash": _required_text(section.get("section_hash"), "section_hash"),
            "source_ids": [str(item) for item in _sequence(revision.get("source_refs"), "revision.source_refs")],
        }

    def _run_planning_path(
        self, profile_id: UUID, knowledge: Mapping[str, Any]
    ) -> dict[str, Any]:
        request_id, context_id = self._submit_planning_request(
            profile_id, "planning-initial", knowledge
        )
        context = self._cp_get(
            f"/api/v1/workspaces/{self.config.workspace_id}/planning-requests/{request_id}/work-context",
            stage="planning_context_created",
        )
        self._attach_knowledge(context, knowledge, "planning-initial-attach")
        context = self._cp_get(
            f"/api/v1/workspaces/{self.config.workspace_id}/planning-requests/{request_id}/work-context",
            stage="planning_context_attached",
        )
        if context.get("current_revision_number") != 2:
            raise GateFailure("planning_context_attached", "context_revision_mismatch")

        detail = self._wait_for(
            "planning_run_created",
            lambda: self._cp_get_optional(
                f"/api/v1/workspaces/{self.config.workspace_id}/planning-requests/{request_id}",
                stage="planning_run_created",
            ),
            lambda value: value is not None and value.get("current_run_id") is not None,
        )
        run_id = _as_uuid(detail.get("current_run_id"), "planning.current_run_id")
        package = self._wait_for_package(
            run_id, "planning_package", ContextConsumerType.PLANNER
        )
        if package.get("work_context_revision_number") != 2:
            raise GateFailure("planning_package", "package_context_revision_mismatch")
        self._assert_recomputable_package(package, "planning_package")

        proposal_detail = self._wait_for(
            "planning_proposal",
            lambda: self._cp_get_optional(
                f"/api/v1/workspaces/{self.config.workspace_id}/planning-requests/{request_id}",
                stage="planning_proposal",
            ),
            lambda value: value is not None
            and value.get("status") == "proposal_ready"
            and value.get("current_proposal_revision") == 1,
        )
        proposal = self._cp_get(
            f"/api/v1/workspaces/{self.config.workspace_id}/planning-requests/{request_id}/proposals/1",
            stage="planning_proposal_detail",
        )
        confirmed = self._cp_command(
            "planning-confirm-proposal",
            "ConfirmPlanningProposal",
            "planning_request",
            {
                "proposal_revision": 1,
                "proposal_content_hash": _required_text(
                    proposal.get("content_hash"), "proposal.content_hash"
                ),
                "profile_id": str(profile_id),
                "profile_version": 1,
            },
            target_id=request_id,
            expected_version=_positive_int(
                proposal_detail.get("version"), "planning.version"
            ),
        )
        root_task_id = _as_uuid(
            _mapping(confirmed.get("data"), "confirm.data").get("root_task_id"),
            "confirm.root_task_id",
        )
        materialization = self._wait_for(
            "planning_materialization",
            lambda: self._cp_get_optional(
                f"/api/v1/workspaces/{self.config.workspace_id}/planning-requests/{request_id}/materialization",
                stage="planning_materialization",
            ),
            lambda value: value is not None,
        )
        return {
            "request_id": str(request_id),
            "context_id": str(context_id),
            "run_id": str(run_id),
            "package_id": _required_text(package.get("package_id"), "package_id"),
            "package_hash": _required_hash(package.get("package_hash"), "package_hash"),
            "context_revision_id": _required_text(
                context.get("current_revision_id"), "context.current_revision_id"
            ),
            "context_revision_number": _positive_int(
                context.get("current_revision_number"), "context.current_revision_number"
            ),
            "proposal_confirmed_revision_number": self._current_context_number(
                root_task_id
            ),
            "root_task_id": str(root_task_id),
            "plan_generation": _positive_int(
                materialization.get("plan_generation"), "materialization.plan_generation"
            ),
            "subtask_task_id": self._first_mapping_uuid(
                materialization.get("subtask_task_mapping"), "subtask_task_mapping"
            ),
            "worker_item_id": self._first_mapping_uuid(
                materialization.get("worker_item_mapping"), "worker_item_mapping"
            ),
        }

    def _run_execution_path(
        self,
        planning: Mapping[str, Any],
        worker: Mapping[str, UUID],
        knowledge: Mapping[str, Any],
    ) -> dict[str, Any]:
        subtask_id = _as_uuid(planning.get("subtask_task_id"), "subtask_task_id")
        worker_id = _as_uuid(worker.get("worker_id"), "worker_id")
        executions = self._wait_for(
            "execution_created",
            lambda: self._cp_list_executions(subtask_id),
            lambda value: bool(value),
        )
        execution = _mapping(executions[0], "execution")
        execution_id = _as_uuid(execution.get("id"), "execution.id")
        package = self._wait_for_package(
            execution_id, "execution_package", ContextConsumerType.WORKER
        )
        self._assert_recomputable_package(package, "execution_package")
        if package.get("work_context_revision_number") != 3:
            raise GateFailure("execution_package", "package_context_revision_mismatch")
        started = self._wait_for(
            "execution_started",
            lambda: self._cp_get(
                f"/api/v1/executions/{execution_id}",
                stage="execution_started",
                params={"workspace_id": str(self.config.workspace_id)},
            ),
            lambda value: value.get("status") == "running",
        )
        sessions = self._wait_for(
            "execution_session_started",
            lambda: self._cp_get(
                "/api/v1/gateway/sessions",
                stage="execution_session_started",
                params={
                    "workspace_id": str(self.config.workspace_id),
                    "execution_id": str(execution_id),
                },
            ).get("items", []),
            lambda value: bool(value),
        )
        session_id = _as_uuid(_mapping(sessions[0], "session").get("id"), "session.id")
        self._wait_for(
            "gateway_assignment_processed",
            lambda: self._gateway_assignment_state(execution_id),
            lambda value: value is not None and value.get("status") == "published",
        )
        binding = self._cp_get(
            f"/api/v1/gateway/executions/{execution_id}/binding",
            params={"workspace_id": str(self.config.workspace_id)},
            stage="execution_binding",
        )
        lease_id = _as_uuid(binding.get("lease_id"), "binding.lease_id")
        fencing_token = _positive_int(binding.get("fencing_token"), "binding.fencing_token")
        latest_execution = self._cp_get(
            f"/api/v1/executions/{execution_id}",
            params={"workspace_id": str(self.config.workspace_id)},
            stage="execution_before_finish",
        )
        self._cp_command(
            "execution-report-succeeded",
            "ReportExecutionSucceeded",
            "execution",
            {
                "worker_id": str(worker_id),
                "lease_id": str(lease_id),
                "fencing_token": fencing_token,
            },
            actor={"type": "worker", "id": str(worker_id)},
            target_id=execution_id,
            expected_version=_positive_int(
                latest_execution.get("version"), "execution.version"
            ),
        )
        finished = self._wait_for(
            "execution_succeeded",
            lambda: self._cp_get(
                f"/api/v1/executions/{execution_id}",
                stage="execution_succeeded",
                params={"workspace_id": str(self.config.workspace_id)},
            ),
            lambda value: value.get("status") == "succeeded",
        )
        subtask = self._cp_get(
            f"/api/v1/tasks/{subtask_id}",
            stage="task_before_complete",
            params={"workspace_id": str(self.config.workspace_id)},
        )
        if subtask.get("status") != "waiting":
            raise GateFailure("task_before_complete", "task_not_waiting")
        self._cp_command(
            "task-complete",
            "CompleteTask",
            "task",
            {},
            target_id=subtask_id,
            expected_version=_positive_int(subtask.get("version"), "task.version"),
        )
        completed_task = self._wait_for(
            "task_completed",
            lambda: self._cp_get(
                f"/api/v1/tasks/{subtask_id}",
                stage="task_completed",
                params={"workspace_id": str(self.config.workspace_id)},
            ),
            lambda value: value.get("status") == "succeeded",
        )
        overview = self._wait_for(
            "task_completed_projection",
            lambda: self._cp_get(
                f"/api/v1/tasks/{planning['root_task_id']}/overview",
                stage="task_completed_projection",
                params={"workspace_id": str(self.config.workspace_id)},
            ),
            self._task_overview_is_complete,
        )
        del knowledge, started, completed_task
        return {
            "execution_id": str(execution_id),
            "worker_item_id": _required_text(
                execution.get("worker_item_id"), "execution.worker_item_id"
            ),
            "status": _required_text(finished.get("status"), "execution.status"),
            "session_id": str(session_id),
            "package_id": _required_text(package.get("package_id"), "package_id"),
            "package_hash": _required_hash(package.get("package_hash"), "package_hash"),
            "task_completed_count": _positive_or_zero(
                overview.get("completed_subtask_count"),
                "overview.completed_subtask_count",
            ),
        }

    def _gateway_assignment_state(self, execution_id: UUID) -> dict[str, str] | None:
        with self.database.session() as session:
            rows = session.scalars(
                select(OutboxMessageModel).where(
                    OutboxMessageModel.topic == "execution.ExecutionLeased"
                )
            ).all()
        for row in rows:
            payload = dict(row.payload)
            if str(payload.get("aggregate_id")) != str(execution_id):
                continue
            if row.published_at is not None:
                return {"status": "published"}
            return {"status": "failed" if row.last_error else "pending"}
        return None

    def _run_worker_candidate_path(
        self,
        planning: Mapping[str, Any],
        execution: Mapping[str, Any],
        knowledge: Mapping[str, Any],
    ) -> dict[str, Any]:
        execution_id = _as_uuid(execution.get("execution_id"), "execution_id")
        worker_item_id = _as_uuid(execution.get("worker_item_id"), "worker_item_id")
        page_id = _as_uuid(knowledge.get("page_id"), "knowledge.page_id")
        initial_revision_id = _as_uuid(
            knowledge.get("initial_revision_id"), "knowledge.initial_revision_id"
        )
        proposal = KnowledgeCandidateProposalV1(
            operation="revise_page",
            target_page_id=page_id,
            expected_revision_id=initial_revision_id,
            expected_revision_hash=_required_hash(
                knowledge.get("initial_revision_hash"), "initial_revision_hash"
            ),
            title="R5.3 deterministic worker knowledge fixture",
            slug=self._knowledge_slug(),
            summary="A worker candidate queued after successful execution.",
            tags=("r5-3", "worker-result"),
            sections=(
                KnowledgeCandidateSectionV1(
                    section_key="worker-result",
                    heading="Worker result",
                    summary="Pending owner review.",
                    body_markdown=self.identity.marker("knowledge-worker"),
                    ordinal=0,
                ),
            ),
            source_suggestions=(
                self._worker_candidate_source(
                    self.identity.entity("knowledge-worker-source"),
                ),
            ),
            artifact_refs=(),
            evidence_refs=(),
        )
        spool = FileCandidateProposalSpool(
            self.config.candidate_spool_dir,
            quota_bytes=8 * 1024 * 1024,
        )
        service = KnowledgeCandidateSubmissionService(spool)
        with SqlAlchemyUnitOfWork(self.database) as uow:
            submission = service.queue_in_uow(
                uow,
                workspace_id=self.config.workspace_id,
                producer_type="worker",
                producer_id=execution_id,
                proposal=proposal,
                status=KnowledgeCandidateSubmissionStatus.QUEUED,
                now=datetime.now(UTC),
                correlation_id=self.identity.entity("knowledge-worker-correlation"),
                execution_id=execution_id,
                worker_item_id=worker_item_id,
                task_id=_as_uuid(planning.get("root_task_id"), "planning.root_task_id"),
                subtask_task_id=_as_uuid(
                    planning.get("subtask_task_id"), "planning.subtask_task_id"
                ),
                actor_id="r5-3-runner",
            )
        status = self._wait_for(
            "worker_candidate_submitted",
            lambda: self._cp_get(
                f"/api/v1/workspaces/{self.config.workspace_id}/tasks/{planning['root_task_id']}/knowledge-submissions",
                stage="worker_candidate_submitted",
            ).get("items", []),
            lambda value: any(
                _mapping(item, "submission").get("submission_id") == str(submission.id)
                and _mapping(item, "submission").get("status") == "submitted"
                for item in value
            ),
        )
        submission_doc = next(
            _mapping(item, "submission")
            for item in status
            if _mapping(item, "submission").get("submission_id") == str(submission.id)
        )
        candidate_id = _as_uuid(
            submission_doc.get("terminal_candidate_id"),
            "submission.terminal_candidate_id",
        )
        candidate = self._knowledge_get(
            f"/api/v1/workspaces/{self.config.workspace_id}/knowledge/candidates/{candidate_id}",
            stage="worker_candidate_detail",
        )
        candidate_summary = _mapping(candidate.get("summary"), "worker_candidate.summary")
        execution_after_submission = self._cp_get(
            f"/api/v1/executions/{execution_id}",
            params={"workspace_id": str(self.config.workspace_id)},
            stage="execution_after_candidate_submission",
        )
        if execution_after_submission.get("status") != "succeeded":
            raise GateFailure("execution_after_candidate_submission", "task_waited_for_candidate")
        context_after_submission = self._cp_get(
            f"/api/v1/workspaces/{self.config.workspace_id}/tasks/{planning['root_task_id']}/work-context",
            stage="context_after_candidate_submission",
        )
        review = self._knowledge_command(
            "knowledge-worker-review",
            actor_type="local_owner",
            payload={
                "candidate_id": str(candidate_id),
                "decision": "accept",
                "reason": "Accepted by the deterministic owner gate.",
                "observed_candidate_hash": _required_hash(
                    candidate_summary.get("candidate_hash"), "worker.candidate_hash"
                ),
                "observed_target_revision_id": str(initial_revision_id),
                "observed_target_revision_hash": _required_hash(
                    knowledge.get("initial_revision_hash"), "initial_revision_hash"
                ),
                "expected_candidate_version": _positive_int(
                    candidate_summary.get("version"), "worker.candidate.version"
                ),
            },
            target_id=candidate_id,
        )
        review_result = _mapping(review.get("result"), "worker_review.result")
        revision_id = _as_uuid(review_result.get("revision_id"), "worker.revision_id")
        revision = self._knowledge_get(
            f"/api/v1/workspaces/{self.config.workspace_id}/knowledge/pages/{page_id}/revisions/{revision_id}",
            stage="worker_revision_detail",
        )
        revision_sections = _sequence(
            revision.get("sections"), "worker_revision.sections"
        )
        if not revision_sections:
            raise GateFailure("worker_revision_detail", "revision_has_no_sections")
        worker_section = _mapping(revision_sections[0], "worker_revision.section")
        worker_section_key = _required_text(
            worker_section.get("section_key"), "worker.section_key"
        )
        worker_section_hash = _required_hash(
            worker_section.get("section_hash"), "worker.section_hash"
        )
        worker_source_ids = tuple(
            sorted(
                (
                    _as_uuid(item, "worker.source_refs.item")
                    for item in _sequence(
                        revision.get("source_refs"), "worker.source_refs"
                    )
                ),
                key=str,
            )
        )
        spool_files = tuple(self.config.candidate_spool_dir.glob("*.json"))
        if spool_files:
            raise GateFailure("worker_candidate_spool_cleanup", "terminal_spool_not_deleted")
        context_after_review = self._cp_get(
            f"/api/v1/workspaces/{self.config.workspace_id}/tasks/{planning['root_task_id']}/work-context",
            stage="context_after_worker_review",
        )
        if (
            context_after_review.get("current_revision_id")
            != context_after_submission.get("current_revision_id")
            or context_after_review.get("current_revision_number")
            != context_after_submission.get("current_revision_number")
        ):
            raise GateFailure("context_after_worker_review", "implicit_context_mutation")
        return {
            "submission_id": str(submission.id),
            "candidate_id": str(candidate_id),
            "status": _required_text(submission_doc.get("status"), "submission.status"),
            "revision_id": str(revision_id),
            "revision_hash": _required_hash(revision.get("content_hash"), "worker.content_hash"),
            "revision_number": _positive_int(
                revision.get("revision_number"), "worker.revision_number"
            ),
            "section_key": worker_section_key,
            "section_hash": worker_section_hash,
            "source_ids": [str(item) for item in worker_source_ids],
            "post_review_revision_number": _positive_int(
                context_after_review.get("current_revision_number"),
                "context_after_review.current_revision_number",
            ),
        }

    @staticmethod
    def _worker_candidate_source(source_id: UUID) -> KnowledgeCandidateSourceSuggestionV1:
        return KnowledgeCandidateSourceSuggestionV1(
            source_type="task",
            label="r5-3 deterministic worker result",
            source_id=source_id,
        )

    @staticmethod
    def _knowledge_ref_after_revision(
        knowledge: Mapping[str, Any],
        worker_candidate: Mapping[str, Any],
        *,
        section_key: str,
        section_hash: str,
        source_ids: Sequence[object],
    ) -> dict[str, Any]:
        revised = dict(knowledge)
        revised["initial_revision_id"] = worker_candidate["revision_id"]
        revised["initial_revision_hash"] = worker_candidate["revision_hash"]
        revised["section_key"] = _required_text(section_key, "worker.section_key")
        revised["section_hash"] = _required_hash(section_hash, "worker.section_hash")
        revised["source_ids"] = [
            str(_as_uuid(item, "worker.source_ids.item")) for item in source_ids
        ]
        return revised

    def _run_explicit_replan(
        self,
        profile_id: UUID,
        knowledge: Mapping[str, Any],
        worker_candidate: Mapping[str, Any],
    ) -> dict[str, Any]:
        revised_knowledge = self._knowledge_ref_after_revision(
            knowledge,
            worker_candidate,
            section_key=_required_text(
                worker_candidate.get("section_key"), "worker.section_key"
            ),
            section_hash=_required_hash(
                worker_candidate.get("section_hash"), "worker.section_hash"
            ),
            source_ids=_sequence(
                worker_candidate.get("source_ids"), "worker.source_ids"
            ),
        )
        request_id, context_id = self._submit_planning_request(
            profile_id, "planning-replan", revised_knowledge
        )
        context = self._cp_get(
            f"/api/v1/workspaces/{self.config.workspace_id}/planning-requests/{request_id}/work-context",
            stage="replan_context_created",
        )
        self._attach_knowledge(context, revised_knowledge, "planning-replan-attach")
        context = self._cp_get(
            f"/api/v1/workspaces/{self.config.workspace_id}/planning-requests/{request_id}/work-context",
            stage="replan_context_attached",
        )
        detail = self._wait_for(
            "replan_run_created",
            lambda: self._cp_get_optional(
                f"/api/v1/workspaces/{self.config.workspace_id}/planning-requests/{request_id}",
                stage="replan_run_created",
            ),
            lambda value: value is not None and value.get("current_run_id") is not None,
        )
        run_id = _as_uuid(detail.get("current_run_id"), "replan.current_run_id")
        package = self._wait_for_package(
            run_id, "replan_package", ContextConsumerType.PLANNER
        )
        if package.get("work_context_revision_number") != 2:
            raise GateFailure("replan_package", "package_context_revision_mismatch")
        self._assert_recomputable_package(package, "replan_package")
        return {
            "request_id": str(request_id),
            "context_id": str(context_id),
            "run_id": str(run_id),
            "package_id": _required_text(package.get("package_id"), "replan.package_id"),
            "package_hash": _required_hash(package.get("package_hash"), "replan.package_hash"),
            "context_revision_number": _positive_int(
                context.get("current_revision_number"), "replan.context_revision_number"
            ),
        }

    def _submit_planning_request(
        self, profile_id: UUID, label: str, knowledge: Mapping[str, Any]
    ) -> tuple[UUID, UUID]:
        result = self._cp_command(
            f"{label}-submit",
            "SubmitPlanningRequest",
            "workspace",
            {
                "profile_id": str(profile_id),
                "profile_version": 1,
                "objective": "Produce one deterministic reviewable work item.",
                "constraints": (
                    "Use one subtask and one worker item.",
                    "Do not ask for clarification.",
                ),
                "acceptance_criteria": ("The work item reaches a successful execution state.",),
                "context_refs": (
                    f"knowledge://{knowledge['page_id']}/{knowledge['initial_revision_id']}",
                ),
                "requested_outputs": ("execution evidence",),
                "source_type": "integration",
                "source_ref": f"r5-3-vertical-loop:{label}",
            },
        )
        data = _mapping(result.get("data"), f"{label}.data")
        return (
            _as_uuid(data.get("planning_request_id"), f"{label}.planning_request_id"),
            _as_uuid(data.get("work_context_id"), f"{label}.work_context_id"),
        )

    def _attach_knowledge(
        self,
        context: Mapping[str, Any],
        knowledge: Mapping[str, Any],
        label: str,
    ) -> None:
        self._cp_command(
            label,
            "AttachKnowledgeRevision",
            "work_context",
            {
                "knowledge_ref": {
                    "page_id": knowledge["page_id"],
                    "revision_id": knowledge["initial_revision_id"],
                    "revision_hash": knowledge["initial_revision_hash"],
                    "section_key": knowledge["section_key"],
                    "section_hash": knowledge["section_hash"],
                    "source_ids": knowledge["source_ids"],
                    "ordinal": 0,
                },
                "selection_reason": "The gate explicitly selected the accepted revision.",
            },
            target_id=_as_uuid(context.get("id"), f"{label}.context_id"),
            expected_version=_positive_int(context.get("version"), f"{label}.context.version"),
        )

    def _seed_planning_profile(self) -> UUID:
        definition_id = self.identity.entity("planner-module-definition")
        instance_id = self.identity.entity("planner-module-instance")
        profile_id = self.identity.entity("planning-profile")
        capabilities = self._planner_capabilities()
        definition = ModuleDefinition(
            id=definition_id,
            workspace_id=self.config.workspace_id,
            module_type="planner_adapter",
            provider="fake",
            interface_version="sidera.planner.v1",
            config_schema_version=1,
            capabilities=capabilities,
            lifecycle=ModuleLifecycle.ACTIVE,
        )
        instance = ModuleInstance(
            id=instance_id,
            workspace_id=self.config.workspace_id,
            definition_id=definition_id,
            instance_name=(
                f"r5-3-deterministic-planner-{self.config.run_id.hex[:12]}"
            ),
            config_version=1,
            config_namespace="planning",
            config={"planning.scenario": "proposal"},
            secret_refs=(),
            lifecycle=ModuleLifecycle.ACTIVE,
        )
        profile = PlanningProfile(
            id=profile_id,
            workspace_id=self.config.workspace_id,
            profile_key=f"r5-3-deterministic-{self.config.run_id.hex[:12]}",
            version=1,
            display_name="R5.3 deterministic planner",
            planner_module_instance_id=instance_id,
            planner_module_config_version=1,
            planner_interface_version="sidera.planner.v1",
            prompt_policy_version="r5-3-deterministic",
            system_policy={},
            proposal_schema_version=2,
            allowed_capabilities=("code",),
            adapter_preferences=("fake",),
            adapter_forbidden=(),
            budget=PlanningBudgetPolicy(
                max_context_bytes=64_000,
                max_tokens=10_000,
                max_cost_microunits=1_000_000,
                max_runtime_seconds=600,
                max_revisions=3,
                max_nodes=20,
                max_concurrency=2,
            ),
            retry_policy=PlanningRetryPolicy(
                max_retries=0,
                backoff_seconds=(),
                unknown_outcome_policy=UnknownOutcomePolicy.BLOCK,
                invalid_output_policy=InvalidOutputPolicy.BLOCK,
            ),
            default_priority=50,
            execution_max_attempts=1,
            execution_budget_level="standard",
            user_selectable=True,
            status=PlanningProfileStatus.ACTIVE,
            planner_secret_refs=(),
        )
        with SqlAlchemyUnitOfWork(self.database) as uow:
            if uow.modules.get_definition(definition_id, self.config.workspace_id) is None:
                uow.modules.add_definition(definition)
            if uow.modules.get_instance(instance_id, self.config.workspace_id) is None:
                uow.modules.add_instance(instance)
            if uow.planning_profiles.get(profile_id, self.config.workspace_id, version=1) is None:
                uow.planning_profiles.add(profile)
        return profile_id

    def _seed_worker_runtime(self) -> dict[str, UUID]:
        adapter_definition_id = self.identity.entity("worker-adapter-definition")
        workspace_definition_id = self.identity.entity("worker-workspace-definition")
        adapter_instance_id = self.identity.entity("worker-adapter-instance")
        workspace_instance_id = self.identity.entity("worker-workspace-instance")
        worker_id = self.identity.entity("worker")
        adapter_capabilities = WorkerAdapterCapabilityProfile(
            input=CapabilityInput.QUEUED,
            idempotency=IdempotencyCapability.NONE,
            resume=ResumeCapability.SAME_PROCESS,
            stop=StopCapability.EXACT_EXECUTION,
            sandbox=SandboxCapability.FILESYSTEM,
            stability=StabilityLevel.STABLE,
            context_package_versions=(1,),
        )
        workspace_capabilities = WorkspaceProviderCapabilityProfile(
            isolation=SandboxCapability.ISOLATED_RUNTIME,
            writable=True,
            git_worktree=True,
            stability=StabilityLevel.STABLE,
        )
        repository, revision = self._prepare_gate_repository()
        definitions = (
            ModuleDefinition(
                id=adapter_definition_id,
                workspace_id=self.config.workspace_id,
                module_type="worker_adapter",
                provider="fake",
                interface_version="sidera.worker-adapter.v1",
                config_schema_version=1,
                capabilities=adapter_capabilities,
                lifecycle=ModuleLifecycle.ACTIVE,
            ),
            ModuleDefinition(
                id=workspace_definition_id,
                workspace_id=self.config.workspace_id,
                module_type="workspace_provider",
                provider="git-worktree",
                interface_version="sidera.workspace-provider.v1",
                config_schema_version=1,
                capabilities=workspace_capabilities,
                lifecycle=ModuleLifecycle.ACTIVE,
            ),
        )
        instances = (
            ModuleInstance(
                id=adapter_instance_id,
                workspace_id=self.config.workspace_id,
                definition_id=adapter_definition_id,
                instance_name=(
                    f"r5-3-deterministic-worker-{self.config.run_id.hex[:12]}"
                ),
                config_version=1,
                config_namespace="fake",
                config={
                    "fake.mode": "r5-3-deterministic",
                    "fake.artifact_acceptance_criterion_ref": (
                        "acceptance:implementation"
                    ),
                },
                secret_refs=(),
                lifecycle=ModuleLifecycle.ACTIVE,
            ),
            ModuleInstance(
                id=workspace_instance_id,
                workspace_id=self.config.workspace_id,
                definition_id=workspace_definition_id,
                instance_name=(
                    f"r5-3-deterministic-workspace-{self.config.run_id.hex[:12]}"
                ),
                config_version=1,
                config_namespace="workspace",
                config={
                    "workspace.repository_ref": str(repository),
                    "workspace.base_revision": revision,
                    "workspace.expected_outputs": ["diff"],
                },
                secret_refs=(),
                lifecycle=ModuleLifecycle.ACTIVE,
            ),
        )
        with SqlAlchemyUnitOfWork(self.database) as uow:
            for definition in definitions:
                if uow.modules.get_definition(definition.id, self.config.workspace_id) is None:
                    uow.modules.add_definition(definition)
            for instance in instances:
                if uow.modules.get_instance(instance.id, self.config.workspace_id) is None:
                    uow.modules.add_instance(instance)
            worker = uow.workers.get(worker_id, self.config.workspace_id)
            if worker is None:
                worker = Worker.register(
                    worker_id=worker_id,
                    workspace_id=self.config.workspace_id,
                    adapter_type="fake",
                    display_name="R5.3 deterministic worker",
                    capabilities=frozenset({"code", "python"}),
                    capacity=1,
                    cost_per_hour_microunits=1,
                    startup_latency_ms=1,
                    now=datetime.now(UTC),
                )
                uow.workers.add(worker)
            if uow.worker_runtime_configurations.get_for_worker(
                worker_id, self.config.workspace_id
            ) is None:
                uow.flush()
                uow.worker_runtime_configurations.add(
                    WorkerRuntimeConfiguration.create(
                        worker_id=worker_id,
                        workspace_id=self.config.workspace_id,
                        adapter_module_instance_id=adapter_instance_id,
                        adapter_module_config_version=1,
                        workspace_module_instance_id=workspace_instance_id,
                        workspace_module_config_version=1,
                    )
                )
        return {
            "worker_id": worker_id,
            "adapter_instance_id": adapter_instance_id,
            "workspace_instance_id": workspace_instance_id,
        }

    def _planner_capabilities(self):
        from sidera_control_plane.domain.modules import (
            CancellationCapability,
            PlannerAdapterCapabilityProfile,
            StructuredOutputCapability,
            UsageReportingCapability,
        )

        return PlannerAdapterCapabilityProfile(
            structured_output=StructuredOutputCapability.JSON_SCHEMA,
            clarification=True,
            idempotency=IdempotencyCapability.OPERATION_ID,
            cancellation=CancellationCapability.RUN,
            usage_reporting=UsageReportingCapability.TOKENS,
            max_context_bytes=128_000,
            stability=StabilityLevel.STABLE,
            interface_version="sidera.planner.v1",
            proposal_schema_versions=(2,),
            context_package_versions=(1,),
        )

    def _ensure_workspace(self) -> None:
        with self.database.session() as session:
            if session.get(WorkspaceModel, self.config.workspace_id) is None:
                session.add(
                    WorkspaceModel(
                        id=self.config.workspace_id,
                        name="SIDERA R5.3 deterministic gate workspace",
                        status="active",
                        version=1,
                    )
                )
                session.commit()

    def _current_context_number(self, root_task_id: UUID) -> int:
        context = self._cp_get(
            f"/api/v1/workspaces/{self.config.workspace_id}/tasks/{root_task_id}/work-context",
            stage="proposal_confirmed_context",
        )
        return _positive_int(context.get("current_revision_number"), "context.revision")

    @staticmethod
    def _package_path(
        workspace_id: UUID,
        consumer_id: UUID,
        consumer_type: ContextConsumerType,
    ) -> str:
        resource = {
            ContextConsumerType.PLANNER: "planning-runs",
            ContextConsumerType.WORKER: "executions",
        }.get(ContextConsumerType(consumer_type))
        if resource is None:
            raise ValueError("unsupported context package consumer type")
        return (
            f"/api/v1/workspaces/{workspace_id}/{resource}/"
            f"{consumer_id}/context-package"
        )

    def _cp_list_executions(self, task_id: UUID) -> list[dict[str, Any]]:
        path = "/api/v1/executions"
        path = f"{path}?{urlencode({
            'workspace_id': str(self.config.workspace_id),
            'task_id': str(task_id),
        })}"
        _, document = self.cp.request(
            "GET",
            path,
            stage="execution_list",
            allow_json_array=True,
        )
        if document is None:
            raise GateFailure("execution_list", "empty_response")
        return [cast(dict[str, Any], item) for item in _sequence(document, "executions")]

    def _wait_for_package(
        self,
        consumer_id: UUID,
        stage: str,
        consumer_type: ContextConsumerType,
    ) -> dict[str, Any]:
        return self._wait_for(
            stage,
            lambda: self._cp_get_optional(
                self._package_path(
                    self.config.workspace_id, consumer_id, consumer_type
                ),
                stage=stage,
            ),
            lambda value: value is not None,
        )

    def _wait_for(
        self,
        stage: str,
        fetch: Callable[[], Any],
        predicate: Callable[[Any], bool],
    ) -> Any:
        deadline = Deadline(seconds=self.config.deadline_seconds)
        last_state = "not_ready"
        while True:
            deadline.require(stage)
            try:
                value = fetch()
            except GateFailure as exc:
                if exc.error_code not in {"http_404", "http_409"}:
                    raise
                value = None
                last_state = exc.error_code
            if predicate(value):
                return value
            if isinstance(value, Mapping):
                last_state = _safe_code(
                    str(value.get("status", "not_ready")), fallback="not_ready"
                )
            deadline.sleep(self.config.poll_interval_seconds)
            if deadline.remaining <= 0:
                raise GateFailure(stage, f"deadline_exceeded_{last_state}")

    def _cp_get_optional(
        self, path: str, *, stage: str = "cp_poll"
    ) -> dict[str, Any] | None:
        status, document = self.cp.request(
            "GET", path, stage=stage, allow_status=frozenset({404})
        )
        if status == 404:
            return None
        if document is None:
            raise GateFailure(stage, "empty_response")
        return document

    def _cp_get(
        self,
        path: str,
        *,
        stage: str,
        params: Mapping[str, str] | None = None,
    ) -> dict[str, Any]:
        if params:
            path = f"{path}?{urlencode(params)}"
        _, document = self.cp.request("GET", path, stage=stage)
        if document is None:
            raise GateFailure(stage, "empty_response")
        return document

    def _cp_command(
        self,
        label: str,
        command_type: str,
        target_type: str,
        payload: Mapping[str, object],
        *,
        actor: Mapping[str, str] | None = None,
        target_id: UUID | None = None,
        expected_version: int | None = None,
    ) -> dict[str, Any]:
        command_id = self.identity.entity(f"cp-command:{label}")
        body = {
            "command_id": str(command_id),
            "workspace_id": str(self.config.workspace_id),
            "command_type": command_type,
            "target_type": target_type,
            "actor": dict(actor or {"type": "local", "id": "aitejiu"}),
            "target_id": str(target_id) if target_id else None,
            "expected_version": expected_version,
            "idempotency_key": self.identity.idempotency(label),
            "correlation_id": str(self.identity.entity(f"cp-correlation:{label}")),
            "payload": dict(payload),
        }
        _, document = self.cp.request("POST", "/api/v1/commands", stage=label, payload=body)
        if document is None:
            raise GateFailure(label, "empty_response")
        return document

    def _knowledge_command(
        self,
        label: str,
        *,
        actor_type: str,
        payload: Mapping[str, object],
        target_id: UUID,
    ) -> dict[str, Any]:
        command_id = self.identity.entity(f"knowledge-command:{label}")
        body = {
            "command_id": str(command_id),
            "workspace_id": str(self.config.workspace_id),
            "target_type": "knowledge_candidate",
            "target_id": str(target_id),
            "actor": {"type": actor_type, "id": "r5-3-runner"},
            "expected_version": None,
            "idempotency_key": self.identity.idempotency(label),
            "correlation_id": str(self.identity.entity(f"knowledge-correlation:{label}")),
            "command_type": (
                "ReviewKnowledgeCandidate"
                if "review" in label
                else "SubmitKnowledgeCandidate"
            ),
            "payload": dict(payload),
        }
        client = (
            self.knowledge_owner
            if actor_type == "local_owner"
            else self.knowledge_control_plane
        )
        _, document = client.request("POST", "/api/v1/commands", stage=label, payload=body)
        if document is None:
            raise GateFailure(label, "empty_response")
        return document

    def _knowledge_get(self, path: str, *, stage: str) -> dict[str, Any]:
        _, document = self.knowledge_owner.request("GET", path, stage=stage)
        if document is None:
            raise GateFailure(stage, "empty_response")
        return document

    def _assert_recomputable_package(
        self, package: Mapping[str, Any], stage: str
    ) -> None:
        expected = _required_hash(package.get("package_hash"), f"{stage}.package_hash")
        try:
            actual = recompute_package_hash(package)
        except (TypeError, ValueError, KeyError) as exc:
            raise GateFailure(stage, "package_hash_not_recomputable") from exc
        if actual != expected:
            raise GateFailure(stage, "package_hash_mismatch")

    def _assert_no_marker_in_control_plane(self) -> None:
        marker = self.identity.marker("knowledge-initial")
        worker_marker = self.identity.marker("knowledge-worker")
        with self.database.engine.connect() as connection:
            rows = connection.exec_driver_sql(
                "SELECT event.payload::text FROM events AS event "
                "WHERE event.workspace_id = %s "
                "UNION ALL SELECT outbox.payload::text "
                "FROM outbox_messages AS outbox "
                "JOIN events AS event ON event.id = outbox.event_id "
                "WHERE event.workspace_id = %s",
                (str(self.config.workspace_id), str(self.config.workspace_id)),
            ).all()
        if any(marker in str(row[0]) or worker_marker in str(row[0]) for row in rows):
            raise GateFailure("control_plane_secret_scan", "knowledge_body_leaked")


def recompute_package_hash(package: Mapping[str, Any]) -> str:
    """Reconstruct the domain manifest to verify the published package hash."""

    budget_raw = _mapping(package.get("budget"), "package.budget")
    budget = ContextBudgetV1(
        profile_id=_as_uuid(budget_raw.get("profile_id"), "budget.profile_id"),
        profile_version=_positive_int(budget_raw.get("profile_version"), "budget.profile_version"),
        max_sections=_positive_int(budget_raw.get("max_sections"), "budget.max_sections"),
        max_chars=_positive_int(budget_raw.get("max_chars"), "budget.max_chars"),
        max_input_tokens=_positive_int(
            budget_raw.get("max_input_tokens"), "budget.max_input_tokens"
        ),
        actual_sections=_positive_or_zero(
            budget_raw.get("actual_sections"), "budget.actual_sections"
        ),
        actual_chars=_positive_or_zero(budget_raw.get("actual_chars"), "budget.actual_chars"),
        actual_input_tokens=_positive_or_zero(
            budget_raw.get("actual_input_tokens"), "budget.actual_input_tokens"
        ),
    )
    knowledge_refs = tuple(
        ContextPackageKnowledgeRef(
            page_id=_as_uuid(item.get("page_id"), "package.knowledge.page_id"),
            revision_id=_as_uuid(item.get("revision_id"), "package.knowledge.revision_id"),
            revision_hash=_required_hash(item.get("revision_hash"), "package.knowledge.revision_hash"),
            section_key=_required_text(item.get("section_key"), "package.knowledge.section_key"),
            section_hash=_required_hash(item.get("section_hash"), "package.knowledge.section_hash"),
            source_ids=tuple(
                _as_uuid(value, "package.knowledge.source_id")
                for value in _sequence(item.get("source_ids"), "package.knowledge.source_ids")
            ),
            selection_reason=ContextSelectionReason(
                _required_text(item.get("selection_reason"), "package.knowledge.selection_reason")
            ),
            trust_level=ContextTrustLevel(
                _required_text(item.get("trust_level"), "package.knowledge.trust_level")
            ),
            ordinal=_positive_or_zero(item.get("ordinal"), "package.knowledge.ordinal"),
        )
        for item in (
            _mapping(value, "package.knowledge_ref")
            for value in _sequence(package.get("knowledge_refs"), "package.knowledge_refs")
        )
    )
    artifact_refs = tuple(
        _work_reference(_mapping(item, "package.artifact_ref"), "package.artifact_ref")
        for item in _sequence(package.get("artifact_refs"), "package.artifact_refs")
    )
    evidence_refs = tuple(
        _work_reference(_mapping(item, "package.evidence_ref"), "package.evidence_ref")
        for item in _sequence(package.get("evidence_refs"), "package.evidence_refs")
    )
    expires_at = _aware_datetime(package.get("expires_at"), "package.expires_at")
    manifest = ContextPackageManifestV1.create(
        package_id=_as_uuid(package.get("package_id"), "package.package_id"),
        workspace_id=_as_uuid(package.get("workspace_id"), "package.workspace_id"),
        purpose=ContextPurpose(_required_text(package.get("purpose"), "package.purpose")),
        consumer_type=ContextConsumerType(
            _required_text(package.get("consumer_type"), "package.consumer_type")
        ),
        consumer_id=_as_uuid(package.get("consumer_id"), "package.consumer_id"),
        worker_item_id=(
            _as_uuid(package.get("worker_item_id"), "package.worker_item_id")
            if package.get("worker_item_id") is not None
            else None
        ),
        work_context_revision_id=_as_uuid(
            package.get("work_context_revision_id"), "package.work_context_revision_id"
        ),
        context_version=(
            _positive_int(package.get("context_version"), "package.context_version")
            if package.get("context_version") is not None
            else None
        ),
        control_plane_fact_snapshot=_mapping(
            package.get("control_plane_fact_snapshot"),
            "package.control_plane_fact_snapshot",
        ),
        knowledge_refs=knowledge_refs,
        artifact_refs=artifact_refs,
        evidence_refs=evidence_refs,
        budget=budget,
        expires_at=expires_at,
        idempotency_key=_required_text(package.get("idempotency_key"), "package.idempotency_key"),
        created_at=expires_at - timedelta(seconds=1),
    )
    if manifest.status is not ContextPackageStatus.ACTIVE:
        raise ValueError("package is not active")
    return manifest.package_hash


def fault_recovery_test_paths() -> tuple[Path, ...]:
    root = Path(__file__).resolve().parents[1]
    return tuple(root / relative_path for relative_path in _FAULT_RECOVERY_RELATIVE_PATHS)


def run_fault_recovery(config: RunnerConfig) -> dict[str, Any]:
    """Run the fixed CP recovery suite without forwarding test output to the gate."""

    paths = fault_recovery_test_paths()
    if not paths or any(not path.is_file() for path in paths):
        raise GateFailure("fault_recovery", "test_scope_unavailable")
    environment = os.environ.copy()
    environment["SIDERA_DATABASE_URL"] = config.test_database_url
    command = [
        sys.executable,
        "-m",
        "pytest",
        "-m",
        "integration",
        "-q",
        *(str(path) for path in paths),
    ]
    try:
        completed = subprocess.run(
            command,
            cwd=Path(__file__).resolve().parents[1],
            env=environment,
            check=False,
            capture_output=True,
            text=True,
            timeout=config.deadline_seconds,
        )
    except subprocess.TimeoutExpired as exc:
        raise GateFailure("fault_recovery", "deadline_exceeded") from exc
    except OSError as exc:
        raise GateFailure("fault_recovery", "test_runner_unavailable") from exc
    if completed.returncode != 0:
        raise GateFailure("fault_recovery", "tests_failed")
    return {
        "status": "passed",
        "mode": "fault",
        "run_id": str(config.run_id),
        "test_file_count": len(paths),
        "database_scope": "isolated_test_database",
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--mode", choices=("deterministic", "fault", "real-evidence"), required=True
    )
    parser.add_argument("--env-file", type=Path, default=None)
    parser.add_argument("--control-plane-url", default=None)
    parser.add_argument("--knowledge-base-url", default=None)
    parser.add_argument("--workspace-id", default=None)
    parser.add_argument("--run-id", default=None)
    parser.add_argument("--deadline-seconds", type=float, default=60.0)
    parser.add_argument("--poll-interval-seconds", type=float, default=0.5)
    parser.add_argument("--cleanup-test-run", action="store_true")
    parser.add_argument("--task-id", default=None)
    parser.add_argument("--execution-id", default=None)
    parser.add_argument("--session-id", default=None)
    parser.add_argument("--acceptance-ref", default=None)
    parser.add_argument("--run-marker", default=None)
    parser.add_argument("--repository-path", type=Path, default=None)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    runner: VerticalLoopRunner | None = None
    try:
        config = load_config(args)
        if args.cleanup_test_run and args.run_id is None:
            raise GateFailure("cleanup", "run_id_required")
        if args.mode == "fault":
            if args.cleanup_test_run:
                raise GateFailure("cleanup", "unsupported_mode")
            result = run_fault_recovery(config)
        elif args.mode == "real-evidence":
            if args.cleanup_test_run:
                raise GateFailure("cleanup", "unsupported_mode")
            if not all(
                (
                    args.task_id,
                    args.execution_id,
                    args.acceptance_ref,
                    args.run_marker,
                )
            ):
                raise GateFailure("configuration", "real_evidence_ids_required")
            runner = VerticalLoopRunner(config)
            result = runner.record_real_execution_evidence(
                task_id=UUID(args.task_id),
                execution_id=UUID(args.execution_id),
                session_id=UUID(args.session_id) if args.session_id else None,
                acceptance_ref=args.acceptance_ref,
                run_marker=args.run_marker,
                repository_path=args.repository_path,
            )
        else:
            runner = VerticalLoopRunner(config)
            result = (
                runner.cleanup_test_run() if args.cleanup_test_run else runner.run()
            )
        print(json.dumps(result, sort_keys=True, separators=(",", ":")))
        return 0
    except GateFailure as exc:
        print(
            json.dumps(
                {
                    "status": "failed",
                    "mode": args.mode,
                    "run_id": _safe_run_id(args.run_id),
                    "error": exc.as_dict(),
                },
                sort_keys=True,
                separators=(",", ":"),
            ),
            file=sys.stderr,
        )
        return 1
    except (OSError, TypeError, ValueError) as exc:
        del exc
        print(
            json.dumps(
                {
                    "status": "failed",
                    "mode": args.mode,
                    "run_id": _safe_run_id(args.run_id),
                    "error": {"stage": "configuration", "error_code": "invalid_configuration"},
                },
                sort_keys=True,
                separators=(",", ":"),
            ),
            file=sys.stderr,
        )
        return 2
    except Exception as exc:  # noqa: BLE001 - gate output must never expose runtime details
        del exc
        print(
            json.dumps(
                {
                    "status": "failed",
                    "mode": args.mode,
                    "run_id": _safe_run_id(args.run_id),
                    "error": {"stage": "runner", "error_code": "runner_failed"},
                },
                sort_keys=True,
                separators=(",", ":"),
            ),
            file=sys.stderr,
        )
        return 3
    finally:
        if runner is not None:
            runner.close()


def load_config(args: argparse.Namespace) -> RunnerConfig:
    values = parse_env_file(
        args.env_file
        or (
            Path(os.environ["SIDERA_R5_3_ENV_FILE"])
            if os.environ.get("SIDERA_R5_3_ENV_FILE")
            else Path(__file__).resolve().parents[1] / ".sidera" / "r5-3.local.env"
        )
    )
    for key in ALLOWED_ENV_KEYS:
        if key in os.environ:
            values[key] = os.environ[key]
    cp_url = args.control_plane_url or values.get("SIDERA_CONTROL_PLANE_URL")
    knowledge_url = args.knowledge_base_url or values.get("SIDERA_KNOWLEDGE_BASE_URL")
    workspace_text = args.workspace_id or values.get("SIDERA_WORKSPACE_ID")
    database_url = values.get("SIDERA_DATABASE_URL")
    test_database_url = values.get("SIDERA_R5_3_TEST_DATABASE_URL") or database_url
    owner_token = values.get("SIDERA_KNOWLEDGE_LOCAL_OWNER_TOKEN")
    control_plane_token = values.get("SIDERA_KNOWLEDGE_CONTROL_PLANE_TOKEN")
    spool_dir = values.get("SIDERA_KNOWLEDGE_CANDIDATE_SPOOL_DIR")
    if not all(
        (
            cp_url,
            knowledge_url,
            workspace_text,
            database_url,
            test_database_url,
            owner_token,
            control_plane_token,
            spool_dir,
        )
    ):
        raise ValueError("required runner configuration is missing")
    if args.deadline_seconds <= 0 or args.poll_interval_seconds <= 0:
        raise ValueError("poll limits must be positive")
    return RunnerConfig(
        control_plane_url=_loopback_url(cp_url),
        knowledge_base_url=_loopback_url(knowledge_url),
        workspace_id=UUID(workspace_text),
        database_url=database_url,
        test_database_url=test_database_url,
        candidate_spool_dir=Path(spool_dir),
        owner_token=owner_token,
        control_plane_token=control_plane_token,
        deadline_seconds=args.deadline_seconds,
        poll_interval_seconds=args.poll_interval_seconds,
        run_id=UUID(args.run_id) if args.run_id else uuid4(),
        opencode_executable=values.get("SIDERA_OPENCODE_EXECUTABLE") or "opencode",
        opencode_expected_version=values.get("SIDERA_OPENCODE_EXPECTED_VERSION"),
        planning_profile_id=(
            UUID(values["SIDERA_PLANNING_PROFILE_ID"])
            if values.get("SIDERA_PLANNING_PROFILE_ID")
            else None
        ),
        planning_profile_version=(
            int(values["SIDERA_PLANNING_PROFILE_VERSION"])
            if values.get("SIDERA_PLANNING_PROFILE_VERSION")
            else None
        ),
    )


def parse_env_file(path: Path) -> dict[str, str]:
    if not path.exists():
        return {}
    if path.is_symlink():
        raise ValueError("runner env file must not be a symlink")
    values: dict[str, str] = {}
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.rstrip("\r")
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        if any(token in line for token in ("$", "`", ";", "|", "<", ">", "&")):
            raise ValueError("runner env file contains shell syntax")
        if "=" not in line:
            raise ValueError("runner env file contains an invalid line")
        key, value = line.split("=", 1)
        if _ENV_KEY.fullmatch(key) is None or key not in ALLOWED_ENV_KEYS:
            raise ValueError("runner env file contains an unknown key")
        if key in values:
            raise ValueError("runner env file contains a duplicate key")
        values[key] = value
    return values


def recompute_package_hash_from_json(package: Mapping[str, Any]) -> str:
    return recompute_package_hash(package)


def _work_reference(value: Mapping[str, Any], field_name: str):
    from sidera_control_plane.domain.work_context import WorkReference

    return WorkReference(
        ref_id=_as_uuid(value.get("ref_id"), f"{field_name}.ref_id"),
        ref_type=_required_text(value.get("ref_type"), f"{field_name}.ref_type"),
        ordinal=_positive_or_zero(value.get("ordinal"), f"{field_name}.ordinal"),
    )


def _loopback_url(value: str | None) -> str:
    if value is None or not re.fullmatch(
        r"http://(?:127\.0\.0\.1|localhost):[0-9]{1,5}", value
    ):
        raise ValueError("runner URL must be an http loopback origin")
    return value


def _safe_run_id(value: object) -> str | None:
    if value is None:
        return None
    try:
        return str(UUID(str(value)))
    except (AttributeError, TypeError, ValueError):
        return None


def _safe_code(value: str, *, fallback: str) -> str:
    normalized = _SAFE_CODE.sub("_", value.strip().lower()).strip("_")
    return (normalized or fallback)[:100]


def _required_text(value: object, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field_name} is required")
    return value.strip()


def _required_hash(value: object, field_name: str) -> str:
    result = _required_text(value, field_name)
    if not re.fullmatch(r"[0-9a-f]{64}", result):
        raise ValueError(f"{field_name} must be a sha256 hash")
    return result


def _positive_int(value: object, field_name: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 1:
        raise ValueError(f"{field_name} must be positive")
    return value


def _positive_or_zero(value: object, field_name: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise ValueError(f"{field_name} must be non-negative")
    return value


def _as_uuid(value: object, field_name: str) -> UUID:
    try:
        return UUID(str(value))
    except (AttributeError, TypeError, ValueError) as exc:
        raise ValueError(f"{field_name} must be a UUID") from exc


def _mapping(value: object, field_name: str) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise TypeError(f"{field_name} must be an object")
    return dict(value)


def _sequence(value: object, field_name: str) -> list[Any]:
    if not isinstance(value, (list, tuple)):
        raise TypeError(f"{field_name} must be an array")
    return list(value)


def _aware_datetime(value: object, field_name: str) -> datetime:
    if not isinstance(value, str):
        raise TypeError(f"{field_name} must be a datetime")
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError(f"{field_name} must be timezone aware")
    return parsed.astimezone(UTC)


if __name__ == "__main__":
    raise SystemExit(main())
