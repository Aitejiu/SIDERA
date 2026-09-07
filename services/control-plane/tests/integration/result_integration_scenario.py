from __future__ import annotations

import hashlib
import json
import os
import subprocess
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from uuid import NAMESPACE_URL, UUID, uuid4, uuid5

import pytest
from sqlalchemy import select, update

from sidera_control_plane.adapters.git_results import GitResultStore
from sidera_control_plane.application.code_result_capture import (
    CodeResultCaptureService,
    code_capture_operation_id,
)
from sidera_control_plane.application.command_service import CommandService
from sidera_control_plane.application.context_requests import (
    build_execution_context_assembly_request,
)
from sidera_control_plane.application.event_ingestor import ArtifactRegistry
from sidera_control_plane.application.execution_commands import ExecutionCommandHandler
from sidera_control_plane.application.result_input_evaluation import (
    build_code_input_manifest,
)
from sidera_control_plane.application.result_integration import ResultIntegrationService
from sidera_control_plane.application.worker_gateway import GatewayMessageError
from sidera_control_plane.domain.artifacts import ArtifactCandidate, ArtifactType
from sidera_control_plane.domain.code_results import (
    CodeInputManifestV1,
    CodeResultSnapshotV1,
)
from sidera_control_plane.domain.commands import CommandEnvelope
from sidera_control_plane.domain.context_packages import ResultInputBudgetV1
from sidera_control_plane.domain.result_operations import TaskResultHead
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
        self._integration_operation_id: UUID | None = None
        self._repository = tmp_path / "result-integration-repository"
        self._worktree_root = tmp_path / "result-integration-worktrees"
        self._artifact_store = LocalArtifactContentStore(
            tmp_path / "result-integration-artifacts"
        )
        self._capture_service: CodeResultCaptureService | None = None
        self.code_provider_failure: str | None = None
        self.worker_context_versions: tuple[int, ...] = (1, 2, 3)
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

    @property
    def root_id(self) -> UUID:
        return self.root_task_id

    @property
    def plan_snapshot_id(self) -> UUID:
        return self.ids["plan_snapshot"]

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
        self, *, base_commit: str | None = None
    ) -> CodeInputManifestV1:
        """Build the exact code input manifest for the integration WorkerItem."""

        self._planning.reconcile()
        self._planning.activate()
        summary_execution_id = self._planning._execution_id("summary")
        if summary_execution_id is None:
            raise AssertionError("integration execution was not activated")
        with self.uow() as uow:
            execution_request = uow.execution_requests.get(
                self.request_ids["summary"],
                self.workspace_id,
            )
            execution = uow.executions.get(summary_execution_id, self.workspace_id)
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

    def candidate(self, *, run_checks: bool = False):
        """Run the real code integration WorkerItem and publish its candidate."""

        if type(run_checks) is not bool:
            raise ValueError("run_checks must be a boolean")
        if run_checks:
            raise NotImplementedError("result integration checks are added in Task 18")
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
        return ResultIntegrationService(
            self.uow,
            clock=lambda: self._planning.now,
        ).publish_candidate(
            workspace_id=self.workspace_id,
            execution_id=self.integration_execution_id,
            operation_id=self.integration_operation_id,
        )

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

    def _run_integration_execution(self) -> None:
        self._planning.reconcile()
        self._planning.activate()
        execution_id = self._planning._execution_id("summary")
        if execution_id is None:
            raise AssertionError("integration execution was not activated")
        worker_id, lease_id, _session_id, execution_version = self._planning._start_source(
            execution_id, key="summary"
        )
        worktree, manifest = self._create_worktree(execution_id)
        with self.uow() as uow:
            uow.workspaces.add(manifest)
        code_inputs = self.code_input_manifest(base_commit=self._base_commit)
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
        (self._repository / ".gitignore").write_text("*.ignored\n", encoding="utf-8")
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
        self, execution_id: UUID
    ) -> tuple[Path, ExecutionWorkspaceManifest]:
        worktree = self._worktree_root / str(execution_id)
        branch = f"sidera/execution/{execution_id}"
        self._git(
            self._repository,
            "worktree",
            "add",
            "-b",
            branch,
            str(worktree),
            self._base_commit,
        )
        manifest = ExecutionWorkspaceManifest.create(
            id=uuid4(),
            workspace_id=self.workspace_id,
            task_id=self._planning._execution_task_id(execution_id),
            execution_id=execution_id,
            checkouts=(
                WorkspaceCheckout(
                    repository_ref="repo://integration-test",
                    base_revision=self._base_commit,
                    branch=branch,
                    current_head=self._base_commit,
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
        session.add(request_row(ids, request_id=request_id))
        session.commit()
    return ResultIntegrationScenario(
        database,
        workspace_id,
        tmp_path,
        ids,
        request_id,
    )


__all__ = ["ResultIntegrationScenario", "result_scenario"]
