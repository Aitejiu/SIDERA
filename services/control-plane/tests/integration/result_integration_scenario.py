from __future__ import annotations

import hashlib
import json
import os
import subprocess
import time
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import NAMESPACE_URL, UUID, uuid4, uuid5

import pytest
from sqlalchemy import select, update

from sidera_control_plane.adapters.git_results import GitResultStore
from sidera_control_plane.adapters.local_verification import LocalCheckRunner
from sidera_control_plane.application.activation_reconciler import ActivationReconciler
from sidera_control_plane.application.code_result_capture import (
    CodeResultCaptureService,
    code_capture_operation_id,
)
from sidera_control_plane.application.command_service import (
    CommandService,
    hash_request,
)
from sidera_control_plane.application.context_requests import (
    build_execution_context_assembly_request,
)
from sidera_control_plane.application.event_ingestor import (
    ArtifactRegistry,
    SessionEventIngestor,
)
from sidera_control_plane.application.execution_commands import ExecutionCommandHandler
from sidera_control_plane.application.result_acceptance import (
    ResultAcceptanceService,
    build_acceptance_basis_hash,
)
from sidera_control_plane.application.result_finalization import (
    ResultFinalizationService,
)
from sidera_control_plane.application.result_input_evaluation import (
    build_code_input_manifest,
)
from sidera_control_plane.application.result_integration import ResultIntegrationService
from sidera_control_plane.application.result_ports import (
    CheckRunObservation,
    CheckRunRequest,
    CheckRunStatus,
    ResultOperationAttemptV1,
)
from sidera_control_plane.application.result_rework import ResultReworkService
from sidera_control_plane.application.result_verification import (
    ResultVerificationService,
)
from sidera_control_plane.application.worker_gateway import GatewayMessageError
from sidera_control_plane.domain.artifacts import ArtifactCandidate, ArtifactType
from sidera_control_plane.domain.code_results import (
    CodeInputManifestV1,
    CodeResultSnapshotV1,
    IntegrationCandidateV1,
)
from sidera_control_plane.domain.commands import CommandEnvelope
from sidera_control_plane.domain.context_packages import (
    ContextPurpose,
    ResultInputBudgetV1,
)
from sidera_control_plane.domain.gateway import AdapterEvent
from sidera_control_plane.domain.result_operations import (
    ResultOperationStatus,
    ReworkRecordV1,
    TaskResultHead,
)
from sidera_control_plane.domain.result_verification import (
    CriterionDecisionType,
    CriterionDecisionV1,
    CriterionObservedStatus,
    VerificationResultV1,
    evaluate_criteria,
)
from sidera_control_plane.domain.workspace import (
    ExecutionWorkspaceManifest,
    WorkspaceCheckout,
)
from sidera_control_plane.persistence.artifact_content_store import (
    LocalArtifactContentStore,
)
from sidera_control_plane.persistence.database import Database
from sidera_control_plane.persistence.models import EventModel, ExecutionRequestModel
from sidera_control_plane.persistence.unit_of_work import SqlAlchemyUnitOfWork
from tests.unit.result_integration_fixtures import snapshot_mapping

from .result_input_scenario import ResultInputScenario
from .test_r5_1_execution_request_schema import (
    request_row,
    seed_execution_request_facts,
)

NOW = datetime(2026, 9, 7, 12, 0, tzinfo=UTC)


class ResultIntegrationScenario:
    """Small real-persistence harness extended by later result tasks."""

    def __init__(
        self,
        database: Database,
        workspace_id: UUID,
        tmp_path: Path,
        ids: dict[str, UUID],
        request_id: UUID,
    ) -> None:
        self.database = database
        self.workspace_id = workspace_id
        self.tmp_path = tmp_path
        self.ids = ids
        self.request_id = request_id
        self.operation_id = UUID("80000000-0000-0000-0000-000000000002")
        self._planning = ResultInputScenario(database, workspace_id, tmp_path)
        self._seeded = False
        self.worker_item_ids: dict[str, UUID] = {}
        self.request_ids: dict[str, UUID] = {}
        self._source_execution_ids: dict[str, UUID] = {}
        self._source_worktrees: dict[str, Path] = {}
        self._integration_execution_id: UUID | None = None
        self._integration_request_id: UUID | None = None
        self._integration_operation_id: UUID | None = None
        self._pending_reintegration_request_id: UUID | None = None
        self._repository = tmp_path / "result-integration-repository"
        self._worktree_root = tmp_path / "result-integration-worktrees"
        self._artifact_store = LocalArtifactContentStore(
            tmp_path / "result-integration-artifacts"
        )
        self._capture_service: CodeResultCaptureService | None = None
        self.code_provider_failure: str | None = None
        self.worker_context_versions: tuple[int, ...] = (1, 2, 3)
        self._check_exit_code: int | None = None
        self._modify_source_during_check = False
        self._verification_runner: LocalCheckRunner | None = None
        self._verification_operation_ids: tuple[UUID, ...] = ()
        self._command_expected_versions: dict[str, int] = {}
        self._base_commit = self._init_repository()

    @property
    def root_task_id(self) -> UUID:
        return self.ids["root_task"]

    @property
    def execution_id(self) -> UUID:
        return self._source_execution_ids.get("a", self.ids["execution"])

    @property
    def capture_operation_id(self) -> UUID:
        return code_capture_operation_id(self.execution_id)

    @property
    def integration_execution_id(self) -> UUID:
        if self._integration_execution_id is None:
            raise AssertionError("integration execution has not been started")
        return self._integration_execution_id

    @property
    def integration_operation_id(self) -> UUID:
        if self._integration_operation_id is None:
            raise AssertionError("integration operation has not been created")
        return self._integration_operation_id

    @property
    def capture_service(self) -> CodeResultCaptureService:
        if self._capture_service is None:
            self._capture_service = self._new_capture_service()
        return self._capture_service

    @property
    def base_commit(self) -> str:
        return self._base_commit

    def target_commit(self) -> str:
        return self._git(self._repository, "rev-parse", "refs/heads/main")

    @property
    def root_id(self) -> UUID:
        return self.root_task_id

    @property
    def plan_snapshot_id(self) -> UUID:
        return self.ids["plan_snapshot"]

    def plan_generation(self) -> int:
        with self.uow() as uow:
            head = uow.plans.get_head(self.root_task_id, self.workspace_id)
        if head is None:
            raise AssertionError("result scenario plan head was not found")
        return head.active_generation

    @property
    def worker_ids(self) -> tuple[UUID, ...]:
        return tuple(self.worker_item_ids.values())

    def seed_plan(self, *, apply: bool = False) -> None:
        if apply:
            raise NotImplementedError("result integration apply is added in Task 25")
        if self._seeded:
            return
        self._planning.confirm_plan(
            result_inputs_enabled=True,
            proposal_schema_version=6,
            profile_key="r6-result-planning",
        )
        self.ids.update(self._planning.ids)
        self.request_id = self._planning.ids["planning_request"]
        self.worker_item_ids = dict(self._planning.worker_item_ids)
        self.request_ids = dict(self._planning.request_ids)
        self._capture_service = self._new_capture_service()
        self._seeded = True

    def source(
        self,
        key: str,
        *,
        files: dict[str, bytes],
        commit: bool,
        capture: bool = True,
    ) -> CodeResultSnapshotV1 | None:
        """Run one source WorkerItem through a real Git worktree."""
        if not isinstance(key, str) or not key.strip():
            raise ValueError("source key is required")
        if not isinstance(files, dict) or not files:
            raise ValueError("source files are required")
        self.seed_plan()
        source_key = "a" if key == "login" else key
        if source_key not in self.worker_item_ids:
            raise KeyError(source_key)
        if source_key in self._source_execution_ids:
            raise AssertionError(f"source {source_key!r} has already been started")
        self._planning._ensure_source_executions()
        execution_id = self._planning._execution_id(source_key)
        if execution_id is None:
            raise AssertionError(f"source execution {source_key!r} was not created")
        worker_id, lease_id, session_id, execution_version = self._planning._start_source(
            execution_id, key=source_key
        )
        del execution_version
        worktree, manifest = self._create_worktree(execution_id)
        self._source_execution_ids[source_key] = execution_id
        self._source_worktrees[source_key] = worktree
        with self.uow() as uow:
            uow.workspaces.add(manifest)
        service = self.capture_service
        service.record_execution_start(
            manifest=manifest,
            input_commit=self._base_commit,
            writable_paths=(".",),
            operation_id=code_capture_operation_id(execution_id),
        )
        for relative, body in files.items():
            path = (worktree / relative).resolve()
            if worktree not in path.parents or path == worktree:
                raise ValueError("source file escapes the worktree")
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(body)
        if commit:
            self._git(worktree, "add", "-A")
            self._git(
                worktree,
                "-c",
                "user.name=Result Integration Worker",
                "-c",
                "user.email=worker@sidera.local",
                "commit",
                "-m",
                f"source result {source_key}",
            )
            service.observe_terminal(
                workspace_id=self.workspace_id,
                execution_id=execution_id,
                operation_id=code_capture_operation_id(execution_id),
                outcome="succeeded",
                fencing_token=1,
            )
            if not capture:
                return None
            snapshot = service.capture_execution(
                workspace_id=self.workspace_id,
                execution_id=execution_id,
                operation_id=code_capture_operation_id(execution_id),
            )
            with self.uow() as uow:
                current = uow.executions.get(execution_id, self.workspace_id)
            if current is None:
                raise AssertionError("source execution disappeared")
            CommandService(
                lambda: self.uow(),
                (ExecutionCommandHandler(lambda: self._planning.now),),
            ).handle(
                CommandEnvelope(
                    id=uuid4(),
                    workspace_id=self.workspace_id,
                    command_type="ReportExecutionSucceeded",
                    target_type="execution",
                    actor_type="worker",
                    actor_id=str(worker_id),
                    target_id=execution_id,
                    payload={
                        "worker_id": str(worker_id),
                        "lease_id": str(lease_id),
                        "fencing_token": 1,
                    },
                    expected_version=current.version,
                    idempotency_key=f"result-capture-source-succeeded:{execution_id}",
                    correlation_id=self.root_task_id,
                )
            )
            artifact_body = b"".join(
                relative.encode("utf-8") + b"\0" + body
                for relative, body in sorted(files.items())
            )
            artifact_hash = hashlib.sha256(artifact_body).hexdigest()
            storage_ref = self._planning.artifact_store.put(
                self.workspace_id,
                execution_id,
                artifact_hash,
                artifact_body,
            )
            ArtifactRegistry(
                lambda: self.uow(),
                clock=lambda: self._planning.now,
            ).register(
                workspace_id=self.workspace_id,
                task_id=self._planning._execution_task_id(execution_id),
                execution_id=execution_id,
                session_id=session_id,
                candidates=(
                    ArtifactCandidate(
                        artifact_type=ArtifactType.BUILD,
                        storage_ref=storage_ref,
                        content_hash=artifact_hash,
                        size_bytes=len(artifact_body),
                        media_type="text/plain",
                        provenance={
                            "producer": f"scenario:{source_key}",
                            "environment_ref": "env://result-integration-scenario",
                            "acceptance_criterion_ref": "build",
                            "conclusion": f"{source_key} produced its build result",
                        },
                    ),
                ),
                acceptance_criteria=("build",),
                producer=f"scenario:{source_key}",
                environment_ref="env://result-integration-scenario",
            )
            return snapshot
        return None

    def code_input_manifest(
        self,
        *,
        base_commit: str | None = None,
        request_id: UUID | None = None,
        execution_id: UUID | None = None,
    ) -> CodeInputManifestV1:
        """Build the exact code input manifest for the integration WorkerItem."""

        self._planning.reconcile()
        self._planning.activate()
        integration_request_id = request_id or self._integration_request_id
        if integration_request_id is None:
            integration_request_id = self.request_ids["summary"]
        integration_execution_id = execution_id or self._planning._execution_id("summary")
        if integration_execution_id is None:
            raise AssertionError("integration execution was not activated")
        with self.uow() as uow:
            execution_request = uow.execution_requests.get(
                integration_request_id,
                self.workspace_id,
            )
            execution = uow.executions.get(integration_execution_id, self.workspace_id)
            selection = (
                None
                if execution_request is None
                or execution_request.input_selection_id is None
                else uow.input_selections.get(
                    self.workspace_id,
                    execution_request.input_selection_id,
                )
            )
            snapshots = uow.results.retained_code_results(self.workspace_id)
        if execution_request is None or execution is None or selection is None:
            raise AssertionError("integration input Selection is not ready")
        selected_execution_ids = {
            source.effective_execution_id
            for group in selection.groups
            for source in group.sources
        }
        selected_execution_ids.update(
            item.source.effective_execution_id for item in selection.hard_inputs
        )
        selected_snapshots = tuple(
            snapshot
            for snapshot in snapshots
            if snapshot.execution_id in selected_execution_ids
        )
        return build_code_input_manifest(
            selection=selection,
            snapshots=selected_snapshots,
            request_id=execution_request.id,
            execution_id=execution.id,
            repository_ref="repo://integration-test",
            base_commit=self._base_commit if base_commit is not None else "a" * 40,
        )

    def candidate(
        self,
        *,
        check_exit_code: int | None = None,
        run_checks: bool = False,
        tamper_actual_tree: bool = False,
        modify_source_during_check: bool = False,
    ):
        """Run the real code integration WorkerItem and publish its candidate."""

        if type(run_checks) is not bool:
            raise ValueError("run_checks must be a boolean")
        if type(tamper_actual_tree) is not bool:
            raise ValueError("tamper_actual_tree must be a boolean")
        if type(modify_source_during_check) is not bool:
            raise ValueError("modify_source_during_check must be a boolean")
        if check_exit_code is not None and (
            isinstance(check_exit_code, bool) or not isinstance(check_exit_code, int)
        ):
            raise ValueError("check_exit_code must be an integer or null")
        if check_exit_code is not None:
            run_checks = True
        if run_checks and check_exit_code is None:
            check_exit_code = 0
        if self._integration_execution_id is not None and (
            (check_exit_code is not None and self._check_exit_code != check_exit_code)
            or self._modify_source_during_check != modify_source_during_check
        ):
            raise AssertionError("the integration candidate already has a check setup")
        self._check_exit_code = check_exit_code
        self._modify_source_during_check = modify_source_during_check
        self.seed_plan()
        if "a" not in self._source_execution_ids:
            self.source(
                "a",
                files={"login.py": b"ALLOW_EXPIRED = False\n"},
                commit=True,
            )
        if "b" not in self._source_execution_ids:
            self.source(
                "b",
                files={"profile.py": b"PROFILE_ENABLED = True\n"},
                commit=True,
            )
        if self._integration_execution_id is None:
            self._run_integration_execution()
        if self._integration_operation_id is None:
            self._integration_operation_id = uuid5(
                NAMESPACE_URL,
                f"sidera.integration-candidate-operation:{self.integration_execution_id}",
            )
        candidate = ResultIntegrationService(
            self.uow,
            clock=lambda: self._planning.now,
        ).publish_candidate(
            workspace_id=self.workspace_id,
            execution_id=self.integration_execution_id,
            operation_id=self.integration_operation_id,
        )
        if run_checks:
            self._run_verification_checks(
                candidate,
                tamper_actual_tree=tamper_actual_tree,
            )
        return candidate

    def rework_payload(
        self,
        candidate: IntegrationCandidateV1,
        *,
        mode: str = "user",
        target_worker_item_keys: tuple[str, ...] = ("login",),
        impact_worker_item_keys: tuple[str, ...] | None = None,
        reason: str = "repair the failed result",
    ) -> dict[str, object]:
        def worker_id(key: str) -> UUID:
            normalized = "summary" if key == "integrate" else ("a" if key == "login" else key)
            value = self.worker_item_ids.get(normalized)
            if value is None:
                raise KeyError(normalized)
            return value

        target_ids = tuple(worker_id(key) for key in target_worker_item_keys)
        impact_keys = (
            target_worker_item_keys
            if impact_worker_item_keys is None
            else impact_worker_item_keys
        )
        return {
            "candidate_id": str(candidate.id),
            "candidate_hash": candidate.content_hash,
            "mode": mode,
            "issue_keys": ["check:unit"],
            "target_worker_item_ids": [str(item) for item in target_ids],
            "impact_worker_item_ids": [str(worker_id(key)) for key in impact_keys],
            "reason": reason,
        }

    def command(
        self,
        command_type: str,
        payload: dict[str, object],
        *,
        actor_type: str = "local",
        actor_id: str = "operator",
        idempotency_key: str | None = None,
        command_id: UUID | None = None,
        expected_version: int | None = None,
        target_id: UUID | None = None,
    ):
        key = idempotency_key or f"scenario:{command_type}:{uuid4()}"
        command_id = command_id or uuid5(NAMESPACE_URL, f"result-scenario-command:{key}")
        if expected_version is None:
            version = self._command_expected_versions.setdefault(
                key,
                self.root().version
                if command_type == "CompleteTask"
                else self.head().result_version,
            )
        else:
            version = expected_version
        service = CommandService(
            lambda: self.uow(),
            (
                ResultAcceptanceService(
                    clock=lambda: self._planning.now,
                    local_actor_type="local",
                    local_actor_id="operator",
                ),
                ResultReworkService(
                    clock=lambda: self._planning.now,
                    local_actor_type="local",
                    local_actor_id="operator",
                ),
                ResultIntegrationService(
                    lambda: self.uow(),
                    clock=lambda: self._planning.now,
                ),
            ),
        )
        result = service.handle(
            CommandEnvelope(
                id=command_id,
                workspace_id=self.workspace_id,
                command_type=command_type,
                target_type="task",
                actor_type=actor_type,
                actor_id=actor_id,
                target_id=target_id or self.root_task_id,
                payload=payload,
                expected_version=version,
                idempotency_key=key,
                correlation_id=self.root_task_id,
            )
        )
        if command_type == "ReintegrateTaskResult":
            request_id = UUID(str(result.data["execution_request_id"]))
            self._pending_reintegration_request_id = request_id
            self._integration_request_id = request_id
            self._integration_execution_id = None
            self._integration_operation_id = UUID(
                str(result.data["integration_operation_id"])
            )
        return result

    def accept_payload(self, candidate: IntegrationCandidateV1) -> dict[str, object]:
        """Build the exact acceptance contract from persisted candidate evidence."""

        if not isinstance(candidate, IntegrationCandidateV1):
            raise TypeError("candidate must be a typed integration candidate")
        with self.uow() as uow:
            plan = uow.plans.get(candidate.plan_snapshot_id, self.workspace_id)
        if plan is None or plan.integration_spec is None:
            raise AssertionError("integration acceptance spec is unavailable")
        results = self.verification_results(candidate.id)
        evaluations = evaluate_criteria(plan.integration_spec.criteria, results)
        decisions: list[CriterionDecisionV1] = []
        for criterion, evaluation in zip(
            plan.integration_spec.criteria, evaluations, strict=True
        ):
            if evaluation.observed_status is CriterionObservedStatus.PASSED:
                decision = CriterionDecisionType.ACCEPT_PASSED
                reason = None
            elif evaluation.eligible_manual:
                decision = CriterionDecisionType.MANUAL_PASSED
                reason = "operator reviewed the manual criterion"
            else:
                decision = CriterionDecisionType.EXCEPTION_ACCEPTED
                reason = "operator accepted the candidate exception"
            decisions.append(CriterionDecisionV1(criterion.key, decision, reason))
        result_version = self.head().result_version
        return {
            "candidate_id": str(candidate.id),
            "candidate_hash": candidate.content_hash,
            "acceptance_basis_hash": build_acceptance_basis_hash(
                candidate,
                results,
                decisions,
                result_version=result_version,
                integration_spec=plan.integration_spec,
            ),
            "expected_result_version": result_version,
            "criteria": [decision.to_mapping() for decision in decisions],
        }

    def selection(self, selection_id: UUID):
        with self.uow() as uow:
            selection = uow.input_selections.get(self.workspace_id, selection_id)
        if selection is None:
            raise AssertionError(f"input selection {selection_id} was not found")
        return selection

    def release_for_request(self, request_id: UUID):
        with self.uow() as uow:
            release = uow.collaboration_ledger.get_release_for_request(
                self.workspace_id, request_id
            )
        if release is None:
            raise AssertionError(f"release for request {request_id} was not found")
        return release

    def latest_candidate(self) -> IntegrationCandidateV1:
        head = self.head()
        if head.current_candidate_id is None:
            raise AssertionError("current integration candidate is not available")
        with self.uow() as uow:
            candidate = uow.results.get_record(
                "candidate", head.current_candidate_id, self.workspace_id
            )
        if not isinstance(candidate, IntegrationCandidateV1):
            raise TypeError("current integration candidate is not typed")
        return candidate

    def latest_source(self, key: str) -> CodeResultSnapshotV1:
        source_key = "a" if key == "login" else key
        if source_key not in self.worker_item_ids:
            raise KeyError(source_key)
        execution_id = self._planning._execution_id(source_key)
        if execution_id is None:
            raise AssertionError(f"source execution {source_key!r} was not created")
        with self.uow() as uow:
            snapshot = uow.results.get_code_result_by_capture_operation(
                code_capture_operation_id(execution_id), self.workspace_id
            )
        if not isinstance(snapshot, CodeResultSnapshotV1):
            raise TypeError(f"source result {source_key!r} was not captured")
        return snapshot

    def reintegration_payload(
        self,
        candidate: IntegrationCandidateV1,
        replacement: CodeResultSnapshotV1,
        *,
        target_observation_id: UUID | None = None,
        reason: str = "reintegrate repaired source results",
    ) -> dict[str, object]:
        if not isinstance(candidate, IntegrationCandidateV1):
            raise TypeError("candidate must be a typed integration candidate")
        if not isinstance(replacement, CodeResultSnapshotV1):
            raise TypeError("replacement must be a typed code result snapshot")
        return {
            "source_candidate_id": str(candidate.id),
            "source_candidate_hash": candidate.content_hash,
            "replacement_result_ids": [str(replacement.id)],
            "expected_result_version": self.head().result_version,
            "target_observation_id": (
                None
                if target_observation_id is None
                else str(target_observation_id)
            ),
            "reason": reason,
        }

    def rework_source(
        self, request_id: UUID, *, files: dict[str, bytes]
    ) -> CodeResultSnapshotV1:
        """Run a rework ExecutionRequest through the real capture boundary."""

        if not isinstance(request_id, UUID):
            raise TypeError("request_id must be a UUID")
        if not isinstance(files, dict) or not files:
            raise ValueError("rework files are required")
        self.seed_plan()
        self.tick()
        request = self.request(request_id)
        if request.result_derivation_kind != "rework":
            raise AssertionError("request is not a rework request")
        execution_id = request.first_execution_id
        if execution_id is None:
            raise AssertionError("rework execution was not materialized")
        worker_id, lease_id, session_id, execution_version = self._planning._start_source(
            execution_id, key=request.worker_item_key
        )
        worktree, manifest = self._create_worktree(execution_id)
        with self.uow() as uow:
            uow.workspaces.add(manifest)
        capture_operation = code_capture_operation_id(execution_id)
        service = self.capture_service
        service.record_execution_start(
            manifest=manifest,
            input_commit=self._base_commit,
            writable_paths=(".",),
            operation_id=capture_operation,
        )
        for relative, body in files.items():
            path = (worktree / relative).resolve()
            if worktree not in path.parents or path == worktree:
                raise ValueError("rework file escapes the worktree")
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(body)
        self._git(worktree, "add", "-A")
        self._git(
            worktree,
            "-c",
            "user.name=Result Integration Worker",
            "-c",
            "user.email=worker@sidera.local",
            "commit",
            "-m",
            f"rework result {request.worker_item_key}",
        )
        service.observe_terminal(
            workspace_id=self.workspace_id,
            execution_id=execution_id,
            operation_id=capture_operation,
            outcome="succeeded",
            fencing_token=1,
        )
        snapshot = service.capture_execution(
            workspace_id=self.workspace_id,
            execution_id=execution_id,
            operation_id=capture_operation,
        )
        with self.uow() as uow:
            current = uow.executions.get(execution_id, self.workspace_id)
        if current is None:
            raise AssertionError("rework execution disappeared")
        CommandService(
            lambda: self.uow(),
            (ExecutionCommandHandler(lambda: self._planning.now),),
        ).handle(
            CommandEnvelope(
                id=uuid4(),
                workspace_id=self.workspace_id,
                command_type="ReportExecutionSucceeded",
                target_type="execution",
                actor_type="worker",
                actor_id=str(worker_id),
                target_id=execution_id,
                payload={
                    "worker_id": str(worker_id),
                    "lease_id": str(lease_id),
                    "fencing_token": 1,
                },
                expected_version=current.version,
                idempotency_key=f"result-capture-rework-succeeded:{execution_id}",
                correlation_id=self.root_task_id,
            )
        )
        del session_id, execution_version
        return snapshot

    def rework_records(self) -> tuple[ReworkRecordV1, ...]:
        with self.uow() as uow:
            return uow.results.list_rework(self.root_task_id, self.workspace_id)

    def revise_plan(self, *, minimum_count: int) -> object:
        """Advance the planning head so an older integration execution is stale."""

        if type(minimum_count) is not int or minimum_count < 1:
            raise ValueError("minimum_count must be a positive integer")
        with self.uow() as uow:
            head = uow.plans.get_head(
                self.root_task_id, self.workspace_id, for_update=True
            )
            current = (
                None
                if head is None
                else uow.plans.get(head.active_snapshot_id, self.workspace_id)
            )
            if head is None or current is None:
                raise AssertionError("current plan is unavailable")
            revised_id = uuid4()
            revised = replace(
                current,
                id=revised_id,
                generation=current.generation + 1,
                parent_snapshot_id=current.id,
                source_ref=f"test:plan-revision:{revised_id}",
                goal_summary=(
                    f"{current.goal_summary} (revision quorum {minimum_count})"
                ),
                published_at=self._planning.now,
            )
            uow.plans.add_snapshot_and_advance_head(revised, expected_head=head)
        self.ids["plan_snapshot"] = revised.id
        return revised

    def corrupt_current_selection_hash(self) -> None:
        """Corrupt only the request's persisted selection reference for a stale-input test."""

        request_id = self.request_ids.get("summary")
        if request_id is None:
            raise AssertionError("summary request is not available")
        with self.database.session() as session:
            session.execute(
                update(ExecutionRequestModel)
                .where(
                    ExecutionRequestModel.workspace_id == self.workspace_id,
                    ExecutionRequestModel.id == request_id,
                )
                .values(input_selection_hash="f" * 64)
            )
            session.commit()

    def drive(self, *, limit: int = 30) -> object:
        """Prepare the V3 code package and drive the real Gateway start path."""

        if type(limit) is not int or limit < 1:
            raise ValueError("drive limit must be positive")
        del limit
        with self.uow() as uow:
            head = uow.results.get_head(self.workspace_id, self.root_task_id)
        if head is not None and head.current_acceptance_id is not None:
            return ResultFinalizationService(
                self.uow,
                clock=lambda: self._planning.now,
            ).reconcile(
                workspace_id=self.workspace_id,
                root_task_id=self.root_task_id,
            )
        if self._pending_reintegration_request_id is not None:
            request_id = self._pending_reintegration_request_id
            self._run_integration_execution(request_id=request_id)
            candidate = ResultIntegrationService(
                self.uow,
                clock=lambda: self._planning.now,
            ).publish_candidate(
                workspace_id=self.workspace_id,
                execution_id=self.integration_execution_id,
                operation_id=self.integration_operation_id,
            )
            self._pending_reintegration_request_id = None
            return candidate
        self._planning.worker_context_versions = self.worker_context_versions
        self._planning.code_provider_failure = self.code_provider_failure
        code_manifest = self.code_input_manifest()
        self._install_code_context(code_manifest)
        context_result = self._planning.run_context_worker("summary")
        if context_result.ready < 1 or context_result.failed != 0:
            raise AssertionError(
                f"code context package was not materialized: {context_result}"
            )
        try:
            return self._planning.start_worker("summary")
        except GatewayMessageError as exc:
            if exc.code == "context_adapter_unsupported":
                return exc
            if exc.code is not None and exc.code.startswith("code_input_"):
                return exc
            raise

    def _install_code_context(self, code_manifest: CodeInputManifestV1) -> None:
        with self.uow() as uow:
            execution_request = uow.execution_requests.get(
                code_manifest.request_id,
                self.workspace_id,
            )
            execution = uow.executions.get(code_manifest.execution_id, self.workspace_id)
            worker_item = (
                None
                if execution_request is None
                else uow.worker_items.get(
                    execution_request.worker_item_id,
                    self.workspace_id,
                )
            )
            context = (
                None
                if execution_request is None
                else uow.work_contexts.get_by_planning_request(
                    execution_request.planning_request_id,
                    self.workspace_id,
                )
            )
            revision = (
                None
                if context is None
                else uow.work_contexts.get_revision(
                    context.current_revision_id,
                    self.workspace_id,
                )
            )
        if execution_request is None or execution is None or worker_item is None:
            raise AssertionError("integration execution facts are unavailable")
        if revision is None:
            raise AssertionError("integration WorkContext revision is unavailable")
        with self.uow() as uow:
            assembly = build_execution_context_assembly_request(
                execution=execution,
                execution_request=execution_request,
                worker_item=worker_item,
                work_context_revision=revision,
                ttl_seconds=3600,
                created_at=NOW,
                manifest_schema_version=3,
                input_selection=uow.input_selections.get(
                    self.workspace_id,
                    code_manifest.selection_id,
                ),
                result_input_budget=ResultInputBudgetV1(),
                code_input_manifest=code_manifest,
                idempotency_key=(
                    f"result-code-context:{execution.id}:v3:{code_manifest.input_hash}"
                ),
            )
            uow.context_assemblies.add(assembly)

    def _run_integration_execution(self, *, request_id: UUID | None = None) -> None:
        request_id = request_id or self._integration_request_id or self.request_ids["summary"]
        self._planning.reconcile()
        self._planning.activate()
        execution_id = self._planning._execution_id("summary")
        if execution_id is None:
            raise AssertionError("integration execution was not activated")
        with self.uow() as uow:
            execution_request = uow.execution_requests.get(request_id, self.workspace_id)
            execution = uow.executions.get(execution_id, self.workspace_id)
        if execution_request is None or execution is None:
            raise AssertionError("integration execution facts are unavailable")
        if execution_request.first_execution_id != execution_id:
            raise AssertionError("latest integration execution does not match request")
        worker_id, lease_id, _session_id, execution_version = self._planning._start_source(
            execution_id, key="summary"
        )
        worktree, manifest = self._create_worktree(execution_id)
        with self.uow() as uow:
            uow.workspaces.add(manifest)
        code_inputs = self.code_input_manifest(
            base_commit=self._base_commit,
            request_id=request_id,
            execution_id=execution_id,
        )
        materialization_operation_id = uuid5(
            NAMESPACE_URL,
            f"sidera.integration-input:{execution_id}:{code_inputs.input_hash}",
        )
        receipt = GitResultStore({"repo://integration-test": self._repository}).materialize(
            workspace_manifest=manifest,
            code_inputs=code_inputs,
            operation_id=materialization_operation_id,
        )
        if not receipt.succeeded:
            raise AssertionError(f"integration inputs conflicted: {receipt.conflict_paths}")
        self._integration_execution_id = execution_id
        self._integration_request_id = request_id
        if self._integration_operation_id is None:
            self._integration_operation_id = uuid5(
                NAMESPACE_URL,
                f"sidera.integration-candidate-operation:{execution_id}",
            )
        capture_operation_id = code_capture_operation_id(execution_id)
        service = self.capture_service
        service.record_execution_start(
            manifest=manifest,
            input_commit=receipt.actual_commit,
            writable_paths=(".",),
            operation_id=capture_operation_id,
        )
        output = worktree / "integration.txt"
        output.write_text("integrated source results\n", encoding="utf-8")
        if self._check_exit_code is not None:
            if self._modify_source_during_check:
                script = (
                    "from pathlib import Path\n"
                    "import os\n"
                    "Path('integration.txt').write_text('tampered\\n')\n"
                    f"os._exit({self._check_exit_code})\n"
                )
            else:
                script = f"import os\n\nos._exit({self._check_exit_code})\n"
            (worktree / "test_sidera_result_check.py").write_text(
                script, encoding="utf-8"
            )
        self._git(worktree, "add", "-A")
        self._git(
            worktree,
            "-c",
            "user.name=Result Integration Worker",
            "-c",
            "user.email=worker@sidera.local",
            "commit",
            "-m",
            "integrate source results",
        )
        service.observe_terminal(
            workspace_id=self.workspace_id,
            execution_id=execution_id,
            operation_id=capture_operation_id,
            outcome="succeeded",
            fencing_token=1,
        )
        service.capture_execution(
            workspace_id=self.workspace_id,
            execution_id=execution_id,
            operation_id=capture_operation_id,
        )
        CommandService(
            lambda: self.uow(),
            (ExecutionCommandHandler(lambda: self._planning.now),),
        ).handle(
            CommandEnvelope(
                id=uuid4(),
                workspace_id=self.workspace_id,
                command_type="ReportExecutionSucceeded",
                target_type="execution",
                actor_type="worker",
                actor_id=str(worker_id),
                target_id=execution_id,
                payload={
                    "worker_id": str(worker_id),
                    "lease_id": str(lease_id),
                    "fencing_token": 1,
                },
                expected_version=execution_version,
                idempotency_key=f"result-capture-integration-succeeded:{execution_id}",
                correlation_id=self.root_task_id,
            )
        )

    def reopen_services(self) -> None:
        self._capture_service = self._new_capture_service()

    def retained_code_results(self) -> tuple[CodeResultSnapshotV1, ...]:
        with self.uow() as uow:
            return uow.results.retained_code_results(self.workspace_id)

    def code_materialization_attempts(self, key: str) -> int:
        return self._planning.code_materialization_attempts(key)

    def adapter_start_count(self, key: str) -> int:
        return self._planning.start_call_count(key)

    @property
    def verification_service(self) -> ResultVerificationService:
        return ResultVerificationService(
            self.uow,
            clock=lambda: self._planning.now,
        )

    def request(self, request_id: UUID):
        with self.uow() as uow:
            request = uow.execution_requests.get(request_id, self.workspace_id)
        if request is None:
            raise AssertionError(f"execution request {request_id} was not found")
        return request

    def worker_configuration(self, key: str):
        normalized_key = "summary" if key == "integrate" else key
        worker_item_id = self.worker_item_ids.get(normalized_key)
        if worker_item_id is None:
            raise KeyError(normalized_key)
        with self.uow() as uow:
            worker_item = uow.worker_items.get(worker_item_id, self.workspace_id)
        if worker_item is None:
            raise AssertionError(f"WorkerItem {normalized_key!r} was not found")
        return worker_item

    def verification_results(
        self, candidate_id: UUID
    ) -> tuple[VerificationResultV1, ...]:
        with self.uow() as uow:
            return uow.results.list_verification_results(candidate_id, self.workspace_id)

    def candidate_commit(self, candidate: object) -> str:
        candidate_id = getattr(candidate, "id", None)
        code_result_id = getattr(candidate, "code_result_id", None)
        if not isinstance(candidate_id, UUID) or not isinstance(code_result_id, UUID):
            raise TypeError("candidate is not a typed integration candidate")
        with self.uow() as uow:
            snapshot = uow.results.get_record(
                "code_snapshot", code_result_id, self.workspace_id
            )
        if not isinstance(snapshot, CodeResultSnapshotV1):
            raise TypeError(f"candidate {candidate_id} has no code snapshot")
        return snapshot.result_commit

    def candidate_tree(self, candidate: object) -> str:
        code_result_id = getattr(candidate, "code_result_id", None)
        if not isinstance(code_result_id, UUID):
            raise TypeError("candidate is not a typed integration candidate")
        with self.uow() as uow:
            snapshot = uow.results.get_record(
                "code_snapshot", code_result_id, self.workspace_id
            )
        if not isinstance(snapshot, CodeResultSnapshotV1):
            raise TypeError("candidate code snapshot is unavailable")
        return snapshot.result_tree

    def finish_verification(
        self, candidate_id: UUID
    ) -> tuple[VerificationResultV1, ...]:
        del candidate_id
        if not self._verification_operation_ids:
            return ()
        results: tuple[VerificationResultV1, ...] = ()
        for operation_id in self._verification_operation_ids:
            results = self.verification_service.finish(
                workspace_id=self.workspace_id,
                operation_id=operation_id,
            )
        return results

    def verification_operation(self):
        if len(self._verification_operation_ids) != 1:
            raise AssertionError("verification operation is not available")
        with self.uow() as uow:
            operation = uow.results.get_operation(
                self._verification_operation_ids[0], self.workspace_id
            )
        if not isinstance(operation, ResultOperationAttemptV1):
            raise TypeError("verification operation was not found")
        return operation

    def verification_log_snapshot(self) -> tuple[tuple[str, bytes], ...]:
        operation = self.verification_operation()
        observation = operation.payload.get("observation")
        if not isinstance(observation, dict):
            raise TypeError("verification observation is unavailable")
        execution_id = UUID(str(operation.payload["execution_id"]))
        log_refs = observation.get("log_refs")
        if not isinstance(log_refs, list):
            raise TypeError("verification log refs are unavailable")
        return tuple(
            (
                str(log_ref),
                (self._worktree_root / str(execution_id) / str(log_ref)).read_bytes(),
            )
            for log_ref in log_refs
        )

    def mark_verification_interrupted(self) -> None:
        operation = self.verification_operation()
        raw_observation = operation.payload.get("observation")
        if not isinstance(raw_observation, dict):
            raise TypeError("verification observation is unavailable")
        observation = CheckRunObservation.from_mapping(
            {
                **raw_observation,
                "status": CheckRunStatus.INTERRUPTED.value,
                "finished_at": NOW.isoformat(),
                "exit_code": -15,
                "failure_code": "stopped_by_request",
            }
        )
        with self.uow() as uow:
            current = uow.results.get_operation(
                operation.operation_id, self.workspace_id, for_update=True
            )
            if not isinstance(current, ResultOperationAttemptV1):
                raise TypeError("verification operation was not found")
            updated = replace(
                current,
                status=ResultOperationStatus.FAILED,
                payload={**dict(current.payload), "observation": observation.to_mapping()},
                failure_code="stopped_by_request",
                version=current.version + 1,
                updated_at=NOW,
            )
            uow.results.save_operation(updated, expected_version=current.version)

    def retry_verification(self, *, reason: str = "check failed"):
        operation = self.verification_operation()
        with self.uow() as uow:
            head = uow.results.get_head(
                self.workspace_id, self.root_task_id, for_update=True
            )
            candidate = uow.results.get_record(
                "candidate",
                UUID(str(operation.payload["candidate_id"])),
                self.workspace_id,
            )
        if not isinstance(candidate, IntegrationCandidateV1):
            raise TypeError("verification candidate was not found")
        candidate_id = UUID(str(operation.payload["candidate_id"]))
        if candidate.id != candidate_id:
            raise AssertionError("verification operation candidate changed")
        command = CommandEnvelope(
            id=uuid5(
                NAMESPACE_URL,
                f"result-integration-verification-retry:{operation.operation_id}",
            ),
            workspace_id=self.workspace_id,
            command_type="RetryResultVerification",
            target_type="verification_operation",
            actor_type="user",
            actor_id="result-integration-test",
            target_id=operation.operation_id,
            payload={
                "verification_operation_id": str(operation.operation_id),
                "expected_operation_version": operation.version,
                "expected_result_version": head.result_version if head else 0,
                "candidate_id": str(candidate.id),
                "candidate_hash": candidate.content_hash,
                "reason": reason,
            },
            expected_version=operation.version,
            idempotency_key=f"result-integration-verification-retry:{operation.operation_id}",
            correlation_id=self.root_task_id,
        )
        with self.uow() as uow:
            existing_command = uow.commands.find_by_id(self.workspace_id, command.id)
            result = self.verification_service.retry(uow, command)
            if existing_command is None:
                uow.commands.record_applied(command, hash_request(command), result)
        return result

    def _run_verification_checks(
        self,
        candidate: object,
        *,
        tamper_actual_tree: bool = False,
    ) -> None:
        candidate_id = getattr(candidate, "id", None)
        if not isinstance(candidate, IntegrationCandidateV1):
            raise TypeError("candidate is not typed")
        candidate_id = candidate.id
        request_id = self.verification_service.schedule(
            workspace_id=self.workspace_id,
            candidate_id=candidate_id,
            command_id=uuid5(
                NAMESPACE_URL,
                f"result-integration-verification-schedule:{candidate_id}",
            ),
        )
        self.tick()
        request = self.request(request_id)
        execution_id = request.first_execution_id
        if execution_id is None:
            raise AssertionError("verification request was not materialized")
        _worker_id, lease_id, session_id, _execution_version = self._planning._start_source(
            execution_id,
            key="summary",
        )
        result_commit = self.candidate_commit(candidate)
        result_tree = self.candidate_tree(candidate)
        worktree, manifest = self._create_worktree(
            execution_id,
            base_revision=result_commit,
        )
        with self.uow() as uow:
            uow.workspaces.add(manifest)
            plan = uow.plans.get(candidate.plan_snapshot_id, self.workspace_id)
        if plan is None or plan.integration_spec is None:
            raise AssertionError("verification integration spec is unavailable")
        check = plan.integration_spec.checks[0]
        operation_id = uuid5(
            NAMESPACE_URL,
            f"result-integration-verification-check:{execution_id}:{check.key}",
        )
        runner = self._verification_runner
        if runner is None:
            runner = LocalCheckRunner(
                state_root=self.tmp_path / "verification-state",
                clock=lambda: NOW,
            )
            self._verification_runner = runner
        observation = runner.start(
            CheckRunRequest(
                operation_id=operation_id,
                candidate_id=candidate_id,
                candidate_hash=candidate.content_hash,
                actual_commit=result_commit,
                actual_tree=result_tree,
                check=check,
                workspace_location=worktree,
                deadline=NOW + timedelta(seconds=check.timeout_seconds),
                log_ref=f"verification/{operation_id}/{check.key}",
            )
        )
        deadline = time.monotonic() + 10
        while observation.status is CheckRunStatus.RUNNING and time.monotonic() < deadline:
            time.sleep(0.01)
            refreshed = runner.inspect(operation_id)
            if refreshed is None:
                raise AssertionError("verification observation disappeared")
            observation = refreshed
        if observation.status is CheckRunStatus.RUNNING:
            observation = runner.stop(operation_id)
        if observation is None or observation.status is CheckRunStatus.RUNNING:
            raise AssertionError("verification check did not reach a terminal state")
        event_type = (
            "session.succeeded"
            if observation.status is CheckRunStatus.PASSED
            else "session.canceled"
            if observation.status is CheckRunStatus.INTERRUPTED
            else "session.failed"
        )
        payload = observation.to_mapping()
        payload.update(
            {
                "purpose": "verification",
                "provider": "local_verification",
                "candidate_id": str(candidate_id),
                "candidate_hash": candidate.content_hash,
                "check_key": check.key,
                "check_hash": hashlib.sha256(
                    json.dumps(
                        check.to_mapping(),
                        ensure_ascii=False,
                        sort_keys=True,
                        separators=(",", ":"),
                    ).encode("utf-8")
                ).hexdigest(),
                "attempt": 1,
            }
        )
        if tamper_actual_tree:
            payload["actual_tree"] = "0" * 40 if result_tree != "0" * 40 else "1" * 40
        SessionEventIngestor(
            self.uow,
            clock=lambda: self._planning.now,
        ).ingest_event(
            workspace_id=self.workspace_id,
            execution_id=execution_id,
            session_id=session_id,
            lease_id=lease_id,
            fencing_token=1,
            event=AdapterEvent(
                event_type=event_type,
                sequence=1,
                payload=payload,
                occurred_at=observation.finished_at or NOW,
            ),
        )
        self._verification_operation_ids = (operation_id,)

    def tick(self, *, limit: int = 20):
        return ActivationReconciler(
            uow_factory=self.uow,
            owner_id="result-verification-activation",
            clock=lambda: self._planning.now,
        ).run_once(limit=limit)

    def context_assembly_for_request(self, request_id: UUID):
        request = self.request(request_id)
        if request.first_execution_id is None:
            raise AssertionError("execution request has not been materialized")
        with self.uow() as uow:
            assembly = uow.context_assemblies.get_for_consumer(
                ContextPurpose.EXECUTION,
                request.first_execution_id,
                self.workspace_id,
                manifest_schema_version=3,
            )
        if assembly is None:
            raise AssertionError("execution context assembly was not found")
        return assembly

    def _new_capture_service(self) -> CodeResultCaptureService:
        return CodeResultCaptureService(
            lambda: self.uow(),
            GitResultStore({"repo://integration-test": self._repository}),
            artifact_content_store=self._artifact_store,
            clock=lambda: self._planning.now,
        )

    def _init_repository(self) -> str:
        self._repository.mkdir(parents=True, exist_ok=True)
        self._worktree_root.mkdir(parents=True, exist_ok=True)
        self._git(self._repository, "init", "--initial-branch=main")
        (self._repository / ".gitignore").write_text(
            "*.ignored\n__pycache__/\n", encoding="utf-8"
        )
        (self._repository / "README.md").write_text("base\n", encoding="utf-8")
        self._git(self._repository, "add", "-A")
        self._git(
            self._repository,
            "-c",
            "user.name=Result Integration Base",
            "-c",
            "user.email=base@sidera.local",
            "commit",
            "-m",
            "base",
        )
        return self._git(self._repository, "rev-parse", "HEAD")

    def _create_worktree(
        self, execution_id: UUID, *, base_revision: str | None = None
    ) -> tuple[Path, ExecutionWorkspaceManifest]:
        worktree = self._worktree_root / str(execution_id)
        branch = f"sidera/execution/{execution_id}"
        revision = base_revision or self._base_commit
        self._git(
            self._repository,
            "worktree",
            "add",
            "-b",
            branch,
            str(worktree),
            revision,
        )
        manifest = ExecutionWorkspaceManifest.create(
            id=uuid4(),
            workspace_id=self.workspace_id,
            task_id=self._planning._execution_task_id(execution_id),
            execution_id=execution_id,
            checkouts=(
                WorkspaceCheckout(
                    repository_ref="repo://integration-test",
                    base_revision=revision,
                    branch=branch,
                    current_head=revision,
                ),
            ),
            location_ref=f"git-worktree://{execution_id}",
            retention_seconds=3600,
            fencing_token=1,
            created_at=NOW,
        )
        manifest.mark_preparing()
        manifest.mark_ready()
        manifest.mark_active()
        return worktree, manifest

    @staticmethod
    def _git(cwd: Path, *args: str) -> str:
        result = subprocess.run(
            ["git", *args],
            cwd=cwd,
            check=True,
            capture_output=True,
            env={**os.environ, "GIT_OPTIONAL_LOCKS": "0"},
        )
        return result.stdout.decode("utf-8").strip()

    def confirm_with_missing_spec_module(self) -> None:
        self._planning.confirm_plan(
            result_inputs_enabled=True,
            proposal_schema_version=6,
            profile_key="r6-invalid-result-planning",
            verification_module_refs=(
                UUID("00000000-0000-0000-0000-000000000090"),
                UUID("00000000-0000-0000-0000-000000000091"),
            ),
        )

    def reconfirm(self) -> None:
        if not self._seeded:
            raise AssertionError("result integration plan has not been seeded")
        self._planning.confirm_prepared_plan(result_inputs_enabled=True)

    def uow(self) -> SqlAlchemyUnitOfWork:
        return SqlAlchemyUnitOfWork(self.database)

    def root(self):
        with self.uow() as uow:
            root = uow.tasks.get(self.root_task_id, self.workspace_id)
        if root is None:
            raise AssertionError("result scenario root task was not found")
        return root

    def head(self) -> TaskResultHead:
        with self.uow() as uow:
            head = uow.results.get_head(self.workspace_id, self.root_task_id)
        if head is None:
            raise AssertionError("result scenario head was not found")
        return head

    def head_fixture(self) -> TaskResultHead:
        return TaskResultHead(
            workspace_id=self.workspace_id,
            root_task_id=self.root_task_id,
            current_candidate_id=None,
            current_acceptance_id=None,
            result_version=1,
            active_operation_id=None,
            auto_rounds_used=0,
            auto_rounds_limit=2,
            auto_seconds_limit=1800,
            auto_started_at=None,
            auto_deadline=None,
        )

    def snapshot(self, *, result_commit: str | None = None) -> CodeResultSnapshotV1:
        raw = snapshot_mapping(
            workspace_id=str(self.workspace_id),
            root_task_id=str(self.root_task_id),
            worker_item_id=str(self.ids["worker_item"]),
            request_id=str(self.request_id),
            execution_id=str(self.execution_id),
        )
        if result_commit is not None:
            raw["result_commit"] = result_commit
        content = {
            "schema_version": raw["schema_version"],
            "workspace_id": raw["workspace_id"],
            "root_task_id": raw["root_task_id"],
            "worker_item_id": raw["worker_item_id"],
            "request_id": raw["request_id"],
            "execution_id": raw["execution_id"],
            "repository_ref": raw["repository_ref"],
            "input_commit": raw["input_commit"],
            "input_tree": raw["input_tree"],
            "result_commit": raw["result_commit"],
            "result_tree": raw["result_tree"],
            "consumed_result_ids": raw["consumed_result_ids"],
            "artifact_ids": raw["artifact_ids"],
            "capture_operation_id": raw["capture_operation_id"],
        }
        raw["content_hash"] = hashlib.sha256(
            json.dumps(
                content,
                ensure_ascii=False,
                allow_nan=False,
                separators=(",", ":"),
                sort_keys=True,
            ).encode("utf-8")
        ).hexdigest()
        return CodeResultSnapshotV1.from_mapping(raw)

    def events(self, event_type: str | None = None) -> tuple[EventModel, ...]:
        with self.database.session() as session:
            statement = select(EventModel).where(
                EventModel.workspace_id == self.workspace_id
            )
            if event_type is not None:
                statement = statement.where(EventModel.event_type == event_type)
            return tuple(
                session.scalars(
                    statement.order_by(EventModel.created_at.asc(), EventModel.id.asc())
                ).all()
            )


@pytest.fixture
def result_scenario(
    database: Database, workspace_id: UUID, tmp_path: Path
) -> ResultIntegrationScenario:
    ids = seed_execution_request_facts(database, workspace_id)
    request_id = uuid4()
    with database.session() as session:
        legacy_request = request_row(ids, request_id=request_id)
        legacy_request.status = "obsolete"
        legacy_request.gate_reason = "result_integration_fixture_legacy_request"
        session.add(legacy_request)
        session.commit()
    return ResultIntegrationScenario(
        database,
        workspace_id,
        tmp_path,
        ids,
        request_id,
    )


__all__ = ["ResultIntegrationScenario", "result_scenario"]
