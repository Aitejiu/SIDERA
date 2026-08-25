import argparse
import json
import sys
import time
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from sidera_control_plane.adapters.fake_worker import FakeWorkerAdapter
from sidera_control_plane.adapters.git_workspace import GitWorktreeWorkspaceManager
from sidera_control_plane.adapters.knowledge_http import KnowledgeHttpAdapter
from sidera_control_plane.adapters.opencode_acp import OpenCodeACPAdapter
from sidera_control_plane.application.context_materialization import ContextMaterializer
from sidera_control_plane.application.knowledge_candidate_submission import (
    FileCandidateProposalSpool,
    KnowledgeCandidateSubmissionService,
)
from sidera_control_plane.application.ports import WorkerAdapter
from sidera_control_plane.application.worker_gateway import (
    ConfiguredExecutionSpecFactory,
    GatewayAssignmentRunner,
    WorkerGateway,
)
from sidera_control_plane.config import Settings
from sidera_control_plane.domain.modules import ModuleInstance
from sidera_control_plane.persistence.artifact_content_store import (
    LocalArtifactContentStore,
)
from sidera_control_plane.persistence.database import Database
from sidera_control_plane.persistence.outbox_store import SqlAlchemyOutboxStore
from sidera_control_plane.persistence.unit_of_work import SqlAlchemyUnitOfWork


@dataclass(slots=True)
class GatewayRuntime:
    gateway: GatewayAssignmentRunner
    database: Database
    catalog: KnowledgeHttpAdapter


def build_gateway(settings: Settings) -> GatewayRuntime:
    database = Database(settings.database_url)
    catalog = KnowledgeHttpAdapter(
        base_url=settings.knowledge_base_url,
        control_plane_token=settings.knowledge_control_plane_token,
        metadata_timeout_seconds=settings.knowledge_metadata_timeout_seconds,
        materialization_timeout_seconds=settings.knowledge_materialization_timeout_seconds,
    )
    try:
        workspace_manager = GitWorktreeWorkspaceManager(
            settings.workspace_root,
            retention_seconds=settings.workspace_retention_seconds,
            git_executable=settings.git_executable,
        )
        adapter_cache: dict[UUID, WorkerAdapter] = {}

        def get_adapter(
            instance: ModuleInstance,
            factory: Any,
        ) -> WorkerAdapter:
            adapter = adapter_cache.get(instance.id)
            if adapter is None:
                adapter = factory(instance)
                adapter_cache[instance.id] = adapter
            return adapter

        def fake_factory(instance: ModuleInstance) -> WorkerAdapter:
            del instance
            return FakeWorkerAdapter()

        def opencode_factory(instance: ModuleInstance) -> WorkerAdapter:
            return OpenCodeACPAdapter(
                executable=_module_config_text(
                    instance, "executable", settings.opencode_executable
                ),
                model=_module_config_optional_text(instance, "model"),
                workspace_root=settings.workspace_root,
                request_timeout_seconds=_module_config_float(
                    instance,
                    "request_timeout_seconds",
                    settings.opencode_request_timeout_seconds,
                ),
                stop_grace_seconds=_module_config_float(
                    instance,
                    "stop_grace_seconds",
                    settings.opencode_stop_grace_seconds,
                ),
                git_executable=settings.git_executable,
            )

        adapter_factories = {
            "fake": lambda instance: get_adapter(instance, fake_factory),
            "opencode": lambda instance: get_adapter(instance, opencode_factory),
            "opencode-acp": lambda instance: get_adapter(instance, opencode_factory),
        }
        workspace_factories = {
            "git-worktree": lambda _instance: workspace_manager,
            "git_worktree": lambda _instance: workspace_manager,
        }
        worker_gateway = WorkerGateway(
            lambda: SqlAlchemyUnitOfWork(database),
            adapter_factories=adapter_factories,
            workspace_factories=workspace_factories,
            spec_factory=ConfiguredExecutionSpecFactory(),
            artifact_content_store=LocalArtifactContentStore(
                settings.artifact_root,
                max_bytes=settings.artifact_max_bytes,
            ),
            context_materializer=ContextMaterializer(catalog),
            context_package_cutover_at=settings.context_package_cutover_at,
            candidate_submission_service=KnowledgeCandidateSubmissionService(
                FileCandidateProposalSpool(
                    settings.knowledge_candidate_spool_dir,
                    quota_bytes=settings.knowledge_candidate_spool_quota_bytes,
                )
            ),
            clock=lambda: datetime.now(UTC),
        )
        runner = GatewayAssignmentRunner(
            SqlAlchemyOutboxStore(
                database, claim_seconds=settings.assignment_lease_seconds
            ),
            worker_gateway,
            clock=lambda: datetime.now(UTC),
        )
    except BaseException:
        catalog.close()
        database.dispose()
        raise
    return GatewayRuntime(gateway=runner, database=database, catalog=catalog)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run the SIDERA Worker Gateway")
    parser.add_argument(
        "--once",
        action="store_true",
        help="run one Gateway assignment batch and exit",
    )
    args = parser.parse_args(argv)
    runtime: GatewayRuntime | object | None = None
    try:
        settings = Settings()  # pyright: ignore[reportCallIssue]
        runtime = build_gateway(settings)
        gateway = _service(runtime, "gateway")
        while True:
            claimed, processed, failed = gateway.run_once(
                settings.gateway_batch_size
            )
            _print_summary(claimed, processed, failed)
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


def _module_config_text(
    instance: ModuleInstance, name: str, default: str
) -> str:
    for key in (f"{instance.config_namespace}.{name}", name):
        value = instance.config.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return default


def _module_config_optional_text(
    instance: ModuleInstance, name: str
) -> str | None:
    for key in (f"{instance.config_namespace}.{name}", name):
        value = instance.config.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def _module_config_float(
    instance: ModuleInstance, name: str, default: float
) -> float:
    for key in (f"{instance.config_namespace}.{name}", name):
        value = instance.config.get(key)
        if (
            isinstance(value, (int, float))
            and not isinstance(value, bool)
            and value > 0
        ):
            return float(value)
    return default


def _service(runtime: object, name: str) -> Any:
    service = getattr(runtime, name, None)
    if service is None:
        service = runtime
    return service


def _dispose_runtime(runtime: object | None) -> None:
    if runtime is None:
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
