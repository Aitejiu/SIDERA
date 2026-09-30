"""Seed the deterministic R6.2 G4 handoff fixture."""

from __future__ import annotations

import argparse
import json
import os
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import cast
from urllib.parse import urlsplit
from uuid import NAMESPACE_URL, UUID, uuid5

from sqlalchemy import delete, select, text, update

from sidera_control_plane.adapters.fake_worker import FakeWorkerAdapter
from sidera_control_plane.application.command_service import hash_request
from sidera_control_plane.application.ports import WorkerItemPlacement
from sidera_control_plane.domain.commands import CommandResult
from sidera_control_plane.domain.context_packages import (
    ContextAcknowledgement,
    ContextConsumerType,
)
from sidera_control_plane.domain.execution import Execution, ExecutionSession
from sidera_control_plane.domain.execution_requests import (
    DependencyEnforcement,
    ExecutionRequest,
    ExecutionRequestDependency,
)
from sidera_control_plane.domain.gateway import GatewayOperation, GatewayOperationType
from sidera_control_plane.domain.handoffs import HandoffTransferMode
from sidera_control_plane.domain.lease import AssignmentLease
from sidera_control_plane.domain.modules import (
    ExecutionBinding,
    ModuleDefinition,
    ModuleInstance,
    ModuleLifecycle,
    PlannerAdapterCapabilityProfile,
    SandboxCapability,
    StabilityLevel,
    WorkspaceProviderCapabilityProfile,
)
from sidera_control_plane.domain.planning import PlanningRequest, PlanningUsage
from sidera_control_plane.domain.plans import (
    DependencyEnforcement as PlanDependencyEnforcement,
)
from sidera_control_plane.domain.plans import (
    DependencyKind,
    SubtaskDependency,
    SubtaskPlanItem,
    TaskPlanSnapshot,
    WorkerItemPlanItem,
    WorkItemKind,
)
from sidera_control_plane.domain.task import Task
from sidera_control_plane.domain.worker import Worker
from sidera_control_plane.domain.worker_item import WorkerItem
from sidera_control_plane.domain.worker_runtime import WorkerRuntimeConfiguration
from sidera_control_plane.domain.workspace import (
    ExecutionWorkspaceManifest,
    WorkspaceCheckout,
)
from sidera_control_plane.persistence.database import Database
from sidera_control_plane.persistence.models import (
    Base,
    ContextPackageModel,
    EventModel,
    ExecutionWorkspaceModel,
    OutboxMessageModel,
    RootTaskPlanHeadModel,
    WorkspaceModel,
)
from sidera_control_plane.persistence.unit_of_work import SqlAlchemyUnitOfWork
from tests.e2e.seed_r6_1_revision import (
    _active_worker_context,
    _internal_command,
    _planning_profile,
)

NOW = datetime(2026, 9, 2, 12, 0, tzinfo=UTC)
FIXTURE_NAMESPACE = UUID("f6200000-0000-0000-0000-000000000001")
WORKSPACE_NAME = "SIDERA R6.2 G4 handoff workspace"


class G4Scenario(StrEnum):
    NORMAL = "normal"
    TARGET_START_UNKNOWN = "target-start-unknown"


@dataclass(frozen=True, slots=True)
class G4Fixture:
    workspace_id: UUID
    root_task_id: UUID
    subtask_ids: Mapping[str, UUID]
    worker_item_ids: Mapping[str, UUID]
    worker_a_execution_id: UUID
    source_execution_id: UUID
    source_session_id: UUID
    source_manifest_id: UUID
    source_fencing_token: int
    source_package_id: UUID
    source_package_hash: str
    source_context_version: int
    delivered_input_id: UUID
    queued_input_id: UUID
    source_request_id: UUID
    request_c_id: UUID
    target_worker_ids: tuple[UUID, UUID]
    adapter_instance_id: UUID
    workspace_instance_id: UUID
    plan_snapshot_id: UUID
    plan_generation: int
    plan_head_hash: str
    planning_request_id: UUID
    planning_profile_id: UUID
    adapter_contract: str
    workspace_contract: str
    scenario: G4Scenario


def seed_g4_fixture(
    database: Database,
    workspace_id: UUID,
    *,
    scenario: G4Scenario | str = G4Scenario.NORMAL,
    now: datetime = NOW,
    reset: bool = False,
) -> G4Fixture:
    """Create one exact G4 fixture without invoking a worker or provider."""

    scenario = G4Scenario(scenario)
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("fixture timestamp must be timezone-aware")
    ids = _ids(workspace_id)
    _ensure_workspace(database, workspace_id, now=now, reset=reset)

    root_task = Task.create(
        task_id=ids["root_task_id"],
        workspace_id=workspace_id,
        title="G4 cross-agent handoff control room",
        description="Preserve the task while moving one WorkerItem to another agent.",
        acceptance_criteria=("handoff preserves WorkerItem identity",),
        priority=50,
        requested_by="r6.2-e2e",
    )
    root_task.mark_ready()
    root_task.mark_running()
    subtasks = {
        key: _subtask(ids[f"subtask_{key}_id"], workspace_id, key, running=key != "c", now=now)
        for key in ("a", "b", "c")
    }
    items = {
        key: WorkerItem.create(
            item_id=ids[f"worker_{key}_item_id"],
            workspace_id=workspace_id,
            root_task_id=root_task.id,
            stable_key=f"worker-{key}",
            title=f"Worker {key.upper()}",
            responsibility=(
                "Independently validate the parallel branch."
                if key == "a"
                else "Own the source branch and continue its implementation."
                if key == "b"
                else "Integrate B only after B has actually completed."
            ),
            now=now,
        )
        for key in ("a", "b", "c")
    }

    source_worker = _worker(ids["source_worker_id"], workspace_id, "Source Agent B", now, cost=30)
    worker_a = _worker(ids["worker_a_id"], workspace_id, "Independent Worker A", now, cost=25)
    target_one = _worker(ids["target_one_worker_id"], workspace_id, "Target Agent One", now, cost=10)
    target_two = _worker(ids["target_two_worker_id"], workspace_id, "Target Agent Two", now, cost=20)
    source_worker.reserve()
    source_worker.mark_running()
    worker_a.reserve()
    worker_a.mark_running()

    snapshot = _snapshot(
        workspace_id=workspace_id,
        root_task_id=root_task.id,
        subtasks=subtasks,
        items=items,
        snapshot_id=ids["plan_snapshot_id"],
        now=now,
    )
    planner_definition, planner_instance, profile = _planning_profile(
        workspace_id,
        {
            "planner_definition_id": ids["planner_definition_id"],
            "planner_instance_id": ids["planner_instance_id"],
            "planning_profile_id": ids["planning_profile_id"],
        },
        now,
    )
    planning_request = PlanningRequest.create(
        request_id=ids["planning_request_id"],
        workspace_id=workspace_id,
        profile_snapshot=profile.snapshot(
            cast(PlannerAdapterCapabilityProfile, planner_definition.capabilities)
        ),
        objective="Move the source WorkerItem to a compatible agent without changing the plan.",
        constraints=("Keep the original WorkerItem and plan generation stable.",),
        acceptance_criteria=("the handoff can be inspected and recovered",),
        context_refs=(),
        requested_outputs=("handoff",),
        source_type="r6.2-e2e",
        source_ref="fake-g4",
        actor="r6.2-e2e",
        correlation_id=ids["planning_correlation_id"],
        run_id=ids["planning_run_id"],
        now=now,
    )
    planning_lease_id = uuid5(
        FIXTURE_NAMESPACE,
        f"{planning_request.id}:planning-lease",
    )
    planning_request.start_planning(
        run_id=planning_request.current_run_id,
        lease_id=planning_lease_id,
        fencing_token=1,
        now=now,
    )
    planning_request.succeed_run(
        run_id=planning_request.current_run_id,
        lease_id=planning_lease_id,
        fencing_token=1,
        provider_ref="fake://r6-2-g4-planner",
        structured_result_type="sidera.plan.v1",
        usage=PlanningUsage.zero(),
        now=now,
    )
    planning_request.mark_proposal_ready(
        run_id=planning_request.current_run_id,
        proposal_revision=1,
        content_hash="f" * 64,
        now=now,
    )
    planning_request.materialize(
        proposal_revision=1,
        content_hash="f" * 64,
        now=now,
    )

    adapter_definition = ModuleDefinition(
        id=ids["adapter_definition_id"],
        workspace_id=workspace_id,
        module_type="worker_adapter",
        provider="fake",
        interface_version="sidera.worker-adapter.handoff.v1",
        config_schema_version=1,
        capabilities=FakeWorkerAdapter.capabilities(),
        lifecycle=ModuleLifecycle.ACTIVE,
    )
    adapter_instance = ModuleInstance(
        id=ids["adapter_instance_id"],
        workspace_id=workspace_id,
        definition_id=adapter_definition.id,
        instance_name="r6-2-fake-handoff-adapter",
        config_version=1,
        config_namespace="adapter",
        config={"adapter.mode": "r6-2-deterministic-handoff"},
        secret_refs=(),
        lifecycle=ModuleLifecycle.ACTIVE,
    )
    workspace_definition = ModuleDefinition(
        id=ids["workspace_definition_id"],
        workspace_id=workspace_id,
        module_type="workspace_provider",
        provider="fake-workspace",
        interface_version="sidera.workspace-provider.reuse-exact.v1",
        config_schema_version=1,
        capabilities=WorkspaceProviderCapabilityProfile(
            isolation=SandboxCapability.ISOLATED_RUNTIME,
            writable=True,
            git_worktree=True,
            stability=StabilityLevel.STABLE,
            workspace_handoff_versions=(1,),
            workspace_transfer_modes=(HandoffTransferMode.REUSE_EXACT,),
            handoff_inspection=True,
        ),
        lifecycle=ModuleLifecycle.ACTIVE,
    )
    workspace_instance = ModuleInstance(
        id=ids["workspace_instance_id"],
        workspace_id=workspace_id,
        definition_id=workspace_definition.id,
        instance_name="r6-2-fake-reuse-exact-workspace",
        config_version=1,
        config_namespace="workspace",
        config={
            "workspace.mode": "reuse_exact",
            "workspace.repository_ref": "fake-repository://g4",
            "workspace.base_revision": "a" * 40,
        },
        secret_refs=(),
        lifecycle=ModuleLifecycle.ACTIVE,
    )

    source_active = _active_execution(
        execution_id=ids["source_execution_id"],
        session_id=ids["source_session_id"],
        lease_id=ids["source_lease_id"],
        worker_id=source_worker.id,
        worker_item=items["b"],
        task=subtasks["b"],
        workspace_id=workspace_id,
        adapter_instance=adapter_instance,
        workspace_instance=workspace_instance,
        now=now,
    )
    # The G4 source must be writable so the browser vertical can exercise the
    # exact WorkerItem intervention and handoff entry point.
    source_active.session.input_mode = "queued"
    # The fake adapter's initial session.started event is represented by this
    # persisted sequence, so the first handoff stop event remains contiguous.
    source_active.session.record_activity(sequence=1, now=now)
    independent_active = _active_execution(
        execution_id=ids["worker_a_execution_id"],
        session_id=ids["worker_a_session_id"],
        lease_id=ids["worker_a_lease_id"],
        worker_id=worker_a.id,
        worker_item=items["a"],
        task=subtasks["a"],
        workspace_id=workspace_id,
        adapter_instance=adapter_instance,
        workspace_instance=workspace_instance,
        now=now,
    )
    source_manifest = ExecutionWorkspaceManifest.create(
        id=ids["source_manifest_id"],
        workspace_id=workspace_id,
        task_id=subtasks["b"].id,
        execution_id=source_active.execution.id,
        checkouts=(
            WorkspaceCheckout(
                repository_ref="fake-repository://g4",
                base_revision="a" * 40,
                branch="sidera/g4-source",
                current_head="a" * 40,
            ),
        ),
        location_ref="fake-workspace://r6-2/g4/source",
        retention_seconds=3600,
        fencing_token=1,
        created_at=now,
    )
    source_manifest.mark_preparing()
    source_manifest.mark_ready()
    source_manifest.mark_active()

    source_request = _execution_request(
        request_id=ids["source_request_id"],
        planning_request=planning_request,
        proposal_id=ids["proposal_id"],
        snapshot=snapshot,
        root_task=root_task,
        subtask=subtasks["b"],
        item=items["b"],
        now=now,
    )
    source_request.mark_ready(now=now)
    source_request.materialize(
        execution_lineage_id=source_active.execution.lineage_id,
        first_execution_id=source_active.execution.id,
        now=now,
    )
    request_a = _execution_request(
        request_id=ids["request_a_id"],
        planning_request=planning_request,
        proposal_id=ids["proposal_id"],
        snapshot=snapshot,
        root_task=root_task,
        subtask=subtasks["a"],
        item=items["a"],
        now=now,
    )
    request_a.mark_ready(now=now)
    request_a.materialize(
        execution_lineage_id=independent_active.execution.lineage_id,
        first_execution_id=independent_active.execution.id,
        now=now,
    )
    request_c = _execution_request(
        request_id=ids["request_c_id"],
        planning_request=planning_request,
        proposal_id=ids["proposal_id"],
        snapshot=snapshot,
        root_task=root_task,
        subtask=subtasks["c"],
        item=items["c"],
        now=now,
    )
    request_c.mark_ready(now=now)

    with SqlAlchemyUnitOfWork(database) as uow:
        uow.modules.add_definition(planner_definition)
        uow.modules.add_instance(planner_instance)
        uow.modules.add_definition(adapter_definition)
        uow.modules.add_instance(adapter_instance)
        uow.modules.add_definition(workspace_definition)
        uow.modules.add_instance(workspace_instance)
        uow.planning_profiles.add(profile)
        uow.tasks.add(root_task)
        for task in subtasks.values():
            uow.tasks.add(task)
        for item in items.values():
            uow.worker_items.add(item)
        for worker in (source_worker, worker_a, target_one, target_two):
            uow.workers.add(worker)
        # Break the request <-> proposal <-> run foreign-key cycle into
        # explicit persistence steps.  The domain object remains materialized;
        # only the first insert temporarily omits its current proposal pointer.
        proposal_revision = planning_request.current_proposal_revision
        proposal_content_hash = planning_request.current_proposal_content_hash
        planning_request.current_proposal_revision = None
        planning_request.current_proposal_content_hash = None
        uow.planning_requests.add(planning_request)
        uow.session.flush()
        uow.session.add(
            _proposal_model(
                ids,
                workspace_id,
                planning_request,
                profile,
                snapshot,
                now,
            )
        )
        uow.session.flush()
        planning_request.current_proposal_revision = proposal_revision
        planning_request.current_proposal_content_hash = proposal_content_hash
        uow.planning_requests.save(
            planning_request,
            previous_version=planning_request.version,
        )
        uow.plans.add_snapshot_and_advance_head(snapshot, expected_head=None)
        for plan in snapshot.worker_item_plans:
            key = plan.key.removeprefix("worker-")
            uow.worker_items.add_placement(
                WorkerItemPlacement(
                    plan_snapshot_id=snapshot.id,
                    plan_generation=snapshot.generation,
                    workspace_id=workspace_id,
                    root_task_id=root_task.id,
                    worker_item_id=items[key].id,
                    worker_item_key=plan.key,
                    subtask_key=plan.subtask_key,
                    subtask_task_id=subtasks[key].id,
                    role=plan.role,
                    capability_requirements=plan.capability_requirements,
                    worker_policy_ref=plan.worker_policy_ref,
                    configuration_ref=plan.configuration_ref,
                    created_at=now,
                )
            )
        context_revision = _context_revision(planning_request, ids, now)
        context = _context(planning_request, root_task, context_revision, now)
        uow.work_contexts.add(context, context_revision)
        uow.executions.add(source_active.execution)
        uow.executions.add(independent_active.execution)
        uow.session.flush()
        for active in (source_active, independent_active):
            uow.leases.add(active.lease)
            uow.bindings.add(active.binding)
            uow.sessions.add(active.session)
        uow.workspaces.add(source_manifest)
        for worker in (source_worker, worker_a, target_one, target_two):
            uow.worker_runtime_configurations.add(
                WorkerRuntimeConfiguration.create(
                    worker_id=worker.id,
                    workspace_id=workspace_id,
                    adapter_module_instance_id=adapter_instance.id,
                    adapter_module_config_version=1,
                    workspace_module_instance_id=workspace_instance.id,
                    workspace_module_config_version=1,
                )
            )
        uow.execution_requests.add(request_a)
        uow.execution_requests.add(source_request)
        uow.execution_requests.add(request_c)
        uow.session.flush()
        uow.execution_requests.add_dependency(
            ExecutionRequestDependency(
                downstream_request_id=request_c.id,
                upstream_request_id=source_request.id,
                enforcement=DependencyEnforcement.HARD,
            )
        )
        delivered = _input(
            ids["delivered_input_id"],
            workspace_id,
            source_active.execution.id,
            source_active.session.id,
            "already-delivered-input",
            "The delivered source instruction is retained only in the source trace.",
            now,
            delivered=True,
        )
        queued = _input(
            ids["queued_input_id"],
            workspace_id,
            source_active.execution.id,
            source_active.session.id,
            "queued-source-input",
            "Transfer this queued instruction exactly once.",
            now,
            delivered=False,
        )
        uow.execution_inputs.add(delivered)
        uow.execution_inputs.add(queued)
        delivered_operation = GatewayOperation.create(
            id=ids["delivered_input_operation_row_id"],
            operation_id=delivered_operation_id(delivered.id),
            workspace_id=workspace_id,
            task_id=subtasks["b"].id,
            execution_id=source_active.execution.id,
            session_id=source_active.session.id,
            operation_type=GatewayOperationType.SEND_INPUT,
            module_instance_id=adapter_instance.id,
            config_version=adapter_instance.config_version,
            lease_id=source_active.lease.id,
            fencing_token=source_active.lease.fencing_token,
            now=now,
        )
        delivered_operation.start(now=now)
        delivered_operation.succeed(result_ref="sequence:1", now=now)
        uow.gateway_operations.add(delivered_operation)
        package, assembly = _active_worker_context(
            uow,
            workspace_id=workspace_id,
            execution=source_active.execution,
            worker_item=items["b"],
            revision_id=context_revision.revision_id,
            package_id=ids["source_package_id"],
            assembly_id=ids["source_assembly_id"],
            now=now,
        )
        package_command = _internal_command(
            command_id=ids["source_package_command_id"],
            workspace_id=workspace_id,
            command_type="R6FixtureAssembleHandoffContext",
            target_id=package.package_id,
            idempotency_key="r6-2-g4-assemble-source-context",
        )
        uow.commands.record_applied(
            package_command,
            hash_request(package_command),
            CommandResult(
                command_id=package_command.id,
                aggregate_id=package.package_id,
                aggregate_version=1,
                event_ids=(),
            ),
        )
        uow.context_assemblies.complete_with_package(
            assembly,
            package,
            owner="r6-1-fixture",
            expected_version=assembly.version,
            actor_type="system",
            actor_id="r6-2-g4-seed",
            causation_id=package_command.id,
            correlation_id=planning_request.correlation_id,
        )
        if package.context_version is None:
            raise AssertionError("G4 source context package version is missing")
        source_context_version = package.context_version
        acknowledgement = ContextAcknowledgement(
            package_id=package.package_id,
            package_hash=package.package_hash,
            consumer_type=ContextConsumerType.WORKER,
            consumer_id=source_active.execution.id,
            context_version=source_context_version,
            adapter_module_id=adapter_instance.id,
            adapter_config_version=adapter_instance.config_version,
            acknowledged_at=now,
            operation_id=ids["source_context_ack_operation_id"],
        )
        ack_command = _internal_command(
            command_id=ids["source_context_ack_command_id"],
            workspace_id=workspace_id,
            command_type="R6FixtureAcknowledgeHandoffContext",
            target_id=package.package_id,
            idempotency_key="r6-2-g4-ack-source-context",
        )
        uow.commands.record_applied(
            ack_command,
            hash_request(ack_command),
            CommandResult(
                command_id=ack_command.id,
                aggregate_id=package.package_id,
                aggregate_version=1,
                event_ids=(),
            ),
        )
        uow.context_packages.add_ack(
            acknowledgement,
            actor_type="system",
            actor_id="r6-2-g4-seed",
            causation_id=ack_command.id,
            correlation_id=planning_request.correlation_id,
        )

    return G4Fixture(
        workspace_id=workspace_id,
        root_task_id=root_task.id,
        subtask_ids={key: task.id for key, task in subtasks.items()},
        worker_item_ids={key: item.id for key, item in items.items()},
        worker_a_execution_id=independent_active.execution.id,
        source_execution_id=source_active.execution.id,
        source_session_id=source_active.session.id,
        source_manifest_id=source_manifest.id,
        source_fencing_token=source_manifest.fencing_token,
        source_package_id=package.package_id,
        source_package_hash=package.package_hash,
        source_context_version=source_context_version,
        delivered_input_id=delivered.id,
        queued_input_id=queued.id,
        source_request_id=source_request.id,
        request_c_id=request_c.id,
        target_worker_ids=(target_one.id, target_two.id),
        adapter_instance_id=adapter_instance.id,
        workspace_instance_id=workspace_instance.id,
        plan_snapshot_id=snapshot.id,
        plan_generation=snapshot.generation,
        plan_head_hash=_snapshot_hash(snapshot),
        planning_request_id=planning_request.id,
        planning_profile_id=profile.id,
        adapter_contract=adapter_definition.interface_version,
        workspace_contract=workspace_definition.interface_version,
        scenario=scenario,
    )


def load_g4_fixture(
    database: Database,
    workspace_id: UUID,
    *,
    scenario: G4Scenario | str = G4Scenario.NORMAL,
) -> G4Fixture:
    """Load deterministic G4 facts after browser commands have run.

    The browser vertical confirms through HTTP first. Its fake runtime then
    reconstructs immutable fixture facts without reseeding or mutating a
    Transition directly.
    """

    scenario = G4Scenario(scenario)
    ids = _ids(workspace_id)
    with database.session() as session:
        source_manifest = session.get(ExecutionWorkspaceModel, ids["source_manifest_id"])
        source_package = session.get(ContextPackageModel, ids["source_package_id"])
        plan_head = session.get(
            RootTaskPlanHeadModel,
            (workspace_id, ids["root_task_id"]),
        )
    if source_manifest is None or source_package is None or plan_head is None:
        raise RuntimeError("G4 source facts are incomplete; seed the workspace first")
    if source_manifest.workspace_id != workspace_id:
        raise RuntimeError("G4 source manifest workspace changed")
    if source_package.workspace_id != workspace_id:
        raise RuntimeError("G4 source package workspace changed")
    return G4Fixture(
        workspace_id=workspace_id,
        root_task_id=ids["root_task_id"],
        subtask_ids={
            key.removeprefix("subtask_").removesuffix("_id"): ids[key]
            for key in ("subtask_a_id", "subtask_b_id", "subtask_c_id")
        },
        worker_item_ids={
            key.removeprefix("worker_").removesuffix("_item_id"): ids[key]
            for key in ("worker_a_item_id", "worker_b_item_id", "worker_c_item_id")
        },
        worker_a_execution_id=ids["worker_a_execution_id"],
        source_execution_id=ids["source_execution_id"],
        source_session_id=ids["source_session_id"],
        source_manifest_id=source_manifest.id,
        source_fencing_token=source_manifest.fencing_token,
        source_package_id=source_package.id,
        source_package_hash=source_package.package_hash,
        source_context_version=source_package.context_version or 0,
        delivered_input_id=ids["delivered_input_id"],
        queued_input_id=ids["queued_input_id"],
        source_request_id=ids["source_request_id"],
        request_c_id=ids["request_c_id"],
        target_worker_ids=(ids["target_one_worker_id"], ids["target_two_worker_id"]),
        adapter_instance_id=ids["adapter_instance_id"],
        workspace_instance_id=ids["workspace_instance_id"],
        plan_snapshot_id=plan_head.active_snapshot_id,
        plan_generation=plan_head.active_generation,
        plan_head_hash=plan_head.active_snapshot_hash,
        planning_request_id=ids["planning_request_id"],
        planning_profile_id=ids["planning_profile_id"],
        adapter_contract="sidera.worker-adapter.handoff.v1",
        workspace_contract="sidera.workspace-provider.reuse-exact.v1",
        scenario=scenario,
    )


def _ids(workspace_id: UUID) -> dict[str, UUID]:
    labels = (
        "root_task_id",
        "subtask_a_id",
        "subtask_b_id",
        "subtask_c_id",
        "worker_a_item_id",
        "worker_b_item_id",
        "worker_c_item_id",
        "worker_a_id",
        "source_worker_id",
        "target_one_worker_id",
        "target_two_worker_id",
        "plan_snapshot_id",
        "planning_request_id",
        "planning_run_id",
        "planning_correlation_id",
        "planner_definition_id",
        "planner_instance_id",
        "planning_profile_id",
        "adapter_definition_id",
        "adapter_instance_id",
        "workspace_definition_id",
        "workspace_instance_id",
        "source_execution_id",
        "source_session_id",
        "source_lease_id",
        "worker_a_execution_id",
        "worker_a_session_id",
        "worker_a_lease_id",
        "source_manifest_id",
        "source_request_id",
        "request_a_id",
        "request_c_id",
        "proposal_id",
        "source_package_id",
        "source_assembly_id",
        "source_package_command_id",
        "source_context_ack_operation_id",
        "source_context_ack_command_id",
        "delivered_input_operation_row_id",
        "delivered_input_id",
        "queued_input_id",
    )
    ids = {label: uuid5(FIXTURE_NAMESPACE, f"{workspace_id}:{label}") for label in labels}
    ids["source_session_id"] = uuid5(
        NAMESPACE_URL,
        f"sidera.fake.execution-session:{ids['source_execution_id']}",
    )
    return ids


def _ensure_workspace(database: Database, workspace_id: UUID, *, now: datetime, reset: bool) -> None:
    with database.session() as session:
        existing = session.get(WorkspaceModel, workspace_id)
        if existing is not None and not reset:
            if existing.name != WORKSPACE_NAME:
                raise RuntimeError("refusing to seed an existing non-G4 workspace without --reset")
            raise RuntimeError("G4 workspace already exists; pass --reset explicitly")
        if reset and existing is not None:
            _clean_workspace(session, workspace_id)
        if session.get(WorkspaceModel, workspace_id) is None:
            session.add(
                WorkspaceModel(
                    id=workspace_id,
                    name=WORKSPACE_NAME,
                    status="active",
                    version=1,
                    created_at=now,
                    updated_at=now,
                )
            )
        session.commit()


def _clean_workspace(session, workspace_id: UUID) -> None:
    event_ids = select(EventModel.id).where(EventModel.workspace_id == workspace_id)
    session.execute(delete(OutboxMessageModel).where(OutboxMessageModel.event_id.in_(event_ids)))

    tables = Base.metadata.tables

    def clear(table_name: str, **values: object) -> None:
        table = tables[table_name]
        session.execute(
            update(table)
            .where(table.c.workspace_id == workspace_id)
            .values(**values)
        )

    # Null the few bidirectional references before deleting the explicit
    # workspace-scoped dependency order below.
    clear(
        "planning_requests",
        current_brief_revision=None,
        current_proposal_revision=None,
        current_proposal_content_hash=None,
    )
    session.execute(
        text(
            "ALTER TABLE work_contexts "
            "DROP CONSTRAINT IF EXISTS fk_work_contexts_current_revision"
        )
    )
    session.execute(
        delete(tables["context_package_knowledge_refs"])
        .where(
            tables["context_package_knowledge_refs"].c.package_id.in_(
                select(tables["context_packages"].c.id).where(
                    tables["context_packages"].c.workspace_id == workspace_id
                )
            )
        )
    )
    clear(
        "task_plan_snapshots",
        parent_snapshot_id=None,
    )
    clear(
        "execution_inputs",
        source_command_id=None,
        source_input_id=None,
        handoff_transition_id=None,
    )
    clear(
        "executions",
        active_lease_id=None,
        source_execution_id=None,
        source_session_id=None,
        handoff_transition_id=None,
        worker_item_id=None,
    )
    # Handoff Transition and Bundle intentionally form a deferred FK cycle;
    # release both sides before deleting the old E2E attempt. Recovery
    # decisions are also referenced by Bundles, so clear that edge first.
    clear("handoff_context_bundles", recovery_decision_id=None)
    clear("handoff_transitions", handoff_context_bundle_id=None)

    deletion_order = (
        "attention_acknowledgements",
        "scheduling_blocks",
        "projection_changes",
        "workspace_projection_cursors",
        "context_patch_acknowledgements",
        "context_patches",
        "execution_workspace_transfers",
        "handoff_context_bundles",
        "execution_request_dependencies",
        "execution_requests",
        "handoff_transitions",
        "handoff_proposals",
        "context_package_acknowledgements",
        "context_package_materialization_audits",
        "context_packages",
        "context_assembly_requests",
        "knowledge_candidate_submissions",
        "plan_entity_lineages",
        "worker_transitions",
        "proposal_materializations",
        "plan_transitions",
        "work_context_revisions",
        "work_contexts",
        "planning_clarifications",
        "planning_run_leases",
        "planning_proposal_revisions",
        "planning_runs",
        "planning_brief_revisions",
        "planning_requests",
        "planning_profiles",
        "worker_item_placements",
        "root_task_plan_heads",
        "task_plan_snapshots",
        "execution_inputs",
        "evidence_claims",
        "artifacts",
        "execution_events",
        "gateway_operations",
        "process_descriptors",
        "execution_workspaces",
        "execution_bindings",
        "execution_sessions",
        "assignment_leases",
        "executions",
        "worker_runtime_configurations",
        "module_instances",
        "module_definitions",
        "worker_item_placements",
        "worker_items",
        "workers",
        "events",
        "commands",
        "tasks",
    )
    # Recovery decisions are append-only in production. The deterministic E2E
    # workspace is reset between scenarios, so remove its old audit rows only
    # inside this test transaction while the guard trigger is disabled.
    session.execute(
        text(
            "ALTER TABLE handoff_recovery_decisions "
            "DISABLE TRIGGER trg_handoff_recovery_decisions_append_only"
        )
    )
    try:
        session.execute(
            delete(tables["handoff_recovery_decisions"]).where(
                tables["handoff_recovery_decisions"].c.workspace_id == workspace_id
            )
        )
    finally:
        session.execute(
            text(
                "ALTER TABLE handoff_recovery_decisions "
                "ENABLE TRIGGER trg_handoff_recovery_decisions_append_only"
            )
        )
    for table_name in dict.fromkeys(deletion_order):
        table = tables[table_name]
        session.execute(delete(table).where(table.c.workspace_id == workspace_id))
    session.execute(
        text(
            "ALTER TABLE work_contexts "
            "ADD CONSTRAINT fk_work_contexts_current_revision "
            "FOREIGN KEY (current_revision_id) REFERENCES work_context_revisions (id) "
            "ON DELETE RESTRICT DEFERRABLE INITIALLY DEFERRED"
        )
    )
    session.execute(delete(WorkspaceModel).where(WorkspaceModel.id == workspace_id))
    session.flush()


def _subtask(task_id: UUID, workspace_id: UUID, key: str, *, running: bool, now: datetime) -> Task:
    task = Task.create(
        task_id=task_id,
        workspace_id=workspace_id,
        title=f"Subtask {key.upper()}",
        description=f"Deterministic G4 branch {key.upper()}.",
        acceptance_criteria=(f"acceptance:g4-{key}",),
        priority=50,
        requested_by="r6.2-e2e",
    )
    task.mark_ready()
    if running:
        task.mark_running()
    else:
        task.wait_for_decision()
    del now
    return task


def _worker(worker_id: UUID, workspace_id: UUID, name: str, now: datetime, *, cost: int) -> Worker:
    return Worker.register(
        worker_id=worker_id,
        workspace_id=workspace_id,
        adapter_type="fake",
        display_name=name,
        capabilities=frozenset({"code"}),
        capacity=1,
        cost_per_hour_microunits=cost,
        startup_latency_ms=0,
        now=now,
    )


def _snapshot(*, workspace_id: UUID, root_task_id: UUID, subtasks: Mapping[str, Task], items: Mapping[str, WorkerItem], snapshot_id: UUID, now: datetime) -> TaskPlanSnapshot:
    return TaskPlanSnapshot.create(
        snapshot_id=snapshot_id,
        schema_version=2,
        workspace_id=workspace_id,
        root_task_id=root_task_id,
        generation=1,
        parent_snapshot_id=None,
        source_type="r6.2-e2e",
        source_ref="fake-g4",
        title="G4 cross-agent handoff control room",
        goal_summary="Move Worker B while retaining the same task graph.",
        acceptance_refs=("handoff:identity-preserved",),
        published_by="r6.2-e2e",
        published_at=now,
        subtasks=tuple(
            SubtaskPlanItem(
                key=f"subtask-{key}",
                task_id=subtasks[key].id,
                kind=WorkItemKind.CHILD,
                title=subtasks[key].title,
                goal_summary=subtasks[key].description,
                acceptance_refs=subtasks[key].acceptance_criteria,
                milestone_key=None,
                expected_outputs=("diff",),
                source_refs=("r6.2-e2e",),
            )
            for key in ("a", "b", "c")
        ),
        worker_item_plans=tuple(
            WorkerItemPlanItem(
                key=f"worker-{key}",
                worker_item_id=items[key].id,
                subtask_key=f"subtask-{key}",
                title=items[key].title,
                role="developer",
                responsibility=items[key].responsibility,
                capability_requirements=("code",),
                worker_policy_ref="policy:r6.2-fake",
                configuration_ref=None,
                concurrency_limit=1,
                source_refs=("r6.2-e2e",),
            )
            for key in ("a", "b", "c")
        ),
        subtask_dependencies=(
            SubtaskDependency(
                upstream_subtask_key="subtask-b",
                downstream_subtask_key="subtask-c",
                kind=DependencyKind.HARD,
                required_evidence_refs=(),
                enforcement=PlanDependencyEnforcement.CONTROL_PLANE,
            ),
        ),
    )


def _active_execution(*, execution_id: UUID, session_id: UUID, lease_id: UUID, worker_id: UUID, worker_item: WorkerItem, task: Task, workspace_id: UUID, adapter_instance: ModuleInstance, workspace_instance: ModuleInstance, now: datetime):
    execution = Execution.create(
        execution_id=execution_id,
        workspace_id=workspace_id,
        task_id=task.id,
        worker_item_id=worker_item.id,
        lineage_id=execution_id,
        attempt=1,
        max_attempts=2,
        purpose=f"Run {worker_item.stable_key}",
        capability_requirements=frozenset({"code"}),
        pinned_worker_id=worker_id,
        not_before=now,
        adapter_preferences=("fake",),
    )
    lease = AssignmentLease.issue(
        lease_id=lease_id,
        workspace_id=workspace_id,
        execution_id=execution_id,
        worker_id=worker_id,
        issued_at=now,
        expires_at=now + timedelta(hours=1),
        fencing_token=1,
        assignment_reason={"fixture": "r6.2-g4"},
    )
    execution.assign(lease_id=lease.id, worker_id=worker_id, fencing_token=lease.fencing_token)
    execution.start(lease_id=lease.id, fencing_token=lease.fencing_token, now=now)
    session = ExecutionSession.start(
        session_id=session_id,
        workspace_id=workspace_id,
        execution_id=execution_id,
        worker_id=worker_id,
        adapter_session_id=f"fake-session-{execution_id}",
        now=now,
        adapter_module_id=adapter_instance.id,
        adapter_config_version=1,
        workspace_module_id=workspace_instance.id,
        workspace_config_version=1,
        capability_snapshot=FakeWorkerAdapter.capabilities(),
        process_ref=f"fake://{execution_id}",
        workspace_ref=workspace_instance.instance_name,
    )
    from sidera_control_plane.domain.modules import ExecutionBinding

    binding = ExecutionBinding(
        id=uuid5(FIXTURE_NAMESPACE, f"{workspace_id}:binding:{execution_id}"),
        workspace_id=workspace_id,
        execution_id=execution_id,
        worker_id=worker_id,
        adapter_module_id=adapter_instance.id,
        workspace_module_id=workspace_instance.id,
        adapter_config_version=1,
        workspace_config_version=1,
        capability_snapshot=FakeWorkerAdapter.capabilities(),
        lease_id=lease.id,
        fencing_token=lease.fencing_token,
        version=1,
    )
    return _ActiveExecution(execution, lease, session, binding)


@dataclass(frozen=True, slots=True)
class _ActiveExecution:
    execution: Execution
    lease: AssignmentLease
    session: ExecutionSession
    binding: ExecutionBinding


def _execution_request(*, request_id: UUID, planning_request: PlanningRequest, proposal_id: UUID, snapshot: TaskPlanSnapshot, root_task: Task, subtask: Task, item: WorkerItem, now: datetime) -> ExecutionRequest:
    return ExecutionRequest.create(
        request_id=request_id,
        workspace_id=planning_request.workspace_id,
        planning_request_id=planning_request.id,
        proposal_revision_id=proposal_id,
        plan_snapshot_id=snapshot.id,
        plan_generation=snapshot.generation,
        root_task_id=root_task.id,
        subtask_id=subtask.id,
        worker_item_id=item.id,
        worker_item_key=item.stable_key,
        intent_key=f"g4:{item.stable_key}",
        purpose="Continue the WorkerItem task",
        capability_requirements=("code",),
        adapter_preferences=("fake",),
        budget_class="standard",
        not_before=now,
        max_attempts=2,
        expected_artifact_refs=(),
        expected_evidence_refs=(),
        expected_outputs=("diff",),
        now=now,
    )


def _proposal_model(
    ids: Mapping[str, UUID],
    workspace_id: UUID,
    request: PlanningRequest,
    profile,
    snapshot: TaskPlanSnapshot,
    now: datetime,
):
    from sidera_control_plane.persistence.models import PlanningProposalRevisionModel

    run = request.runs[0]
    subtasks = [
        {
            "key": item.key,
            "kind": item.kind.value,
            "title": item.title,
            "goal_summary": item.goal_summary,
            "acceptance_refs": list(item.acceptance_refs),
            "expected_outputs": list(item.expected_outputs),
            "milestone_key": item.milestone_key,
            "source_refs": list(item.source_refs),
        }
        for item in snapshot.subtasks
    ]
    worker_items = [
        {
            "key": item.key,
            "subtask_key": item.subtask_key,
            "title": item.title,
            "role": item.role,
            "responsibility": item.responsibility,
            "capability_requirements": list(item.capability_requirements),
            "worker_policy_ref": item.worker_policy_ref,
            "configuration_ref": item.configuration_ref,
            "concurrency_limit": item.concurrency_limit,
            "source_refs": list(item.source_refs),
            "pinned_worker_id": None,
        }
        for item in snapshot.worker_item_plans
    ]
    execution_intents = [
        {
            "worker_item_key": item.key,
            "purpose": item.responsibility,
            "context_refs": ["r6.2-e2e"],
            "expected_artifact_refs": ["artifact_type:diff"],
            "required_evidence_refs": ["acceptance:g4"],
            "capability_requirements": list(item.capability_requirements),
            "adapter_preferences": ["fake"],
            "budget_class": "standard",
            "deadline_seconds": 300,
            "not_before_seconds": None,
            "max_attempts": 2,
            "activation_dependencies": [],
        }
        for item in snapshot.worker_item_plans
    ]
    plan_template = {
        "root_task": {
            "title": snapshot.title,
            "goal_summary": snapshot.goal_summary,
            "acceptance_refs": list(snapshot.acceptance_refs),
        },
        "subtasks": subtasks,
        "worker_items": worker_items,
        "milestones": [],
        "subtask_dependencies": [
            {
                "upstream_subtask_key": item.upstream_subtask_key,
                "downstream_subtask_key": item.downstream_subtask_key,
                "kind": item.kind.value,
                "required_evidence_refs": list(item.required_evidence_refs),
                "enforcement": item.enforcement.value,
            }
            for item in snapshot.subtask_dependencies
        ],
        "worker_item_dependencies": [],
        "base_descriptor": None,
    }
    return PlanningProposalRevisionModel(
        id=ids["proposal_id"],
        planning_request_id=request.id,
        revision=1,
        workspace_id=workspace_id,
        brief_revision=1,
        planning_run_id=run.id,
        proposal_schema_version=2,
        kind="create",
        profile_id=profile.id,
        profile_version=1,
        planner_module_instance_id=run.planner_module_instance_id,
        planner_module_config_version=1,
        decision_summary={
            "understanding": "Move Worker B while retaining the same task graph.",
            "assumptions": [],
            "risks": [],
            "estimated_cost_microunits": 0,
            "estimated_tokens": 0,
            "estimated_runtime_seconds": 300,
            "estimated_concurrency": 2,
            "scope_includes": ["worker handoff"],
            "scope_excludes": [],
            "metadata": {"fixture": "g4"},
        },
        plan_template=plan_template,
        execution_intents=execution_intents,
        impact_policy={
            "activate_immediately": True,
            "budget_level": "standard",
            "failure_policy": "block",
            "manual_gate": False,
            "revision_operation": None,
        },
        derived_diff={},
        canonical_payload={"fixture": "g4", "plan": plan_template},
        content_hash="f" * 64,
        created_at=now,
    )


def _context_revision(request: PlanningRequest, ids: Mapping[str, UUID], now: datetime):
    from sidera_control_plane.application.work_context_commands import (
        WorkContextRevisionFactory,
    )
    from sidera_control_plane.domain.work_context import WorkContextRevisionReason

    return WorkContextRevisionFactory().create(
        work_context_id=ids["work_context_id"] if "work_context_id" in ids else uuid5(FIXTURE_NAMESPACE, f"{request.id}:work-context"),
        workspace_id=request.workspace_id,
        brief=request.briefs[0],
        revision_number=1,
        reason=WorkContextRevisionReason.REQUEST_CREATED,
        actor="r6.2-e2e",
        created_at=now,
        revision_id=uuid5(FIXTURE_NAMESPACE, f"{request.id}:work-context-revision"),
    )


def _context(request: PlanningRequest, root_task: Task, revision, now: datetime):
    from sidera_control_plane.domain.work_context import WorkContext

    context = WorkContext.create(
        work_context_id=revision.work_context_id,
        workspace_id=request.workspace_id,
        planning_request_id=request.id,
        first_revision=revision,
        created_at=now,
    )
    context.bind_root_task(root_task.id, expected_version=1, now=now)
    return context


def _input(input_id: UUID, workspace_id: UUID, execution_id: UUID, session_id: UUID, key: str, text: str, now: datetime, *, delivered: bool):
    from sidera_control_plane.domain.inputs import ExecutionInput

    item = ExecutionInput.accept(
        id=input_id,
        workspace_id=workspace_id,
        execution_id=execution_id,
        session_id=session_id,
        client_idempotency_key=key,
        text=text,
        attachment_refs=(),
        now=now,
    )
    item.queue()
    if delivered:
        item.begin_dispatch(operation_id=delivered_operation_id(item.id))
        item.deliver()
    return item


def delivered_operation_id(input_id: UUID) -> UUID:
    from sidera_control_plane.domain.inputs import input_operation_id

    return input_operation_id(input_id)


def _snapshot_hash(snapshot: TaskPlanSnapshot) -> str:
    from sidera_control_plane.domain.plan_revisions import plan_snapshot_content_hash

    return plan_snapshot_content_hash(snapshot)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workspace-id", required=True)
    parser.add_argument("--scenario", choices=tuple(item.value for item in G4Scenario), default=G4Scenario.NORMAL.value)
    parser.add_argument("--reset", action="store_true")
    args = parser.parse_args(argv)
    if os.environ.get("SIDERA_ALLOW_E2E_SEED") != "1":
        raise SystemExit("SIDERA_ALLOW_E2E_SEED=1 is required for the R6.2 seed")
    database_url = os.environ.get("SIDERA_DATABASE_URL")
    if not database_url:
        raise SystemExit("SIDERA_DATABASE_URL is required for the R6.2 seed")
    parsed = urlsplit(database_url)
    if parsed.hostname not in {"127.0.0.1", "localhost", "::1"}:
        raise SystemExit("R6.2 E2E seed requires a loopback database host")
    try:
        workspace_id = UUID(args.workspace_id)
    except ValueError as exc:
        raise SystemExit("--workspace-id must be a UUID") from exc
    database = Database(database_url)
    try:
        fixture = seed_g4_fixture(
            database,
            workspace_id,
            scenario=G4Scenario(args.scenario),
            now=datetime.now(UTC),
            reset=args.reset,
        )
    finally:
        database.dispose()
    print(json.dumps({
        "workspace_id": str(fixture.workspace_id),
        "root_task_id": str(fixture.root_task_id),
        "source_execution_id": str(fixture.source_execution_id),
        "source_session_id": str(fixture.source_session_id),
        "worker_item_ids": {key: str(value) for key, value in fixture.worker_item_ids.items()},
        "target_worker_ids": [str(value) for value in fixture.target_worker_ids],
        "scenario": fixture.scenario.value,
    }, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
