import os
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path
from typing import cast
from uuid import UUID

from sidera_control_plane.adapters.demo_worker import DemoWorkerAdapter
from sidera_control_plane.adapters.dsh_acp import DshACPAdapter
from sidera_control_plane.adapters.fake_worker import FakeWorkerAdapter
from sidera_control_plane.adapters.git_workspace import GitWorktreeWorkspaceManager
from sidera_control_plane.adapters.local_verification import (
    LocalCheckRunner,
    LocalVerificationAdapter,
)
from sidera_control_plane.adapters.opencode_acp import OpenCodeACPAdapter
from sidera_control_plane.application.ports import WorkerAdapter
from sidera_control_plane.application.result_ports import LocalCheckRunnerPort
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


def _deterministic_artifact_storage_ref(instance_name: str) -> str:
    """Keep the R5.3 fixture artifact identity stable within one gate run."""

    clean_name = instance_name.strip()
    if not clean_name:
        raise ValueError("deterministic artifact instance name is required")
    return f"artifact://r5-3-deterministic/{clean_name}/diff"


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
        validate_manifest(demo_manifest, Path(__file__).resolve().parents[5])

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
