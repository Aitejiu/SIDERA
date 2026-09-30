import argparse
import json
import os
import sys
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from hashlib import sha256
from pathlib import Path
from typing import Any, Protocol, cast
from uuid import UUID

from sidera_control_plane.acceptance_guard import load_acceptance_guard
from sidera_control_plane.adapters.demo_worker import DemoWorkerAdapter
from sidera_control_plane.adapters.dsh_acp import DshACPAdapter
from sidera_control_plane.adapters.fake_worker import FakeWorkerAdapter
from sidera_control_plane.adapters.git_results import GitResultStore
from sidera_control_plane.adapters.git_workspace import (
    GitWorktreeWorkspaceManager,
    opaque_repository_ref,
)
from sidera_control_plane.adapters.knowledge_http import KnowledgeHttpAdapter
from sidera_control_plane.adapters.local_verification import (
    LocalCheckRunner,
    LocalVerificationAdapter,
)
from sidera_control_plane.adapters.opencode_acp import OpenCodeACPAdapter
from sidera_control_plane.application.artifact_content import ArtifactContentService
from sidera_control_plane.application.code_result_capture import (
    CodeResultCaptureService,
)
from sidera_control_plane.application.context_materialization import (
    ContextMaterializer,
)
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
from sidera_control_plane.demo.faults import consume_fault_once
from sidera_control_plane.demo.manifest import load_manifest, validate_manifest
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


@dataclass(frozen=True, slots=True)
class WorkerAdapterRegistration:
    """Production registration facts shared by the factory map and gates."""

    adapter_type: str
    factory_key: str
    implementation: str
    capability_versions: tuple[str, ...]
    is_fake: bool
    delegates_to_opencode: bool
    is_local_verification: bool
    eligibility_note: str


_WORKER_ADAPTER_REGISTRATIONS = (
    WorkerAdapterRegistration(
        adapter_type="fake",
        factory_key="fake",
        implementation="FakeWorkerAdapter",
        capability_versions=("context-package:v1", "handoff:v1"),
        is_fake=True,
        delegates_to_opencode=False,
        is_local_verification=False,
        eligibility_note="deterministic test boundary; excluded from the real gate",
    ),
    WorkerAdapterRegistration(
        adapter_type="opencode",
        factory_key="opencode",
        implementation="OpenCodeACPAdapter",
        capability_versions=(
            "context-package:v1/v2/v3",
            "handoff:v1",
            "turn-completion:v1",
        ),
        is_fake=False,
        delegates_to_opencode=True,
        is_local_verification=False,
        eligibility_note="same provider as source; excluded from the distinct gate",
    ),
    WorkerAdapterRegistration(
        adapter_type="opencode-acp",
        factory_key="opencode",
        implementation="OpenCodeACPAdapter",
        capability_versions=(
            "context-package:v1/v2/v3",
            "handoff:v1",
            "turn-completion:v1",
        ),
        is_fake=False,
        delegates_to_opencode=True,
        is_local_verification=False,
        eligibility_note="alias of opencode; excluded from the distinct gate",
    ),
    WorkerAdapterRegistration(
        adapter_type=LOCAL_VERIFICATION_PROVIDER,
        factory_key="local_verification",
        implementation="LocalVerificationAdapter",
        capability_versions=("context-package:v3", "verification:v1"),
        is_fake=False,
        delegates_to_opencode=False,
        is_local_verification=True,
        eligibility_note="local deterministic verification; not an execution Agent",
    ),
    WorkerAdapterRegistration(
        adapter_type="interview-demo-worker",
        factory_key="demo",
        implementation="DemoWorkerAdapter",
        capability_versions=("context-package:v1/v2/v3", "durable-session:v1"),
        is_fake=True,
        delegates_to_opencode=False,
        is_local_verification=False,
        eligibility_note="isolated interview demo only; excluded from production evidence",
    ),
    WorkerAdapterRegistration(
        adapter_type="dsh-acp",
        factory_key="dsh_acp",
        implementation="DshACPAdapter",
        capability_versions=("context-package:v1/v2", "handoff-target:v1"),
        is_fake=False,
        delegates_to_opencode=False,
        is_local_verification=False,
        eligibility_note=(
            "independent ACP target; target-only until a distinct source checkpoint "
            "contract is proven"
        ),
    ),
)


def worker_adapter_inventory() -> tuple[WorkerAdapterRegistration, ...]:
    """Return immutable production registration facts without constructing adapters."""

    return _WORKER_ADAPTER_REGISTRATIONS


def registered_worker_adapter_types() -> tuple[str, ...]:
    """Return adapter type keys from the same source used by the factory map."""

    return tuple(item.adapter_type for item in _WORKER_ADAPTER_REGISTRATIONS)


def _resolve_active_session_sync_workspace(settings: Settings) -> UUID | None:
    """Resolve the workspace whose active sessions this gateway keeps alive."""

    if settings.interview_demo_manifest is not None:
        manifest = load_manifest(Path(settings.interview_demo_manifest))
        validate_manifest(manifest, Path(__file__).resolve().parents[4])
        return manifest.workspace_id
    return getattr(settings, "gateway_sync_workspace_id", None)


def build_worker_adapter_factories(
    settings: Settings,
    *,
    workspace_manager: GitWorktreeWorkspaceManager,
    check_runner: LocalCheckRunnerPort | None = None,
    adapter_cache: dict[tuple[UUID, int], WorkerAdapter] | None = None,
) -> dict[str, Callable[[ModuleInstance], WorkerAdapter]]:
    """Build provider factories while keeping verification off the LLM path."""

    cache = adapter_cache if adapter_cache is not None else {}
    local_runner = check_runner or LocalCheckRunner(
        state_root=Path(settings.workspace_root).expanduser().resolve().parent
        / "verification-state"
    )

    demo_manifest_path = getattr(settings, "interview_demo_manifest", None)
    demo_manifest = None
    if demo_manifest_path is not None:
        demo_manifest = load_manifest(Path(demo_manifest_path))
        validate_manifest(demo_manifest, Path(__file__).resolve().parents[4])

    def get_adapter(
        instance: ModuleInstance,
        factory: Callable[[ModuleInstance], WorkerAdapter],
    ) -> WorkerAdapter:
        cache_key = (instance.id, instance.config_version)
        adapter = cache.get(cache_key)
        if adapter is None:
            adapter = factory(instance)
            cache[cache_key] = adapter
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
            context_package_versions=_module_config_context_package_versions(
                instance
            ),
            adapter_module_id=instance.id,
            adapter_config_version=instance.config_version,
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
        controls_demo_manifest = (
            demo_manifest
            if demo_manifest is not None and demo_manifest.scenario == "controls"
            else None
        )
        return LocalVerificationAdapter(
            runner=local_runner,
            check_specs=_module_config_check_specs(instance),
            workspace_locator=workspace_locator,
            adapter_module_id=instance.id,
            adapter_config_version=instance.config_version,
            start_interruption=(
                (
                    lambda operation_id: consume_fault_once(
                        controls_demo_manifest,
                        "verification-fails-once",
                        operation_id,
                    )
                )
                if controls_demo_manifest is not None
                else None
            ),
        )

    def dsh_acp_factory(instance: ModuleInstance) -> WorkerAdapter:
        return DshACPAdapter(
            argv=_module_config_argv(
                instance,
                "argv",
                ("node", "integrations/dsh-worker/src/acp-host.ts"),
            ),
            process_env=_module_config_process_env(instance),
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
            context_package_versions=_module_config_context_package_versions(instance),
            handoff_target_enabled=True,
        )

    def demo_factory(instance: ModuleInstance) -> WorkerAdapter:
        if demo_manifest is None:
            raise ValueError("interview demo worker requires an explicit manifest")
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
        return DemoWorkerAdapter(demo_manifest, instance, workspace_locator)

    builders = {
        "fake": fake_factory,
        "opencode": opencode_factory,
        "local_verification": local_verification_factory,
        "dsh_acp": dsh_acp_factory,
        "demo": demo_factory,
    }
    return {
        registration.adapter_type: (
            lambda instance, builder=builders[registration.factory_key]: get_adapter(
                instance, builder
            )
        )
        for registration in worker_adapter_inventory()
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


def _module_config_context_package_versions(
    instance: ModuleInstance,
) -> tuple[int, ...]:
    raw = _module_config_value(instance, "context_package_versions")
    if raw is None:
        return (1, 2)
    if not isinstance(raw, Sequence) or isinstance(raw, (str, bytes, bytearray)):
        raise TypeError("opencode context_package_versions must be an array")
    versions = tuple(raw)
    if any(
        not isinstance(version, int) or isinstance(version, bool)
        for version in versions
    ):
        raise TypeError("opencode context_package_versions must contain integers")
    return versions


def _module_config_argv(
    instance: ModuleInstance,
    name: str,
    default: tuple[str, ...],
) -> tuple[str, ...]:
    raw = _module_config_value(instance, name)
    if raw is None:
        return default
    if not isinstance(raw, Sequence) or isinstance(raw, (str, bytes, bytearray)):
        raise TypeError("DSH argv must be an array of argument strings")
    values = tuple(raw)
    if not values or any(not isinstance(value, str) or not value for value in values):
        raise TypeError("DSH argv must contain non-empty strings")
    return values


def _module_config_process_env(instance: ModuleInstance) -> dict[str, str]:
    raw = _module_config_value(instance, "process_env")
    if raw is None:
        # PATH is an execution prerequisite, not a provider credential. All
        # other environment entries must be explicit module configuration.
        return {"PATH": os.environ.get("PATH", "")}
    if not isinstance(raw, Mapping):
        raise TypeError("DSH process_env must be an object")
    values = {str(key): value for key, value in raw.items()}
    if any(not isinstance(value, str) for value in values.values()):
        raise TypeError("DSH process_env values must be strings")
    return values


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
