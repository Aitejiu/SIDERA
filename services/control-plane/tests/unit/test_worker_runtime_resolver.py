from dataclasses import replace
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any, cast
from uuid import UUID, uuid4

import pytest

from sidera_control_plane.application.worker_runtime import (
    ResolvedWorkerRuntime,
    WorkerRuntimeResolver,
)
from sidera_control_plane.domain.execution import Execution
from sidera_control_plane.domain.modules import (
    WORKER_ADAPTER_MODULE_TYPE,
    WORKSPACE_PROVIDER_MODULE_TYPE,
    CapabilityInput,
    IdempotencyCapability,
    ModuleDefinition,
    ModuleInstance,
    ModuleLifecycle,
    ResumeCapability,
    SandboxCapability,
    StabilityLevel,
    StopCapability,
    WorkerAdapterCapabilityProfile,
    WorkspaceProviderCapabilityProfile,
)
from sidera_control_plane.domain.scheduling import SchedulingBlockReason
from sidera_control_plane.domain.worker import Worker
from sidera_control_plane.domain.worker_runtime import WorkerRuntimeConfiguration

NOW = datetime(2026, 8, 23, 12, 0, tzinfo=UTC)
WORKSPACE_ID = UUID("10000000-0000-0000-0000-000000000001")


class FakeRuntimeRepository:
    def __init__(self, configurations: tuple[WorkerRuntimeConfiguration, ...]) -> None:
        self._configurations = {item.worker_id: item for item in configurations}
        self.batch_calls: list[tuple[tuple[UUID, ...], UUID, bool]] = []
        self.single_calls: list[tuple[UUID, UUID, bool]] = []

    def get_for_workers(
        self,
        worker_ids: tuple[UUID, ...],
        workspace_id: UUID,
        *,
        for_update: bool = False,
    ) -> tuple[WorkerRuntimeConfiguration, ...]:
        self.batch_calls.append((worker_ids, workspace_id, for_update))
        return tuple(
            configuration
            for worker_id in worker_ids
            if (configuration := self._configurations.get(worker_id)) is not None
            and configuration.workspace_id == workspace_id
        )

    def get_for_worker(
        self,
        worker_id: UUID,
        workspace_id: UUID,
        *,
        for_update: bool = False,
    ) -> WorkerRuntimeConfiguration | None:
        self.single_calls.append((worker_id, workspace_id, for_update))
        configuration = self._configurations.get(worker_id)
        if configuration is None or configuration.workspace_id != workspace_id:
            return None
        return configuration


class FakeModuleRepository:
    def __init__(
        self,
        instances: tuple[ModuleInstance, ...],
        definitions: tuple[ModuleDefinition, ...],
    ) -> None:
        self._instances = {item.id: item for item in instances}
        self._definitions = {item.id: item for item in definitions}
        self.instance_calls: list[tuple[tuple[UUID, ...], UUID, bool]] = []
        self.definition_calls: list[tuple[tuple[UUID, ...], UUID, bool]] = []

    def get_instances(
        self,
        module_ids: tuple[UUID, ...],
        workspace_id: UUID,
        *,
        for_update: bool = False,
    ) -> tuple[ModuleInstance, ...]:
        self.instance_calls.append((module_ids, workspace_id, for_update))
        return tuple(
            instance
            for module_id in module_ids
            if (instance := self._instances.get(module_id)) is not None
            and instance.workspace_id == workspace_id
        )

    def get_definitions(
        self,
        module_ids: tuple[UUID, ...],
        workspace_id: UUID,
        *,
        for_update: bool = False,
    ) -> tuple[ModuleDefinition, ...]:
        self.definition_calls.append((module_ids, workspace_id, for_update))
        return tuple(
            definition
            for module_id in module_ids
            if (definition := self._definitions.get(module_id)) is not None
            and definition.workspace_id == workspace_id
        )


def make_uow(
    configurations: tuple[WorkerRuntimeConfiguration, ...],
    instances: tuple[ModuleInstance, ...],
    definitions: tuple[ModuleDefinition, ...],
) -> SimpleNamespace:
    return SimpleNamespace(
        worker_runtime_configurations=FakeRuntimeRepository(configurations),
        modules=FakeModuleRepository(instances, definitions),
    )


def make_worker(
    *,
    workspace_id: UUID = WORKSPACE_ID,
    adapter_type: str = "opencode",
    capabilities: frozenset[str] = frozenset({"python"}),
) -> Worker:
    return Worker.register(
        worker_id=uuid4(),
        workspace_id=workspace_id,
        adapter_type=adapter_type,
        display_name="Resolver worker",
        capabilities=capabilities,
        capacity=1,
        cost_per_hour_microunits=1,
        startup_latency_ms=1,
        now=NOW,
    )


def make_execution(
    *,
    workspace_id: UUID = WORKSPACE_ID,
    requirements: frozenset[str] = frozenset({"python"}),
    adapter_forbidden: tuple[str, ...] = (),
) -> Execution:
    execution_id = uuid4()
    return Execution.create(
        execution_id=execution_id,
        workspace_id=workspace_id,
        task_id=uuid4(),
        lineage_id=execution_id,
        attempt=1,
        max_attempts=2,
        purpose="resolve worker runtime",
        capability_requirements=requirements,
        pinned_worker_id=None,
        not_before=NOW,
        adapter_forbidden=adapter_forbidden,
    )


def worker_capabilities() -> WorkerAdapterCapabilityProfile:
    return WorkerAdapterCapabilityProfile(
        input=CapabilityInput.QUEUED,
        idempotency=IdempotencyCapability.OPERATION_ID,
        resume=ResumeCapability.DURABLE,
        stop=StopCapability.EXACT_EXECUTION,
        sandbox=SandboxCapability.FILESYSTEM,
        stability=StabilityLevel.STABLE,
    )


def workspace_capabilities() -> WorkspaceProviderCapabilityProfile:
    return WorkspaceProviderCapabilityProfile(
        isolation=SandboxCapability.ISOLATED_RUNTIME,
        writable=True,
        git_worktree=True,
        stability=StabilityLevel.STABLE,
    )


def make_fixture(
    *,
    adapter_provider: str = "opencode",
    adapter_lifecycle: ModuleLifecycle = ModuleLifecycle.ACTIVE,
    workspace_lifecycle: ModuleLifecycle = ModuleLifecycle.ACTIVE,
    adapter_instance_lifecycle: ModuleLifecycle = ModuleLifecycle.ACTIVE,
    workspace_instance_lifecycle: ModuleLifecycle = ModuleLifecycle.ACTIVE,
    adapter_config_version: int = 1,
    workspace_config_version: int = 1,
) -> tuple[
    Worker,
    Execution,
    WorkerRuntimeConfiguration,
    ModuleInstance,
    ModuleInstance,
    ModuleDefinition,
    ModuleDefinition,
]:
    worker = make_worker()
    execution = make_execution()
    adapter_definition = ModuleDefinition(
        id=uuid4(),
        workspace_id=WORKSPACE_ID,
        module_type=WORKER_ADAPTER_MODULE_TYPE,
        provider=adapter_provider,
        interface_version="sidera.worker-adapter.v1",
        config_schema_version=1,
        capabilities=worker_capabilities(),
        lifecycle=adapter_lifecycle,
    )
    workspace_definition = ModuleDefinition(
        id=uuid4(),
        workspace_id=WORKSPACE_ID,
        module_type=WORKSPACE_PROVIDER_MODULE_TYPE,
        provider="git-worktree",
        interface_version="sidera.workspace-provider.v1",
        config_schema_version=1,
        capabilities=workspace_capabilities(),
        lifecycle=workspace_lifecycle,
    )
    adapter_instance = ModuleInstance(
        id=uuid4(),
        workspace_id=WORKSPACE_ID,
        definition_id=adapter_definition.id,
        instance_name="adapter",
        config_version=adapter_config_version,
        config_namespace="adapter",
        config={"adapter.endpoint": "http://adapter.invalid"},
        secret_refs=("adapter.api_key",),
        lifecycle=adapter_instance_lifecycle,
    )
    workspace_instance = ModuleInstance(
        id=uuid4(),
        workspace_id=WORKSPACE_ID,
        definition_id=workspace_definition.id,
        instance_name="workspace",
        config_version=workspace_config_version,
        config_namespace="workspace",
        config={"workspace.root": "/tmp/workspace"},
        secret_refs=(),
        lifecycle=workspace_instance_lifecycle,
    )
    configuration = WorkerRuntimeConfiguration.create(
        worker_id=worker.id,
        workspace_id=WORKSPACE_ID,
        adapter_module_instance_id=adapter_instance.id,
        adapter_module_config_version=1,
        workspace_module_instance_id=workspace_instance.id,
        workspace_module_config_version=1,
    )
    return (
        worker,
        execution,
        configuration,
        adapter_instance,
        workspace_instance,
        adapter_definition,
        workspace_definition,
    )


def inspect_fixture(
    fixture: tuple[
        Worker,
        Execution,
        WorkerRuntimeConfiguration,
        ModuleInstance,
        ModuleInstance,
        ModuleDefinition,
        ModuleDefinition,
    ],
    *,
    extra_workers: tuple[Worker, ...] = (),
    extra_configurations: tuple[WorkerRuntimeConfiguration, ...] = (),
    instances: tuple[ModuleInstance, ...] | None = None,
    definitions: tuple[ModuleDefinition, ...] | None = None,
) -> tuple[Any, SimpleNamespace]:
    (
        worker,
        execution,
        configuration,
        adapter_instance,
        workspace_instance,
        adapter_definition,
        workspace_definition,
    ) = fixture
    uow = make_uow(
        (configuration, *extra_configurations),
        instances
        if instances is not None
        else (adapter_instance, workspace_instance),
        definitions
        if definitions is not None
        else (adapter_definition, workspace_definition),
    )
    inspection = WorkerRuntimeResolver().inspect_candidates(
        cast(Any, uow),
        (worker, *extra_workers),
        execution,
    )
    return inspection, uow


def test_missing_configuration_is_a_persistent_runtime_issue() -> None:
    fixture = make_fixture()
    worker, execution, _, adapter_instance, workspace_instance, adapter_definition, workspace_definition = fixture
    uow = make_uow(
        (),
        (adapter_instance, workspace_instance),
        (adapter_definition, workspace_definition),
    )

    inspection = WorkerRuntimeResolver().inspect_candidates(
        cast(Any, uow), (worker,), execution
    )

    assert inspection[0].resolved is None
    assert inspection[0].issue is not None
    assert inspection[0].issue.reason_code is SchedulingBlockReason.RUNTIME_CONFIGURATION_MISSING


def test_cross_workspace_worker_is_rejected_before_runtime_resolution() -> None:
    fixture = make_fixture()
    worker, execution, *_ = fixture
    foreign_worker = make_worker(workspace_id=uuid4())

    inspection, uow = inspect_fixture(fixture, extra_workers=(foreign_worker,))

    assert inspection[0].resolved is not None
    assert inspection[1].resolved is None
    assert inspection[1].issue is not None
    assert inspection[1].issue.reason_code is SchedulingBlockReason.RUNTIME_CONFIGURATION_INVALID
    assert uow.worker_runtime_configurations.batch_calls == [
        ((worker.id, foreign_worker.id), execution.workspace_id, False)
    ]


@pytest.mark.parametrize(
    ("field", "instances"),
    [
        ("adapter", "adapter_version"),
        ("workspace", "workspace_version"),
    ],
)
def test_config_version_mismatch_is_rejected(
    field: str,
    instances: str,
) -> None:
    fixture = make_fixture()
    (
        worker,
        execution,
        configuration,
        adapter_instance,
        workspace_instance,
        adapter_definition,
        workspace_definition,
    ) = fixture
    if instances == "adapter_version":
        adapter_instance = replace(adapter_instance, config_version=2)
    else:
        workspace_instance = replace(workspace_instance, config_version=2)
    inspection, _ = inspect_fixture(
        fixture,
        instances=(adapter_instance, workspace_instance),
    )

    assert inspection[0].resolved is None
    assert inspection[0].issue is not None
    assert inspection[0].issue.reason_code is SchedulingBlockReason.RUNTIME_CONFIGURATION_INVALID
    assert field in inspection[0].issue.reason_details["field"]
    assert configuration.version == 1
    assert execution.version == 1
    assert worker.version == 1
    assert adapter_definition.lifecycle is ModuleLifecycle.ACTIVE
    assert workspace_definition.lifecycle is ModuleLifecycle.ACTIVE


def test_wrong_adapter_definition_type_is_rejected_as_unavailable() -> None:
    fixture = make_fixture()
    worker, execution, configuration, adapter_instance, workspace_instance, _, workspace_definition = fixture
    wrong_adapter_instance = replace(
        adapter_instance, definition_id=workspace_definition.id
    )
    inspection, _ = inspect_fixture(
        fixture,
        instances=(wrong_adapter_instance, workspace_instance),
        definitions=(workspace_definition,),
    )

    assert inspection[0].resolved is None
    assert inspection[0].issue is not None
    assert inspection[0].issue.reason_code is SchedulingBlockReason.ADAPTER_MODULE_UNAVAILABLE
    assert configuration.adapter_module_instance_id == adapter_instance.id
    assert execution.workspace_id == worker.workspace_id


def test_wrong_workspace_definition_type_is_rejected_as_unavailable() -> None:
    fixture = make_fixture()
    _, _, _, adapter_instance, workspace_instance, adapter_definition, _ = fixture
    wrong_workspace_instance = replace(
        workspace_instance, definition_id=adapter_definition.id
    )
    inspection, _ = inspect_fixture(
        fixture,
        instances=(adapter_instance, wrong_workspace_instance),
        definitions=(adapter_definition,),
    )

    assert inspection[0].resolved is None
    assert inspection[0].issue is not None
    assert inspection[0].issue.reason_code is SchedulingBlockReason.WORKSPACE_MODULE_UNAVAILABLE


def test_adapter_provider_must_match_worker_adapter_type() -> None:
    fixture = make_fixture(adapter_provider="codex")
    inspection, _ = inspect_fixture(fixture)

    assert inspection[0].resolved is None
    assert inspection[0].issue is not None
    assert inspection[0].issue.reason_code is SchedulingBlockReason.ADAPTER_MODULE_UNAVAILABLE
    assert inspection[0].issue.reason_details["provider"] == "codex"


@pytest.mark.parametrize(
    ("lifecycle_field", "lifecycle"),
    (
        ("adapter_definition", ModuleLifecycle.DRAINING),
        ("adapter_definition", ModuleLifecycle.DISABLED),
        ("workspace_definition", ModuleLifecycle.DRAINING),
        ("workspace_definition", ModuleLifecycle.DISABLED),
        ("adapter_instance", ModuleLifecycle.DRAINING),
        ("adapter_instance", ModuleLifecycle.DISABLED),
        ("workspace_instance", ModuleLifecycle.DRAINING),
        ("workspace_instance", ModuleLifecycle.DISABLED),
    ),
)
def test_draining_or_disabled_resources_cannot_create_a_new_binding(
    lifecycle_field: str,
    lifecycle: ModuleLifecycle,
) -> None:
    if lifecycle_field == "adapter_definition":
        fixture = make_fixture(adapter_lifecycle=lifecycle)
    elif lifecycle_field == "workspace_definition":
        fixture = make_fixture(workspace_lifecycle=lifecycle)
    elif lifecycle_field == "adapter_instance":
        fixture = make_fixture(adapter_instance_lifecycle=lifecycle)
    else:
        fixture = make_fixture(workspace_instance_lifecycle=lifecycle)
    inspection, _ = inspect_fixture(fixture)

    assert inspection[0].resolved is None
    assert inspection[0].issue is not None
    assert inspection[0].issue.reason_code in {
        SchedulingBlockReason.ADAPTER_MODULE_UNAVAILABLE,
        SchedulingBlockReason.WORKSPACE_MODULE_UNAVAILABLE,
    }


def test_valid_configuration_freezes_capability_snapshot_and_safe_facts() -> None:
    fixture = make_fixture()
    inspection, _ = inspect_fixture(fixture)

    resolved = inspection[0].resolved
    assert resolved is not None
    assert resolved.worker_id == fixture[0].id
    assert resolved.adapter_type == fixture[0].adapter_type
    assert resolved.adapter_module_instance_id == fixture[3].id
    assert resolved.workspace_module_instance_id == fixture[4].id
    assert resolved.adapter_definition_id == fixture[5].id
    assert resolved.workspace_definition_id == fixture[6].id
    assert resolved.capability_snapshot == worker_capabilities()
    assert "config" not in resolved.resolution_facts
    assert "secret_refs" not in resolved.resolution_facts
    assert "http://adapter.invalid" not in str(resolved.resolution_facts)
    assert "adapter.api_key" not in str(resolved.resolution_facts)


def test_candidate_inspection_reads_each_repository_in_one_batch() -> None:
    fixture = make_fixture()
    second_fixture = make_fixture()
    inspection, uow = inspect_fixture(
        fixture,
        extra_workers=(second_fixture[0],),
        extra_configurations=(second_fixture[2],),
        instances=(fixture[3], fixture[4], second_fixture[3], second_fixture[4]),
        definitions=(
            fixture[5],
            fixture[6],
            second_fixture[5],
            second_fixture[6],
        ),
    )

    assert [item.resolved is not None for item in inspection] == [True, True]
    assert len(uow.worker_runtime_configurations.batch_calls) == 1
    assert len(uow.modules.instance_calls) == 1
    assert len(uow.modules.definition_calls) == 1
    assert uow.worker_runtime_configurations.single_calls == []


def test_workload_tags_are_not_compared_with_protocol_capability_profile() -> None:
    fixture = make_fixture()
    worker, _, _, _, _, _, _ = fixture
    execution = make_execution(requirements=frozenset({"input", "resume", "sandbox"}))
    uow = make_uow(
        (fixture[2],),
        (fixture[3], fixture[4]),
        (fixture[5], fixture[6]),
    )

    inspection = WorkerRuntimeResolver().inspect_candidates(
        cast(Any, uow), (worker,), execution
    )

    assert inspection[0].resolved is not None


def test_lock_and_resolve_locks_configuration_instances_and_definitions_in_order() -> None:
    fixture = make_fixture()
    worker, execution, configuration, adapter_instance, workspace_instance, adapter_definition, workspace_definition = fixture
    uow = make_uow(
        (configuration,),
        (adapter_instance, workspace_instance),
        (adapter_definition, workspace_definition),
    )

    resolved = WorkerRuntimeResolver().lock_and_resolve(
        cast(Any, uow), worker, execution
    )

    assert isinstance(resolved, ResolvedWorkerRuntime)
    assert resolved.worker_id == worker.id
    assert uow.worker_runtime_configurations.single_calls == [
        (worker.id, execution.workspace_id, True)
    ]
    assert uow.modules.instance_calls == [
        (
            tuple(sorted((adapter_instance.id, workspace_instance.id))),
            execution.workspace_id,
            True,
        )
    ]
    assert uow.modules.definition_calls == [
        (
            tuple(sorted((adapter_definition.id, workspace_definition.id))),
            execution.workspace_id,
            True,
        )
    ]


def test_runtime_issue_details_never_include_module_config_or_secrets() -> None:
    fixture = make_fixture(adapter_provider="wrong")
    inspection, _ = inspect_fixture(fixture)

    assert inspection[0].issue is not None
    details = inspection[0].issue.reason_details
    assert "config" not in details
    assert "secret_refs" not in details
    assert "http://adapter.invalid" not in str(details)
    assert "adapter.api_key" not in str(details)


def test_forbidden_adapter_is_rejected_without_inspecting_protocol_tags() -> None:
    fixture = make_fixture()
    worker, _, configuration, adapter_instance, workspace_instance, adapter_definition, workspace_definition = fixture
    execution = make_execution(
        requirements=frozenset({"not-a-protocol-capability"}),
        adapter_forbidden=(worker.adapter_type,),
    )
    uow = make_uow(
        (configuration,),
        (adapter_instance, workspace_instance),
        (adapter_definition, workspace_definition),
    )

    inspection = WorkerRuntimeResolver().inspect_candidates(
        cast(Any, uow), (worker,), execution
    )

    assert inspection[0].resolved is None
    assert inspection[0].issue is not None
    assert inspection[0].issue.reason_code is SchedulingBlockReason.ADAPTER_POLICY_REJECTED
