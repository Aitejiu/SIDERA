"""Prepare an isolated, repeatable R6 basic-acceptance deployment."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import tempfile
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from math import isfinite
from pathlib import Path
from typing import Any, Literal
from uuid import UUID, uuid5

from sidera_control_plane.adapters.git_workspace import opaque_repository_ref
from sidera_control_plane.application.command_service import hash_request
from sidera_control_plane.application.runtime_commands import RuntimeCommandHandler
from sidera_control_plane.application.worker_commands import WorkerCommandHandler
from sidera_control_plane.basic_acceptance_manifest import (
    AgentProfile,
    BasicRunManifest,
    load_manifest,
    validate_manifest_isolation,
)
from sidera_control_plane.demo.results import (
    closure_check_spec,
    demo_check_spec,
    patch_integrity_check_spec,
    summary_json_check_spec,
)
from sidera_control_plane.domain.collaboration import (
    CollaborationRuntimeBindingSnapshot,
    CollaborationStrategy,
)
from sidera_control_plane.domain.commands import CommandEnvelope
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
from sidera_control_plane.persistence.database import Database
from sidera_control_plane.persistence.models import (
    ModuleDefinitionModel,
    ModuleInstanceModel,
    PlanningProfileModel,
    WorkspaceModel,
)
from sidera_control_plane.persistence.unit_of_work import SqlAlchemyUnitOfWork

BootstrapMode = Literal["check", "apply"]
BootstrapStatus = Literal["checked", "created", "idempotent"]
_ID_NAMESPACE = UUID("7e1c1f4a-d8db-5f17-89ac-7c4f73052b5b")
_SCHEMA_VERSION = 1


class BootstrapConflict(RuntimeError):
    """The requested manifest would mutate an existing acceptance deployment."""


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
    definitions: tuple[ModuleDefinition, ...]
    instances: tuple[ModuleInstance, ...]
    profile: PlanningProfile
    revision_profile: PlanningProfile
    module_ids: Mapping[str, UUID]
    worker_names: Mapping[str, str]


def bootstrap_manifest(
    manifest: BasicRunManifest,
    *,
    database_url: str | None = None,
    database: Database | None = None,
    mode: BootstrapMode,
    state_root: Path = Path(".sidera/basic-acceptance"),
    sidera_root: Path | None = None,
) -> BootstrapResult:
    """Validate or apply one manifest without creating Task/Execution rows."""

    if mode not in {"check", "apply"}:
        raise ValueError("bootstrap mode must be check or apply")
    validate_manifest_isolation(
        manifest,
        sidera_root=Path.cwd() if sidera_root is None else sidera_root,
    )
    state_path = state_root.expanduser().resolve() / f"{manifest.run_id}.json"
    state = _read_state(state_path)
    config_fingerprint = _config_fingerprint(manifest)
    _validate_state_identity(
        state,
        manifest=manifest,
        config_fingerprint=config_fingerprint,
        database_url=database_url,
    )
    initial_commit, repository_head = _prepare_repository(
        manifest,
        state=state,
        mode=mode,
    )
    resources = _resources(manifest)

    if database is None:
        resolved_url = database_url or _resolve_database_ref(manifest.database_ref)
        owned_database = Database(resolved_url)
    else:
        if database_url is not None:
            raise ValueError("database and database_url are mutually exclusive")
        owned_database = None
    active_database = database or owned_database
    assert active_database is not None
    try:
        result = _inspect_or_apply(
            active_database,
            manifest,
            resources,
            mode=mode,
            state=state,
            repository_head=repository_head,
            state_path=state_path,
        )
    finally:
        if owned_database is not None:
            owned_database.dispose()

    if mode == "apply" and state is None:
        _write_state(
            state_path,
            _state_payload(
                manifest=manifest,
                resources=resources,
                initial_commit=initial_commit,
                repository_head=repository_head,
                database_url=database_url,
                config_fingerprint=config_fingerprint,
                result=result,
            ),
        )
    return result


def _inspect_or_apply(
    database: Database,
    manifest: BasicRunManifest,
    resources: _Resources,
    *,
    mode: BootstrapMode,
    state: Mapping[str, Any] | None,
    repository_head: str,
    state_path: Path,
) -> BootstrapResult:
    if mode == "check":
        with database.session() as session:
            _check_workspace_identity(session, manifest.workspace_id)
        return BootstrapResult(
            status="checked",
            workspace_id=manifest.workspace_id,
            module_ids=resources.module_ids,
            profile_ids={
                "planning": resources.profile.id,
                "revision": resources.revision_profile.id,
            },
            worker_ids={},
            runtime_versions={},
            repository_head=repository_head,
            state_path=state_path,
        )

    status: Literal["created", "idempotent"] = "idempotent" if state else "created"
    now = datetime.now(UTC)
    with SqlAlchemyUnitOfWork(database) as uow:
        _ensure_workspace(uow, manifest.workspace_id)
        for definition in resources.definitions:
            _ensure_definition(uow, definition)
        for instance in resources.instances:
            _ensure_instance(uow, instance)
        _ensure_profile(uow, resources.profile)
        _ensure_profile(uow, resources.revision_profile)
        uow.flush()

        worker_ids: dict[str, UUID] = {}
        runtime_versions: dict[str, int] = {}
        roles = ("worker", "second_worker")
        if _result_plan_enabled():
            roles += ("verification",)
        for role in roles:
            profile = None if role == "verification" else _profile(manifest, role)
            worker_id = _ensure_worker(
                uow,
                manifest,
                role=role,
                profile=profile,
                now=now,
            )
            worker_ids[role] = worker_id
            runtime_versions[role] = _ensure_runtime(
                uow,
                manifest,
                role=role,
                worker_id=worker_id,
                adapter_instance_id=resources.module_ids[
                    "local_verification" if role == "verification" else role
                ],
                adapter_config_version=(
                    1 if role == "verification" else profile.config_version
                ),
                workspace_instance_id=resources.module_ids["workspace"],
            )
        uow.flush()

    return BootstrapResult(
        status=status,
        workspace_id=manifest.workspace_id,
        module_ids=resources.module_ids,
        profile_ids={
            "planning": resources.profile.id,
            "revision": resources.revision_profile.id,
        },
        worker_ids=worker_ids,
        runtime_versions=runtime_versions,
        repository_head=repository_head,
        state_path=state_path,
    )


def _resources(manifest: BasicRunManifest) -> _Resources:
    planner_profile = _profile(manifest, "planner")
    worker_profile = _profile(manifest, "worker")
    second_profile = _profile(manifest, "second_worker")
    result_plan_enabled = _result_plan_enabled()
    planner_schema_version = 6 if result_plan_enabled else 2
    planner_schema_versions = (planner_schema_version,)
    module_ids = {
        role: _stable_id(manifest.run_id, f"module-instance:{role}")
        for role in (
            "planner",
            "revision_planner",
            "worker",
            "second_worker",
            "workspace",
            "local_verification",
            "collaboration",
            "notification",
        )
    }
    definition_ids = {
        role: _stable_id(manifest.run_id, f"module-definition:{role}")
        for role in (
            "planner",
            "revision_planner",
            "worker",
            "second_worker",
            "workspace",
            "local_verification",
            "collaboration",
            "notification",
        )
    }
    worker_capability = _worker_capabilities()
    collaboration_capability = CollaborationRuntimeCapabilityProfile(
        interface_version="sidera.collaboration.v1",
        projection_schema_versions=(2,),
        decision_schema_versions=(2,),
        strategy_ids=(
            CollaborationStrategy.ALL_REQUIRED.value,
            COLLABORATION_INPUT_GROUPS_STRATEGY_ID,
        ),
        idempotency=IdempotencyCapability.OPERATION_ID,
        max_projection_bytes=1_048_576,
        max_decision_bytes=262_144,
        max_intents_per_decision=1_000,
        max_strategy_state_bytes=65_536,
        stability=StabilityLevel.STABLE,
    )
    definitions = (
        ModuleDefinition(
            id=definition_ids["planner"],
            workspace_id=manifest.workspace_id,
            module_type="planner_adapter",
            provider=planner_profile.provider,
            interface_version="sidera.planner.v1",
            config_schema_version=1,
            capabilities=_planner_capabilities(
                proposal_schema_versions=planner_schema_versions
            ),
            lifecycle=ModuleLifecycle.ACTIVE,
        ),
        ModuleDefinition(
            id=definition_ids["revision_planner"],
            workspace_id=manifest.workspace_id,
            module_type="planner_adapter",
            provider=planner_profile.provider,
            interface_version="sidera.planner.v1",
            config_schema_version=1,
            capabilities=_planner_capabilities(proposal_schema_versions=(2, 3)),
            lifecycle=ModuleLifecycle.ACTIVE,
        ),
        ModuleDefinition(
            id=definition_ids["worker"],
            workspace_id=manifest.workspace_id,
            module_type=WORKER_ADAPTER_MODULE_TYPE,
            provider=worker_profile.provider,
            interface_version="sidera.worker-adapter.v1",
            config_schema_version=1,
            capabilities=worker_capability,
            lifecycle=ModuleLifecycle.ACTIVE,
        ),
        ModuleDefinition(
            id=definition_ids["second_worker"],
            workspace_id=manifest.workspace_id,
            module_type=WORKER_ADAPTER_MODULE_TYPE,
            provider=second_profile.provider,
            interface_version="sidera.worker-adapter.v1",
            config_schema_version=1,
            capabilities=worker_capability,
            lifecycle=ModuleLifecycle.ACTIVE,
        ),
        ModuleDefinition(
            id=definition_ids["workspace"],
            workspace_id=manifest.workspace_id,
            module_type=WORKSPACE_PROVIDER_MODULE_TYPE,
            provider="git-worktree",
            interface_version="sidera.workspace-provider.v1",
            config_schema_version=1,
            capabilities=WorkspaceProviderCapabilityProfile(
                isolation=SandboxCapability.ISOLATED_RUNTIME,
                writable=True,
                git_worktree=True,
                stability=StabilityLevel.STABLE,
                code_input_versions=(1,),
            ),
            lifecycle=ModuleLifecycle.ACTIVE,
        ),
        ModuleDefinition(
            id=definition_ids["local_verification"],
            workspace_id=manifest.workspace_id,
            module_type=WORKER_ADAPTER_MODULE_TYPE,
            provider="local_verification",
            interface_version="sidera.worker-adapter.v1",
            config_schema_version=1,
            capabilities=WorkerAdapterCapabilityProfile(
                input=CapabilityInput.UNSUPPORTED,
                idempotency=IdempotencyCapability.OPERATION_ID,
                resume=ResumeCapability.DURABLE,
                stop=StopCapability.EXACT_EXECUTION,
                sandbox=SandboxCapability.FILESYSTEM,
                stability=StabilityLevel.STABLE,
                context_package_versions=(VERIFICATION_CONTEXT_PACKAGE_VERSION,),
                verification_versions=(1,),
            ),
            lifecycle=ModuleLifecycle.ACTIVE,
        ),
        ModuleDefinition(
            id=definition_ids["collaboration"],
            workspace_id=manifest.workspace_id,
            module_type=COLLABORATION_RUNTIME_MODULE_TYPE,
            provider="sidera-collaboration",
            interface_version="sidera.collaboration.v1",
            config_schema_version=1,
            capabilities=collaboration_capability,
            lifecycle=ModuleLifecycle.ACTIVE,
        ),
        ModuleDefinition(
            id=definition_ids["notification"],
            workspace_id=manifest.workspace_id,
            module_type=NOTIFICATION_CHANNEL_MODULE_TYPE,
            provider="local-notification",
            interface_version="sidera.notification.v1",
            config_schema_version=1,
            capabilities=NotificationChannelCapabilityProfile(
                channel_types=("local",),
                delivery_contract_versions=(1,),
                idempotency=IdempotencyCapability.OPERATION_ID,
                receipt=NotificationReceiptCapability.PROVIDER_REF,
                max_message_bytes=65_536,
                supports_deep_link=True,
                stability=StabilityLevel.STABLE,
            ),
            lifecycle=ModuleLifecycle.ACTIVE,
        ),
    )
    instances = (
        _instance(
            module_ids["planner"],
            definition_ids["planner"],
            "planning",
            "planning",
            planner_profile.config_version,
            {
                "planning.executable": "opencode",
                "planning.model": planner_profile.model_id,
                "planning.context_package_versions": [1, 2, 3],
            },
            _secret_refs(planner_profile),
            manifest.workspace_id,
        ),
        _instance(
            module_ids["revision_planner"],
            definition_ids["revision_planner"],
            "revision-planning",
            "planning",
            planner_profile.config_version,
            {
                "planning.executable": "opencode",
                "planning.model": planner_profile.model_id,
                "planning.context_package_versions": [1, 2, 3],
            },
            _secret_refs(planner_profile),
            manifest.workspace_id,
        ),
        _instance(
            module_ids["worker"],
            definition_ids["worker"],
            "worker",
            "worker",
            worker_profile.config_version,
            _worker_config(worker_profile),
            _secret_refs(worker_profile),
            manifest.workspace_id,
        ),
        _instance(
            module_ids["second_worker"],
            definition_ids["second_worker"],
            "second-worker",
            "worker",
            second_profile.config_version,
            _worker_config(second_profile),
            _secret_refs(second_profile),
            manifest.workspace_id,
        ),
        _instance(
            module_ids["workspace"],
            definition_ids["workspace"],
            "git-worktree",
            "workspace",
            1,
            {
                "workspace.repository_ref": str(manifest.repository),
                "workspace.base_revision": manifest.base_commit,
                "workspace.expected_outputs": ["diff", "test-report"],
                "workspace.writable_paths": ["."],
                "workspace.readable_paths": ["."],
            },
            (),
            manifest.workspace_id,
        ),
        _instance(
            module_ids["local_verification"],
            definition_ids["local_verification"],
            "local-verification",
            "verification",
            1,
            {
                "verification.check_specs": [
                    demo_check_spec().to_mapping(),
                    closure_check_spec().to_mapping(),
                    summary_json_check_spec().to_mapping(),
                    patch_integrity_check_spec(manifest.base_commit).to_mapping(),
                ]
            },
            (),
            manifest.workspace_id,
        ),
        _instance(
            module_ids["collaboration"],
            definition_ids["collaboration"],
            "collaboration",
            "collaboration",
            1,
            {
                "collaboration.strategy_id": COLLABORATION_INPUT_GROUPS_STRATEGY_ID,
                "collaboration.projection_schema_version": COLLABORATION_PROJECTION_SCHEMA_V2,
                "collaboration.decision_schema_version": 2,
            },
            (),
            manifest.workspace_id,
        ),
        _instance(
            module_ids["notification"],
            definition_ids["notification"],
            "local-notification",
            "notification",
            1,
            {
                "notification.route_ref": manifest.notification_route_ref
                or "module:r6/local-notification"
            },
            (),
            manifest.workspace_id,
        ),
    )
    profile = PlanningProfile(
        id=_stable_id(manifest.run_id, "planning-profile:basic"),
        workspace_id=manifest.workspace_id,
        profile_key=f"r6-basic-{manifest.run_id.hex[:12]}",
        version=1,
        display_name="R6 Basic OpenCode Planner",
        planner_module_instance_id=module_ids["planner"],
        planner_module_config_version=planner_profile.config_version,
        planner_interface_version="sidera.planner.v1",
        prompt_policy_version="r6-basic-v1",
        system_policy={
            "result_integration": {
                "repository_ref": opaque_repository_ref(manifest.repository),
                "base_commit": manifest.base_commit,
                "verification_environment_ref": "env://interview-demo",
                "verification_check_specs": [
                    patch_integrity_check_spec(manifest.base_commit).to_mapping(),
                    closure_check_spec().to_mapping(),
                ],
                "verification_binding": {
                    "adapter_module_ref": str(module_ids["local_verification"]),
                    "adapter_config_version": 1,
                    "workspace_module_ref": str(module_ids["workspace"]),
                    "workspace_config_version": 1,
                },
            }
        },
        proposal_schema_version=planner_schema_version,
        allowed_capabilities=("code", "python"),
        adapter_preferences=(planner_profile.provider,),
        adapter_forbidden=(),
        budget=PlanningBudgetPolicy(
            max_context_bytes=64_000,
            max_tokens=100_000,
            max_cost_microunits=1_000_000_000,
            max_runtime_seconds=3_600,
            max_revisions=20,
            max_nodes=500,
            max_concurrency=4,
        ),
        retry_policy=PlanningRetryPolicy(
            max_retries=1,
            backoff_seconds=(1,),
            unknown_outcome_policy=UnknownOutcomePolicy.BLOCK,
            invalid_output_policy=InvalidOutputPolicy.RETRY,
        ),
        default_priority=50,
        execution_max_attempts=2,
        execution_budget_level="standard",
        user_selectable=True,
        status=PlanningProfileStatus.ACTIVE,
        planner_secret_refs=_secret_refs(planner_profile),
        collaboration_runtime_binding=(
            CollaborationRuntimeBindingSnapshot(
                runtime_definition_id=definition_ids["collaboration"],
                runtime_instance_id=module_ids["collaboration"],
                runtime_interface_version="sidera.collaboration.v1",
                runtime_config_version=1,
                runtime_capability_snapshot=collaboration_capability,
            )
            if result_plan_enabled
            else None
        ),
    )
    revision_profile = replace(
        profile,
        id=_stable_id(manifest.run_id, "planning-profile:revision"),
        profile_key=f"r6-basic-revision-{manifest.run_id.hex[:12]}",
        display_name="R6 Basic OpenCode Revision Planner",
        planner_module_instance_id=module_ids["revision_planner"],
        prompt_policy_version="r6-basic-revision-v1",
        proposal_schema_version=3,
    )
    worker_names = {
        role: f"R6 basic {role.replace('_', ' ')} worker {manifest.run_id.hex[:8]}"
        for role in ("worker", "second_worker")
    }
    if result_plan_enabled:
        worker_names["verification"] = (
            f"R6 basic verification worker {manifest.run_id.hex[:8]}"
        )
    return _Resources(
        definitions=definitions,
        instances=instances,
        profile=profile,
        revision_profile=revision_profile,
        module_ids=module_ids,
        worker_names=worker_names,
    )


def _instance(
    instance_id: UUID,
    definition_id: UUID,
    instance_name: str,
    namespace: str,
    config_version: int,
    config: Mapping[str, Any],
    secret_refs: tuple[str, ...],
    workspace_id: UUID,
) -> ModuleInstance:
    return ModuleInstance(
        id=instance_id,
        workspace_id=workspace_id,
        definition_id=definition_id,
        instance_name=instance_name,
        config_version=config_version,
        config_namespace=namespace,
        config=config,
        secret_refs=secret_refs,
        lifecycle=ModuleLifecycle.ACTIVE,
    )


def _profile(manifest: BasicRunManifest, role: str) -> AgentProfile:
    for profile in manifest.profiles:
        if profile.role == role:
            return profile
    raise BootstrapConflict(f"missing profile: {role}")


def _worker_config(profile: AgentProfile) -> dict[str, object]:
    timeout_seconds = _worker_timeout_seconds()
    return {
        "worker.executable": "opencode",
        "worker.model": profile.model_id,
        "worker.completion_policy": "initial_turn",
        "worker.context_package_versions": [1, 2, 3],
        "worker.request_timeout_seconds": timeout_seconds,
        "worker.handoff_request_timeout_seconds": timeout_seconds,
        "worker.stop_grace_seconds": 5.0,
    }


def _worker_timeout_seconds() -> float:
    raw = os.environ.get("SIDERA_OPENCODE_REQUEST_TIMEOUT_SECONDS")
    if raw is None or not raw.strip():
        return 60.0
    try:
        timeout_seconds = float(raw)
    except ValueError as exc:
        raise ValueError(
            "SIDERA_OPENCODE_REQUEST_TIMEOUT_SECONDS must be a positive number"
        ) from exc
    if not isfinite(timeout_seconds) or timeout_seconds <= 0:
        raise ValueError(
            "SIDERA_OPENCODE_REQUEST_TIMEOUT_SECONDS must be a positive number"
        )
    return timeout_seconds


def _secret_refs(profile: AgentProfile) -> tuple[str, ...]:
    return () if profile.credential_ref == "none" else (profile.credential_ref,)


def _planner_capabilities(
    *, proposal_schema_versions: tuple[int, ...] = (2,)
) -> PlannerAdapterCapabilityProfile:
    return PlannerAdapterCapabilityProfile(
        structured_output=StructuredOutputCapability.JSON_SCHEMA,
        clarification=True,
        idempotency=IdempotencyCapability.OPERATION_ID,
        cancellation=CancellationCapability.RUN,
        usage_reporting=UsageReportingCapability.TOKENS,
        max_context_bytes=128_000,
        stability=StabilityLevel.STABLE,
        interface_version="sidera.planner.v1",
        proposal_schema_versions=proposal_schema_versions,
        context_package_versions=(1, 2, 3),
    )


def _result_plan_enabled() -> bool:
    return os.environ.get("SIDERA_RESULT_INPUTS_ENABLED") == "1" or os.environ.get(
        "SIDERA_RESULT_INTEGRATION_ENABLED"
    ) == "1"


def _worker_capabilities() -> WorkerAdapterCapabilityProfile:
    return WorkerAdapterCapabilityProfile(
        input=CapabilityInput.QUEUED,
        idempotency=IdempotencyCapability.NONE,
        resume=ResumeCapability.SAME_PROCESS,
        stop=StopCapability.EXACT_EXECUTION,
        sandbox=SandboxCapability.FILESYSTEM,
        stability=StabilityLevel.EXPERIMENTAL,
        context_package_versions=(1, 2, 3),
        turn_completion_versions=(1,),
        handoff_checkpoint_mode=HandoffCheckpointMode.CONTROL_EVENT,
        handoff_checkpoint_versions=(1,),
        handoff_session_context_versions=(1,),
        handoff_writer_gate_versions=(1,),
        handoff_inspection=True,
        code_result_versions=(1,),
    )


def _ensure_workspace(uow: SqlAlchemyUnitOfWork, workspace_id: UUID) -> None:
    row = uow.session.get(WorkspaceModel, workspace_id)
    if row is None:
        uow.session.add(
            WorkspaceModel(
                id=workspace_id,
                name="SIDERA R6 Basic Acceptance Workspace",
                status="active",
                version=1,
            )
        )
        uow.flush()


def _check_workspace_identity(session: Any, workspace_id: UUID) -> None:
    row = session.get(WorkspaceModel, workspace_id)
    if row is not None and row.status != "active":
        raise BootstrapConflict("workspace is not active")


def _ensure_definition(uow: SqlAlchemyUnitOfWork, desired: ModuleDefinition) -> None:
    existing = uow.modules.get_definition(desired.id, desired.workspace_id)
    if existing is not None:
        if existing != desired:
            raise BootstrapConflict(f"module definition conflict: {desired.id}")
        return
    raw = uow.session.get(ModuleDefinitionModel, desired.id)
    if raw is not None and raw.workspace_id != desired.workspace_id:
        raise BootstrapConflict("module definition belongs to another workspace")
    uow.modules.add_definition(desired)


def _ensure_instance(uow: SqlAlchemyUnitOfWork, desired: ModuleInstance) -> None:
    existing = uow.modules.get_instance(desired.id, desired.workspace_id)
    if existing is not None:
        if (
            existing != desired
            or dict(existing.config) != dict(desired.config)
            or existing.secret_refs != desired.secret_refs
        ):
            raise BootstrapConflict(
                f"module instance config_version conflict: {desired.instance_name}"
            )
        return
    raw = uow.session.get(ModuleInstanceModel, desired.id)
    if raw is not None and raw.workspace_id != desired.workspace_id:
        raise BootstrapConflict("module instance belongs to another workspace")
    uow.modules.add_instance(desired)


def _ensure_profile(uow: SqlAlchemyUnitOfWork, desired: PlanningProfile) -> None:
    existing = uow.planning_profiles.get(
        desired.id,
        desired.workspace_id,
        version=desired.version,
    )
    if existing is not None:
        if existing != desired:
            raise BootstrapConflict("planning profile config_version conflict")
        return
    raw = uow.session.get(PlanningProfileModel, desired.id)
    if raw is not None and raw.workspace_id != desired.workspace_id:
        raise BootstrapConflict("planning profile belongs to another workspace")
    uow.planning_profiles.add(desired)


def _ensure_worker(
    uow: SqlAlchemyUnitOfWork,
    manifest: BasicRunManifest,
    *,
    role: str,
    profile: AgentProfile | None,
    now: datetime,
) -> UUID:
    resources = _resources(manifest)
    expected_name = resources.worker_names[role]
    if role == "verification":
        adapter_type = "local_verification"
        capabilities = frozenset({"context_package:v3", "verification"})
        capacity = 1
        cost_per_hour_microunits = 0
        startup_latency_ms = 10
    else:
        if profile is None:
            raise BootstrapConflict(f"missing profile: {role}")
        adapter_type = profile.provider
        capabilities = frozenset(
            {
                "code",
                "python",
                "context_package:v3",
                "result_integration:v1",
                "result_inputs:v1",
                "turn-completion:v1",
            }
        )
        capacity = 1
        cost_per_hour_microunits = 1
        startup_latency_ms = 100
    matches = tuple(
        worker
        for worker in uow.workers.list_for_workspace(manifest.workspace_id)
        if worker.display_name == expected_name
    )
    if len(matches) > 1:
        raise BootstrapConflict(f"duplicate worker identity: {expected_name}")
    if matches:
        worker = matches[0]
        if worker.adapter_type != adapter_type or worker.capabilities != capabilities:
            raise BootstrapConflict(f"worker config_version conflict: {role}")
        return worker.id

    envelope = CommandEnvelope(
        id=_stable_id(manifest.run_id, f"command:register-worker:{role}"),
        workspace_id=manifest.workspace_id,
        command_type="RegisterWorker",
        target_type="worker",
        actor_type="bootstrap",
        actor_id="r6-basic-bootstrap",
        target_id=None,
        payload={
            "adapter_type": adapter_type,
            "display_name": expected_name,
            "capabilities": sorted(capabilities),
            "capacity": capacity,
            "cost_per_hour_microunits": cost_per_hour_microunits,
            "startup_latency_ms": startup_latency_ms,
        },
        expected_version=None,
        idempotency_key=f"r6-basic:register-worker:{role}:{manifest.run_id}",
        correlation_id=manifest.run_id,
    )
    result = WorkerCommandHandler(lambda: now).handle(uow, envelope)
    uow.commands.record_applied(envelope, hash_request(envelope), result)
    uow.flush()
    return result.aggregate_id


def _ensure_runtime(
    uow: SqlAlchemyUnitOfWork,
    manifest: BasicRunManifest,
    *,
    role: str,
    worker_id: UUID,
    adapter_instance_id: UUID,
    adapter_config_version: int,
    workspace_instance_id: UUID,
) -> int:
    existing = uow.worker_runtime_configurations.get_for_worker(
        worker_id,
        manifest.workspace_id,
    )
    if existing is not None:
        if existing.runtime_identity != (
            adapter_instance_id,
            adapter_config_version,
            workspace_instance_id,
            1,
        ):
            raise BootstrapConflict(f"runtime config_version conflict: {role}")
        return existing.version
    envelope = CommandEnvelope(
        id=_stable_id(manifest.run_id, f"command:configure-runtime:{role}"),
        workspace_id=manifest.workspace_id,
        command_type="ConfigureWorkerRuntime",
        target_type="worker_runtime_configuration",
        actor_type="bootstrap",
        actor_id="r6-basic-bootstrap",
        target_id=worker_id,
        payload={
            "adapter_module_instance_id": str(adapter_instance_id),
            "adapter_module_config_version": adapter_config_version,
            "workspace_module_instance_id": str(workspace_instance_id),
            "workspace_module_config_version": 1,
        },
        expected_version=None,
        idempotency_key=f"r6-basic:configure-runtime:{role}:{manifest.run_id}",
        correlation_id=manifest.run_id,
    )
    result = RuntimeCommandHandler(lambda: datetime.now(UTC)).handle(uow, envelope)
    uow.commands.record_applied(envelope, hash_request(envelope), result)
    uow.flush()
    return result.aggregate_version


def _prepare_repository(
    manifest: BasicRunManifest,
    *,
    state: Mapping[str, Any] | None,
    mode: BootstrapMode,
) -> tuple[str, str]:
    repository = manifest.repository.expanduser().resolve()
    head = _git(repository, "rev-parse", "HEAD")
    if state is not None:
        expected_head = _text_state(state, "repository_head")
        if head != expected_head:
            raise BootstrapConflict("business repository changed after bootstrap")
        if _git(repository, "status", "--porcelain"):
            raise BootstrapConflict("business repository has uncommitted changes")
        return _text_state(state, "initial_commit"), head
    if head != manifest.base_commit:
        raise BootstrapConflict("repository HEAD does not match manifest base_commit")
    if _git(repository, "status", "--porcelain"):
        raise BootstrapConflict("business repository must be clean at bootstrap")
    if mode == "check":
        return manifest.base_commit, head
    _write_task_skeleton(repository)
    if _git(repository, "status", "--porcelain"):
        _git(
            repository,
            "add",
            "--",
            "record_app.py",
            "test_record_app.py",
            "TASK.md",
        )
        _git(
            repository,
            "-c",
            "user.name=SIDERA R6 bootstrap",
            "-c",
            "user.email=sidera-bootstrap@localhost",
            "commit",
            "-qm",
            "R6 basic acceptance task skeleton",
        )
    return manifest.base_commit, _git(repository, "rev-parse", "HEAD")


def _write_task_skeleton(repository: Path) -> None:
    skeleton = {
        "record_app.py": (
            '"""R6 acceptance task skeleton; Workers must implement the functions."""\n\n'
            "\n"
            "def validate_record(value: object) -> bool:\n"
            '    raise NotImplementedError("WorkerItem must implement validate_record")\n\n'
            "\n"
            "def format_record(value: object) -> str:\n"
            '    raise NotImplementedError("WorkerItem must implement format_record")\n\n'
            "\n"
            "def render_report(value: object) -> str:\n"
            '    raise NotImplementedError("WorkerItem must implement render_report")\n'
        ),
        "test_record_app.py": (
            '"""Fixed acceptance checks for the three WorkerItem outputs."""\n\n'
            "from record_app import format_record, render_report, validate_record\n\n"
            "\n"
            "def test_worker_outputs_are_implemented() -> None:\n"
            '    assert callable(validate_record) and callable(format_record) and callable(render_report)\n'
        ),
        "TASK.md": (
            "# R6 Basic Acceptance Task\n\n"
            "Implement `validate_record`, `format_record`, and `render_report`.\n"
            "Keep each function focused on its assigned WorkerItem responsibility.\n"
        ),
    }
    for name, content in skeleton.items():
        target = repository / name
        if target.exists():
            if target.read_text(encoding="utf-8") != content:
                raise BootstrapConflict(f"bootstrap would overwrite existing {name}")
            continue
        target.write_text(content, encoding="utf-8")


def _git(repository: Path, *args: str) -> str:
    try:
        result = subprocess.run(
            ["git", "-C", str(repository), *args],
            check=True,
            capture_output=True,
            text=True,
        )
    except (OSError, subprocess.CalledProcessError) as exc:
        raise BootstrapConflict("business repository Git operation failed") from exc
    return result.stdout.strip()


def _ensure_state_root(path: Path) -> Path:
    resolved = path.expanduser().resolve()
    resolved.mkdir(parents=True, exist_ok=True)
    return resolved


def _read_state(path: Path) -> Mapping[str, Any] | None:
    if not path.exists():
        return None
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise BootstrapConflict("bootstrap state is unreadable") from exc
    if not isinstance(value, dict) or value.get("schema_version") != _SCHEMA_VERSION:
        raise BootstrapConflict("bootstrap state schema is unsupported")
    return value


def _validate_state_identity(
    state: Mapping[str, Any] | None,
    *,
    manifest: BasicRunManifest,
    config_fingerprint: str,
    database_url: str | None,
) -> None:
    if state is None:
        return
    if state.get("workspace_id") != str(manifest.workspace_id):
        raise BootstrapConflict("bootstrap run cannot move to another workspace")
    if state.get("run_id") != str(manifest.run_id):
        raise BootstrapConflict("bootstrap state run identity mismatch")
    if state.get("config_fingerprint") != config_fingerprint:
        raise BootstrapConflict("bootstrap config_version/fingerprint conflict")
    if database_url is not None and state.get("database_identity") != _digest(database_url):
        raise BootstrapConflict("bootstrap database identity conflict")


def _state_payload(
    *,
    manifest: BasicRunManifest,
    resources: _Resources,
    initial_commit: str,
    repository_head: str,
    database_url: str | None,
    config_fingerprint: str,
    result: BootstrapResult,
) -> dict[str, object]:
    return {
        "schema_version": _SCHEMA_VERSION,
        "run_id": str(manifest.run_id),
        "workspace_id": str(manifest.workspace_id),
        "run_root": str(manifest.run_root),
        "repository": str(manifest.repository),
        "initial_commit": initial_commit,
        "repository_head": repository_head,
        "database_identity": _digest(database_url or manifest.database_ref),
        "runtime_fingerprint": manifest.runtime_fingerprint,
        "config_fingerprint": config_fingerprint,
        "scenarios": sorted(manifest.scenarios),
        "module_ids": {key: str(value) for key, value in result.module_ids.items()},
        "profile_ids": {key: str(value) for key, value in result.profile_ids.items()},
        "worker_ids": {key: str(value) for key, value in result.worker_ids.items()},
        "runtime_versions": dict(result.runtime_versions),
        "created_at": datetime.now(UTC).isoformat(),
    }


def _write_state(path: Path, payload: Mapping[str, object]) -> None:
    path = path.expanduser().resolve()
    _ensure_state_root(path.parent)
    fd, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent, text=True)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(dict(payload), handle, sort_keys=True, separators=(",", ":"))
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary_name, 0o600)
        os.replace(temporary_name, path)
    except BaseException:
        try:
            os.unlink(temporary_name)
        except OSError:
            pass
        raise


def _config_fingerprint(manifest: BasicRunManifest) -> str:
    payload = {
        "workspace_id": str(manifest.workspace_id),
        "run_id": str(manifest.run_id),
        "repository": str(manifest.repository),
        "base_commit": manifest.base_commit,
        "profiles": [
            {
                "role": profile.role,
                "runtime": profile.runtime,
                "provider": profile.provider,
                "model_id": profile.model_id,
                "endpoint_host": profile.endpoint_host,
                "config_version": profile.config_version,
                "fallbacks": list(profile.fallbacks),
            }
            for profile in manifest.profiles
        ],
        "scenarios": {
            key: value.model_dump(mode="json")
            for key, value in sorted(manifest.scenarios.items())
        },
    }
    return _digest(json.dumps(payload, sort_keys=True, separators=(",", ":")))


def _resolve_database_ref(reference: str) -> str:
    if reference.startswith("env:"):
        value = os.environ.get(reference.removeprefix("env:"), "").strip()
    elif reference.startswith("file:"):
        path = Path(reference.removeprefix("file:"))
        try:
            value = path.read_text(encoding="utf-8").strip()
        except OSError as exc:
            raise BootstrapConflict("database reference is unavailable") from exc
    else:
        raise BootstrapConflict("unsupported database reference")
    if not value:
        raise BootstrapConflict("database reference is unavailable")
    return value


def _stable_id(run_id: UUID, name: str) -> UUID:
    return uuid5(_ID_NAMESPACE, f"{run_id}:{name}")


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _text_state(state: Mapping[str, Any], key: str) -> str:
    value = state.get(key)
    if not isinstance(value, str) or not value.strip():
        raise BootstrapConflict(f"bootstrap state field is invalid: {key}")
    return value


def _cli_result(result: BootstrapResult) -> dict[str, object]:
    return {
        "status": result.status,
        "workspace_id": str(result.workspace_id),
        "module_ids": {key: str(value) for key, value in result.module_ids.items()},
        "profile_ids": {key: str(value) for key, value in result.profile_ids.items()},
        "worker_ids": {key: str(value) for key, value in result.worker_ids.items()},
        "runtime_versions": dict(result.runtime_versions),
        "repository_head": result.repository_head,
        "state_path": str(result.state_path),
    }


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, required=True)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--check", action="store_true")
    mode.add_argument("--apply", action="store_true")
    parser.add_argument("--state-root", type=Path, default=Path(".sidera/basic-acceptance"))
    args = parser.parse_args(argv)
    try:
        manifest = load_manifest(args.manifest)
        result = bootstrap_manifest(
            manifest,
            mode="apply" if args.apply else "check",
            state_root=args.state_root,
        )
    except (BootstrapConflict, ValueError) as exc:
        print(json.dumps({"status": "error", "error": str(exc)[:500]}, sort_keys=True))
        return 2
    print(json.dumps(_cli_result(result), sort_keys=True, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
