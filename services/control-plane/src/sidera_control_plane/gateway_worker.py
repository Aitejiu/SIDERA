import argparse
import json
import sys
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from hashlib import sha256
from pathlib import Path
from typing import Any, cast
from uuid import UUID

from sidera_control_plane.adapters.fake_worker import FakeWorkerAdapter
from sidera_control_plane.adapters.git_workspace import GitWorktreeWorkspaceManager
from sidera_control_plane.adapters.knowledge_http import KnowledgeHttpAdapter
from sidera_control_plane.adapters.local_verification import (
    LocalCheckRunner,
    LocalVerificationAdapter,
)
from sidera_control_plane.adapters.opencode_acp import OpenCodeACPAdapter
from sidera_control_plane.application.artifact_content import ArtifactContentService
from sidera_control_plane.application.context_materialization import ContextMaterializer
from sidera_control_plane.application.knowledge_candidate_submission import (
    FileCandidateProposalSpool,
    KnowledgeCandidateSubmissionService,
)
from sidera_control_plane.application.ports import WorkerAdapter
from sidera_control_plane.application.result_ports import LocalCheckRunnerPort
from sidera_control_plane.application.worker_gateway import (
    ConfiguredExecutionSpecFactory,
    GatewayAssignmentRunner,
    WorkerGateway,
)
from sidera_control_plane.config import Settings
from sidera_control_plane.domain.artifacts import ArtifactCandidate, ArtifactType
from sidera_control_plane.domain.modules import (
    LOCAL_VERIFICATION_PROVIDER,
    ModuleInstance,
)
from sidera_control_plane.domain.result_integration import CheckSpecV1
from sidera_control_plane.domain.workspace import ExecutionWorkspaceManifest
from sidera_control_plane.persistence.artifact_content_store import (
    LocalArtifactContentStore,
)
from sidera_control_plane.persistence.database import Database
from sidera_control_plane.persistence.outbox_store import SqlAlchemyOutboxStore
from sidera_control_plane.persistence.unit_of_work import SqlAlchemyUnitOfWork


def _deterministic_artifact_storage_ref(instance_name: str) -> str:
    """Keep the R5.3 fixture artifact identity stable within one gate run."""

    clean_name = instance_name.strip()
    if not clean_name:
        raise ValueError("deterministic artifact instance name is required")
    return f"artifact://r5-3-deterministic/{clean_name}/diff"


@dataclass(slots=True)
class GatewayRuntime:
    gateway: GatewayAssignmentRunner
    worker_gateway: WorkerGateway
    database: Database
    catalog: KnowledgeHttpAdapter


def build_worker_adapter_factories(
    settings: Settings,
    *,
    workspace_manager: GitWorktreeWorkspaceManager,
    check_runner: LocalCheckRunnerPort | None = None,
    adapter_cache: dict[UUID, WorkerAdapter] | None = None,
) -> dict[str, Callable[[ModuleInstance], WorkerAdapter]]:
    """Build provider factories while keeping verification off the LLM path."""

    cache = adapter_cache if adapter_cache is not None else {}
    local_runner = check_runner or LocalCheckRunner(
        state_root=Path(settings.workspace_root).expanduser().resolve().parent
        / "verification-state"
    )

    def get_adapter(
        instance: ModuleInstance,
        factory: Callable[[ModuleInstance], WorkerAdapter],
    ) -> WorkerAdapter:
        adapter = cache.get(instance.id)
        if adapter is None:
            adapter = factory(instance)
            cache[instance.id] = adapter
        return adapter

    def fake_factory(instance: ModuleInstance) -> WorkerAdapter:
        if _module_config_text(instance, "mode", "") != "r5-3-deterministic":
            return FakeWorkerAdapter()
        content = b"SIDERA R5.3 deterministic worker result\n"
        criterion = _module_config_text(
            instance,
            "artifact_acceptance_criterion_ref",
            "acceptance:implementation",
        )
        artifact = ArtifactCandidate(
            artifact_type=ArtifactType.DIFF,
            storage_ref=_deterministic_artifact_storage_ref(instance.instance_name),
            content_hash=sha256(content).hexdigest(),
            size_bytes=len(content),
            media_type="text/plain",
            provenance={
                "producer": "r5-3-deterministic-worker",
                "acceptance_criterion_ref": criterion,
                "conclusion": "The deterministic worker produced its result.",
                "environment_ref": "env://r5-3-deterministic",
            },
        )
        return FakeWorkerAdapter(artifacts=(artifact,))

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
            handoff_request_timeout_seconds=_module_config_float(
                instance,
                "handoff_request_timeout_seconds",
                settings.opencode_handoff_request_timeout_seconds,
            ),
            stop_grace_seconds=_module_config_float(
                instance,
                "stop_grace_seconds",
                settings.opencode_stop_grace_seconds,
            ),
            git_executable=settings.git_executable,
        )

    def local_verification_factory(instance: ModuleInstance) -> WorkerAdapter:
        configured_locator = getattr(workspace_manager, "physical_location", None)
        workspace_locator: Callable[[ExecutionWorkspaceManifest], Path]
        if callable(configured_locator):
            workspace_locator = cast(
                Callable[[ExecutionWorkspaceManifest], Path], configured_locator
            )
        else:
            workspace_locator = lambda manifest: _workspace_location(
                settings.workspace_root, manifest
            )
        return LocalVerificationAdapter(
            runner=local_runner,
            check_specs=_module_config_check_specs(instance),
            workspace_locator=workspace_locator,
            adapter_module_id=instance.id,
            adapter_config_version=instance.config_version,
        )

    return {
        "fake": lambda instance: get_adapter(instance, fake_factory),
        "opencode": lambda instance: get_adapter(instance, opencode_factory),
        "opencode-acp": lambda instance: get_adapter(instance, opencode_factory),
        LOCAL_VERIFICATION_PROVIDER: lambda instance: get_adapter(
            instance, local_verification_factory
        ),
    }


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
        worker_gateway = WorkerGateway(
            lambda: SqlAlchemyUnitOfWork(database),
            adapter_factories=adapter_factories,
            workspace_factories=workspace_factories,
            spec_factory=ConfiguredExecutionSpecFactory(),
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
            assignment_lease_ttl_seconds=settings.assignment_lease_seconds,
            assignment_lease_renew_interval_seconds=(
                settings.assignment_lease_renew_interval_seconds
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


def _module_config_value(instance: ModuleInstance, name: str) -> object | None:
    for key in (f"{instance.config_namespace}.{name}", name):
        if key in instance.config:
            return instance.config[key]
    return None


def _module_config_check_specs(
    instance: ModuleInstance,
) -> dict[str, CheckSpecV1]:
    raw = _module_config_value(instance, "check_specs")
    if raw is None:
        return {}
    if isinstance(raw, Mapping):
        if "key" in raw:
            values: tuple[object, ...] = (raw,)
        else:
            values = tuple(raw.values())
    elif isinstance(raw, Sequence) and not isinstance(raw, (str, bytes, bytearray)):
        values = tuple(raw)
    else:
        raise TypeError("verification check_specs must be an object or array")
    specs: dict[str, CheckSpecV1] = {}
    for value in values:
        check = CheckSpecV1.from_mapping(value)
        if check.key in specs:
            raise ValueError(f"duplicate verification check key: {check.key}")
        specs[check.key] = check
    return specs


def _workspace_location(
    workspace_root: Path,
    manifest: object,
) -> Path:
    execution_id = getattr(manifest, "execution_id", None)
    if not isinstance(execution_id, UUID):
        raise TypeError("verification workspace manifest execution is invalid")
    root = Path(workspace_root).expanduser().resolve()
    candidate = root / str(execution_id)
    if candidate.is_symlink():
        raise ValueError("verification workspace is unavailable")
    location = candidate.resolve()
    if root not in location.parents or not location.is_dir():
        raise ValueError("verification workspace is unavailable")
    return location


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
