"""Deterministic R6.1 revision fixture for the control-plane vertical gate."""

from __future__ import annotations

import argparse
import json
import os
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, cast
from uuid import UUID, uuid5

from sqlalchemy import delete, select, text, update

from sidera_control_plane.application.command_service import hash_request
from sidera_control_plane.application.ports import WorkerItemPlacement
from sidera_control_plane.application.work_context_commands import (
    WorkContextRevisionFactory,
)
from sidera_control_plane.domain.commands import CommandEnvelope, CommandResult
from sidera_control_plane.domain.context_packages import (
    ContextAcknowledgement,
    ContextAssemblyRequest,
    ContextBudgetV1,
    ContextConsumerType,
    ContextPackageManifestV1,
    ContextPurpose,
)
from sidera_control_plane.domain.execution import Execution, ExecutionSession
from sidera_control_plane.domain.inputs import ExecutionInput
from sidera_control_plane.domain.lease import AssignmentLease
from sidera_control_plane.domain.modules import (
    CancellationCapability,
    CapabilityInput,
    ExecutionBinding,
    IdempotencyCapability,
    ModuleDefinition,
    ModuleInstance,
    ModuleLifecycle,
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
from sidera_control_plane.domain.planning import PlanningRequest
from sidera_control_plane.domain.planning_profiles import (
    InvalidOutputPolicy,
    PlanningBudgetPolicy,
    PlanningProfile,
    PlanningProfileStatus,
    PlanningRetryPolicy,
    UnknownOutcomePolicy,
)
from sidera_control_plane.domain.plans import (
    DependencyEnforcement,
    DependencyKind,
    SubtaskDependency,
    SubtaskPlanItem,
    TaskPlanSnapshot,
    WorkerItemPlanItem,
    WorkItemKind,
)
from sidera_control_plane.domain.task import Task
from sidera_control_plane.domain.work_context import (
    WorkContext,
    WorkContextRevisionReason,
)
from sidera_control_plane.domain.worker import Worker
from sidera_control_plane.domain.worker_item import WorkerItem
from sidera_control_plane.persistence.database import Database
from sidera_control_plane.persistence.models import (
    ArtifactModel,
    AssignmentLeaseModel,
    AttentionAcknowledgementModel,
    CommandModel,
    ContextAssemblyRequestModel,
    ContextPackageAcknowledgementModel,
    ContextPackageKnowledgeRefModel,
    ContextPackageMaterializationAuditModel,
    ContextPackageModel,
    ContextPatchAcknowledgementModel,
    ContextPatchModel,
    EventModel,
    EvidenceClaimModel,
    ExecutionBindingModel,
    ExecutionEventModel,
    ExecutionInputModel,
    ExecutionModel,
    ExecutionRequestDependencyModel,
    ExecutionRequestModel,
    ExecutionSessionModel,
    ExecutionWorkspaceModel,
    GatewayOperationModel,
    KnowledgeCandidateSubmissionModel,
    ModuleDefinitionModel,
    ModuleInstanceModel,
    OutboxMessageModel,
    PlanEntityLineageModel,
    PlanningBriefRevisionModel,
    PlanningClarificationModel,
    PlanningProfileModel,
    PlanningProposalRevisionModel,
    PlanningRequestModel,
    PlanningRunLeaseModel,
    PlanningRunModel,
    PlanSnapshotModel,
    PlanTransitionModel,
    ProcessDescriptorModel,
    ProjectionChangeModel,
    ProposalMaterializationModel,
    RootTaskPlanHeadModel,
    SchedulingBlockModel,
    TaskModel,
    WorkContextModel,
    WorkContextRevisionModel,
    WorkerItemModel,
    WorkerItemPlacementModel,
    WorkerModel,
    WorkerRuntimeConfigurationModel,
    WorkerTransitionModel,
    WorkspaceModel,
    WorkspaceProjectionCursorModel,
)
from sidera_control_plane.persistence.unit_of_work import SqlAlchemyUnitOfWork

NOW = datetime(2026, 8, 29, 12, 0, tzinfo=UTC)
FIXTURE_NAMESPACE = UUID("f6000000-0000-0000-0000-000000000001")
WORKSPACE_NAME = "SIDERA R6.1 revision vertical workspace"


@dataclass(frozen=True, slots=True)
class RevisionFixture:
    workspace_id: UUID
    root_task_id: UUID
    base_snapshot_id: UUID
    worker_item_ids: Mapping[str, UUID]
    worker_b_execution_id: UUID
    worker_b_session_id: UUID
    worker_b_package_id: UUID
    worker_b_package_hash: str
    worker_b_input_id: UUID
    planning_request_id: UUID
    planning_profile_id: UUID
    revision_actions: Mapping[str, str]


def seed_revision_fixture(
    database: Database,
    workspace_id: UUID,
    *,
    now: datetime = NOW,
    reset: bool = False,
) -> RevisionFixture:
    """Create one isolated G3 running-plan fixture in a test workspace."""
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("fixture timestamp must be timezone-aware")
    ids = _ids(workspace_id)
    with database.session() as session:
        workspace = session.get(WorkspaceModel, workspace_id)
        if reset:
            _clean_workspace(session, workspace_id)
            workspace = None
        if workspace is None:
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
        elif workspace.name != WORKSPACE_NAME:
            workspace.name = WORKSPACE_NAME
        session.commit()

    root_task = Task.create(
        task_id=ids["root_task_id"],
        workspace_id=workspace_id,
        title="Running revision control room",
        description="Deterministic fan-out/fan-in plan used by the R6.1 release gate.",
        acceptance_criteria=("acceptance:running-plan",),
        priority=50,
        requested_by="r6.1-e2e",
    )
    root_task.mark_ready()
    root_task.mark_running()

    subtask_tasks = {
        key: _subtask_task(ids[f"subtask_{key}_task_id"], workspace_id, key)
        for key in ("a", "b", "c", "d")
    }
    worker_items = {
        key: WorkerItem.create(
            item_id=ids[f"worker_{key}_item_id"],
            workspace_id=workspace_id,
            root_task_id=root_task.id,
            stable_key=f"worker-{key}",
            title=f"Worker {key.upper()}",
            responsibility=(
                "Produce the parallel branch A evidence."
                if key == "a"
                else "Produce the parallel branch B evidence."
                if key == "b"
                else "Wait for both parallel branches and integrate the result."
                if key == "c"
                else "Independent maintenance branch."
            ),
            now=now,
        )
        for key in ("a", "b", "c", "d")
    }
    worker_a = Worker.register(
        worker_id=ids["worker_a_id"],
        workspace_id=workspace_id,
        adapter_type="fake",
        display_name="R6.1 Fake Worker A",
        capabilities=frozenset({"code"}),
        capacity=1,
        cost_per_hour_microunits=0,
        startup_latency_ms=0,
        now=now,
    )
    worker_b = Worker.register(
        worker_id=ids["worker_b_id"],
        workspace_id=workspace_id,
        adapter_type="fake",
        display_name="R6.1 Fake Worker B",
        capabilities=frozenset({"code"}),
        capacity=1,
        cost_per_hour_microunits=0,
        startup_latency_ms=0,
        now=now,
    )
    for worker in (worker_a, worker_b):
        worker.reserve()
        worker.mark_running()

    snapshots = tuple(
        _plan_snapshot(
            workspace_id=workspace_id,
            root_task_id=root_task.id,
            subtask_tasks=subtask_tasks,
            worker_items=worker_items,
            generation=generation,
            parent_snapshot_id=(
                None if generation == 1 else ids[f"snapshot_{generation - 1}_id"]
            ),
            snapshot_id=ids[f"snapshot_{generation}_id"],
            now=now,
        )
        for generation in (1, 2, 3)
    )
    planner_definition, planner_instance, profile = _planning_profile(
        workspace_id, ids, now
    )
    original_request = PlanningRequest.create(
        request_id=ids["planning_request_id"],
        workspace_id=workspace_id,
        profile_snapshot=profile.snapshot(planner_definition.capabilities),
        objective="Revise the running plan while preserving completed context.",
        constraints=("Keep stable WorkerItem identities reusable.",),
        acceptance_criteria=("acceptance:revision-review",),
        context_refs=(),
        requested_outputs=("plan",),
        source_type="r6.1-e2e",
        source_ref="fake-revision-fixture",
        actor="r6.1-e2e",
        correlation_id=ids["planning_correlation_id"],
        run_id=ids["planning_run_id"],
        now=now,
    )
    original_request.cancel(now=now)
    context_revision = WorkContextRevisionFactory().create(
        work_context_id=ids["work_context_id"],
        workspace_id=workspace_id,
        brief=original_request.briefs[0],
        revision_number=1,
        reason=WorkContextRevisionReason.REQUEST_CREATED,
        actor="r6.1-e2e",
        created_at=now,
        revision_id=ids["work_context_revision_id"],
    )
    context = WorkContext.create(
        work_context_id=context_revision.work_context_id,
        workspace_id=workspace_id,
        planning_request_id=original_request.id,
        first_revision=context_revision,
        created_at=now,
    )
    context.bind_root_task(root_task.id, expected_version=1, now=now)

    adapter_capabilities = _worker_capabilities()
    workspace_capabilities = WorkspaceProviderCapabilityProfile(
        isolation=SandboxCapability.ISOLATED_RUNTIME,
        writable=True,
        git_worktree=True,
        stability=StabilityLevel.STABLE,
    )
    adapter_definition = ModuleDefinition(
        id=ids["worker_adapter_definition_id"],
        workspace_id=workspace_id,
        module_type="worker_adapter",
        provider="fake",
        interface_version="sidera.worker-adapter.v1",
        config_schema_version=1,
        capabilities=adapter_capabilities,
        lifecycle=ModuleLifecycle.ACTIVE,
    )
    workspace_definition = ModuleDefinition(
        id=ids["workspace_definition_id"],
        workspace_id=workspace_id,
        module_type="workspace_provider",
        provider="fake-workspace",
        interface_version="sidera.workspace-provider.v1",
        config_schema_version=1,
        capabilities=workspace_capabilities,
        lifecycle=ModuleLifecycle.ACTIVE,
    )
    adapter_instance = ModuleInstance(
        id=ids["worker_adapter_instance_id"],
        workspace_id=workspace_id,
        definition_id=adapter_definition.id,
        instance_name="r6-1-fake-worker-adapter",
        config_version=1,
        config_namespace="adapter",
        config={"adapter.mode": "deterministic-test"},
        secret_refs=(),
        lifecycle=ModuleLifecycle.ACTIVE,
    )
    workspace_instance = ModuleInstance(
        id=ids["workspace_instance_id"],
        workspace_id=workspace_id,
        definition_id=workspace_definition.id,
        instance_name="r6-1-fake-workspace",
        config_version=1,
        config_namespace="workspace",
        config={"workspace.mode": "deterministic-test"},
        secret_refs=(),
        lifecycle=ModuleLifecycle.ACTIVE,
    )
    executions = {
        "a": _active_execution(
            execution_id=ids["worker_a_execution_id"],
            session_id=ids["worker_a_session_id"],
            lease_id=ids["worker_a_lease_id"],
            worker_id=worker_a.id,
            worker_item=worker_items["a"],
            task=subtask_tasks["a"],
            workspace_id=workspace_id,
            adapter_instance=adapter_instance,
            workspace_instance=workspace_instance,
            capabilities=adapter_capabilities,
            now=now,
        ),
        "b": _active_execution(
            execution_id=ids["worker_b_execution_id"],
            session_id=ids["worker_b_session_id"],
            lease_id=ids["worker_b_lease_id"],
            worker_id=worker_b.id,
            worker_item=worker_items["b"],
            task=subtask_tasks["b"],
            workspace_id=workspace_id,
            adapter_instance=adapter_instance,
            workspace_instance=workspace_instance,
            capabilities=adapter_capabilities,
            now=now,
        ),
    }

    with SqlAlchemyUnitOfWork(database) as uow:
        uow.modules.add_definition(planner_definition)
        uow.modules.add_instance(planner_instance)
        uow.modules.add_definition(adapter_definition)
        uow.modules.add_definition(workspace_definition)
        uow.modules.add_instance(adapter_instance)
        uow.modules.add_instance(workspace_instance)
        uow.planning_profiles.add(profile)
        uow.tasks.add(root_task)
        for task in subtask_tasks.values():
            uow.tasks.add(task)
        for item in worker_items.values():
            uow.worker_items.add(item)
        uow.workers.add(worker_a)
        uow.workers.add(worker_b)
        uow.planning_requests.add(original_request)
        uow.work_contexts.add(context, context_revision)
        uow.session.flush()
        head = uow.plans.add_snapshot_and_advance_head(snapshots[0], expected_head=None)
        _add_placements(uow, snapshots[0], subtask_tasks)
        for snapshot in snapshots[1:]:
            head = uow.plans.add_snapshot_and_advance_head(snapshot, expected_head=head)
            _add_placements(uow, snapshot, subtask_tasks)
        del head
        for execution in executions.values():
            uow.executions.add(execution.execution)
        uow.session.flush()
        for execution in executions.values():
            uow.leases.add(execution.lease)
            uow.bindings.add(execution.binding)
            uow.sessions.add(execution.session)
        uow.session.flush()
        queued_input = ExecutionInput.accept(
            id=ids["worker_b_input_id"],
            workspace_id=workspace_id,
            execution_id=executions["b"].execution.id,
            session_id=executions["b"].session.id,
            client_idempotency_key="r6-1-queued-user-input",
            text="Keep the accepted user instruction queued across the patch.",
            attachment_refs=(),
            now=now,
        )
        queued_input.queue()
        uow.execution_inputs.add(queued_input)
        package, assembly = _active_worker_context(
            uow,
            workspace_id=workspace_id,
            execution=executions["b"].execution,
            worker_item=worker_items["b"],
            revision_id=context_revision.revision_id,
            package_id=ids["worker_b_package_id"],
            assembly_id=ids["worker_b_assembly_id"],
            now=now,
        )
        package_command = _internal_command(
            command_id=ids["worker_b_package_command_id"],
            workspace_id=workspace_id,
            command_type="R6FixtureAssembleContext",
            target_id=package.package_id,
            idempotency_key="r6-1-fixture-assemble-context",
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
            actor_id="r6-1-fixture",
            causation_id=package_command.id,
            correlation_id=ids["planning_correlation_id"],
        )
        acknowledgement = ContextAcknowledgement(
            package_id=package.package_id,
            package_hash=package.package_hash,
            consumer_type=ContextConsumerType.WORKER,
            consumer_id=executions["b"].execution.id,
            context_version=1,
            adapter_module_id=adapter_instance.id,
            adapter_config_version=adapter_instance.config_version,
            acknowledged_at=now,
            operation_id=ids["worker_b_context_ack_operation_id"],
        )
        ack_command = _internal_command(
            command_id=ids["worker_b_context_ack_command_id"],
            workspace_id=workspace_id,
            command_type="R6FixtureAcknowledgeContext",
            target_id=package.package_id,
            idempotency_key="r6-1-fixture-ack-context",
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
            actor_id="r6-1-fixture",
            causation_id=ack_command.id,
            correlation_id=ids["planning_correlation_id"],
        )
        package_hash = package.package_hash

    return RevisionFixture(
        workspace_id=workspace_id,
        root_task_id=root_task.id,
        base_snapshot_id=snapshots[-1].id,
        worker_item_ids={
            f"worker-{key}": item.id for key, item in worker_items.items()
        },
        worker_b_execution_id=executions["b"].execution.id,
        worker_b_session_id=executions["b"].session.id,
        worker_b_package_id=package.package_id,
        worker_b_package_hash=package_hash,
        worker_b_input_id=ids["worker_b_input_id"],
        planning_request_id=original_request.id,
        planning_profile_id=profile.id,
        revision_actions={
            "worker-a": "preserve",
            "worker-b": "patch",
            "worker-e": "create",
            "worker-d": "retire",
        },
    )


def _clean_workspace(session, workspace_id: UUID) -> None:
    """Reset only the named R6.1 fixture workspace before a browser seed."""
    existing = session.get(WorkspaceModel, workspace_id)
    if existing is not None and existing.name != WORKSPACE_NAME:
        raise RuntimeError(
            f"refusing to clean workspace {workspace_id}: unexpected name {existing.name!r}"
        )

    session.execute(
        update(PlanningRequestModel)
        .where(PlanningRequestModel.workspace_id == workspace_id)
        .values(
            current_brief_revision=None,
            current_proposal_revision=None,
            current_proposal_content_hash=None,
        )
    )
    session.execute(
        delete(ContextPackageKnowledgeRefModel).where(
            ContextPackageKnowledgeRefModel.package_id.in_(
                select(ContextPackageModel.id).where(
                    ContextPackageModel.workspace_id == workspace_id
                )
            )
        )
    )
    session.execute(
        text(
            "ALTER TABLE work_contexts "
            "DROP CONSTRAINT IF EXISTS fk_work_contexts_current_revision"
        )
    )
    session.execute(
        update(ExecutionModel)
        .where(ExecutionModel.workspace_id == workspace_id)
        .values(
            active_lease_id=None,
            source_execution_id=None,
            source_session_id=None,
            worker_item_id=None,
        )
    )
    models = (
        AttentionAcknowledgementModel,
        SchedulingBlockModel,
        ProjectionChangeModel,
        WorkspaceProjectionCursorModel,
        ContextPackageAcknowledgementModel,
        ContextPackageMaterializationAuditModel,
        ContextPackageModel,
        ContextAssemblyRequestModel,
        ContextPatchAcknowledgementModel,
        ContextPatchModel,
        PlanEntityLineageModel,
        WorkerTransitionModel,
        KnowledgeCandidateSubmissionModel,
        WorkContextRevisionModel,
        WorkContextModel,
        ExecutionRequestDependencyModel,
        ExecutionRequestModel,
        ProposalMaterializationModel,
        PlanTransitionModel,
        PlanningProposalRevisionModel,
        PlanningClarificationModel,
        PlanningRunLeaseModel,
        PlanningRunModel,
        PlanningBriefRevisionModel,
        PlanningRequestModel,
        PlanningProfileModel,
        WorkerItemPlacementModel,
        RootTaskPlanHeadModel,
        PlanSnapshotModel,
        ExecutionInputModel,
        EvidenceClaimModel,
        ArtifactModel,
        ExecutionEventModel,
        GatewayOperationModel,
        ProcessDescriptorModel,
        ExecutionWorkspaceModel,
        ExecutionBindingModel,
        ExecutionSessionModel,
        AssignmentLeaseModel,
        ExecutionModel,
        WorkerRuntimeConfigurationModel,
        ModuleInstanceModel,
        ModuleDefinitionModel,
        WorkerItemModel,
        WorkerModel,
        OutboxMessageModel,
        EventModel,
        CommandModel,
        TaskModel,
    )
    try:
        for model in models:
            if model is OutboxMessageModel:
                session.execute(
                    delete(model).where(
                        model.event_id.in_(
                            select(EventModel.id).where(
                                EventModel.workspace_id == workspace_id
                            )
                        )
                    )
                )
            else:
                session.execute(
                    delete(model).where(cast(Any, model).workspace_id == workspace_id)
                )
    finally:
        session.execute(
            text(
                "ALTER TABLE work_contexts "
                "ADD CONSTRAINT fk_work_contexts_current_revision "
                "FOREIGN KEY (current_revision_id) REFERENCES work_context_revisions (id) "
                "ON DELETE RESTRICT DEFERRABLE INITIALLY DEFERRED"
            )
        )
    if existing is not None:
        session.execute(delete(WorkspaceModel).where(WorkspaceModel.id == workspace_id))
    session.flush()


def _ids(workspace_id: UUID) -> dict[str, UUID]:
    labels = (
        "root_task_id",
        "subtask_a_task_id",
        "subtask_b_task_id",
        "subtask_c_task_id",
        "subtask_d_task_id",
        "worker_a_item_id",
        "worker_b_item_id",
        "worker_c_item_id",
        "worker_d_item_id",
        "worker_a_id",
        "worker_b_id",
        "snapshot_1_id",
        "snapshot_2_id",
        "snapshot_3_id",
        "planning_request_id",
        "planning_run_id",
        "planning_correlation_id",
        "work_context_id",
        "work_context_revision_id",
        "planner_definition_id",
        "planner_instance_id",
        "planning_profile_id",
        "worker_adapter_definition_id",
        "worker_adapter_instance_id",
        "workspace_definition_id",
        "workspace_instance_id",
        "worker_a_execution_id",
        "worker_a_session_id",
        "worker_a_lease_id",
        "worker_b_execution_id",
        "worker_b_session_id",
        "worker_b_lease_id",
        "worker_b_input_id",
        "worker_b_package_id",
        "worker_b_assembly_id",
        "worker_b_package_command_id",
        "worker_b_context_ack_operation_id",
        "worker_b_context_ack_command_id",
    )
    return {
        label: uuid5(FIXTURE_NAMESPACE, f"{workspace_id}:{label}") for label in labels
    }


def _subtask_task(task_id: UUID, workspace_id: UUID, key: str) -> Task:
    task = Task.create(
        task_id=task_id,
        workspace_id=workspace_id,
        title=f"Subtask {key.upper()}",
        description=f"Deterministic subtask {key.upper()}.",
        acceptance_criteria=(f"acceptance:subtask-{key}",),
        priority=50,
        requested_by="r6.1-e2e",
    )
    if key in {"a", "b"}:
        task.mark_ready()
        task.mark_running()
    return task


def _plan_snapshot(
    *,
    workspace_id: UUID,
    root_task_id: UUID,
    subtask_tasks: Mapping[str, Task],
    worker_items: Mapping[str, WorkerItem],
    generation: int,
    parent_snapshot_id: UUID | None,
    snapshot_id: UUID,
    now: datetime,
) -> TaskPlanSnapshot:
    subtasks = tuple(
        SubtaskPlanItem(
            key=f"subtask-{key}",
            task_id=subtask_tasks[key].id,
            kind=WorkItemKind.CHILD,
            title=subtask_tasks[key].title,
            goal_summary=subtask_tasks[key].description,
            acceptance_refs=subtask_tasks[key].acceptance_criteria,
            milestone_key=None,
            expected_outputs=("artifact_type:diff",),
            source_refs=("r6.1-e2e",),
        )
        for key in ("a", "b", "c", "d")
    )
    workers = tuple(
        WorkerItemPlanItem(
            key=f"worker-{key}",
            worker_item_id=worker_items[key].id,
            subtask_key=f"subtask-{key}",
            title=worker_items[key].title,
            role="developer",
            responsibility=worker_items[key].responsibility,
            capability_requirements=("code",),
            worker_policy_ref="policy:r6.1-fake",
            configuration_ref=None,
            concurrency_limit=1,
            source_refs=("r6.1-e2e",),
        )
        for key in ("a", "b", "c", "d")
    )
    return TaskPlanSnapshot.create(
        snapshot_id=snapshot_id,
        schema_version=2,
        workspace_id=workspace_id,
        root_task_id=root_task_id,
        generation=generation,
        parent_snapshot_id=parent_snapshot_id,
        source_type="r6.1-e2e",
        source_ref=f"r6-1-g{generation}",
        title="Running revision control room",
        goal_summary="Preserve parallel work while revising one branch.",
        acceptance_refs=("acceptance:revision-review",),
        published_by="r6.1-e2e",
        published_at=now,
        subtasks=subtasks,
        worker_item_plans=workers,
        subtask_milestones=(),
        subtask_dependencies=(
            SubtaskDependency(
                upstream_subtask_key="subtask-a",
                downstream_subtask_key="subtask-c",
                kind=DependencyKind.HARD,
                required_evidence_refs=(),
                enforcement=DependencyEnforcement.CONTROL_PLANE,
            ),
            SubtaskDependency(
                upstream_subtask_key="subtask-b",
                downstream_subtask_key="subtask-c",
                kind=DependencyKind.HARD,
                required_evidence_refs=(),
                enforcement=DependencyEnforcement.CONTROL_PLANE,
            ),
        ),
        worker_item_dependencies=(),
    )


def _add_placements(
    uow: SqlAlchemyUnitOfWork,
    snapshot: TaskPlanSnapshot,
    subtask_tasks: Mapping[str, Task],
) -> None:
    for item in snapshot.worker_item_plans:
        key = item.key.removeprefix("worker-")
        uow.worker_items.add_placement(
            WorkerItemPlacement(
                plan_snapshot_id=snapshot.id,
                plan_generation=snapshot.generation,
                workspace_id=snapshot.workspace_id,
                root_task_id=snapshot.root_task_id,
                worker_item_id=item.worker_item_id,
                worker_item_key=item.key,
                subtask_key=item.subtask_key,
                subtask_task_id=subtask_tasks[key].id,
                role=item.role,
                capability_requirements=item.capability_requirements,
                worker_policy_ref=item.worker_policy_ref,
                configuration_ref=item.configuration_ref,
                created_at=snapshot.published_at,
            )
        )


@dataclass(frozen=True, slots=True)
class _ActiveExecution:
    execution: Execution
    lease: AssignmentLease
    session: ExecutionSession
    binding: ExecutionBinding


def _active_execution(
    *,
    execution_id: UUID,
    session_id: UUID,
    lease_id: UUID,
    worker_id: UUID,
    worker_item: WorkerItem,
    task: Task,
    workspace_id: UUID,
    adapter_instance: ModuleInstance,
    workspace_instance: ModuleInstance,
    capabilities: WorkerAdapterCapabilityProfile,
    now: datetime,
) -> _ActiveExecution:
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
        assignment_reason={"fixture": "r6.1-revision"},
    )
    execution.assign(
        lease_id=lease.id,
        worker_id=worker_id,
        fencing_token=lease.fencing_token,
    )
    execution.start(
        lease_id=lease.id,
        fencing_token=lease.fencing_token,
        now=now,
    )
    session = ExecutionSession.start(
        session_id=session_id,
        workspace_id=workspace_id,
        execution_id=execution_id,
        worker_id=worker_id,
        adapter_session_id=f"r6-1-fake-session:{worker_item.stable_key}",
        now=now,
        adapter_module_id=adapter_instance.id,
        adapter_config_version=adapter_instance.config_version,
        workspace_module_id=workspace_instance.id,
        workspace_config_version=workspace_instance.config_version,
        capability_snapshot=capabilities,
        process_ref=f"fake-process:{worker_item.stable_key}",
        workspace_ref=workspace_instance.instance_name,
    )
    binding = ExecutionBinding(
        id=uuid5(FIXTURE_NAMESPACE, f"{workspace_id}:binding:{worker_item.id}"),
        workspace_id=workspace_id,
        execution_id=execution_id,
        worker_id=worker_id,
        adapter_module_id=adapter_instance.id,
        workspace_module_id=workspace_instance.id,
        adapter_config_version=adapter_instance.config_version,
        workspace_config_version=workspace_instance.config_version,
        capability_snapshot=capabilities,
        lease_id=lease.id,
        fencing_token=lease.fencing_token,
        version=1,
    )
    return _ActiveExecution(execution, lease, session, binding)


def _active_worker_context(
    uow: SqlAlchemyUnitOfWork,
    *,
    workspace_id: UUID,
    execution: Execution,
    worker_item: WorkerItem,
    revision_id: UUID,
    package_id: UUID,
    assembly_id: UUID,
    now: datetime,
) -> tuple[ContextPackageManifestV1, ContextAssemblyRequest]:
    assembly = ContextAssemblyRequest.create(
        request_id=assembly_id,
        workspace_id=workspace_id,
        purpose=ContextPurpose.EXECUTION,
        consumer_type=ContextConsumerType.WORKER,
        consumer_id=execution.id,
        worker_item_id=worker_item.id,
        work_context_revision_id=revision_id,
        context_version=worker_item.context_version,
        knowledge_search_scope={"query": ""},
        knowledge_filter_refs=(),
        fact_scope_refs=("execution", "worker_item", "revision"),
        budget={"max_chars": 64_000},
        expires_at=now + timedelta(hours=1),
        request_fingerprint="b" * 64,
        idempotency_key=f"r6-1-fixture-context:{execution.id}",
        created_at=now,
    )
    uow.context_assemblies.add(assembly)
    claimed = uow.context_assemblies.claim_next(
        workspace_id=workspace_id,
        owner="r6-1-fixture",
        now=now,
        lease_ttl=timedelta(minutes=5),
        limit=1,
    )[0]
    package = ContextPackageManifestV1.create(
        package_id=package_id,
        workspace_id=workspace_id,
        purpose=ContextPurpose.EXECUTION,
        consumer_type=ContextConsumerType.WORKER,
        consumer_id=execution.id,
        worker_item_id=worker_item.id,
        work_context_revision_id=revision_id,
        context_version=worker_item.context_version,
        control_plane_fact_snapshot={
            "execution_id": str(execution.id),
            "worker_item_key": worker_item.stable_key,
            "fixture_generation": 3,
        },
        knowledge_refs=(),
        artifact_refs=(),
        evidence_refs=(),
        budget=ContextBudgetV1(
            profile_id=uuid5(FIXTURE_NAMESPACE, "execution-context-budget"),
            profile_version=1,
            max_sections=16,
            max_chars=64_000,
            max_input_tokens=16_000,
        ),
        expires_at=now + timedelta(hours=1),
        idempotency_key=claimed.idempotency_key,
        created_at=now,
    )
    return package, claimed


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


def _planning_profile(
    workspace_id: UUID,
    ids: Mapping[str, UUID],
    now: datetime,
) -> tuple[ModuleDefinition, ModuleInstance, PlanningProfile]:
    capabilities = PlannerAdapterCapabilityProfile(
        structured_output=StructuredOutputCapability.JSON_SCHEMA,
        clarification=True,
        idempotency=IdempotencyCapability.OPERATION_ID,
        cancellation=CancellationCapability.RUN,
        usage_reporting=UsageReportingCapability.TOKENS,
        max_context_bytes=128_000,
        stability=StabilityLevel.STABLE,
        interface_version="sidera.planner.v1",
        proposal_schema_versions=(2, 3),
        context_package_versions=(1,),
    )
    definition = ModuleDefinition(
        id=ids["planner_definition_id"],
        workspace_id=workspace_id,
        module_type="planner_adapter",
        provider="fake",
        interface_version="sidera.planner.v1",
        config_schema_version=1,
        capabilities=capabilities,
        lifecycle=ModuleLifecycle.ACTIVE,
    )
    instance = ModuleInstance(
        id=ids["planner_instance_id"],
        workspace_id=workspace_id,
        definition_id=definition.id,
        instance_name="r6-1-fake-planner",
        config_version=1,
        config_namespace="planning",
        config={"planning.scenario": "revision_control_room"},
        secret_refs=(),
        lifecycle=ModuleLifecycle.ACTIVE,
    )
    profile = PlanningProfile(
        id=ids["planning_profile_id"],
        workspace_id=workspace_id,
        profile_key="r6-1-fake-revision",
        version=1,
        display_name="R6.1 Fake Revision Planner",
        planner_module_instance_id=instance.id,
        planner_module_config_version=1,
        planner_interface_version="sidera.planner.v1",
        prompt_policy_version="prompt.r6.1",
        system_policy={"approval": {"required": True}},
        proposal_schema_version=3,
        allowed_capabilities=("code",),
        adapter_preferences=("fake",),
        adapter_forbidden=(),
        budget=PlanningBudgetPolicy(
            max_context_bytes=64_000,
            max_tokens=10_000,
            max_cost_microunits=1_000_000,
            max_runtime_seconds=600,
            max_revisions=3,
            max_nodes=20,
            max_concurrency=2,
        ),
        retry_policy=PlanningRetryPolicy(
            max_retries=1,
            backoff_seconds=(5,),
            unknown_outcome_policy=UnknownOutcomePolicy.BLOCK,
            invalid_output_policy=InvalidOutputPolicy.RETRY,
        ),
        default_priority=50,
        execution_max_attempts=2,
        execution_budget_level="standard",
        user_selectable=True,
        status=PlanningProfileStatus.ACTIVE,
        planner_secret_refs=(),
    )
    del now
    return definition, instance, profile


def _internal_command(
    *,
    command_id: UUID,
    workspace_id: UUID,
    command_type: str,
    target_id: UUID,
    idempotency_key: str,
) -> CommandEnvelope:
    return CommandEnvelope(
        id=command_id,
        workspace_id=workspace_id,
        command_type=command_type,
        target_type="context_package",
        actor_type="system",
        actor_id="r6-1-fixture",
        target_id=target_id,
        payload={},
        expected_version=None,
        idempotency_key=idempotency_key,
        correlation_id=command_id,
    )


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workspace-id", required=True)
    parser.add_argument(
        "--reset",
        action="store_true",
        help="reset the explicitly named R6.1 fixture workspace before seeding",
    )
    args = parser.parse_args(argv)
    if os.environ.get("SIDERA_ALLOW_E2E_SEED") != "1":
        raise SystemExit("SIDERA_ALLOW_E2E_SEED=1 is required for the R6.1 seed")
    database_url = os.environ.get("SIDERA_DATABASE_URL")
    if not database_url:
        raise SystemExit("SIDERA_DATABASE_URL is required for the R6.1 seed")
    try:
        workspace_id = UUID(args.workspace_id)
    except ValueError as exc:
        raise SystemExit("--workspace-id must be a UUID") from exc
    database = Database(database_url)
    try:
        fixture = seed_revision_fixture(database, workspace_id, reset=args.reset)
    finally:
        database.dispose()
    print(
        json.dumps(
            {
                "workspace_id": str(fixture.workspace_id),
                "root_task_id": str(fixture.root_task_id),
                "base_snapshot_id": str(fixture.base_snapshot_id),
                "planning_request_id": str(fixture.planning_request_id),
                "planning_profile_id": str(fixture.planning_profile_id),
                "worker_item_ids": {
                    key: str(value) for key, value in fixture.worker_item_ids.items()
                },
                "worker_b_package_hash_prefix": fixture.worker_b_package_hash[:12],
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
