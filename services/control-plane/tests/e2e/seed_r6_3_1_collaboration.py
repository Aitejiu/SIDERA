"""Deterministic R6.3.1 collaboration runtime fixture."""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import cast
from urllib.parse import urlsplit
from uuid import UUID, uuid5

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from sidera_control_plane.application.ports import WorkerItemPlacement
from sidera_control_plane.application.work_context_commands import (
    WorkContextRevisionFactory,
)
from sidera_control_plane.domain.collaboration import (
    CollaborationPolicyEntry,
    CollaborationPolicySnapshotV1,
    CollaborationStrategy,
)
from sidera_control_plane.domain.execution_requests import (
    DependencyEnforcement as RequestDependencyEnforcement,
)
from sidera_control_plane.domain.execution_requests import (
    ExecutionRequest,
    ExecutionRequestDependency,
)
from sidera_control_plane.domain.modules import (
    CollaborationRuntimeCapabilityProfile,
    IdempotencyCapability,
    ModuleDefinition,
    ModuleInstance,
    ModuleLifecycle,
    PlannerAdapterCapabilityProfile,
    SandboxCapability,
    StabilityLevel,
    WorkspaceProviderCapabilityProfile,
)
from sidera_control_plane.domain.planning import (
    PlanningRequest,
    PlanningRequestKind,
    PlanningUsage,
)
from sidera_control_plane.domain.planning_proposals import (
    DecisionSummary,
    ExecutionIntent,
    ImpactPolicy,
    PlanningProfileDescriptor,
    PlanningProposalPayload,
    ProposalRevision,
    ProposedRootTask,
    ProposedSubtask,
    ProposedWorkerItem,
)
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
from sidera_control_plane.domain.work_context import (
    WorkContext,
    WorkContextRevisionReason,
)
from sidera_control_plane.domain.worker import Worker
from sidera_control_plane.domain.worker_item import WorkerItem
from sidera_control_plane.domain.worker_runtime import WorkerRuntimeConfiguration
from sidera_control_plane.persistence.database import Database
from sidera_control_plane.persistence.models import WorkspaceModel
from sidera_control_plane.persistence.unit_of_work import SqlAlchemyUnitOfWork
from tests.e2e.seed_r6_1_revision import _planning_profile, _worker_capabilities

NOW = datetime(2026, 9, 3, 12, 0, tzinfo=UTC)
FIXTURE_NAMESPACE = UUID("f6310000-0000-0000-0000-000000000001")


@dataclass(frozen=True, slots=True)
class CollaborationSeed:
    workspace_id: UUID
    root_task_id: UUID
    plan_snapshot_id: UUID
    planning_request_id: UUID
    proposal_id: UUID
    runtime_definition_id: UUID
    runtime_instance_id: UUID
    subtask_ids: Mapping[str, UUID]
    worker_item_ids: Mapping[str, UUID]
    request_ids: Mapping[str, UUID]
    worker_ids: Mapping[str, UUID]

    def to_mapping(self) -> dict[str, object]:
        return {
            "workspace_id": str(self.workspace_id),
            "root_task_id": str(self.root_task_id),
            "plan_snapshot_id": str(self.plan_snapshot_id),
            "planning_request_id": str(self.planning_request_id),
            "proposal_id": str(self.proposal_id),
            "runtime_definition_id": str(self.runtime_definition_id),
            "runtime_instance_id": str(self.runtime_instance_id),
            "subtask_ids": {key: str(value) for key, value in self.subtask_ids.items()},
            "worker_item_ids": {
                key: str(value) for key, value in self.worker_item_ids.items()
            },
            "request_ids": {key: str(value) for key, value in self.request_ids.items()},
            "worker_ids": {key: str(value) for key, value in self.worker_ids.items()},
        }


def seed_collaboration_plan(
    database: Database,
    workspace_id: UUID,
    *,
    now: datetime = NOW,
    reset: bool = False,
) -> CollaborationSeed:
    """Create one materialized plan with a two-upstream fan-in gate."""
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("fixture timestamp must be timezone-aware")
    ids = _ids(workspace_id)

    if reset:
        from tests.integration.database_fixtures import _clear_database

        _clear_database(database)

    with database.session() as session:
        workspace = session.get(WorkspaceModel, workspace_id)
        if workspace is None:
            session.add(
                WorkspaceModel(
                    id=workspace_id,
                    name="SIDERA R6.3.1 Collaboration Runtime",
                    status="active",
                    version=1,
                    created_at=now,
                    updated_at=now,
                )
            )
        session.commit()

    root_task = Task.create(
        task_id=ids["root_task_id"],
        workspace_id=workspace_id,
        title="Collaboration runtime rollout",
        description="Run a deterministic fan-in plan through the collaboration control loop.",
        acceptance_criteria=("acceptance:collaboration-rollout",),
        priority=80,
        requested_by="r6.3.1-e2e",
    )
    root_task.mark_ready()
    root_task.mark_running()

    subtask_specs = {
        "upstream-a": (
            "Prepare upstream A",
            "Produce the first independent implementation result.",
            "acceptance:upstream-a",
        ),
        "upstream-b": (
            "Prepare upstream B",
            "Produce the second independent result and verification evidence.",
            "acceptance:upstream-b",
        ),
        "downstream": (
            "Integrate downstream",
            "Integrate both upstream results after required evidence is present.",
            "acceptance:downstream",
        ),
    }
    subtasks = {
        key: _subtask_task(
            ids[f"subtask_{key}_id"],
            workspace_id,
            key,
            title=spec[0],
            description=spec[1],
            acceptance_ref=spec[2],
        )
        for key, spec in subtask_specs.items()
    }
    worker_items = {
        key: WorkerItem.create(
            item_id=ids[f"worker_item_{key}_id"],
            workspace_id=workspace_id,
            root_task_id=root_task.id,
            stable_key=key,
            title=spec[0],
            responsibility=spec[1],
            now=now,
        )
        for key, spec in subtask_specs.items()
    }
    workers = {
        key: Worker.register(
            worker_id=ids[f"worker_{key}_id"],
            workspace_id=workspace_id,
            adapter_type="fake",
            display_name=f"R6.3.1 Fake {key}",
            capabilities=frozenset({"code"}),
            capacity=1,
            cost_per_hour_microunits=0,
            startup_latency_ms=0,
            now=now,
        )
        for key in subtask_specs
    }

    planner_definition, planner_instance, base_profile = _planning_profile(
        workspace_id,
        ids,
        now,
    )
    profile = replace(
        base_profile,
        profile_key="r6-3-1-collaboration",
        display_name="R6.3.1 Collaboration Fixture Planner",
        proposal_schema_version=2,
    )
    profile_snapshot = profile.snapshot(
        cast(PlannerAdapterCapabilityProfile, planner_definition.capabilities)
    )

    worker_adapter_capabilities = _worker_capabilities()
    worker_adapter_definition = ModuleDefinition(
        id=ids["worker_adapter_definition_id"],
        workspace_id=workspace_id,
        module_type="worker_adapter",
        provider="fake",
        interface_version="sidera.worker-adapter.v1",
        config_schema_version=1,
        capabilities=worker_adapter_capabilities,
        lifecycle=ModuleLifecycle.ACTIVE,
    )
    worker_adapter_instance = ModuleInstance(
        id=ids["worker_adapter_instance_id"],
        workspace_id=workspace_id,
        definition_id=worker_adapter_definition.id,
        instance_name="r6-3-1-fake-worker-adapter",
        config_version=1,
        config_namespace="adapter",
        config={"adapter.mode": "deterministic-test"},
        secret_refs=(),
        lifecycle=ModuleLifecycle.ACTIVE,
    )
    workspace_provider_capabilities = WorkspaceProviderCapabilityProfile(
        isolation=SandboxCapability.ISOLATED_RUNTIME,
        writable=True,
        git_worktree=True,
        stability=StabilityLevel.STABLE,
    )
    workspace_definition = ModuleDefinition(
        id=ids["workspace_definition_id"],
        workspace_id=workspace_id,
        module_type="workspace_provider",
        provider="fake-workspace",
        interface_version="sidera.workspace-provider.v1",
        config_schema_version=1,
        capabilities=workspace_provider_capabilities,
        lifecycle=ModuleLifecycle.ACTIVE,
    )
    workspace_instance = ModuleInstance(
        id=ids["workspace_instance_id"],
        workspace_id=workspace_id,
        definition_id=workspace_definition.id,
        instance_name="r6-3-1-fake-workspace",
        config_version=1,
        config_namespace="workspace",
        config={"workspace.mode": "deterministic-test"},
        secret_refs=(),
        lifecycle=ModuleLifecycle.ACTIVE,
    )

    runtime_capability = CollaborationRuntimeCapabilityProfile(
        interface_version="collaboration-runtime.v1",
        projection_schema_versions=(1,),
        decision_schema_versions=(1,),
        strategy_ids=(CollaborationStrategy.ALL_REQUIRED.value,),
        idempotency=IdempotencyCapability.OPERATION_ID,
        max_projection_bytes=262_144,
        max_decision_bytes=65_536,
        max_intents_per_decision=128,
        max_strategy_state_bytes=16_384,
        stability=StabilityLevel.STABLE,
    )
    runtime_definition = ModuleDefinition(
        id=ids["runtime_definition_id"],
        workspace_id=workspace_id,
        module_type="collaboration_runtime",
        provider="reference",
        interface_version="collaboration-runtime.v1",
        config_schema_version=1,
        capabilities=runtime_capability,
        lifecycle=ModuleLifecycle.ACTIVE,
    )
    runtime_instance = ModuleInstance(
        id=ids["runtime_instance_id"],
        workspace_id=workspace_id,
        definition_id=runtime_definition.id,
        instance_name="r6-3-1-reference-collaboration-runtime",
        config_version=1,
        config_namespace="collaboration",
        config={"collaboration.strategy": "all_required"},
        secret_refs=(),
        lifecycle=ModuleLifecycle.ACTIVE,
    )
    policy = CollaborationPolicySnapshotV1(
        schema_version=1,
        runtime_definition_id=runtime_definition.id,
        runtime_instance_id=runtime_instance.id,
        runtime_interface_version=runtime_definition.interface_version,
        runtime_config_version=runtime_instance.config_version,
        runtime_capability_snapshot=runtime_capability,
        default_policy=CollaborationPolicyEntry(
            enabled=True,
            strategy_id=CollaborationStrategy.ALL_REQUIRED,
        ),
        subtask_overrides=(),
    )

    plan_snapshot = _plan_snapshot(
        workspace_id=workspace_id,
        root_task=root_task,
        subtasks=subtasks,
        worker_items=worker_items,
        policy=policy,
        snapshot_id=ids["plan_snapshot_id"],
        now=now,
    )
    planning_request = PlanningRequest.create(
        request_id=ids["planning_request_id"],
        workspace_id=workspace_id,
        profile_snapshot=profile_snapshot,
        objective="Run the fan-in collaboration runtime fixture.",
        constraints=("Preserve the exact WorkerItem identities.",),
        acceptance_criteria=("acceptance:collaboration-rollout",),
        context_refs=(),
        requested_outputs=("plan",),
        source_type="r6.3.1-e2e",
        source_ref="collaboration-runtime-fixture",
        actor="r6.3.1-e2e",
        correlation_id=ids["planning_correlation_id"],
        run_id=ids["planning_run_id"],
        now=now,
    )
    proposal = _proposal(
        planning_request=planning_request,
        profile=profile_snapshot,
        subtasks=subtasks,
        worker_items=worker_items,
        now=now,
        proposal_id=ids["proposal_id"],
    )
    requests = {
        key: _execution_request(
            request_id=ids[f"request_{key}_id"],
            planning_request=planning_request,
            proposal=proposal,
            snapshot=plan_snapshot,
            root_task=root_task,
            subtask=subtasks[key],
            worker_item=worker_items[key],
            key=key,
            now=now,
        )
        for key in subtask_specs
    }
    dependencies = (
        ExecutionRequestDependency(
            downstream_request_id=requests["downstream"].id,
            upstream_request_id=requests["upstream-a"].id,
            enforcement=RequestDependencyEnforcement.HARD,
            required_artifact_refs=("artifact_type:diff",),
            required_evidence_refs=(),
        ),
        ExecutionRequestDependency(
            downstream_request_id=requests["downstream"].id,
            upstream_request_id=requests["upstream-b"].id,
            enforcement=RequestDependencyEnforcement.HARD,
            required_artifact_refs=("artifact_type:diff",),
            required_evidence_refs=("acceptance:verification-report",),
        ),
    )
    context_revision = WorkContextRevisionFactory().create(
        work_context_id=ids["work_context_id"],
        workspace_id=workspace_id,
        brief=planning_request.briefs[0],
        revision_number=1,
        reason=WorkContextRevisionReason.REQUEST_CREATED,
        actor="r6.3.1-e2e",
        created_at=now,
        revision_id=ids["work_context_revision_id"],
    )
    context = WorkContext.create(
        work_context_id=context_revision.work_context_id,
        workspace_id=workspace_id,
        planning_request_id=planning_request.id,
        first_revision=context_revision,
        created_at=now,
    )
    context.bind_root_task(root_task.id, expected_version=1, now=now)

    with SqlAlchemyUnitOfWork(database) as uow:
        for definition, instance in (
            (planner_definition, planner_instance),
            (worker_adapter_definition, worker_adapter_instance),
            (workspace_definition, workspace_instance),
            (runtime_definition, runtime_instance),
        ):
            uow.modules.add_definition(definition)
            uow.modules.add_instance(instance)
        uow.planning_profiles.add(profile)
        uow.tasks.add(root_task)
        for task in subtasks.values():
            uow.tasks.add(task)
        for item in worker_items.values():
            uow.worker_items.add(item)
        for worker in workers.values():
            uow.workers.add(worker)
            uow.worker_runtime_configurations.add(
                WorkerRuntimeConfiguration.create(
                    worker_id=worker.id,
                    workspace_id=workspace_id,
                    adapter_module_instance_id=worker_adapter_instance.id,
                    adapter_module_config_version=worker_adapter_instance.config_version,
                    workspace_module_instance_id=workspace_instance.id,
                    workspace_module_config_version=workspace_instance.config_version,
                )
            )
        uow.flush()
        uow.plans.add_snapshot_and_advance_head(plan_snapshot, expected_head=None)
        _add_placements(uow, plan_snapshot, subtasks)
        uow.planning_requests.add(planning_request)
        uow.work_contexts.add(context, context_revision)
        uow.flush()
        uow.planning_records.add_proposal(proposal, workspace_id)
        uow.flush()
        for request in requests.values():
            uow.execution_requests.add(request)
        uow.flush()
        for dependency in dependencies:
            uow.execution_requests.add_dependency(dependency)
        uow.projection_changes.changed(
            workspace_id=workspace_id,
            projection_type="collaboration_runtime",
            resource_id=root_task.id,
            change_type="plan.published",
            resource_version=plan_snapshot.generation,
            correlation_id=plan_snapshot.id,
        )

        stored = uow.planning_requests.get(planning_request.id, workspace_id)
        if stored is None:
            raise RuntimeError("fixture planning request was not persisted")
        previous_request_version = stored.version
        stored_run = stored.run(stored.current_run_id)
        previous_run_version = stored_run.version
        stored.start_planning(
            run_id=stored.current_run_id,
            lease_id=ids["planning_lease_id"],
            fencing_token=1,
            now=now,
        )
        stored.succeed_run(
            run_id=stored.current_run_id,
            lease_id=ids["planning_lease_id"],
            fencing_token=1,
            provider_ref="fake-planner",
            structured_result_type="planning-proposal",
            usage=PlanningUsage.zero(),
            now=now,
        )
        stored.mark_proposal_ready(
            run_id=stored.current_run_id,
            proposal_revision=proposal.revision,
            content_hash=proposal.content_hash,
            now=now,
        )
        stored.materialize(
            proposal_revision=proposal.revision,
            content_hash=proposal.content_hash,
            now=now,
        )
        uow.planning_runs.save(stored.run(stored.current_run_id), previous_run_version)
        uow.planning_requests.save(stored, previous_request_version)

    return CollaborationSeed(
        workspace_id=workspace_id,
        root_task_id=root_task.id,
        plan_snapshot_id=plan_snapshot.id,
        planning_request_id=planning_request.id,
        proposal_id=proposal.id,
        runtime_definition_id=runtime_definition.id,
        runtime_instance_id=runtime_instance.id,
        subtask_ids={key: task.id for key, task in subtasks.items()},
        worker_item_ids={key: item.id for key, item in worker_items.items()},
        request_ids={key: request.id for key, request in requests.items()},
        worker_ids={key: worker.id for key, worker in workers.items()},
    )


def _subtask_task(
    task_id: UUID,
    workspace_id: UUID,
    key: str,
    *,
    title: str,
    description: str,
    acceptance_ref: str,
) -> Task:
    return Task.create(
        task_id=task_id,
        workspace_id=workspace_id,
        title=title,
        description=description,
        acceptance_criteria=(acceptance_ref,),
        priority=60,
        requested_by="r6.3.1-e2e",
    )


def _plan_snapshot(
    *,
    workspace_id: UUID,
    root_task: Task,
    subtasks: Mapping[str, Task],
    worker_items: Mapping[str, WorkerItem],
    policy: CollaborationPolicySnapshotV1,
    snapshot_id: UUID,
    now: datetime,
) -> TaskPlanSnapshot:
    subtask_plans = tuple(
        SubtaskPlanItem(
            key=f"subtask-{key}",
            task_id=task.id,
            kind=WorkItemKind.CHILD,
            title=task.title,
            goal_summary=task.description,
            acceptance_refs=task.acceptance_criteria,
            milestone_key=None,
            expected_outputs=("artifact_type:diff",),
            source_refs=("r6.3.1-e2e",),
        )
        for key, task in subtasks.items()
    )
    worker_plans = tuple(
        WorkerItemPlanItem(
            key=key,
            worker_item_id=item.id,
            subtask_key=f"subtask-{key}",
            title=item.title,
            role="developer",
            responsibility=item.responsibility,
            capability_requirements=("code",),
            worker_policy_ref="policy:r6.3.1-fake",
            configuration_ref=None,
            concurrency_limit=1,
            source_refs=("r6.3.1-e2e",),
        )
        for key, item in worker_items.items()
    )
    return TaskPlanSnapshot.create(
        snapshot_id=snapshot_id,
        schema_version=3,
        workspace_id=workspace_id,
        root_task_id=root_task.id,
        generation=1,
        parent_snapshot_id=None,
        source_type="r6.3.1-e2e",
        source_ref="collaboration-runtime-fixture",
        title=root_task.title,
        goal_summary=root_task.description,
        acceptance_refs=root_task.acceptance_criteria,
        published_by="r6.3.1-e2e",
        published_at=now,
        subtasks=subtask_plans,
        worker_item_plans=worker_plans,
        subtask_milestones=(),
        subtask_dependencies=(
            SubtaskDependency(
                upstream_subtask_key="subtask-upstream-a",
                downstream_subtask_key="subtask-downstream",
                kind=DependencyKind.HARD,
                required_evidence_refs=(),
                enforcement=PlanDependencyEnforcement.CONTROL_PLANE,
            ),
            SubtaskDependency(
                upstream_subtask_key="subtask-upstream-b",
                downstream_subtask_key="subtask-downstream",
                kind=DependencyKind.HARD,
                required_evidence_refs=("acceptance:verification-report",),
                enforcement=PlanDependencyEnforcement.CONTROL_PLANE,
            ),
        ),
        worker_item_dependencies=(),
        collaboration_policy=policy,
    )


def _proposal(
    *,
    planning_request: PlanningRequest,
    profile,
    subtasks: Mapping[str, Task],
    worker_items: Mapping[str, WorkerItem],
    now: datetime,
    proposal_id: UUID,
) -> ProposalRevision:
    proposed_subtasks = tuple(
        ProposedSubtask(
            key=f"subtask-{key}",
            kind=WorkItemKind.CHILD,
            title=task.title,
            goal_summary=task.description,
            acceptance_refs=task.acceptance_criteria,
            expected_outputs=("artifact_type:diff",),
            milestone_key=None,
            source_refs=("r6.3.1-e2e",),
        )
        for key, task in subtasks.items()
    )
    proposed_workers = tuple(
        ProposedWorkerItem(
            key=key,
            subtask_key=f"subtask-{key}",
            title=item.title,
            role="developer",
            responsibility=item.responsibility,
            capability_requirements=("code",),
            worker_policy_ref="policy:r6.3.1-fake",
            configuration_ref=None,
            concurrency_limit=1,
            source_refs=("r6.3.1-e2e",),
        )
        for key, item in worker_items.items()
    )
    evidence_by_key = {
        "upstream-a": "acceptance:upstream-a",
        "upstream-b": "acceptance:verification-report",
        "downstream": "acceptance:downstream",
    }
    intents = tuple(
        ExecutionIntent(
            worker_item_key=key,
            purpose=item.responsibility,
            context_refs=(),
            expected_artifact_refs=("artifact_type:diff",),
            required_evidence_refs=(evidence_by_key[key],),
            capability_requirements=("code",),
            adapter_preferences=("fake",),
            budget_class="standard",
            deadline_seconds=None,
            not_before_seconds=0,
            max_attempts=2,
            activation_dependencies=(),
        )
        for key, item in worker_items.items()
    )
    payload = PlanningProposalPayload(
        schema_version=2,
        kind=PlanningRequestKind.CREATE,
        root_task=ProposedRootTask(
            title="Collaboration runtime rollout",
            goal_summary="Run a deterministic fan-in plan through the collaboration control loop.",
            acceptance_refs=("acceptance:collaboration-rollout",),
        ),
        subtasks=proposed_subtasks,
        worker_items=proposed_workers,
        subtask_milestones=(),
        subtask_dependencies=(
            SubtaskDependency(
                upstream_subtask_key="subtask-upstream-a",
                downstream_subtask_key="subtask-downstream",
                kind=DependencyKind.HARD,
                required_evidence_refs=(),
                enforcement=PlanDependencyEnforcement.CONTROL_PLANE,
            ),
            SubtaskDependency(
                upstream_subtask_key="subtask-upstream-b",
                downstream_subtask_key="subtask-downstream",
                kind=DependencyKind.HARD,
                required_evidence_refs=("acceptance:verification-report",),
                enforcement=PlanDependencyEnforcement.CONTROL_PLANE,
            ),
        ),
        worker_item_dependencies=(),
        execution_intents=intents,
        decision_summary=DecisionSummary(
            understanding="Two upstream WorkerItems feed one downstream integration after evidence is verified.",
            assumptions=("The fake adapter produces one diff artifact per successful execution.",),
            risks=("The downstream request remains blocked until verification evidence is recorded.",),
            estimated_cost_microunits=0,
            estimated_runtime_seconds=60,
            estimated_concurrency=2,
            scope_includes=("fan-in collaboration gate",),
            scope_excludes=("external provider calls",),
            metadata={"fixture": "r6.3.1"},
        ),
        impact_policy=ImpactPolicy(
            activate_immediately=True,
            budget_level="standard",
            failure_policy="block",
        ),
        base_descriptor=None,
    )
    proposal = ProposalRevision.create(
        planning_request_id=planning_request.id,
        revision=1,
        brief_revision=planning_request.current_brief_revision,
        planning_run_id=planning_request.current_run_id,
        kind=PlanningRequestKind.CREATE,
        payload=payload,
        profile_descriptor=PlanningProfileDescriptor.from_snapshot(profile),
        profile_snapshot=profile,
        created_at=now,
    )
    return replace(proposal, id=proposal_id)


def _execution_request(
    *,
    request_id: UUID,
    planning_request: PlanningRequest,
    proposal: ProposalRevision,
    snapshot: TaskPlanSnapshot,
    root_task: Task,
    subtask: Task,
    worker_item: WorkerItem,
    key: str,
    now: datetime,
) -> ExecutionRequest:
    evidence_ref = {
        "upstream-a": "acceptance:upstream-a",
        "upstream-b": "acceptance:verification-report",
        "downstream": "acceptance:downstream",
    }[key]
    return ExecutionRequest.create(
        request_id=request_id,
        workspace_id=planning_request.workspace_id,
        planning_request_id=planning_request.id,
        proposal_revision_id=proposal.id,
        plan_snapshot_id=snapshot.id,
        plan_generation=snapshot.generation,
        root_task_id=root_task.id,
        subtask_id=subtask.id,
        worker_item_id=worker_item.id,
        worker_item_key=worker_item.stable_key,
        intent_key=f"intent:{key}",
        purpose=worker_item.responsibility,
        capability_requirements=frozenset({"code"}),
        adapter_preferences=("fake",),
        budget_class="standard",
        not_before=now,
        max_attempts=2,
        expected_artifact_refs=("artifact_type:diff",),
        expected_evidence_refs=(evidence_ref,),
        expected_outputs=("diff",),
        collaboration_gate_mode="release_required",
        now=now,
    )


def _add_placements(
    uow: SqlAlchemyUnitOfWork,
    snapshot: TaskPlanSnapshot,
    subtasks: Mapping[str, Task],
) -> None:
    for item in snapshot.worker_item_plans:
        subtask_key = item.subtask_key.removeprefix("subtask-")
        if item.worker_item_id is None:
            raise RuntimeError(f"fixture placement is missing WorkerItem: {item.key}")
        uow.worker_items.add_placement(
            WorkerItemPlacement(
                plan_snapshot_id=snapshot.id,
                plan_generation=snapshot.generation,
                workspace_id=snapshot.workspace_id,
                root_task_id=snapshot.root_task_id,
                worker_item_id=item.worker_item_id,
                worker_item_key=item.key,
                subtask_key=item.subtask_key,
                subtask_task_id=subtasks[subtask_key].id,
                role=item.role,
                capability_requirements=item.capability_requirements,
                worker_policy_ref=item.worker_policy_ref,
                configuration_ref=item.configuration_ref,
                created_at=snapshot.published_at,
            )
        )


def _ids(workspace_id: UUID) -> dict[str, UUID]:
    labels = (
        "root_task_id",
        "subtask_upstream-a_id",
        "subtask_upstream-b_id",
        "subtask_downstream_id",
        "worker_item_upstream-a_id",
        "worker_item_upstream-b_id",
        "worker_item_downstream_id",
        "worker_upstream-a_id",
        "worker_upstream-b_id",
        "worker_downstream_id",
        "plan_snapshot_id",
        "planning_request_id",
        "planning_run_id",
        "planning_lease_id",
        "planning_correlation_id",
        "proposal_id",
        "work_context_id",
        "work_context_revision_id",
        "planner_definition_id",
        "planner_instance_id",
        "planning_profile_id",
        "worker_adapter_definition_id",
        "worker_adapter_instance_id",
        "workspace_definition_id",
        "workspace_instance_id",
        "runtime_definition_id",
        "runtime_instance_id",
        "request_upstream-a_id",
        "request_upstream-b_id",
        "request_downstream_id",
    )
    return {
        label: uuid5(FIXTURE_NAMESPACE, f"{workspace_id}:{label}")
        for label in labels
    }


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workspace-id", required=True)
    parser.add_argument("--reset", action="store_true")
    args = parser.parse_args(argv)
    if os.environ.get("SIDERA_ALLOW_E2E_SEED") != "1":
        raise SystemExit("SIDERA_ALLOW_E2E_SEED=1 is required for the R6.3.1 seed")
    database_url = os.environ.get("SIDERA_DATABASE_URL")
    if not database_url:
        raise SystemExit("SIDERA_DATABASE_URL is required for the R6.3.1 seed")
    parsed = urlsplit(database_url)
    if parsed.hostname not in {"127.0.0.1", "localhost", "::1"}:
        raise SystemExit("R6.3.1 E2E seed requires a loopback database host")
    try:
        workspace_id = UUID(args.workspace_id)
    except ValueError as exc:
        raise SystemExit("--workspace-id must be a UUID") from exc
    database = Database(database_url)
    try:
        seed = seed_collaboration_plan(database, workspace_id, reset=args.reset)
    finally:
        database.dispose()
    print(json.dumps(seed.to_mapping(), sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
