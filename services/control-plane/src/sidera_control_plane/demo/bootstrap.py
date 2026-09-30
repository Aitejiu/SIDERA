from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal
from uuid import UUID

from sidera_control_plane.application.command_service import hash_request
from sidera_control_plane.application.notification_commands import (
    NotificationCommandHandler,
)
from sidera_control_plane.application.runtime_commands import RuntimeCommandHandler
from sidera_control_plane.demo.results import closure_check_spec, demo_check_spec
from sidera_control_plane.domain.collaboration import (
    CollaborationRuntimeBindingSnapshot,
)
from sidera_control_plane.domain.commands import CommandEnvelope, CommandResult
from sidera_control_plane.domain.events import EventData
from sidera_control_plane.domain.handoffs import HandoffTransferMode
from sidera_control_plane.domain.modules import (
    COLLABORATION_INPUT_GROUPS_STRATEGY_ID,
    COLLABORATION_PROJECTION_SCHEMA_V2,
    COLLABORATION_RUNTIME_MODULE_TYPE,
    NOTIFICATION_CHANNEL_MODULE_TYPE,
    VERIFICATION_CONTEXT_PACKAGE_VERSION,
    WORKER_ADAPTER_MODULE_TYPE,
    WORKSPACE_PROVIDER_MODULE_TYPE,
    CancellationCapability,
    CapabilityInput,
    CollaborationRuntimeCapabilityProfile,
    HandoffCheckpointMode,
    IdempotencyCapability,
    ModuleDefinition,
    ModuleInstance,
    ModuleLifecycle,
    NotificationChannelCapabilityProfile,
    NotificationReceiptCapability,
    PlannerAdapterCapabilityProfile,
    ResumeCapability,
    SandboxCapability,
    StabilityLevel,
    StopCapability,
    StructuredOutputCapability,
    UsageReportingCapability,
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
from sidera_control_plane.persistence.database import Database
from sidera_control_plane.persistence.models import WorkspaceModel
from sidera_control_plane.persistence.unit_of_work import SqlAlchemyUnitOfWork

from .contracts import (
    DEMO_NOTIFICATION_SOURCE_REF,
    WORKER_ITEM_CAPABILITIES,
    DemoManifest,
)
from .manifest import stable_id, validate_manifest

BootstrapStatus = Literal["checked", "created", "idempotent"]
_PROJECT_ROOT = Path(__file__).resolve().parents[5]


class DemoBootstrapConflict(RuntimeError):
    """The requested deployment differs from its persisted demo identity."""


@dataclass(frozen=True, slots=True)
class BootstrapResult:
    status: BootstrapStatus
    workspace_id: UUID
    module_ids: Mapping[str, UUID]
    profile_ids: Mapping[str, UUID]
    worker_ids: Mapping[str, UUID]
    runtime_versions: Mapping[str, int]
    repository_head: str
    state_path: Path


@dataclass(frozen=True, slots=True)
class _Resources:
    definitions: Mapping[str, ModuleDefinition]
    instances: Mapping[str, ModuleInstance]
    profile: PlanningProfile


def bootstrap_demo(
    manifest: DemoManifest, *, database: Database, check_only: bool
) -> BootstrapResult:
    validate_manifest(manifest, _PROJECT_ROOT)
    manifest.run_root.mkdir(parents=True, exist_ok=True)
    state_path = manifest.run_root / "bootstrap.json"
    fingerprint = _config_fingerprint(manifest)
    state = _read_state(state_path)
    if state is not None:
        if state.get("run_id") != str(manifest.run_id):
            raise DemoBootstrapConflict("bootstrap run identity conflict")
        if state.get("workspace_id") != str(manifest.workspace_id):
            raise DemoBootstrapConflict("bootstrap workspace identity conflict")
        if state.get("config_fingerprint") != fingerprint:
            raise DemoBootstrapConflict("bootstrap config fingerprint conflict")

    resources = _resources(manifest)
    if check_only:
        with database.session() as session:
            workspace = session.get(WorkspaceModel, manifest.workspace_id)
            if workspace is None or workspace.status != "active":
                raise DemoBootstrapConflict("demo workspace is not active")
        return _result(manifest, resources, "checked", state_path)

    with SqlAlchemyUnitOfWork(database) as uow:
        _ensure_workspace(uow, manifest.workspace_id)
        for definition in resources.definitions.values():
            _ensure_definition(uow, definition)
        for instance in resources.instances.values():
            _ensure_instance(uow, instance)
        _ensure_profile(uow, resources.profile)
        uow.flush()
        _ensure_notification_route(uow, manifest)
        for worker_key in manifest.worker_ids:
            _ensure_worker(uow, manifest, worker_key)
        uow.flush()
        runtime_versions = {
            worker_key: _ensure_runtime(uow, manifest, worker_key)
            for worker_key in manifest.worker_ids
        }
        uow.flush()

    result = _result(
        manifest,
        resources,
        "idempotent" if state is not None else "created",
        state_path,
        runtime_versions=runtime_versions,
    )
    if state is None:
        state_path.write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "run_id": str(manifest.run_id),
                    "workspace_id": str(manifest.workspace_id),
                    "config_fingerprint": fingerprint,
                    "module_ids": {
                        key: str(value) for key, value in manifest.module_ids.items()
                    },
                    "worker_ids": {
                        key: str(value) for key, value in manifest.worker_ids.items()
                    },
                    "runtime_versions": runtime_versions,
                },
                sort_keys=True,
                separators=(",", ":"),
            ),
            encoding="utf-8",
        )
    return result


def _result(
    manifest: DemoManifest,
    resources: _Resources,
    status: BootstrapStatus,
    state_path: Path,
    *,
    runtime_versions: Mapping[str, int] | None = None,
) -> BootstrapResult:
    return BootstrapResult(
        status=status,
        workspace_id=manifest.workspace_id,
        module_ids=manifest.module_ids,
        profile_ids={"planning": resources.profile.id},
        worker_ids=manifest.worker_ids,
        runtime_versions=runtime_versions or {key: 1 for key in manifest.worker_ids},
        repository_head=manifest.base_commit,
        state_path=state_path,
    )


def _resources(manifest: DemoManifest) -> _Resources:
    definitions: dict[str, ModuleDefinition] = {}
    instances: dict[str, ModuleInstance] = {}
    capabilities: dict[str, object] = {
        "planner": PlannerAdapterCapabilityProfile(
            structured_output=StructuredOutputCapability.JSON_SCHEMA,
            clarification=True,
            idempotency=IdempotencyCapability.OPERATION_ID,
            cancellation=CancellationCapability.RUN,
            usage_reporting=UsageReportingCapability.NONE,
            max_context_bytes=128_000,
            stability=StabilityLevel.EXPERIMENTAL,
            interface_version="sidera.planner.v1",
            proposal_schema_versions=(6,),
            context_package_versions=(1, 2, 3),
        ),
        "worker_primary": _worker_capabilities(),
        "worker_secondary": _worker_capabilities(),
        "workspace": WorkspaceProviderCapabilityProfile(
            isolation=SandboxCapability.ISOLATED_RUNTIME,
            writable=True,
            git_worktree=True,
            stability=StabilityLevel.STABLE,
            workspace_handoff_versions=(1,),
            workspace_transfer_modes=(HandoffTransferMode.REUSE_EXACT,),
            handoff_inspection=True,
            code_input_versions=(1,),
        ),
        "collaboration": CollaborationRuntimeCapabilityProfile(
            interface_version="sidera.collaboration.v1",
            projection_schema_versions=(COLLABORATION_PROJECTION_SCHEMA_V2,),
            decision_schema_versions=(2,),
            strategy_ids=("all_required", COLLABORATION_INPUT_GROUPS_STRATEGY_ID),
            idempotency=IdempotencyCapability.OPERATION_ID,
            max_projection_bytes=1_048_576,
            max_decision_bytes=262_144,
            max_intents_per_decision=1_000,
            max_strategy_state_bytes=65_536,
            stability=StabilityLevel.STABLE,
        ),
        "verification": _verification_capabilities(),
        "notification": NotificationChannelCapabilityProfile(
            channel_types=("local",),
            delivery_contract_versions=(1,),
            idempotency=IdempotencyCapability.OPERATION_ID,
            receipt=NotificationReceiptCapability.PROVIDER_REF,
            max_message_bytes=65_536,
            supports_deep_link=True,
            stability=StabilityLevel.STABLE,
        ),
    }
    definition_specs = {
        "planner": ("planner_adapter", "interview-demo-planner"),
        "worker_primary": (WORKER_ADAPTER_MODULE_TYPE, "interview-demo-worker"),
        "worker_secondary": (WORKER_ADAPTER_MODULE_TYPE, "interview-demo-worker"),
        "workspace": (WORKSPACE_PROVIDER_MODULE_TYPE, "git-worktree"),
        "collaboration": (COLLABORATION_RUNTIME_MODULE_TYPE, "sidera-collaboration"),
        "verification": (WORKER_ADAPTER_MODULE_TYPE, "local_verification"),
        "notification": (NOTIFICATION_CHANNEL_MODULE_TYPE, "local-notification"),
    }
    instance_specs = {
        "planner": (
            "planning",
            {"planning.executable": "demo", "planning.model": "deterministic"},
        ),
        "worker_primary": (
            "worker",
            {"worker.executable": "demo", "worker.model": "deterministic"},
        ),
        "worker_secondary": (
            "worker",
            {"worker.executable": "demo", "worker.model": "deterministic"},
        ),
        "workspace": (
            "workspace",
            {
                "workspace.repository_ref": str(manifest.repository),
                "workspace.base_revision": manifest.base_commit,
            },
        ),
        "collaboration": (
            "collaboration",
            {"collaboration.strategy_id": COLLABORATION_INPUT_GROUPS_STRATEGY_ID},
        ),
        "verification": (
            "verification",
            {
                "verification.check_specs": [
                    demo_check_spec().to_mapping(),
                    closure_check_spec().to_mapping(),
                ]
            },
        ),
        "notification": (
            "notification",
            {"notification.route_ref": DEMO_NOTIFICATION_SOURCE_REF},
        ),
    }
    for key, (module_type, provider) in definition_specs.items():
        definition_id = stable_id(manifest.run_id, f"module-definition:{key}")
        definitions[key] = ModuleDefinition(
            id=definition_id,
            workspace_id=manifest.workspace_id,
            module_type=module_type,
            provider=provider,
            interface_version={
                "planner": "sidera.planner.v1",
                "collaboration": "sidera.collaboration.v1",
            }.get(key, f"sidera.{module_type}.v1"),
            config_schema_version=1,
            capabilities=capabilities[key],  # type: ignore[arg-type]
            lifecycle=ModuleLifecycle.ACTIVE,
        )
        namespace, config = instance_specs[key]
        instances[key] = ModuleInstance(
            id=manifest.module_ids[key],
            workspace_id=manifest.workspace_id,
            definition_id=definition_id,
            instance_name=f"interview-demo-{key}",
            config_version=1,
            config_namespace=namespace,
            config=config,
            secret_refs=(),
            lifecycle=ModuleLifecycle.ACTIVE,
        )
    profile = PlanningProfile(
        id=manifest.planning_profile_id,
        workspace_id=manifest.workspace_id,
        profile_key=f"interview-demo-{manifest.run_id.hex[:12]}",
        version=manifest.planning_profile_version,
        display_name="SIDERA Interview Demo Planner",
        planner_module_instance_id=manifest.module_ids["planner"],
        planner_module_config_version=1,
        planner_interface_version="sidera.planner.v1",
        prompt_policy_version="interview-demo-v1",
        system_policy={},
        proposal_schema_version=6,
        allowed_capabilities=(
            "code",
            "python",
            *sorted(WORKER_ITEM_CAPABILITIES.values()),
        ),
        adapter_preferences=("interview-demo-planner",),
        adapter_forbidden=(),
        budget=PlanningBudgetPolicy(
            max_context_bytes=128_000,
            max_tokens=100_000,
            max_cost_microunits=1,
            max_runtime_seconds=manifest.execution_deadline_seconds,
            max_revisions=20,
            max_nodes=500,
            max_concurrency=2,
        ),
        retry_policy=PlanningRetryPolicy(
            max_retries=1,
            backoff_seconds=(1,),
            unknown_outcome_policy=UnknownOutcomePolicy.BLOCK,
            invalid_output_policy=InvalidOutputPolicy.RETRY,
        ),
        default_priority=50,
        execution_max_attempts=2,
        execution_budget_level="demo",
        user_selectable=True,
        status=PlanningProfileStatus.ACTIVE,
        planner_secret_refs=(),
        collaboration_runtime_binding=CollaborationRuntimeBindingSnapshot(
            runtime_definition_id=stable_id(
                manifest.run_id, "module-definition:collaboration"
            ),
            runtime_instance_id=manifest.module_ids["collaboration"],
            runtime_interface_version="sidera.collaboration.v1",
            runtime_config_version=1,
            runtime_capability_snapshot=capabilities["collaboration"],  # type: ignore[arg-type]
        ),
    )
    return _Resources(definitions, instances, profile)


def _worker_capabilities() -> WorkerAdapterCapabilityProfile:
    return WorkerAdapterCapabilityProfile(
        input=CapabilityInput.QUEUED,
        idempotency=IdempotencyCapability.OPERATION_ID,
        resume=ResumeCapability.DURABLE,
        stop=StopCapability.EXACT_EXECUTION,
        sandbox=SandboxCapability.FILESYSTEM,
        stability=StabilityLevel.EXPERIMENTAL,
        context_package_versions=(1, 2, 3),
        handoff_checkpoint_mode=HandoffCheckpointMode.CONTROL_EVENT,
        handoff_checkpoint_versions=(1,),
        handoff_session_context_versions=(1,),
        handoff_writer_gate_versions=(1,),
        handoff_inspection=True,
        code_result_versions=(1,),
    )


def _verification_capabilities() -> WorkerAdapterCapabilityProfile:
    return WorkerAdapterCapabilityProfile(
        input=CapabilityInput.UNSUPPORTED,
        idempotency=IdempotencyCapability.OPERATION_ID,
        resume=ResumeCapability.DURABLE,
        stop=StopCapability.EXACT_EXECUTION,
        sandbox=SandboxCapability.FILESYSTEM,
        stability=StabilityLevel.STABLE,
        context_package_versions=(VERIFICATION_CONTEXT_PACKAGE_VERSION,),
        verification_versions=(1,),
    )


def _ensure_workspace(uow: SqlAlchemyUnitOfWork, workspace_id: UUID) -> None:
    existing = uow.session.get(WorkspaceModel, workspace_id)
    if existing is None:
        uow.session.add(
            WorkspaceModel(
                id=workspace_id,
                name="SIDERA Interview Demo Workspace",
                status="active",
                version=1,
            )
        )
    elif existing.status != "active":
        raise DemoBootstrapConflict("demo workspace is not active")


def _ensure_definition(uow: SqlAlchemyUnitOfWork, desired: ModuleDefinition) -> None:
    existing = uow.modules.get_definition(desired.id, desired.workspace_id)
    if existing is None:
        uow.modules.add_definition(desired)
    elif existing != desired:
        raise DemoBootstrapConflict(f"module definition conflict: {desired.id}")


def _ensure_instance(uow: SqlAlchemyUnitOfWork, desired: ModuleInstance) -> None:
    existing = uow.modules.get_instance(desired.id, desired.workspace_id)
    if existing is None:
        uow.modules.add_instance(desired)
    elif (
        existing != desired
        or dict(existing.config) != dict(desired.config)
        or existing.secret_refs != desired.secret_refs
    ):
        raise DemoBootstrapConflict(f"module instance conflict: {desired.id}")


def _ensure_profile(uow: SqlAlchemyUnitOfWork, desired: PlanningProfile) -> None:
    existing = uow.planning_profiles.get(
        desired.id, desired.workspace_id, version=desired.version
    )
    if existing is None:
        uow.planning_profiles.add(desired)
    elif existing != desired:
        raise DemoBootstrapConflict("planning profile conflict")


def _ensure_notification_route(
    uow: SqlAlchemyUnitOfWork, manifest: DemoManifest
) -> None:
    envelope = CommandEnvelope(
        id=stable_id(manifest.run_id, "command:register-notification-route"),
        workspace_id=manifest.workspace_id,
        command_type="RegisterNotificationRoute",
        target_type="notification_route",
        actor_type="local",
        actor_id="aitejiu",
        target_id=None,
        payload={
            "recipient_type": "workspace_operator",
            "recipient_id": "local",
            "channel_type": "local",
            "channel_module_instance_id": str(manifest.module_ids["notification"]),
            "source_ref": DEMO_NOTIFICATION_SOURCE_REF,
        },
        expected_version=None,
        idempotency_key=f"interview-demo:register-notification-route:{manifest.run_id}",
        correlation_id=manifest.run_id,
    )
    if uow.commands.find_by_id(manifest.workspace_id, envelope.id) is not None:
        return
    result = NotificationCommandHandler(
        local_actor_type="local", local_actor_id="aitejiu"
    ).handle(uow, envelope)
    uow.commands.record_applied(envelope, hash_request(envelope), result)


def _ensure_worker(
    uow: SqlAlchemyUnitOfWork, manifest: DemoManifest, worker_key: str
) -> None:
    worker_id = manifest.worker_ids[worker_key]
    existing = uow.workers.get(worker_id, manifest.workspace_id)
    if worker_key == "verification":
        capabilities = frozenset({"context_package:v3", "verification"})
        adapter_type = "local_verification"
    else:
        capabilities = frozenset(
            {
                "code",
                "python",
                "context_package:v3",
                "result_integration:v1",
                "result_inputs:v1",
                WORKER_ITEM_CAPABILITIES[worker_key],
                *(
                    (WORKER_ITEM_CAPABILITIES["validation"],)
                    if worker_key == "replacement"
                    else ()
                ),
            }
        )
        adapter_type = "interview-demo-worker"
    display_names = {
        "validation": "校验执行节点",
        "report": "报告执行节点",
        "review": "报告复核节点",
        "integration": "集成执行节点",
        "replacement": "演示备用执行节点",
        "verification": "本地验证节点",
    }
    desired_values = {
        "adapter_type": adapter_type,
        "display_name": display_names[worker_key],
        "capabilities": capabilities,
        "capacity": 1,
        "cost_per_hour_microunits": 0,
        "startup_latency_ms": 10,
    }
    if existing is not None:
        for key, value in desired_values.items():
            if getattr(existing, key) != value:
                raise DemoBootstrapConflict(f"worker conflict: {worker_key}")
        return
    worker = Worker.register(
        worker_id=worker_id,
        workspace_id=manifest.workspace_id,
        now=datetime.now(UTC),
        **desired_values,
    )
    event_id = stable_id(manifest.run_id, f"event:register-worker:{worker_key}")
    command_id = stable_id(manifest.run_id, f"command:register-worker:{worker_key}")
    event = EventData(
        id=event_id,
        workspace_id=manifest.workspace_id,
        aggregate_type="worker",
        aggregate_id=worker.id,
        aggregate_version=worker.version,
        event_type="WorkerRegistered",
        payload={
            "adapter_type": worker.adapter_type,
            "display_name": worker.display_name,
            "status": worker.status.value,
            "desired_state": worker.desired_state.value,
            "capabilities": sorted(worker.capabilities),
            "capacity": worker.capacity,
            "current_load": worker.current_load,
            "cost_per_hour_microunits": worker.cost_per_hour_microunits,
            "startup_latency_ms": worker.startup_latency_ms,
        },
        actor_type="bootstrap",
        actor_id="interview-demo-bootstrap",
        causation_id=command_id,
        correlation_id=manifest.run_id,
    )
    uow.workers.add(worker)
    command = CommandEnvelope(
        id=command_id,
        workspace_id=manifest.workspace_id,
        command_type="RegisterWorker",
        target_type="worker",
        actor_type="bootstrap",
        actor_id="interview-demo-bootstrap",
        target_id=worker.id,
        payload={
            "adapter_type": worker.adapter_type,
            "display_name": worker.display_name,
            "capabilities": sorted(worker.capabilities),
            "capacity": worker.capacity,
            "cost_per_hour_microunits": worker.cost_per_hour_microunits,
            "startup_latency_ms": worker.startup_latency_ms,
        },
        expected_version=None,
        idempotency_key=f"interview-demo:register-worker:{worker_key}:{manifest.run_id}",
        correlation_id=manifest.run_id,
    )
    uow.commands.record_applied(
        command,
        hash_request(command),
        CommandResult(
            command_id=command.id,
            aggregate_id=worker.id,
            aggregate_version=worker.version,
            event_ids=(event_id,),
        ),
    )
    uow.events.append_with_outbox(event)


def _ensure_runtime(
    uow: SqlAlchemyUnitOfWork, manifest: DemoManifest, worker_key: str
) -> int:
    worker_id = manifest.worker_ids[worker_key]
    existing = uow.worker_runtime_configurations.get_for_worker(
        worker_id, manifest.workspace_id
    )
    if worker_key == "verification":
        adapter_key = "verification"
    elif worker_key in {"validation", "report", "integration"}:
        adapter_key = "worker_primary"
    else:
        adapter_key = "worker_secondary"
    if existing is not None:
        if existing.runtime_identity != (
            manifest.module_ids[adapter_key],
            1,
            manifest.module_ids["workspace"],
            1,
        ):
            raise DemoBootstrapConflict(f"runtime configuration conflict: {worker_key}")
        return existing.version
    envelope = CommandEnvelope(
        id=stable_id(manifest.run_id, f"command:configure-runtime:{worker_key}"),
        workspace_id=manifest.workspace_id,
        command_type="ConfigureWorkerRuntime",
        target_type="worker",
        actor_type="bootstrap",
        actor_id="interview-demo-bootstrap",
        target_id=worker_id,
        payload={
            "adapter_module_instance_id": str(manifest.module_ids[adapter_key]),
            "adapter_module_config_version": 1,
            "workspace_module_instance_id": str(manifest.module_ids["workspace"]),
            "workspace_module_config_version": 1,
        },
        expected_version=None,
        idempotency_key=f"interview-demo:configure-runtime:{worker_key}:{manifest.run_id}",
        correlation_id=manifest.run_id,
    )
    result = RuntimeCommandHandler(lambda: datetime.now(UTC)).handle(uow, envelope)
    uow.commands.record_applied(envelope, hash_request(envelope), result)
    return result.aggregate_version


def _config_fingerprint(manifest: DemoManifest) -> str:
    payload = manifest.model_dump(mode="json")
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def _read_state(path: Path) -> dict[str, Any] | None:
    if not path.exists():
        return None
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise DemoBootstrapConflict("bootstrap state is unreadable") from exc
    if not isinstance(value, dict) or value.get("schema_version") != 1:
        raise DemoBootstrapConflict("bootstrap state schema is unsupported")
    return value
