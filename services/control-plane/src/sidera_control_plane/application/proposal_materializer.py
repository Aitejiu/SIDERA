from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
from uuid import UUID, uuid4

from sidera_control_plane.application.collaboration_decisions import (
    CollaborationGateFact,
    ComposedActivationGate,
)
from sidera_control_plane.application.errors import NotFound
from sidera_control_plane.application.ports import ControlPlaneUnitOfWork
from sidera_control_plane.application.work_context_commands import (
    WorkContextRevisionFactory,
    work_context_bound_event,
    work_context_event,
)
from sidera_control_plane.application.worker_item_materializer import (
    MaterializedPlan,
    WorkerItemMaterializer,
)
from sidera_control_plane.domain.collaboration import (
    CollaborationGateMode,
    CollaborationPolicySnapshotV1,
    CollaborationPolicySnapshotV2,
    resolve_collaboration_policy,
    resolve_collaboration_policy_v2,
)
from sidera_control_plane.domain.commands import CommandEnvelope
from sidera_control_plane.domain.errors import InvalidTransition, ValidationError
from sidera_control_plane.domain.events import EventData
from sidera_control_plane.domain.execution_requests import (
    ActivationGate,
    DependencyEnforcement,
    ExecutionRequest,
    ExecutionRequestDependency,
    collaboration_gate_mode_for_policy,
)
from sidera_control_plane.domain.planning import (
    PlanningRequest,
    PlanningRequestStatus,
    ProposalMaterialization,
)
from sidera_control_plane.domain.planning_profiles import PlanningProfile
from sidera_control_plane.domain.planning_proposals import (
    PlanningProposalPayload,
    PlanningProposalPayloadV4,
    PlanningProposalPayloadV5,
    ProposalRevision,
    proposal_content_hash,
)
from sidera_control_plane.domain.planning_revision_proposals import (
    PlanningRevisionProposalPayloadV3,
    PlanningRevisionProposalPayloadV4,
)
from sidera_control_plane.domain.plans import (
    DependencyKind,
    SubtaskPlanItem,
    TaskPlanSnapshot,
    WorkerItemDependency,
    WorkerItemPlanItem,
)
from sidera_control_plane.domain.task import Task
from sidera_control_plane.domain.work_context import WorkContextRevisionReason


@dataclass(frozen=True, slots=True)
class ProposalMaterializationResult:
    materialization: ProposalMaterialization
    snapshot: TaskPlanSnapshot | None
    tasks: tuple[Task, ...]
    execution_requests: tuple[ExecutionRequest, ...]
    event_ids: tuple[UUID, ...]

    @property
    def execution_ids(self) -> tuple[UUID, ...]:
        return ()


class ProposalMaterializer:
    """Materialize one confirmed proposal without dispatching another command."""

    def __init__(
        self, worker_item_materializer: WorkerItemMaterializer | None = None
    ) -> None:
        self._worker_item_materializer = (
            worker_item_materializer or WorkerItemMaterializer()
        )
        self._work_context_factory = WorkContextRevisionFactory()

    def materialize(
        self,
        uow: ControlPlaneUnitOfWork,
        *,
        request: PlanningRequest,
        proposal: ProposalRevision,
        profile: PlanningProfile,
        envelope: CommandEnvelope,
        now: datetime,
    ) -> ProposalMaterializationResult:
        previous_request_version = request.version
        self._validate_input(request, proposal, profile, envelope)
        collaboration_policy = self._resolve_collaboration_policy(request, proposal)

        existing = uow.proposal_materializations.get_by_request(
            request.id, request.workspace_id
        )
        if existing is not None:
            if (
                existing.proposal_revision_id != proposal.id
                or existing.plan_generation != 1
            ):
                raise ValidationError(
                    "planning request is already materialized with another proposal"
                )
            return ProposalMaterializationResult(
                materialization=existing,
                snapshot=None,
                tasks=(),
                execution_requests=(),
                event_ids=(),
            )

        if request.status is not PlanningRequestStatus.PROPOSAL_READY:
            raise InvalidTransition("only a proposal-ready request can materialize")
        profile.assert_user_selectable()
        work_context = uow.work_contexts.get_by_planning_request(
            request.id, request.workspace_id, for_update=True
        )
        if work_context is None:
            raise NotFound("work context not found")
        current_context_revision = uow.work_contexts.get_revision(
            work_context.current_revision_id,
            request.workspace_id,
        )
        if current_context_revision is None:
            raise NotFound("work context current revision not found")
        previous_context_version = work_context.version
        brief = next(
            (
                candidate
                for candidate in request.briefs
                if candidate.revision == proposal.brief_revision
            ),
            None,
        )
        if brief is None:
            raise ValidationError("proposal brief revision is not available")

        root_task_id = uuid4()
        root_task = Task.create(
            task_id=root_task_id,
            workspace_id=request.workspace_id,
            title=proposal.payload.root_task.title,
            description=proposal.payload.root_task.goal_summary,
            acceptance_criteria=proposal.payload.root_task.acceptance_refs,
            priority=profile.default_priority,
            requested_by=envelope.actor_id,
        )
        subtasks = tuple(
            Task.create(
                task_id=uuid4(),
                workspace_id=request.workspace_id,
                title=item.title,
                description=item.goal_summary,
                acceptance_criteria=item.acceptance_refs,
                priority=profile.default_priority,
                requested_by=envelope.actor_id,
            )
            for item in proposal.payload.subtasks
        )
        for task in (root_task, *subtasks):
            uow.tasks.add(task)

        subtask_task_mapping = {
            item.key: task.id
            for item, task in zip(proposal.payload.subtasks, subtasks, strict=True)
        }
        snapshot = self._snapshot(
            request=request,
            proposal=proposal,
            root_task_id=root_task.id,
            subtask_task_mapping=subtask_task_mapping,
            published_by=envelope.actor_id,
            now=now,
            collaboration_policy=collaboration_policy,
        )
        materialized_plan = self._worker_item_materializer.materialize(
            uow, snapshot, now=now
        )
        uow.plans.add_snapshot_and_advance_head(
            materialized_plan.snapshot, expected_head=None
        )

        execution_requests, dependencies = self._execution_requests(
            request=request,
            proposal=proposal,
            materialized_plan=materialized_plan,
            subtask_task_mapping=subtask_task_mapping,
            now=now,
            collaboration_policy=collaboration_policy,
        )
        self._evaluate_initial_gates(execution_requests, dependencies, now=now)
        for execution_request in execution_requests:
            uow.execution_requests.add(execution_request)
        for dependency in dependencies:
            uow.execution_requests.add_dependency(dependency)

        worker_item_mapping = {
            item.key: item.worker_item_id
            for item in materialized_plan.canonical.worker_items
        }
        if any(value is None for value in worker_item_mapping.values()):
            raise ValidationError(
                "WorkerItem materialization did not resolve every key"
            )
        resolved_worker_item_mapping = {
            key: value
            for key, value in worker_item_mapping.items()
            if value is not None
        }
        execution_request_mapping = {
            item.worker_item_key: item.id for item in execution_requests
        }
        materialization = ProposalMaterialization(
            id=uuid4(),
            workspace_id=request.workspace_id,
            planning_request_id=request.id,
            proposal_revision_id=proposal.id,
            root_task_id=root_task.id,
            plan_snapshot_id=materialized_plan.snapshot.id,
            plan_generation=materialized_plan.snapshot.generation,
            subtask_task_mapping=subtask_task_mapping,
            worker_item_mapping=resolved_worker_item_mapping,
            execution_request_mapping=execution_request_mapping,
            deep_link=(f"/workspaces/{request.workspace_id}/tasks/{root_task.id}"),
            confirmed_by=envelope.actor_id,
            created_at=now,
        )
        request.materialize(
            proposal_revision=proposal.revision,
            content_hash=proposal.content_hash,
            now=now,
        )
        uow.planning_requests.save(request, previous_request_version)
        uow.proposal_materializations.add(materialization)
        proposal_decisions = self._work_context_factory.accepted_proposal_decisions(
            proposal,
            ordinal_offset=len(current_context_revision.snapshot.accepted_decisions),
        )
        context_revision = self._work_context_factory.create(
            work_context_id=work_context.id,
            workspace_id=request.workspace_id,
            brief=brief,
            revision_number=work_context.current_revision_number + 1,
            reason=WorkContextRevisionReason.PROPOSAL_CONFIRMED,
            actor=envelope.actor_id,
            created_at=now,
            base_snapshot=current_context_revision.snapshot,
            accepted_decisions=(
                *current_context_revision.snapshot.accepted_decisions,
                *proposal_decisions,
            ),
            proposal_revision_id=proposal.id,
            plan_snapshot_id=materialized_plan.snapshot.id,
        )
        uow.work_contexts.bind_root_task(
            work_context,
            root_task.id,
            context_revision,
            expected_version=previous_context_version,
        )

        events = self._events(
            envelope=envelope,
            request=request,
            proposal=proposal,
            root_task=root_task,
            subtasks=subtasks,
            materialized_plan=materialized_plan,
            execution_requests=execution_requests,
            materialization=materialization,
        )
        events = (
            *events,
            work_context_event(
                envelope,
                context=work_context,
                revision=context_revision,
                event_type="WorkContextRevised",
            ),
            work_context_bound_event(
                envelope,
                context=work_context,
                revision=context_revision,
                root_task_id=root_task.id,
            ),
        )
        for event in events:
            uow.events.append_with_outbox(event)

        return ProposalMaterializationResult(
            materialization=materialization,
            snapshot=materialized_plan.snapshot,
            tasks=(root_task, *subtasks),
            execution_requests=execution_requests,
            event_ids=tuple(event.id for event in events),
        )

    @staticmethod
    def _validate_input(
        request: PlanningRequest,
        proposal: ProposalRevision,
        profile: PlanningProfile,
        envelope: CommandEnvelope,
    ) -> None:
        if proposal.planning_request_id != request.id:
            raise ValidationError("proposal does not belong to the planning request")
        if request.workspace_id != envelope.workspace_id:
            raise ValidationError("planning request workspace does not match command")
        if request.current_proposal_revision != proposal.revision:
            raise ValidationError("proposal revision does not match current proposal")
        if request.current_proposal_content_hash != proposal.content_hash:
            raise ValidationError(
                "proposal content hash does not match current proposal"
            )
        revision = _positive_int(envelope.payload, "proposal_revision")
        content_hash = _content_hash(envelope.payload, "proposal_content_hash")
        if revision != proposal.revision:
            raise ValidationError("proposal revision does not match current proposal")
        if content_hash != proposal.content_hash:
            raise ValidationError(
                "proposal content hash does not match current proposal"
            )
        profile_id = _uuid(envelope.payload, "profile_id")
        profile_version = _positive_int(envelope.payload, "profile_version")
        if profile.id != profile_id or profile.version != profile_version:
            raise ValidationError("planning profile does not match confirmation")
        if request.planning_profile_id != profile.id:
            raise ValidationError(
                "planning request profile does not match confirmation"
            )
        if request.planning_profile_version != profile.version:
            raise ValidationError(
                "planning request profile version does not match confirmation"
            )
        snapshot = request.profile_snapshot
        if (
            snapshot.profile_id != profile.id
            or snapshot.profile_version != profile.version
            or snapshot.workspace_id != request.workspace_id
            or snapshot.planner_module_instance_id != profile.planner_module_instance_id
            or snapshot.planner_module_config_version
            != profile.planner_module_config_version
            or snapshot.proposal_schema_version != profile.proposal_schema_version
        ):
            raise ValidationError(
                "planning profile snapshot does not match confirmation"
            )
        descriptor = proposal.profile_descriptor
        if (
            descriptor.profile_id != profile.id
            or descriptor.profile_version != profile.version
            or descriptor.planner_module_instance_id
            != snapshot.planner_module_instance_id
            or descriptor.planner_module_config_version
            != snapshot.planner_module_config_version
        ):
            raise ValidationError(
                "proposal profile descriptor does not match confirmation"
            )
        if proposal.proposal_schema_version != snapshot.proposal_schema_version:
            raise ValidationError(
                "proposal schema version does not match profile snapshot"
            )
        expected_hash = proposal_content_hash(
            proposal.payload,
            brief_revision=proposal.brief_revision,
            profile_descriptor=descriptor,
        )
        if expected_hash != proposal.content_hash:
            raise ValidationError("proposal content hash is invalid")

        try:
            current_snapshot = profile.snapshot(snapshot.planner_capabilities)
        except ValidationError as exc:
            raise ValidationError(
                "planning profile snapshot does not match confirmation"
            ) from exc
        if current_snapshot != snapshot:
            raise ValidationError(
                "planning profile snapshot does not match confirmation"
            )

    @staticmethod
    def _resolve_collaboration_policy(
        request: PlanningRequest, proposal: ProposalRevision
    ) -> CollaborationPolicySnapshotV1 | CollaborationPolicySnapshotV2 | None:
        payload = proposal.payload
        if isinstance(payload, PlanningProposalPayloadV4):
            return resolve_collaboration_policy(
                payload.collaboration_policy_request,
                request.profile_snapshot.collaboration_runtime_binding,
                subtask_keys=frozenset(item.key for item in payload.subtasks),
            )
        if isinstance(payload, PlanningProposalPayloadV5):
            return resolve_collaboration_policy_v2(
                payload.collaboration_policy_request,
                request.profile_snapshot.collaboration_runtime_binding,
                subtask_keys=frozenset(item.key for item in payload.subtasks),
            )
        return None

    @staticmethod
    def _snapshot(
        *,
        request: PlanningRequest,
        proposal: ProposalRevision,
        root_task_id: UUID,
        subtask_task_mapping: Mapping[str, UUID],
        published_by: str,
        now: datetime,
        collaboration_policy: CollaborationPolicySnapshotV1
        | CollaborationPolicySnapshotV2
        | None,
    ) -> TaskPlanSnapshot:
        payload = proposal.payload
        source_ref = (
            f"planning_request:{request.id}/proposal:{proposal.revision}/"
            f"hash:{proposal.content_hash}"
        )
        subtasks = tuple(
            SubtaskPlanItem(
                key=item.key,
                task_id=subtask_task_mapping[item.key],
                kind=item.kind,
                title=item.title,
                goal_summary=item.goal_summary,
                acceptance_refs=item.acceptance_refs,
                milestone_key=item.milestone_key,
                expected_outputs=item.expected_outputs,
                source_refs=item.source_refs,
            )
            for item in payload.subtasks
        )
        workers = tuple(
            WorkerItemPlanItem(
                key=item.key,
                worker_item_id=None,
                subtask_key=item.subtask_key,
                title=item.title,
                role=item.role,
                responsibility=item.responsibility,
                capability_requirements=item.capability_requirements,
                worker_policy_ref=item.worker_policy_ref,
                configuration_ref=item.configuration_ref,
                concurrency_limit=item.concurrency_limit,
                source_refs=item.source_refs,
            )
            for item in payload.worker_items
        )
        return TaskPlanSnapshot.create(
            snapshot_id=uuid4(),
            schema_version=(
                4
                if isinstance(payload, PlanningProposalPayloadV5)
                else 3
                if isinstance(payload, PlanningProposalPayloadV4)
                else 2
            ),
            workspace_id=request.workspace_id,
            root_task_id=root_task_id,
            generation=1,
            parent_snapshot_id=None,
            source_type="planning_proposal",
            source_ref=source_ref,
            title=payload.root_task.title,
            goal_summary=payload.root_task.goal_summary,
            acceptance_refs=payload.root_task.acceptance_refs,
            published_by=published_by,
            published_at=now,
            subtasks=subtasks,
            worker_item_plans=workers,
            subtask_milestones=payload.subtask_milestones,
            subtask_dependencies=payload.subtask_dependencies,
            worker_item_dependencies=_plan_worker_dependencies(payload),
            collaboration_policy=collaboration_policy,
        )

    @staticmethod
    def _execution_requests(
        *,
        request: PlanningRequest,
        proposal: ProposalRevision,
        materialized_plan: MaterializedPlan,
        subtask_task_mapping: Mapping[str, UUID],
        now: datetime,
        collaboration_policy: CollaborationPolicySnapshotV1
        | CollaborationPolicySnapshotV2
        | None,
    ) -> tuple[tuple[ExecutionRequest, ...], tuple[ExecutionRequestDependency, ...]]:
        payload = proposal.payload
        worker_by_key = {
            item.key: item for item in materialized_plan.canonical.worker_items
        }
        subtask_by_key = {item.key: item for item in payload.subtasks}
        requests_by_worker: dict[str, ExecutionRequest] = {}
        for intent in payload.execution_intents:
            worker = worker_by_key.get(intent.worker_item_key)
            if worker is None or worker.worker_item_id is None:
                raise ValidationError(
                    f"execution intent references an unresolved WorkerItem: {intent.worker_item_key}"
                )
            subtask_key = worker.subtask_key
            subtask_plan = subtask_by_key.get(subtask_key)
            if subtask_plan is None:
                raise ValidationError(
                    f"WorkerItem references unknown subtask: {subtask_key}"
                )
            execution_request = ExecutionRequest.create(
                request_id=uuid4(),
                workspace_id=request.workspace_id,
                planning_request_id=request.id,
                proposal_revision_id=proposal.id,
                plan_snapshot_id=materialized_plan.snapshot.id,
                plan_generation=materialized_plan.snapshot.generation,
                root_task_id=materialized_plan.snapshot.root_task_id,
                subtask_id=subtask_task_mapping[subtask_key],
                worker_item_id=worker.worker_item_id,
                worker_item_key=worker.key,
                intent_key=worker.key,
                purpose=intent.purpose,
                capability_requirements=frozenset(intent.capability_requirements),
                adapter_preferences=intent.adapter_preferences,
                adapter_forbidden=request.profile_snapshot.adapter_forbidden,
                budget_class=intent.budget_class,
                not_before=now + timedelta(seconds=intent.not_before_seconds or 0),
                max_attempts=intent.max_attempts,
                expected_artifact_refs=intent.expected_artifact_refs,
                expected_evidence_refs=intent.required_evidence_refs,
                expected_outputs=subtask_plan.expected_outputs,
                collaboration_gate_mode=collaboration_gate_mode_for_policy(
                    collaboration_policy,
                    subtask_key=subtask_key,
                ),
                now=now,
            )
            requests_by_worker[worker.key] = execution_request

        if len(requests_by_worker) != len(payload.worker_items):
            raise ValidationError("every WorkerItem must have one ExecutionRequest")

        edges: dict[tuple[str, str], _DependencyAccumulator] = {}

        def add_edge(
            *,
            upstream: str,
            downstream: str,
            enforcement: DependencyEnforcement,
            artifacts: tuple[str, ...] = (),
            evidence: tuple[str, ...] = (),
        ) -> None:
            if upstream == downstream:
                raise ValidationError(
                    "execution request dependency cannot be a self edge"
                )
            key = (downstream, upstream)
            current = edges.get(key)
            if current is None:
                edges[key] = _DependencyAccumulator(
                    enforcement=enforcement,
                    required_artifact_refs=artifacts,
                    required_evidence_refs=evidence,
                )
                return
            edges[key] = current.merge(
                enforcement=enforcement,
                artifacts=artifacts,
                evidence=evidence,
            )

        for dependency in payload.worker_item_dependencies:
            add_edge(
                upstream=dependency.upstream_worker_item_key,
                downstream=dependency.downstream_worker_item_key,
                enforcement=_execution_enforcement(dependency.kind),
                artifacts=dependency.required_artifact_refs,
            )
        for intent in payload.execution_intents:
            for dependency in intent.activation_dependencies:
                add_edge(
                    upstream=dependency.upstream_worker_item_key,
                    downstream=dependency.downstream_worker_item_key,
                    enforcement=_execution_enforcement(dependency.kind),
                    artifacts=dependency.required_artifact_refs,
                )
        workers_by_subtask: dict[str, list[str]] = {}
        for worker in payload.worker_items:
            workers_by_subtask.setdefault(worker.subtask_key, []).append(worker.key)
        for dependency in payload.subtask_dependencies:
            enforcement = (
                DependencyEnforcement.HARD
                if dependency.kind is DependencyKind.HARD
                else DependencyEnforcement.INFORMATIONAL
            )
            for downstream in workers_by_subtask[dependency.downstream_subtask_key]:
                for upstream in workers_by_subtask[dependency.upstream_subtask_key]:
                    add_edge(
                        upstream=upstream,
                        downstream=downstream,
                        enforcement=enforcement,
                        evidence=dependency.required_evidence_refs,
                    )

        requests = tuple(requests_by_worker.values())
        dependencies = tuple(
            ExecutionRequestDependency(
                downstream_request_id=requests_by_worker[downstream].id,
                upstream_request_id=requests_by_worker[upstream].id,
                enforcement=accumulator.enforcement,
                required_artifact_refs=accumulator.required_artifact_refs,
                required_evidence_refs=accumulator.required_evidence_refs,
            )
            for downstream, upstream in sorted(edges)
            for accumulator in (edges[(downstream, upstream)],)
        )
        return requests, dependencies

    @staticmethod
    def _evaluate_initial_gates(
        requests: tuple[ExecutionRequest, ...],
        dependencies: tuple[ExecutionRequestDependency, ...],
        *,
        now: datetime,
    ) -> None:
        dependencies_by_downstream: dict[UUID, list[ExecutionRequestDependency]] = {}
        for dependency in dependencies:
            dependencies_by_downstream.setdefault(
                dependency.downstream_request_id, []
            ).append(dependency)
        for request in requests:
            static_decision = ActivationGate.evaluate(
                dependencies=dependencies_by_downstream.get(request.id, ())
            )
            collaboration = (
                CollaborationGateFact.run_pending()
                if request.collaboration_gate_mode is CollaborationGateMode.RELEASE_REQUIRED
                else CollaborationGateFact.unmanaged()
            )
            decision = ComposedActivationGate.evaluate(
                plan_transition_allows=True,
                static_decision=static_decision,
                collaboration=collaboration,
            )
            if decision.ready:
                request.mark_ready(now=now, decision=decision)
            else:
                request.refresh_gate(decision=decision, now=now)

    @staticmethod
    def _events(
        *,
        envelope: CommandEnvelope,
        request: PlanningRequest,
        proposal: ProposalRevision,
        root_task: Task,
        subtasks: tuple[Task, ...],
        materialized_plan: MaterializedPlan,
        execution_requests: tuple[ExecutionRequest, ...],
        materialization: ProposalMaterialization,
    ) -> tuple[EventData, ...]:
        events: list[EventData] = []
        for task, task_kind in (
            (root_task, "root"),
            *((task, "subtask") for task in subtasks),
        ):
            events.append(
                _event(
                    envelope,
                    aggregate_type="task",
                    aggregate_id=task.id,
                    aggregate_version=task.version,
                    event_type="TaskCreated",
                    payload={
                        "title": task.title,
                        "description": task.description,
                        "acceptance_criteria": list(task.acceptance_criteria),
                        "status": task.status.value,
                        "priority": task.priority,
                        "requested_by": task.requested_by,
                        "root_task_id": str(root_task.id),
                        "task_kind": task_kind,
                    },
                )
            )
        events.append(
            _event(
                envelope,
                aggregate_type="task_plan",
                aggregate_id=materialized_plan.snapshot.id,
                aggregate_version=materialized_plan.snapshot.generation,
                event_type="task.plan_snapshot_published",
                payload={
                    "snapshot_id": str(materialized_plan.snapshot.id),
                    "generation": materialized_plan.snapshot.generation,
                    "root_task_id": str(root_task.id),
                    "planning_request_id": str(request.id),
                },
            )
        )
        events.extend(
            _event(
                envelope,
                aggregate_type="worker_item_placement",
                aggregate_id=worker_item_id,
                event_type="worker_item.created",
                payload={
                    "root_task_id": str(root_task.id),
                    "snapshot_id": str(materialized_plan.snapshot.id),
                    "generation": materialized_plan.snapshot.generation,
                    "worker_item_id": str(worker_item_id),
                },
            )
            for worker_item_id in materialized_plan.created_worker_item_ids
        )
        events.extend(
            _event(
                envelope,
                aggregate_type="worker_item",
                aggregate_id=placement.worker_item_id,
                aggregate_version=materialized_plan.snapshot.generation,
                event_type="worker_item.placement_materialized",
                payload={
                    "root_task_id": str(root_task.id),
                    "snapshot_id": str(materialized_plan.snapshot.id),
                    "generation": materialized_plan.snapshot.generation,
                    "subtask_key": placement.subtask_key,
                    "worker_item_id": str(placement.worker_item_id),
                },
            )
            for placement in materialized_plan.placements
        )
        events.extend(
            _event(
                envelope,
                aggregate_type="execution_request",
                aggregate_id=execution_request.id,
                aggregate_version=execution_request.version,
                event_type="ExecutionRequestCreated",
                payload={
                    "planning_request_id": str(request.id),
                    "proposal_revision_id": str(proposal.id),
                    "plan_snapshot_id": str(materialized_plan.snapshot.id),
                    "root_task_id": str(root_task.id),
                    "worker_item_id": str(execution_request.worker_item_id),
                    "execution_request_id": str(execution_request.id),
                    "status": execution_request.status.value,
                    "gate_reason": execution_request.gate_reason,
                },
            )
            for execution_request in execution_requests
        )
        events.extend(
            (
                _event(
                    envelope,
                    aggregate_type="planning_proposal",
                    aggregate_id=proposal.id,
                    aggregate_version=proposal.revision,
                    event_type="PlanningProposalConfirmed",
                    payload={
                        "planning_request_id": str(request.id),
                        "proposal_revision": proposal.revision,
                        "proposal_content_hash": proposal.content_hash,
                        "root_task_id": str(root_task.id),
                        "plan_snapshot_id": str(materialized_plan.snapshot.id),
                    },
                ),
                _event(
                    envelope,
                    aggregate_type="planning_request",
                    aggregate_id=request.id,
                    aggregate_version=request.version,
                    event_type="PlanningMaterialized",
                    payload={
                        "proposal_revision": proposal.revision,
                        "proposal_content_hash": proposal.content_hash,
                        "root_task_id": str(root_task.id),
                        "plan_snapshot_id": str(materialized_plan.snapshot.id),
                        "materialization_id": str(materialization.id),
                        "status": request.status.value,
                    },
                ),
            )
        )
        return tuple(events)


@dataclass(frozen=True, slots=True)
class _DependencyAccumulator:
    enforcement: DependencyEnforcement
    required_artifact_refs: tuple[str, ...]
    required_evidence_refs: tuple[str, ...]

    def merge(
        self,
        *,
        enforcement: DependencyEnforcement,
        artifacts: tuple[str, ...],
        evidence: tuple[str, ...],
    ) -> _DependencyAccumulator:
        return _DependencyAccumulator(
            enforcement=(
                DependencyEnforcement.HARD
                if DependencyEnforcement.HARD in {self.enforcement, enforcement}
                else DependencyEnforcement.INFORMATIONAL
            ),
            required_artifact_refs=_merge_refs(self.required_artifact_refs, artifacts),
            required_evidence_refs=_merge_refs(self.required_evidence_refs, evidence),
        )


def _plan_worker_dependencies(
    payload: (
        PlanningProposalPayload
        | PlanningProposalPayloadV4
        | PlanningProposalPayloadV5
        | PlanningRevisionProposalPayloadV3
        | PlanningRevisionProposalPayloadV4
    ),
) -> tuple[WorkerItemDependency, ...]:
    edges: dict[tuple[str, str], WorkerItemDependency] = {}
    for dependency in payload.worker_item_dependencies:
        key = (
            dependency.upstream_worker_item_key,
            dependency.downstream_worker_item_key,
        )
        current = edges.get(key)
        if current is None:
            edges[key] = WorkerItemDependency(
                upstream_worker_item_key=dependency.upstream_worker_item_key,
                downstream_worker_item_key=dependency.downstream_worker_item_key,
                kind=_plan_worker_kind(dependency.kind),
                required_artifact_refs=dependency.required_artifact_refs,
            )
            continue
        edges[key] = WorkerItemDependency(
            upstream_worker_item_key=current.upstream_worker_item_key,
            downstream_worker_item_key=current.downstream_worker_item_key,
            kind=(
                "dependency"
                if "dependency" in {current.kind, _plan_worker_kind(dependency.kind)}
                else "context"
            ),
            required_artifact_refs=_merge_refs(
                current.required_artifact_refs,
                dependency.required_artifact_refs,
            ),
        )
    return tuple(edges.values())


def _plan_worker_kind(kind: str) -> str:
    return "dependency" if kind == DependencyKind.HARD.value else "context"


def _execution_enforcement(kind: str) -> DependencyEnforcement:
    return (
        DependencyEnforcement.HARD
        if kind == DependencyKind.HARD.value
        else DependencyEnforcement.INFORMATIONAL
    )


def _merge_refs(first: tuple[str, ...], second: tuple[str, ...]) -> tuple[str, ...]:
    return tuple(dict.fromkeys((*first, *second)))


def _event(
    envelope: CommandEnvelope,
    *,
    aggregate_type: str,
    aggregate_id: UUID,
    event_type: str,
    payload: dict[str, object],
    aggregate_version: int = 1,
) -> EventData:
    return EventData(
        id=uuid4(),
        workspace_id=envelope.workspace_id,
        aggregate_type=aggregate_type,
        aggregate_id=aggregate_id,
        aggregate_version=aggregate_version,
        event_type=event_type,
        payload=payload,
        actor_type=envelope.actor_type,
        actor_id=envelope.actor_id,
        causation_id=envelope.id,
        correlation_id=envelope.correlation_id,
    )


def _uuid(payload: Mapping[str, object], name: str) -> UUID:
    value = payload.get(name)
    try:
        return value if isinstance(value, UUID) else UUID(str(value))
    except (TypeError, ValueError) as exc:
        raise ValidationError(f"{name} must be a UUID") from exc


def _positive_int(payload: Mapping[str, object], name: str) -> int:
    value = payload.get(name)
    if not isinstance(value, int) or isinstance(value, bool) or value < 1:
        raise ValidationError(f"{name} must be positive")
    return value


def _content_hash(payload: Mapping[str, object], name: str) -> str:
    value = payload.get(name)
    if not isinstance(value, str) or len(value) != 64:
        raise ValidationError(f"{name} must be a SHA-256 hash")
    if value != value.lower() or any(
        character not in "0123456789abcdef" for character in value
    ):
        raise ValidationError(f"{name} must be a SHA-256 hash")
    return value
