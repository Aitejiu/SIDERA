"""Small deterministic runtime activator for the R6.1 Fake vertical gate."""

from __future__ import annotations

from datetime import datetime, timedelta
from uuid import NAMESPACE_URL, UUID, uuid5

from sidera_control_plane.application.command_service import hash_request
from sidera_control_plane.domain.commands import CommandEnvelope, CommandResult
from sidera_control_plane.domain.context_packages import (
    ContextAcknowledgement,
    ContextConsumerType,
    ContextPurpose,
)
from sidera_control_plane.domain.execution import ExecutionSession
from sidera_control_plane.domain.lease import AssignmentLease
from sidera_control_plane.domain.modules import (
    CapabilityInput,
    ExecutionBinding,
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


def activate_fake_target(
    uow_factory,
    execution_id: UUID,
    worker_item_id: UUID,
    workspace_id: UUID,
    *,
    now: datetime,
) -> UUID:
    """Make an already materialized target observable to the coordinator."""
    ids = {
        label: uuid5(NAMESPACE_URL, f"sidera.r6-1.fake-target:{execution_id}:{label}")
        for label in (
            "worker",
            "lease",
            "session",
            "adapter-definition",
            "workspace-definition",
            "adapter-instance",
            "workspace-instance",
            "ack-operation",
            "ack-command",
        )
    }
    capabilities = _worker_capabilities()
    with uow_factory() as uow:
        execution = uow.executions.get(execution_id, workspace_id, for_update=True)
        worker_item = uow.worker_items.get(
            worker_item_id, workspace_id, for_update=True
        )
        if execution is None or worker_item is None:
            raise AssertionError("fake target execution and WorkerItem are required")
        package = uow.context_packages.get_for_consumer(
            ContextPurpose.EXECUTION,
            execution.id,
            workspace_id,
        )
        if package is None:
            raise AssertionError("fake target context package is required")

        worker = Worker.register(
            worker_id=ids["worker"],
            workspace_id=workspace_id,
            adapter_type="fake",
            display_name="R6.1 Fake Target Worker",
            capabilities=frozenset({"code"}),
            capacity=1,
            cost_per_hour_microunits=0,
            startup_latency_ms=0,
            now=now,
        )
        worker.reserve()
        worker.mark_running()
        lease = AssignmentLease.issue(
            lease_id=ids["lease"],
            workspace_id=workspace_id,
            execution_id=execution.id,
            worker_id=worker.id,
            issued_at=now,
            expires_at=now + timedelta(hours=1),
            fencing_token=1,
            assignment_reason={"fixture": "r6.1-fake-target"},
        )
        previous_execution_version = execution.version
        execution.assign(
            lease_id=lease.id,
            worker_id=worker.id,
            fencing_token=lease.fencing_token,
        )
        execution.start(
            lease_id=lease.id,
            fencing_token=lease.fencing_token,
            now=now,
        )

        adapter_definition = ModuleDefinition(
            id=ids["adapter-definition"],
            workspace_id=workspace_id,
            module_type="worker_adapter",
            provider="fake",
            interface_version="sidera.worker-adapter.v1",
            config_schema_version=1,
            capabilities=capabilities,
            lifecycle=ModuleLifecycle.ACTIVE,
        )
        workspace_definition = ModuleDefinition(
            id=ids["workspace-definition"],
            workspace_id=workspace_id,
            module_type="workspace_provider",
            provider="fake-workspace",
            interface_version="sidera.workspace-provider.v1",
            config_schema_version=1,
            capabilities=WorkspaceProviderCapabilityProfile(
                isolation=SandboxCapability.ISOLATED_RUNTIME,
                writable=True,
                git_worktree=True,
                stability=StabilityLevel.STABLE,
            ),
            lifecycle=ModuleLifecycle.ACTIVE,
        )
        adapter_instance = ModuleInstance(
            id=ids["adapter-instance"],
            workspace_id=workspace_id,
            definition_id=adapter_definition.id,
            instance_name="r6-1-fake-target-adapter",
            config_version=1,
            config_namespace="adapter",
            config={"adapter.mode": "deterministic-test"},
            secret_refs=(),
            lifecycle=ModuleLifecycle.ACTIVE,
        )
        workspace_instance = ModuleInstance(
            id=ids["workspace-instance"],
            workspace_id=workspace_id,
            definition_id=workspace_definition.id,
            instance_name="r6-1-fake-target-workspace",
            config_version=1,
            config_namespace="workspace",
            config={"workspace.mode": "deterministic-test"},
            secret_refs=(),
            lifecycle=ModuleLifecycle.ACTIVE,
        )
        binding = ExecutionBinding(
            id=uuid5(NAMESPACE_URL, f"sidera.r6-1.fake-target:{execution.id}:binding"),
            workspace_id=workspace_id,
            execution_id=execution.id,
            worker_id=worker.id,
            adapter_module_id=adapter_instance.id,
            workspace_module_id=workspace_instance.id,
            adapter_config_version=adapter_instance.config_version,
            workspace_config_version=workspace_instance.config_version,
            capability_snapshot=capabilities,
            lease_id=lease.id,
            fencing_token=lease.fencing_token,
            version=1,
        )
        if not worker_item.context_is_synchronized:
            previous_worker_item_version = worker_item.version
            worker_item.acknowledge_context(
                expected_context_version=worker_item.context_version,
                now=now,
            )
            uow.worker_items.save(worker_item, previous_worker_item_version)

        uow.modules.add_definition(adapter_definition)
        uow.modules.add_definition(workspace_definition)
        uow.modules.add_instance(adapter_instance)
        uow.modules.add_instance(workspace_instance)
        uow.workers.add(worker)
        uow.session.flush()
        uow.leases.add(lease)
        uow.bindings.add(binding)
        uow.executions.save(execution, previous_execution_version)
        uow.sessions.add(
            ExecutionSession.start(
                session_id=ids["session"],
                workspace_id=workspace_id,
                execution_id=execution.id,
                worker_id=worker.id,
                adapter_session_id="r6-1-fake-target-session",
                now=now,
                adapter_module_id=adapter_instance.id,
                adapter_config_version=adapter_instance.config_version,
                workspace_module_id=workspace_instance.id,
                workspace_config_version=workspace_instance.config_version,
                capability_snapshot=capabilities,
                process_ref="fake-process:r6-1-target",
                workspace_ref=workspace_instance.instance_name,
            )
        )
        acknowledgement = ContextAcknowledgement(
            package_id=package.package_id,
            package_hash=package.package_hash,
            consumer_type=ContextConsumerType.WORKER,
            consumer_id=execution.id,
            context_version=package.context_version,
            adapter_module_id=adapter_instance.id,
            adapter_config_version=adapter_instance.config_version,
            acknowledged_at=now,
            operation_id=ids["ack-operation"],
        )
        command = CommandEnvelope(
            id=ids["ack-command"],
            workspace_id=workspace_id,
            command_type="R6FixtureAcknowledgeTargetContext",
            actor_type="system",
            actor_id="r6-1-fake-runtime",
            target_type="context_package",
            target_id=package.package_id,
            payload={
                "execution_id": str(execution.id),
                "package_id": str(package.package_id),
                "package_hash": package.package_hash,
            },
            expected_version=None,
            idempotency_key=f"r6-1-fake-target-context:{execution.id}",
            correlation_id=execution.id,
        )
        uow.commands.record_applied(
            command,
            hash_request(command),
            CommandResult(
                command_id=command.id,
                aggregate_id=package.package_id,
                aggregate_version=1,
                event_ids=(),
            ),
        )
        uow.context_packages.add_ack(
            acknowledgement,
            actor_type="system",
            actor_id="r6-1-fake-runtime",
            causation_id=command.id,
            correlation_id=execution.id,
        )
    return ids["session"]


def _worker_capabilities() -> WorkerAdapterCapabilityProfile:
    return WorkerAdapterCapabilityProfile(
        input=CapabilityInput.QUEUED,
        idempotency=IdempotencyCapability.OPERATION_ID,
        resume=ResumeCapability.DURABLE,
        stop=StopCapability.EXACT_EXECUTION,
        sandbox=SandboxCapability.FILESYSTEM,
        stability=StabilityLevel.STABLE,
        context_package_versions=(1,),
        context_patch_versions=(1,),
    )
