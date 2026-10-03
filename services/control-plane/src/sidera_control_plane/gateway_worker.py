import argparse
import json
import sys
import time
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol
from uuid import UUID

from sidera_control_plane.acceptance_guard import load_acceptance_guard
from sidera_control_plane.adapters.git_results import GitResultStore
from sidera_control_plane.adapters.git_workspace import (
    GitWorktreeWorkspaceManager,
    opaque_repository_ref,
)
from sidera_control_plane.adapters.knowledge_http import KnowledgeHttpAdapter
from sidera_control_plane.application.agent_sources import TaskContextSourceReader
from sidera_control_plane.application.agent_worker_adapter import NativeWorkerServices
from sidera_control_plane.application.artifact_content import ArtifactContentService
from sidera_control_plane.application.code_result_capture import (
    CodeResultCaptureService,
)
from sidera_control_plane.application.context_materialization import (
    ContextMaterializer,
)
from sidera_control_plane.application.execution_authority import (
    ExecutionAuthorityReader,
)
from sidera_control_plane.application.knowledge_candidate_submission import (
    FileCandidateProposalSpool,
    KnowledgeCandidateSubmissionService,
)
from sidera_control_plane.application.worker_gateway import (
    ConfiguredExecutionSpecFactory,
    GatewayAssignmentRunner,
    WorkerGateway,
)
from sidera_control_plane.composition import worker_adapters as _worker_adapters
from sidera_control_plane.config import Settings
from sidera_control_plane.demo.manifest import load_manifest, validate_manifest
from sidera_control_plane.persistence.artifact_content_store import (
    LocalArtifactContentStore,
)
from sidera_control_plane.persistence.database import Database
from sidera_control_plane.persistence.outbox_store import SqlAlchemyOutboxStore
from sidera_control_plane.persistence.unit_of_work import SqlAlchemyUnitOfWork

WorkerAdapterRegistration = _worker_adapters.WorkerAdapterRegistration
_workspace_location = _worker_adapters._workspace_location
build_worker_adapter_factories = _worker_adapters.build_worker_adapter_factories
registered_worker_adapter_types = _worker_adapters.registered_worker_adapter_types
worker_adapter_inventory = _worker_adapters.worker_adapter_inventory


@dataclass(slots=True)
class GatewayRuntime:
    gateway: GatewayAssignmentRunner
    worker_gateway: WorkerGateway
    database: Database
    catalog: KnowledgeHttpAdapter
    _closed: bool = False

    def close(self) -> None:
        """Release the shared runtime resources exactly once."""

        if self._closed:
            return
        self._closed = True
        self.catalog.close()
        self.database.dispose()


class GatewayOwnerPort(Protocol):
    """Runtime surface consumed by the gateway process entrypoint."""

    @property
    def gateway(self) -> GatewayAssignmentRunner: ...

    input_runtime: Any
    handoff_runtime: Any


def _resolve_active_session_sync_workspace(settings: Settings) -> UUID | None:
    """Resolve the workspace whose active sessions this gateway keeps alive."""

    if settings.interview_demo_manifest is not None:
        manifest = load_manifest(Path(settings.interview_demo_manifest))
        validate_manifest(manifest, Path(__file__).resolve().parents[4])
        return manifest.workspace_id
    return getattr(settings, "gateway_sync_workspace_id", None)


def build_gateway(
    settings: Settings, *, native_services: NativeWorkerServices | None = None
) -> GatewayRuntime:
    database = Database(settings.database_url)
    catalog = KnowledgeHttpAdapter(
        base_url=settings.knowledge_base_url,
        control_plane_token=settings.knowledge_control_plane_token,
        metadata_timeout_seconds=settings.knowledge_metadata_timeout_seconds,
        materialization_timeout_seconds=settings.knowledge_materialization_timeout_seconds,
    )
    try:
        acceptance_guard = load_acceptance_guard()
        uow_factory = lambda: SqlAlchemyUnitOfWork(
            database,
            projection_change_retention_seconds=settings.projection_change_retention_seconds,
        )
        workspace_manager = GitWorktreeWorkspaceManager(
            settings.workspace_root,
            retention_seconds=settings.workspace_retention_seconds,
            git_executable=settings.git_executable,
        )
        adapter_factories = build_worker_adapter_factories(
            settings,
            workspace_manager=workspace_manager,
        )
        workspace_factories = {
            "git-worktree": lambda _instance: workspace_manager,
            "git_worktree": lambda _instance: workspace_manager,
        }
        artifact_content_store = LocalArtifactContentStore(
            settings.artifact_root,
            max_bytes=settings.artifact_max_bytes,
        )
        artifact_content_service = ArtifactContentService(
            database,
            artifact_content_store,
        )
        code_result_capture_service = None
        if settings.result_repository_path is not None and (
            settings.result_inputs_enabled or settings.result_integration_enabled
        ):
            result_repository = settings.result_repository_path.expanduser().resolve(
                strict=True
            )
            result_repositories = {
                opaque_repository_ref(result_repository): result_repository,
            }
            code_result_capture_service = CodeResultCaptureService(
                uow_factory,
                GitResultStore(
                    result_repositories,
                    git_executable=settings.git_executable,
                ),
                artifact_content_store=artifact_content_store,
                clock=lambda: datetime.now(UTC),
            )
        elif settings.result_integration_enabled:
            raise ValueError(
                "result_repository_path is required when result integration is enabled"
            )
        active_session_sync_workspace_id = _resolve_active_session_sync_workspace(
            settings
        )
        worker_gateway = WorkerGateway(
            uow_factory,
            adapter_factories=adapter_factories,
            workspace_factories=workspace_factories,
            spec_factory=ConfiguredExecutionSpecFactory(),
            native_services=native_services,
            native_source_reader=(
                TaskContextSourceReader(
                    uow_factory,
                    authorizer=ExecutionAuthorityReader(
                        uow_factory,
                        principal=lambda: native_services.principal_id,
                        authorizer=native_services.authorizer,
                    ),
                    catalog=catalog,
                    result_input_content=artifact_content_service,
                )
                if native_services is not None
                else None
            ),
            artifact_content_store=artifact_content_store,
            context_materializer=ContextMaterializer(
                catalog,
                result_input_content=artifact_content_service,
            ),
            context_package_cutover_at=settings.context_package_cutover_at,
            candidate_submission_service=KnowledgeCandidateSubmissionService(
                FileCandidateProposalSpool(
                    settings.knowledge_candidate_spool_dir,
                    quota_bytes=settings.knowledge_candidate_spool_quota_bytes,
                )
            ),
            code_result_capture_service=code_result_capture_service,
            interview_demo_completion_enabled=(
                settings.interview_demo_manifest is not None
            ),
            result_integration_enabled=settings.result_integration_enabled,
            assignment_lease_ttl_seconds=settings.assignment_lease_seconds,
            assignment_lease_renew_interval_seconds=(
                settings.assignment_lease_renew_interval_seconds
            ),
            acceptance_guard=acceptance_guard,
            clock=lambda: datetime.now(UTC),
        )
        runner = GatewayAssignmentRunner(
            SqlAlchemyOutboxStore(
                database, claim_seconds=settings.assignment_lease_seconds
            ),
            worker_gateway,
            active_session_sync=(
                (
                    lambda: worker_gateway.sync_active_sessions(
                        active_session_sync_workspace_id
                    )
                )
                if active_session_sync_workspace_id is not None
                else None
            ),
            clock=lambda: datetime.now(UTC),
            claim_renew_interval_seconds=max(
                settings.assignment_lease_seconds / 3, 0.1
            ),
            start_concurrency=settings.gateway_owner_start_concurrency,
        )
    except BaseException:
        catalog.close()
        database.dispose()
        raise
    return GatewayRuntime(
        gateway=runner,
        worker_gateway=worker_gateway,
        database=database,
        catalog=catalog,
    )


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run the SIDERA Worker Gateway")
    parser.add_argument(
        "--once",
        action="store_true",
        help="run one Gateway assignment batch and exit",
    )
    args = parser.parse_args(argv)
    runtime: object | None = None
    try:
        settings = Settings()  # pyright: ignore[reportCallIssue]
        runtime = _build_gateway_owner(settings)
        while True:
            _run_owner_once(runtime, settings)
            if args.once:
                return 0
            time.sleep(settings.gateway_poll_interval_seconds)
    except KeyboardInterrupt:
        return 0
    except Exception as exc:  # noqa: BLE001
        print(
            json.dumps(
                {
                    "component": "gateway",
                    "status": "error",
                    "error": f"{type(exc).__name__}: {exc}"[:1_000],
                },
                sort_keys=True,
                separators=(",", ":"),
            ),
            file=sys.stderr,
        )
        return 1
    finally:
        _dispose_runtime(runtime)


def _build_gateway_owner(settings: Settings) -> GatewayOwnerPort:
    # Keep the import lazy: gateway_owner injects build_gateway from this
    # module, while this entrypoint is the process boundary that owns it.
    from sidera_control_plane.gateway_owner import build_gateway_owner

    return build_gateway_owner(settings)


def _run_owner_once(owner: GatewayOwnerPort, settings: Settings) -> None:
    gateway = getattr(owner, "gateway", None)
    if gateway is None:
        gateway = _service(owner, "gateway")
    claimed, processed, failed = gateway.run_once(settings.gateway_batch_size)
    _print_summary(claimed, processed, failed)

    input_runtime = owner.input_runtime
    input_result = input_runtime.dispatcher.run_once(settings.gateway_batch_size)
    from sidera_control_plane.input_dispatcher_worker import (
        summary_json as input_summary,
    )

    print(input_summary(input_result))

    handoff_runtime = owner.handoff_runtime
    handoff_result = handoff_runtime.coordinator.run_once(settings.handoff_batch_size)
    from sidera_control_plane.handoff_worker import summary_json as handoff_summary

    print(handoff_summary(handoff_result))


def _service(runtime: object, name: str) -> Any:
    service = getattr(runtime, name, None)
    if service is None:
        service = runtime
    return service


def _dispose_runtime(runtime: object | None) -> None:
    if runtime is None:
        return
    close = getattr(runtime, "close", None)
    if callable(close):
        close()
        return
    database = getattr(runtime, "database", None)
    if database is not None:
        database.dispose()
    catalog = getattr(runtime, "catalog", None)
    if catalog is not None:
        catalog.close()


def _print_summary(claimed: int, processed: int, failed: int) -> None:
    print(
        json.dumps(
            {
                "component": "gateway",
                "status": "ok",
                "claimed": claimed,
                "processed": processed,
                "failed": failed,
            },
            sort_keys=True,
            separators=(",", ":"),
        )
    )


if __name__ == "__main__":
    raise SystemExit(main())
