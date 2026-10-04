"""Ordinary, explicitly authorized native workspace configuration."""

import json
import subprocess
from dataclasses import asdict
from datetime import UTC, datetime
from hashlib import sha256
from pathlib import Path
from uuid import UUID, uuid5

from sidera_agents.contracts import ProfileRef
from sidera_agents.errors import AgentError
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from sidera_control_plane.adapters.git_workspace import opaque_repository_ref
from sidera_control_plane.adapters.local_verification import LocalVerificationAdapter
from sidera_control_plane.application.agent_worker_adapter import native_capabilities
from sidera_control_plane.application.assignments import AssignmentCommandHandler
from sidera_control_plane.domain.collaboration import (
    CollaborationRuntimeBindingSnapshot,
    CollaborationStrategy,
)
from sidera_control_plane.domain.errors import DomainError
from sidera_control_plane.domain.modules import (
    COLLABORATION_INPUT_GROUPS_STRATEGY_ID,
    COLLABORATION_RUNTIME_MODULE_TYPE,
    LOCAL_VERIFICATION_PROVIDER,
    WORKER_ADAPTER_MODULE_TYPE,
    WORKSPACE_PROVIDER_MODULE_TYPE,
    CollaborationRuntimeCapabilityProfile,
    IdempotencyCapability,
    ModuleDefinition,
    ModuleInstance,
    ModuleLifecycle,
    SandboxCapability,
    StabilityLevel,
    WorkspaceProviderCapabilityProfile,
)
from sidera_control_plane.domain.native_setup import (
    NATIVE_SETUP_POLICY_KEY,
    PlanningPolicyRef,
    WorkspaceSetupReceipt,
    WorkspaceSetupRequest,
    native_local_slot_ids,
)
from sidera_control_plane.domain.planning_profiles import (
    InvalidOutputPolicy,
    PlanningBudgetPolicy,
    PlanningProfile,
    PlanningProfileStatus,
    PlanningRetryPolicy,
    UnknownOutcomePolicy,
)
from sidera_control_plane.domain.result_integration import (
    CheckSpecV1,
    VerificationBindingV1,
)
from sidera_control_plane.domain.worker import Worker
from sidera_control_plane.domain.worker_runtime import WorkerRuntimeConfiguration
from sidera_control_plane.persistence.models import (
    ModuleDefinitionModel,
    ModuleInstanceModel,
    PlanningProfileModel,
    WorkerModel,
    WorkspaceModel,
)


def _validate_checks(checks) -> tuple[CheckSpecV1, ...]:
    try:
        if (
            not isinstance(checks, tuple)
            or not checks
            or any(not isinstance(check, CheckSpecV1) for check in checks)
        ):
            raise ValueError()
        canonical = tuple(
            CheckSpecV1.from_mapping(check.to_mapping()) for check in checks
        )
        if (
            len({check.key for check in canonical}) != len(canonical)
            or len({check.environment_ref for check in canonical}) != 1
        ):
            raise ValueError()
        return canonical
    except (DomainError, TypeError, ValueError, AttributeError):
        raise AgentError(
            "validation_error",
            "Valid explicit verification checks are required",
            "verification_checks",
        ) from None


def _repository_identity(directory: Path) -> tuple[Path, str]:
    try:
        if not isinstance(directory, Path) or not directory.is_absolute():
            raise ValueError()
        if any(path.is_symlink() for path in (directory, *directory.parents)):
            raise ValueError()
        canonical = directory.resolve(strict=True)
        if not canonical.is_dir():
            raise ValueError()
        top = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            cwd=canonical,
            capture_output=True,
            text=True,
            check=True,
            timeout=5,
        ).stdout.strip()
        if Path(top).resolve(strict=True) != canonical:
            raise ValueError()
        head = subprocess.run(
            ["git", "rev-parse", "--verify", "HEAD^{commit}"],
            cwd=canonical,
            capture_output=True,
            text=True,
            check=True,
            timeout=5,
        ).stdout.strip()
        return canonical, head
    except (OSError, ValueError, subprocess.SubprocessError):
        raise AgentError(
            "setup_required",
            "An explicit local Git repository is required",
            "repository_required",
        ) from None


def _resources(workspace_id, repository, head, checks):
    collaboration = CollaborationRuntimeCapabilityProfile(
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
    specs = (
        (
            "agent-run",
            WORKER_ADAPTER_MODULE_TYPE,
            "sidera.worker-adapter.v1",
            native_capabilities(),
            "worker",
            {},
        ),
        (
            "git-worktree",
            WORKSPACE_PROVIDER_MODULE_TYPE,
            "sidera.workspace-provider.v1",
            WorkspaceProviderCapabilityProfile(
                isolation=SandboxCapability.ISOLATED_RUNTIME,
                writable=True,
                git_worktree=True,
                stability=StabilityLevel.STABLE,
                code_input_versions=(1,),
            ),
            "workspace",
            {
                "workspace.repository_ref": str(repository),
                "workspace.base_revision": head,
                "workspace.expected_outputs": ["diff", "test-report"],
                "workspace.writable_paths": ["."],
                "workspace.readable_paths": ["."],
            },
        ),
        (
            LOCAL_VERIFICATION_PROVIDER,
            WORKER_ADAPTER_MODULE_TYPE,
            "sidera.worker-adapter.v1",
            LocalVerificationAdapter.capabilities(),
            "verification",
            {
                "verification.check_specs": [check.to_mapping() for check in checks],
            },
        ),
        (
            "sidera-collaboration",
            COLLABORATION_RUNTIME_MODULE_TYPE,
            "sidera.collaboration.v1",
            collaboration,
            "collaboration",
            {
                "collaboration.strategy_id": COLLABORATION_INPUT_GROUPS_STRATEGY_ID,
                "collaboration.projection_schema_version": 2,
                "collaboration.decision_schema_version": 2,
            },
        ),
    )
    definitions, instances = [], []
    for provider, module_type, interface, capabilities, namespace, config in specs:
        definition_id = uuid5(workspace_id, "sidera.native.definition:" + provider)
        digest = sha256(
            json.dumps(config, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
        instance_id = uuid5(definition_id, digest)
        definitions.append(
            ModuleDefinition(
                definition_id,
                workspace_id,
                module_type,
                provider,
                interface,
                1,
                capabilities,
                ModuleLifecycle.ACTIVE,
            )
        )
        instances.append(
            ModuleInstance(
                instance_id,
                workspace_id,
                definition_id,
                f"native-{provider}-{digest[:12]}",
                1,
                namespace,
                config,
                (),
                ModuleLifecycle.ACTIVE,
            )
        )
    return tuple(definitions), tuple(instances)


def _ensure_resources(uow, definitions, instances, *, create):
    for definition in definitions:
        existing = uow.modules.get_definition(definition.id, definition.workspace_id)
        if existing is not None:
            if existing != definition:
                raise AgentError(
                    "configuration_mismatch", "Native module definition changed"
                )
        elif (
            not create
            or uow.session.get(ModuleDefinitionModel, definition.id) is not None
        ):
            raise AgentError(
                "configuration_mismatch", "Native module definition is unavailable"
            )
        else:
            uow.modules.add_definition(definition)
    uow.flush()
    for instance in instances:
        existing = uow.modules.get_instance(instance.id, instance.workspace_id)
        if existing is not None:
            if existing != instance:
                raise AgentError(
                    "configuration_mismatch", "Native module configuration changed"
                )
        elif (
            not create or uow.session.get(ModuleInstanceModel, instance.id) is not None
        ):
            raise AgentError(
                "configuration_mismatch", "Native module configuration is unavailable"
            )
        else:
            uow.modules.add_instance(instance)
    uow.flush()


def _ensure_slot(
    uow, *, workspace_id, worker_id, adapter, workspace, provider, capacity, create, now
):
    capabilities = (
        frozenset(
            {
                "code",
                "python",
                "context_package:v3",
                "result_integration:v1",
                "result_inputs:v1",
            }
        )
        if provider == "agent-run"
        else frozenset({"context_package:v3", "verification"})
    )
    desired = Worker.register(
        worker_id=worker_id,
        workspace_id=workspace_id,
        adapter_type=provider,
        display_name="Native " + provider + " " + worker_id.hex[:12],
        capabilities=capabilities,
        capacity=capacity,
        cost_per_hour_microunits=0,
        startup_latency_ms=0,
        now=now,
    )
    existing = uow.workers.get(worker_id, workspace_id, for_update=True)
    if existing is not None:
        if (
            existing.adapter_type,
            existing.capabilities,
            existing.capacity,
            existing.cost_per_hour_microunits,
            existing.startup_latency_ms,
        ) != (
            provider,
            capabilities,
            capacity,
            0,
            0,
        ) or not 0 <= existing.current_load <= existing.capacity:
            raise AgentError("configuration_mismatch", "Native execution slot changed")
    elif not create or uow.session.get(WorkerModel, worker_id) is not None:
        raise AgentError(
            "configuration_mismatch", "Native execution slot is unavailable"
        )
    else:
        uow.workers.add(desired)
        uow.flush()
    runtime = WorkerRuntimeConfiguration.create(
        worker_id=worker_id,
        workspace_id=workspace_id,
        adapter_module_instance_id=adapter.id,
        adapter_module_config_version=adapter.config_version,
        workspace_module_instance_id=workspace.id,
        workspace_module_config_version=workspace.config_version,
    )
    current = uow.worker_runtime_configurations.get_for_worker(
        worker_id, workspace_id, for_update=True
    )
    if current is not None:
        if current != runtime:
            raise AgentError(
                "configuration_mismatch", "Native execution slot runtime changed"
            )
    elif not create:
        raise AgentError(
            "configuration_mismatch", "Native execution slot runtime is unavailable"
        )
    else:
        uow.worker_runtime_configurations.add(runtime)


def _current_policy(uow, workspace_id, version):
    policy_id = uow.session.scalar(
        select(PlanningProfileModel.id).where(
            PlanningProfileModel.workspace_id == workspace_id,
            PlanningProfileModel.profile_key == NATIVE_SETUP_POLICY_KEY,
            PlanningProfileModel.version == version,
        )
    )
    if policy_id is None:
        return None
    policy = uow.planning_profiles.get(policy_id, workspace_id, version=version)
    if (
        policy is None
        or policy.execution_mode != "native_profile"
        or policy.status is not PlanningProfileStatus.ACTIVE
    ):
        raise AgentError("configuration_mismatch", "Native setup policy is unavailable")
    return policy


def configure_native_workspace(
    uow_factory, request, *, identity, settings, catalog, runtimes
):
    if (
        not isinstance(request, WorkspaceSetupRequest)
        or not isinstance(request.workspace_id, UUID)
        or type(request.expected_version) is not int
        or request.expected_version < 0
        or not isinstance(request.planning_budget, PlanningBudgetPolicy)
        or (
            request.planner_agent_ref is not None
            and (
                not isinstance(request.planner_agent_ref, ProfileRef)
                or request.planner_agent_ref.workspace_id != request.workspace_id
            )
        )
    ):
        raise AgentError("validation_error", "Valid workspace setup is required")
    try:
        budget = PlanningBudgetPolicy(**asdict(request.planning_budget))
    except (DomainError, TypeError, ValueError):
        raise AgentError(
            "validation_error", "Valid planning budget is required", "planning_budget"
        ) from None
    checks = (
        None
        if request.verification_checks is None
        else _validate_checks(request.verification_checks)
    )
    with uow_factory() as uow:
        existing = uow.session.get(WorkspaceModel, request.workspace_id)
        version = 0 if existing is None else existing.version
        if version != request.expected_version:
            raise AgentError("revision_conflict", "Workspace configuration changed")
        previous = (
            None
            if existing is None
            else _current_policy(uow, request.workspace_id, version)
        )
    pin = (
        request.planner_agent_ref
        if request.planner_agent_ref is not None
        else None
        if previous is None
        else previous.planner_agent_ref
    )
    if pin is None:
        raise AgentError(
            "setup_required",
            "An explicit Planner selection is required",
            "planner_selection_required",
        )
    if checks is None:
        if previous is None:
            raise AgentError(
                "setup_required",
                "Explicit verification checks are required",
                "verification_policy_required",
            )
        try:
            checks = _validate_checks(
                tuple(
                    CheckSpecV1.from_mapping(value)
                    for value in previous.system_policy["result_integration"][
                        "verification_check_specs"
                    ]
                )
            )
        except (KeyError, TypeError, DomainError):
            raise AgentError(
                "setup_required",
                "Explicit verification checks are required",
                "verification_policy_required",
            ) from None
    if catalog is None or runtimes is None:
        raise AgentError(
            "engine_unavailable", "Native Planner services are unavailable"
        )
    try:
        actual_pin, reasons = AssignmentCommandHandler(
            catalog=catalog, runtimes=runtimes, identity=identity
        ).resolve_profile(request.workspace_id, pin.profile_id, pin.revision)
    except DomainError:
        raise AgentError(
            "planning_profile_unavailable", "Selected Planner is unavailable"
        ) from None
    if actual_pin != pin or reasons:
        raise AgentError(
            "planning_profile_unavailable", "Selected Planner is unavailable"
        )
    repository, head = _repository_identity(request.repository_directory)
    repository_ref = opaque_repository_ref(repository)
    definitions, instances = _resources(request.workspace_id, repository, head, checks)
    agent, workspace, verification, collaboration = instances
    binding = VerificationBindingV1(
        verification.id,
        verification.config_version,
        workspace.id,
        workspace.config_version,
    )
    agent_slot, verification_slot = native_local_slot_ids(workspace.id, verification.id)
    slots = {
        "schema_version": 1,
        "agent_run_worker_id": str(agent_slot),
        "verification_worker_id": str(verification_slot),
    }
    unchanged = False
    if previous is not None:
        try:
            result = previous.system_policy["result_integration"]
            unchanged = (
                previous.planner_agent_ref == pin
                and previous.budget == budget
                and result["repository_ref"] == repository_ref
                and result["base_commit"] == head
                and result["verification_environment_ref"] == checks[0].environment_ref
                and tuple(
                    CheckSpecV1.from_mapping(value)
                    for value in result["verification_check_specs"]
                )
                == checks
                and VerificationBindingV1.from_mapping(result["verification_binding"])
                == binding
                and dict(previous.system_policy["local_execution_slots"]) == slots
            )
        except (KeyError, TypeError, DomainError):
            raise AgentError(
                "configuration_mismatch", "Native setup configuration changed"
            ) from None
    try:
        with uow_factory() as uow:
            row = uow.session.scalar(
                select(WorkspaceModel)
                .where(WorkspaceModel.id == request.workspace_id)
                .with_for_update()
            )
            current_version = 0 if row is None else row.version
            if current_version != request.expected_version:
                raise AgentError("revision_conflict", "Workspace configuration changed")
            if row is not None and row.status != "active":
                raise AgentError("configuration_mismatch", "Workspace is unavailable")
            if (
                previous is not None
                and _current_policy(uow, request.workspace_id, current_version)
                != previous
            ):
                raise AgentError(
                    "configuration_mismatch", "Native setup policy changed"
                )
            next_version = current_version if unchanged else current_version + 1
            if row is None:
                row = WorkspaceModel(
                    id=request.workspace_id,
                    name="Native workspace",
                    status="active",
                    version=next_version,
                )
                uow.session.add(row)
                uow.flush()
            elif not unchanged:
                row.version = next_version
            _ensure_resources(uow, definitions, instances, create=not unchanged)
            now = datetime.now(UTC)
            _ensure_slot(
                uow,
                workspace_id=request.workspace_id,
                worker_id=agent_slot,
                adapter=agent,
                workspace=workspace,
                provider="agent-run",
                capacity=settings.gateway_owner_start_concurrency,
                create=not unchanged,
                now=now,
            )
            _ensure_slot(
                uow,
                workspace_id=request.workspace_id,
                worker_id=verification_slot,
                adapter=verification,
                workspace=workspace,
                provider=LOCAL_VERIFICATION_PROVIDER,
                capacity=1,
                create=not unchanged,
                now=now,
            )
            if unchanged:
                if previous is None:
                    raise AgentError(
                        "configuration_mismatch", "Native setup policy is unavailable"
                    )
                policy = previous
            else:
                policy = PlanningProfile(
                    id=uuid5(
                        request.workspace_id, f"sidera.native.policy:{next_version}"
                    ),
                    workspace_id=request.workspace_id,
                    profile_key=NATIVE_SETUP_POLICY_KEY,
                    version=next_version,
                    display_name="Native planning policy",
                    planner_module_instance_id=None,
                    planner_module_config_version=None,
                    planner_interface_version="sidera.planner.v1",
                    prompt_policy_version="sidera.native.v1",
                    system_policy={
                        "result_integration": {
                            "repository_ref": repository_ref,
                            "base_commit": head,
                            "verification_environment_ref": checks[0].environment_ref,
                            "verification_check_specs": [
                                check.to_mapping() for check in checks
                            ],
                            "verification_binding": binding.to_mapping(),
                        },
                        "local_execution_slots": slots,
                    },
                    proposal_schema_version=6,
                    allowed_capabilities=("code", "python"),
                    adapter_preferences=(),
                    adapter_forbidden=(),
                    budget=budget,
                    retry_policy=PlanningRetryPolicy(
                        0, (), UnknownOutcomePolicy.BLOCK, InvalidOutputPolicy.BLOCK
                    ),
                    default_priority=50,
                    execution_max_attempts=2,
                    execution_budget_level="standard",
                    user_selectable=True,
                    status=PlanningProfileStatus.ACTIVE,
                    planner_secret_refs=(),
                    execution_mode="native_profile",
                    planner_agent_ref=pin,
                    collaboration_runtime_binding=CollaborationRuntimeBindingSnapshot(
                        definitions[3].id,
                        collaboration.id,
                        "sidera.collaboration.v1",
                        collaboration.config_version,
                        definitions[3].capabilities,
                    ),
                )
                if uow.session.get(PlanningProfileModel, policy.id) is not None:
                    raise AgentError(
                        "configuration_mismatch", "Native setup policy identity changed"
                    )
                uow.planning_profiles.add(policy)
            receipt = WorkspaceSetupReceipt(
                request.workspace_id,
                next_version,
                PlanningPolicyRef(policy.id, policy.version),
                repository_ref,
                "unchanged"
                if unchanged
                else "created"
                if current_version == 0
                else "updated",
            )
        return receipt
    except IntegrityError as exc:
        if (
            getattr(getattr(exc.orig, "diag", None), "constraint_name", None)
            == "workspaces_pkey"
        ):
            raise AgentError(
                "revision_conflict", "Workspace configuration changed"
            ) from None
        raise
