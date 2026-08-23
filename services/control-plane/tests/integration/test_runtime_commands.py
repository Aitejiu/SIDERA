from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from sidera_control_plane.api.app import create_app
from sidera_control_plane.config import Settings
from sidera_control_plane.domain.modules import (
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
from sidera_control_plane.domain.worker import Worker
from sidera_control_plane.persistence.database import Database
from sidera_control_plane.persistence.models import (
    EventModel,
    ProjectionChangeModel,
    WorkspaceModel,
)
from sidera_control_plane.persistence.unit_of_work import SqlAlchemyUnitOfWork

pytestmark = pytest.mark.integration

NOW = datetime(2026, 8, 23, 12, 0, tzinfo=UTC)


@pytest.fixture
def client(database_url: str):
    settings = Settings(database_url=database_url)
    with TestClient(create_app(settings)) as test_client:
        yield test_client


@dataclass(frozen=True, slots=True)
class RuntimeResources:
    worker: Worker
    adapter_definition: ModuleDefinition
    workspace_definition: ModuleDefinition
    wrong_adapter_definition: ModuleDefinition
    adapter_instance: ModuleInstance
    alternate_adapter_instance: ModuleInstance
    workspace_instance: ModuleInstance
    wrong_adapter_instance: ModuleInstance


def _worker_capabilities() -> WorkerAdapterCapabilityProfile:
    return WorkerAdapterCapabilityProfile(
        input=CapabilityInput.QUEUED,
        idempotency=IdempotencyCapability.OPERATION_ID,
        resume=ResumeCapability.DURABLE,
        stop=StopCapability.EXACT_EXECUTION,
        sandbox=SandboxCapability.FILESYSTEM,
        stability=StabilityLevel.STABLE,
    )


def _workspace_capabilities() -> WorkspaceProviderCapabilityProfile:
    return WorkspaceProviderCapabilityProfile(
        isolation=SandboxCapability.ISOLATED_RUNTIME,
        writable=True,
        git_worktree=True,
        stability=StabilityLevel.STABLE,
    )


def _seed_runtime_resources(
    database: Database, workspace_id: UUID
) -> RuntimeResources:
    worker = Worker.register(
        worker_id=uuid4(),
        workspace_id=workspace_id,
        adapter_type="opencode",
        display_name="Runtime command worker",
        capabilities=frozenset({"python"}),
        capacity=1,
        cost_per_hour_microunits=100,
        startup_latency_ms=250,
        now=NOW,
    )
    adapter_definition = ModuleDefinition(
        id=uuid4(),
        workspace_id=workspace_id,
        module_type="worker_adapter",
        provider="opencode",
        interface_version="sidera.worker-adapter.v1",
        config_schema_version=1,
        capabilities=_worker_capabilities(),
        lifecycle=ModuleLifecycle.ACTIVE,
    )
    workspace_definition = ModuleDefinition(
        id=uuid4(),
        workspace_id=workspace_id,
        module_type="workspace_provider",
        provider="git-worktree",
        interface_version="sidera.workspace-provider.v1",
        config_schema_version=1,
        capabilities=_workspace_capabilities(),
        lifecycle=ModuleLifecycle.ACTIVE,
    )
    wrong_adapter_definition = ModuleDefinition(
        id=uuid4(),
        workspace_id=workspace_id,
        module_type="worker_adapter",
        provider="codex",
        interface_version="sidera.worker-adapter.v1",
        config_schema_version=1,
        capabilities=_worker_capabilities(),
        lifecycle=ModuleLifecycle.ACTIVE,
    )
    adapter_instance = _module_instance(
        workspace_id,
        adapter_definition.id,
        "adapter-opencode",
        "adapter.command",
    )
    alternate_adapter_instance = _module_instance(
        workspace_id,
        adapter_definition.id,
        "adapter-opencode-alternate",
        "adapter.command",
    )
    workspace_instance = _module_instance(
        workspace_id,
        workspace_definition.id,
        "workspace-git",
        "workspace.root",
    )
    wrong_adapter_instance = _module_instance(
        workspace_id,
        wrong_adapter_definition.id,
        "adapter-codex",
        "adapter.command",
    )
    with SqlAlchemyUnitOfWork(database) as uow:
        uow.workers.add(worker)
        uow.modules.add_definition(adapter_definition)
        uow.modules.add_definition(workspace_definition)
        uow.modules.add_definition(wrong_adapter_definition)
        uow.modules.add_instance(adapter_instance)
        uow.modules.add_instance(alternate_adapter_instance)
        uow.modules.add_instance(workspace_instance)
        uow.modules.add_instance(wrong_adapter_instance)
    return RuntimeResources(
        worker=worker,
        adapter_definition=adapter_definition,
        workspace_definition=workspace_definition,
        wrong_adapter_definition=wrong_adapter_definition,
        adapter_instance=adapter_instance,
        alternate_adapter_instance=alternate_adapter_instance,
        workspace_instance=workspace_instance,
        wrong_adapter_instance=wrong_adapter_instance,
    )


def _module_instance(
    workspace_id: UUID,
    definition_id: UUID,
    name: str,
    config_key: str,
) -> ModuleInstance:
    namespace = config_key.split(".", 1)[0]
    return ModuleInstance(
        id=uuid4(),
        workspace_id=workspace_id,
        definition_id=definition_id,
        instance_name=name,
        config_version=1,
        config_namespace=namespace,
        config={config_key: name},
        secret_refs=(),
        lifecycle=ModuleLifecycle.ACTIVE,
    )


def _configure_request(
    workspace_id: UUID,
    worker_id: UUID,
    *,
    adapter_instance_id: UUID,
    workspace_instance_id: UUID,
    expected_version: int | None,
    idempotency_key: str,
    adapter_config_version: int = 1,
    workspace_config_version: int = 1,
) -> dict[str, Any]:
    return {
        "command_id": str(uuid4()),
        "workspace_id": str(workspace_id),
        "command_type": "ConfigureWorkerRuntime",
        "target_type": "worker_runtime_configuration",
        "actor": {"type": "user", "id": "user:runtime"},
        "target_id": str(worker_id),
        "expected_version": expected_version,
        "idempotency_key": idempotency_key,
        "correlation_id": str(uuid4()),
        "payload": {
            "adapter_module_instance_id": str(adapter_instance_id),
            "adapter_module_config_version": adapter_config_version,
            "workspace_module_instance_id": str(workspace_instance_id),
            "workspace_module_config_version": workspace_config_version,
        },
    }


def _lifecycle_request(
    workspace_id: UUID,
    target_type: str,
    target_id: UUID,
    expected_version: int | None,
    lifecycle: ModuleLifecycle,
    idempotency_key: str,
) -> dict[str, Any]:
    return {
        "command_id": str(uuid4()),
        "workspace_id": str(workspace_id),
        "command_type": "SetModuleLifecycle",
        "target_type": target_type,
        "actor": {"type": "user", "id": "user:runtime"},
        "target_id": str(target_id),
        "expected_version": expected_version,
        "idempotency_key": idempotency_key,
        "correlation_id": str(uuid4()),
        "payload": {"lifecycle": lifecycle.value},
    }


def _runtime_configuration(
    database: Database, worker_id: UUID, workspace_id: UUID
):
    with SqlAlchemyUnitOfWork(database) as uow:
        return uow.worker_runtime_configurations.get_for_worker(
            worker_id, workspace_id
        )


def test_configure_worker_runtime_creates_version_one_without_mutating_worker(
    client: TestClient,
    database: Database,
    workspace_id: UUID,
) -> None:
    resources = _seed_runtime_resources(database, workspace_id)
    request = _configure_request(
        workspace_id,
        resources.worker.id,
        adapter_instance_id=resources.adapter_instance.id,
        workspace_instance_id=resources.workspace_instance.id,
        expected_version=None,
        idempotency_key="runtime-create-001",
    )

    response = client.post("/api/v1/commands", json=request)

    assert response.status_code == 201, response.text
    assert response.json()["aggregate_version"] == 1
    with SqlAlchemyUnitOfWork(database) as uow:
        configuration = uow.worker_runtime_configurations.get_for_worker(
            resources.worker.id, workspace_id
        )
        worker = uow.workers.get(resources.worker.id, workspace_id)
    assert configuration is not None
    assert configuration.version == 1
    assert configuration.adapter_module_instance_id == resources.adapter_instance.id
    assert worker is not None
    assert worker.version == resources.worker.version == 1
    with database.session() as session:
        event = session.scalar(
            select(EventModel).where(EventModel.event_type == "WorkerRuntimeConfigured")
        )
    assert event is not None
    assert event.aggregate_type == "worker_runtime_configuration"
    assert event.aggregate_id == resources.worker.id
    assert event.aggregate_version == 1
    assert event.payload["configuration_version"] == 1


def test_configure_worker_runtime_is_idempotent_and_does_not_duplicate_event(
    client: TestClient,
    database: Database,
    workspace_id: UUID,
    row_counts,
) -> None:
    resources = _seed_runtime_resources(database, workspace_id)
    request = _configure_request(
        workspace_id,
        resources.worker.id,
        adapter_instance_id=resources.adapter_instance.id,
        workspace_instance_id=resources.workspace_instance.id,
        expected_version=None,
        idempotency_key="runtime-create-replay-001",
    )

    first = client.post("/api/v1/commands", json=request)
    replay = client.post(
        "/api/v1/commands",
        json={**request, "command_id": str(uuid4()), "correlation_id": str(uuid4())},
    )

    assert first.status_code == 201, first.text
    assert replay.status_code == 200, replay.text
    assert replay.json() == first.json()
    assert row_counts()["commands"] == 1
    assert row_counts()["events"] == 1


def test_configure_worker_runtime_requires_expected_version_for_update_and_uses_occ(
    client: TestClient,
    database: Database,
    workspace_id: UUID,
) -> None:
    resources = _seed_runtime_resources(database, workspace_id)
    create = _configure_request(
        workspace_id,
        resources.worker.id,
        adapter_instance_id=resources.adapter_instance.id,
        workspace_instance_id=resources.workspace_instance.id,
        expected_version=None,
        idempotency_key="runtime-occ-create-001",
    )
    assert client.post("/api/v1/commands", json=create).status_code == 201

    missing_expected = _configure_request(
        workspace_id,
        resources.worker.id,
        adapter_instance_id=resources.alternate_adapter_instance.id,
        workspace_instance_id=resources.workspace_instance.id,
        expected_version=None,
        idempotency_key="runtime-occ-missing-expected-001",
    )
    rejected = client.post("/api/v1/commands", json=missing_expected)
    assert rejected.status_code == 422
    assert rejected.json()["error"]["code"] == "validation_error"

    update = _configure_request(
        workspace_id,
        resources.worker.id,
        adapter_instance_id=resources.alternate_adapter_instance.id,
        workspace_instance_id=resources.workspace_instance.id,
        expected_version=1,
        idempotency_key="runtime-occ-update-001",
    )
    updated = client.post("/api/v1/commands", json=update)
    assert updated.status_code == 200, updated.text
    assert updated.json()["aggregate_version"] == 2

    stale = _configure_request(
        workspace_id,
        resources.worker.id,
        adapter_instance_id=resources.adapter_instance.id,
        workspace_instance_id=resources.workspace_instance.id,
        expected_version=1,
        idempotency_key="runtime-occ-stale-001",
    )
    stale_response = client.post("/api/v1/commands", json=stale)
    assert stale_response.status_code == 409
    assert stale_response.json()["error"]["code"] == "version_conflict"
    configuration = _runtime_configuration(database, resources.worker.id, workspace_id)
    assert configuration is not None
    assert configuration.version == 2


@pytest.mark.parametrize(
    ("payload_field", "payload_value", "expected_reason"),
    [
        (
            "workspace_module_instance_id",
            "adapter_instance",
            "workspace_module_unavailable",
        ),
        (
            "adapter_module_instance_id",
            "wrong_adapter_instance",
            "adapter_module_unavailable",
        ),
    ],
)
def test_configure_worker_runtime_rejects_module_type_and_provider_mismatch(
    client: TestClient,
    database: Database,
    workspace_id: UUID,
    payload_field: str,
    payload_value: str,
    expected_reason: str,
) -> None:
    resources = _seed_runtime_resources(database, workspace_id)
    values = {
        "adapter_module_instance_id": resources.adapter_instance.id,
        "workspace_module_instance_id": resources.workspace_instance.id,
    }
    values[payload_field] = getattr(resources, payload_value).id
    request = _configure_request(
        workspace_id,
        resources.worker.id,
        adapter_instance_id=values["adapter_module_instance_id"],
        workspace_instance_id=values["workspace_module_instance_id"],
        expected_version=None,
        idempotency_key=f"runtime-invalid-{payload_field}",
    )

    response = client.post("/api/v1/commands", json=request)

    assert response.status_code == 422, response.text
    assert response.json()["error"]["code"] == "validation_error"
    assert response.json()["error"]["details"]["reason_code"] == expected_reason


def test_configure_worker_runtime_rejects_config_version_and_inactive_instance(
    client: TestClient,
    database: Database,
    workspace_id: UUID,
) -> None:
    resources = _seed_runtime_resources(database, workspace_id)
    version_mismatch = _configure_request(
        workspace_id,
        resources.worker.id,
        adapter_instance_id=resources.adapter_instance.id,
        workspace_instance_id=resources.workspace_instance.id,
        expected_version=None,
        idempotency_key="runtime-invalid-version-001",
        adapter_config_version=2,
    )
    mismatch_response = client.post("/api/v1/commands", json=version_mismatch)
    assert mismatch_response.status_code == 422
    assert (
        mismatch_response.json()["error"]["details"]["reason_code"]
        == "runtime_configuration_invalid"
    )

    with SqlAlchemyUnitOfWork(database) as uow:
        current = uow.modules.get_instance(
            resources.adapter_instance.id, workspace_id
        )
        assert current is not None
        uow.modules.save_instance(
            current.with_lifecycle(ModuleLifecycle.DRAINING),
            current.resource_version,
        )

    inactive = _configure_request(
        workspace_id,
        resources.worker.id,
        adapter_instance_id=resources.adapter_instance.id,
        workspace_instance_id=resources.workspace_instance.id,
        expected_version=None,
        idempotency_key="runtime-invalid-lifecycle-001",
    )
    inactive_response = client.post("/api/v1/commands", json=inactive)
    assert inactive_response.status_code == 422
    assert (
        inactive_response.json()["error"]["details"]["reason_code"]
        == "adapter_module_unavailable"
    )


def test_configure_worker_runtime_is_tenant_safe_and_requires_existing_worker(
    client: TestClient,
    database: Database,
    workspace_id: UUID,
) -> None:
    resources = _seed_runtime_resources(database, workspace_id)
    foreign_workspace_id = uuid4()
    foreign_worker = Worker.register(
        worker_id=uuid4(),
        workspace_id=foreign_workspace_id,
        adapter_type="opencode",
        display_name="Foreign runtime worker",
        capabilities=frozenset({"python"}),
        capacity=1,
        cost_per_hour_microunits=1,
        startup_latency_ms=1,
        now=NOW,
    )
    with database.session() as session:
        session.add(
            WorkspaceModel(
                id=foreign_workspace_id,
                name="Foreign workspace",
                status="active",
                version=1,
            )
        )
        session.flush()
        session.add(
            # Only the foreign worker is needed to prove the composite tenant filter.
            # Its runtime modules are intentionally absent.
            _worker_model(foreign_worker)
        )
        session.commit()

    request = _configure_request(
        workspace_id,
        foreign_worker.id,
        adapter_instance_id=resources.adapter_instance.id,
        workspace_instance_id=resources.workspace_instance.id,
        expected_version=None,
        idempotency_key="runtime-cross-workspace-001",
    )
    response = client.post("/api/v1/commands", json=request)
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "not_found"

    missing = _configure_request(
        workspace_id,
        uuid4(),
        adapter_instance_id=resources.adapter_instance.id,
        workspace_instance_id=resources.workspace_instance.id,
        expected_version=None,
        idempotency_key="runtime-missing-worker-001",
    )
    missing_response = client.post("/api/v1/commands", json=missing)
    assert missing_response.status_code == 404


def _worker_model(worker: Worker):
    from sidera_control_plane.persistence.models import WorkerModel

    return WorkerModel(
        id=worker.id,
        workspace_id=worker.workspace_id,
        adapter_type=worker.adapter_type,
        display_name=worker.display_name,
        status=worker.status.value,
        desired_state=worker.desired_state.value,
        capabilities=sorted(worker.capabilities),
        capacity=worker.capacity,
        current_load=worker.current_load,
        cost_per_hour_microunits=worker.cost_per_hour_microunits,
        startup_latency_ms=worker.startup_latency_ms,
        last_heartbeat_at=worker.last_heartbeat_at,
        version=worker.version,
    )


@pytest.mark.parametrize("target_type", ("module_definition", "module_instance"))
def test_set_module_lifecycle_supports_allowed_transitions_and_disabled_terminal_state(
    client: TestClient,
    database: Database,
    workspace_id: UUID,
    target_type: str,
) -> None:
    resources = _seed_runtime_resources(database, workspace_id)
    target_id = (
        resources.adapter_definition.id
        if target_type == "module_definition"
        else resources.adapter_instance.id
    )
    transitions = (
        (ModuleLifecycle.DRAINING, 1, 2),
        (ModuleLifecycle.ACTIVE, 2, 3),
        (ModuleLifecycle.DISABLED, 3, 4),
    )
    for index, (lifecycle, expected, aggregate_version) in enumerate(transitions):
        response = client.post(
            "/api/v1/commands",
            json=_lifecycle_request(
                workspace_id,
                target_type,
                target_id,
                expected,
                lifecycle,
                f"runtime-lifecycle-{target_type}-{index}",
            ),
        )
        assert response.status_code == 200, response.text
        assert response.json()["aggregate_version"] == aggregate_version

    terminal = client.post(
        "/api/v1/commands",
        json=_lifecycle_request(
            workspace_id,
            target_type,
            target_id,
            4,
            ModuleLifecycle.ACTIVE,
            f"runtime-lifecycle-terminal-{target_type}",
        ),
    )
    assert terminal.status_code == 409
    assert terminal.json()["error"]["code"] == "invalid_transition"

    with SqlAlchemyUnitOfWork(database) as uow:
        resource = (
            uow.modules.get_definition(target_id, workspace_id)
            if target_type == "module_definition"
            else uow.modules.get_instance(target_id, workspace_id)
        )
    assert resource is not None
    assert resource.lifecycle is ModuleLifecycle.DISABLED
    assert resource.resource_version == 4


def test_set_module_lifecycle_uses_resource_occ_event_version_and_projection_invalidation(
    client: TestClient,
    database: Database,
    workspace_id: UUID,
) -> None:
    resources = _seed_runtime_resources(database, workspace_id)
    request = _lifecycle_request(
        workspace_id,
        "module_definition",
        resources.adapter_definition.id,
        1,
        ModuleLifecycle.DRAINING,
        "runtime-lifecycle-event-001",
    )
    response = client.post("/api/v1/commands", json=request)
    assert response.status_code == 200, response.text

    stale = client.post(
        "/api/v1/commands",
        json={
            **_lifecycle_request(
                workspace_id,
                "module_definition",
                resources.adapter_definition.id,
                1,
                ModuleLifecycle.ACTIVE,
                "runtime-lifecycle-stale-001",
            ),
        },
    )
    assert stale.status_code == 409
    assert stale.json()["error"]["code"] == "version_conflict"

    with database.session() as session:
        event = session.scalar(
            select(EventModel).where(EventModel.event_type == "ModuleLifecycleChanged")
        )
        changes = session.scalars(
            select(ProjectionChangeModel).where(
                ProjectionChangeModel.change_type
                == "module.definition_lifecycle_changed",
                ProjectionChangeModel.resource_id.is_(None),
            )
        ).all()
    assert event is not None
    assert event.aggregate_type == "module_definition"
    assert event.aggregate_id == resources.adapter_definition.id
    assert event.aggregate_version == 2
    assert event.payload == {
        "previous_lifecycle": "active",
        "lifecycle": "draining",
        "previous_resource_version": 1,
        "resource_version": 2,
    }
    assert changes
    assert changes[-1].resource_version == 2


def test_runtime_command_schema_restricts_lifecycle_target_and_required_versions(
    client: TestClient,
    workspace_id: UUID,
) -> None:
    base = {
        "command_id": str(uuid4()),
        "workspace_id": str(workspace_id),
        "command_type": "SetModuleLifecycle",
        "target_type": "worker",
        "actor": {"type": "user", "id": "user:runtime"},
        "target_id": str(uuid4()),
        "expected_version": 1,
        "idempotency_key": "runtime-schema-001",
        "correlation_id": str(uuid4()),
        "payload": {"lifecycle": "active"},
    }
    invalid_target = client.post("/api/v1/commands", json=base)
    missing_version = client.post(
        "/api/v1/commands",
        json={**base, "command_id": str(uuid4()), "expected_version": None},
    )
    assert invalid_target.status_code == 422
    assert missing_version.status_code == 422
