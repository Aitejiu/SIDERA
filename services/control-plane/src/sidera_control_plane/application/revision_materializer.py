from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Any, cast
from uuid import UUID, uuid4

from sidera_control_plane.application.handoff_gate import HandoffInProgress
from sidera_control_plane.application.ports import ControlPlaneUnitOfWork
from sidera_control_plane.application.worker_item_materializer import (
    WorkerItemMaterializer,
)
from sidera_control_plane.domain.commands import CommandEnvelope
from sidera_control_plane.domain.errors import (
    DomainError,
    InvalidTransition,
    ValidationError,
)
from sidera_control_plane.domain.events import EventData
from sidera_control_plane.domain.execution import Execution
from sidera_control_plane.domain.plan_revisions import (
    ContextDisposition,
    IdentityStrategy,
    RootTaskPlanHead,
    WorkerTransitionAction,
    plan_snapshot_content_hash,
)
from sidera_control_plane.domain.plan_transitions import (
    PlanEntityLineage,
    PlanTransition,
    PlanTransitionStatus,
    WorkerTransition,
    WorkerTransitionStatus,
    WriterState,
)
from sidera_control_plane.domain.planning import (
    PlanningRequest,
    PlanningRequestKind,
    PlanningRequestStatus,
    ProposalMaterialization,
)
from sidera_control_plane.domain.planning_profiles import PlanningProfile
from sidera_control_plane.domain.planning_proposals import (
    ProposalRevision,
    proposal_content_hash,
)
from sidera_control_plane.domain.planning_revision_proposals import (
    PlanningRevisionProposalPayloadV3,
)
from sidera_control_plane.domain.plans import (
    SubtaskPlanItem,
    TaskPlanSnapshot,
    WorkerItemPlanItem,
)
from sidera_control_plane.domain.revision_diff import (
    NormalizedRevisionDiff,
    RevisionDiffNormalizer,
    WorkerTransitionPreview,
)
from sidera_control_plane.domain.task import TERMINAL_TASK_STATUSES, Task
from sidera_control_plane.domain.worker_item import WorkerItem, WorkerItemStatus


class RevisionStaleBase(DomainError):
    code = "revision_stale_base"


class RevisionConfirmationBlocked(DomainError):
    code = "revision_confirmation_blocked"


@dataclass(frozen=True, slots=True)
class RevisionMaterializationResult:
    root_task_id: UUID
    plan_snapshot_id: UUID
    plan_generation: int
    plan_transition_id: UUID
    proposal_revision_id: UUID
    diff_hash: str
    materialization: ProposalMaterialization
    snapshot: TaskPlanSnapshot
    plan_transition: PlanTransition
    worker_transitions: tuple[WorkerTransition, ...]
    tasks: tuple[Task, ...]
    lineages: tuple[PlanEntityLineage, ...]
    event_ids: tuple[UUID, ...]


class RevisionMaterializer:
    """Atomically publish one confirmed schema-v3 plan revision."""

    def __init__(
        self,
        *,
        diff_normalizer: RevisionDiffNormalizer | None = None,
        worker_item_materializer: WorkerItemMaterializer | None = None,
    ) -> None:
        self._diff_normalizer = diff_normalizer or RevisionDiffNormalizer()
        self._worker_item_materializer = (
            worker_item_materializer or WorkerItemMaterializer()
        )

    def materialize(
        self,
        uow: ControlPlaneUnitOfWork,
        *,
        request: PlanningRequest,
        proposal: ProposalRevision,
        profile: PlanningProfile,
        envelope: CommandEnvelope,
        now: datetime,
    ) -> RevisionMaterializationResult:
        payload = self._validate_input(
            request=request,
            proposal=proposal,
            profile=profile,
            envelope=envelope,
        )
        existing = uow.proposal_materializations.get_by_request(
            request.id, request.workspace_id
        )
        if existing is not None:
            if (
                existing.kind is not PlanningRequestKind.REVISION
                or existing.proposal_revision_id != proposal.id
                or existing.diff_hash
                != _required_hash(envelope.payload, "expected_diff_hash")
            ):
                raise ValidationError(
                    "planning request is already materialized with another revision"
                )
            snapshot = uow.plans.get(existing.plan_snapshot_id, request.workspace_id)
            transition = (
                uow.plan_transitions.get(
                    existing.plan_transition_id,
                    request.workspace_id,
                )
                if existing.plan_transition_id is not None
                else None
            )
            if snapshot is None or transition is None:
                raise ValidationError(
                    "revision materialization references missing records"
                )
            return RevisionMaterializationResult(
                root_task_id=existing.root_task_id,
                plan_snapshot_id=existing.plan_snapshot_id,
                plan_generation=existing.plan_generation,
                plan_transition_id=transition.id,
                proposal_revision_id=proposal.id,
                diff_hash=existing.diff_hash or "",
                materialization=existing,
                snapshot=snapshot,
                plan_transition=transition,
                worker_transitions=uow.worker_transitions.list_for_plan_transition(
                    transition.id, request.workspace_id
                ),
                tasks=(),
                lineages=uow.plan_entity_lineages.list_for_transition(
                    transition.id, request.workspace_id
                ),
                event_ids=(),
            )

        if request.status is not PlanningRequestStatus.PROPOSAL_READY:
            raise InvalidTransition("only a proposal-ready revision can materialize")

        base_snapshot = uow.plans.get(
            payload.base_descriptor.plan_snapshot_id,
            request.workspace_id,
        )
        if base_snapshot is None:
            raise RevisionStaleBase(
                "revision base snapshot is no longer available",
                details={"reason": "stale_base"},
            )
        payload.validate_against_base(base_snapshot)
        if (
            proposal_content_hash(
                proposal.payload,
                brief_revision=proposal.brief_revision,
                profile_descriptor=proposal.profile_descriptor,
            )
            != proposal.content_hash
        ):
            raise ValidationError("proposal content hash is invalid")
        diff = self._diff_normalizer.normalize(base_snapshot, payload)
        expected_diff_hash = _required_hash(envelope.payload, "expected_diff_hash")
        if diff.diff_hash != expected_diff_hash:
            raise ValidationError(
                "normalized revision diff does not match confirmation",
                details={
                    "reason": "diff_mismatch",
                    "expected_diff_hash": expected_diff_hash,
                    "actual_diff_hash": diff.diff_hash,
                },
            )
        if not isinstance(proposal.derived_diff, NormalizedRevisionDiff):
            raise ValidationError(
                "revision proposal does not contain a normalized diff"
            )
        if proposal.derived_diff.diff_hash != diff.diff_hash:
            raise ValidationError("stored revision diff does not match the proposal")

        # The request is already locked by PlanningCommandHandler. Keep the
        # remaining lock order deterministic: head, Tasks, then WorkerItems.
        head = uow.plans.get_head(
            request.root_task_id or payload.base_descriptor.root_task_id,
            request.workspace_id,
            for_update=True,
        )
        if head is None or not _head_matches_base(head, payload, base_snapshot):
            raise RevisionStaleBase(
                "revision base is stale",
                details={
                    "reason": "stale_base",
                    "current_snapshot_id": str(head.active_snapshot_id)
                    if head is not None
                    else None,
                    "current_generation": head.active_generation if head else None,
                    "current_snapshot_hash": head.active_snapshot_hash
                    if head is not None
                    else None,
                },
            )

        task_ids = _task_lock_ids(base_snapshot, payload)
        tasks_by_id = {
            task.id: task
            for task in (
                uow.tasks.get(task_id, request.workspace_id, for_update=True)
                for task_id in task_ids
            )
            if task is not None
        }
        if len(tasks_by_id) != len(task_ids):
            raise ValidationError("revision identity references a missing Task")
        root_task = tasks_by_id.get(payload.base_descriptor.root_task_id)
        if root_task is None:
            raise ValidationError("revision root Task is not available")
        if root_task.version != payload.base_descriptor.root_task_version:
            raise RevisionStaleBase(
                "root Task changed after the revision was planned",
                details={
                    "reason": "stale_base",
                    "current_root_task_version": root_task.version,
                },
            )
        if root_task.status in TERMINAL_TASK_STATUSES:
            raise ValidationError(
                "terminal root Task cannot be revised",
                details={"status": root_task.status.value},
            )

        worker_ids = _worker_lock_ids(base_snapshot, payload)
        workers_by_id = {
            item.id: item
            for item in (
                uow.worker_items.get(item_id, request.workspace_id, for_update=True)
                for item_id in worker_ids
            )
            if item is not None
        }
        if len(workers_by_id) != len(worker_ids):
            raise ValidationError("revision identity references a missing WorkerItem")
        self._validate_previous_transitions(
            uow,
            root_task_id=root_task.id,
            workspace_id=request.workspace_id,
            worker_ids=worker_ids,
            request_id=request.id,
        )
        self._validate_worker_identities(
            payload,
            base_snapshot=base_snapshot,
            workers_by_id=workers_by_id,
        )

        previous_request_version = request.version
        previous_root_task_version = root_task.version
        plan_transition_id = uuid4()
        target_subtask_ids, materialized_tasks, task_lineages = self._materialize_tasks(
            uow,
            payload=payload,
            base_snapshot=base_snapshot,
            tasks_by_id=tasks_by_id,
            root_task=root_task,
            actor_id=envelope.actor_id,
            now=now,
            plan_transition_id=plan_transition_id,
        )
        uow.flush()

        target_worker_ids, target_workers, worker_lineages = self._materialize_workers(
            uow,
            payload=payload,
            base_snapshot=base_snapshot,
            workers_by_id=workers_by_id,
            root_task_id=root_task.id,
            now=now,
            plan_transition_id=plan_transition_id,
        )
        uow.flush()

        target_snapshot = self._target_snapshot(
            request=request,
            proposal=proposal,
            payload=payload,
            subtask_ids=target_subtask_ids,
            worker_ids=target_worker_ids,
            published_by=envelope.actor_id,
            now=now,
        )
        uow.plans.add(target_snapshot)
        uow.flush()
        materialized_plan = self._worker_item_materializer.materialize(
            uow,
            target_snapshot,
            now=now,
            allow_paused_reuse=True,
        )
        target_snapshot = materialized_plan.snapshot
        uow.flush()

        source_executions = self._source_executions(
            uow,
            tasks_by_id=tasks_by_id,
            workspace_id=request.workspace_id,
        )
        worker_transitions = self._worker_transitions(
            uow,
            payload=payload,
            diff=diff,
            base_snapshot=base_snapshot,
            target_snapshot=target_snapshot,
            target_workers=target_workers,
            source_workers=workers_by_id,
            source_executions=source_executions,
            plan_transition_id=plan_transition_id,
            now=now,
        )
        plan_transition = PlanTransition(
            id=plan_transition_id,
            workspace_id=request.workspace_id,
            root_task_id=root_task.id,
            planning_request_id=request.id,
            proposal_revision_id=proposal.id,
            from_snapshot_id=base_snapshot.id,
            from_generation=base_snapshot.generation,
            to_snapshot_id=target_snapshot.id,
            to_generation=target_snapshot.generation,
            status=(
                PlanTransitionStatus.CONVERGED
                if all(
                    item.status is WorkerTransitionStatus.CONVERGED
                    for item in worker_transitions
                )
                else PlanTransitionStatus.CONVERGING
            ),
            started_at=now,
            completed_at=(
                now
                if all(
                    item.status is WorkerTransitionStatus.CONVERGED
                    for item in worker_transitions
                )
                else None
            ),
            version=1,
        )
        uow.plan_transitions.add(plan_transition)
        uow.flush()
        for transition in worker_transitions:
            uow.worker_transitions.add(transition)
        lineages = (*task_lineages, *worker_lineages)
        for lineage in lineages:
            uow.plan_entity_lineages.add(lineage)
        uow.flush()

        materialization = ProposalMaterialization(
            id=uuid4(),
            workspace_id=request.workspace_id,
            planning_request_id=request.id,
            proposal_revision_id=proposal.id,
            root_task_id=root_task.id,
            plan_snapshot_id=target_snapshot.id,
            plan_generation=target_snapshot.generation,
            subtask_task_mapping=target_subtask_ids,
            worker_item_mapping=target_worker_ids,
            execution_request_mapping={},
            deep_link=f"/workspaces/{request.workspace_id}/tasks/{root_task.id}",
            confirmed_by=envelope.actor_id,
            created_at=now,
            kind=PlanningRequestKind.REVISION,
            diff_hash=diff.diff_hash,
            plan_transition_id=plan_transition.id,
        )
        uow.proposal_materializations.add(materialization)
        uow.plans.advance_head(
            head,
            expected_snapshot_id=base_snapshot.id,
            target=target_snapshot,
        )
        request.materialize(
            proposal_revision=proposal.revision,
            content_hash=proposal.content_hash,
            now=now,
        )
        uow.planning_requests.save(request, previous_request_version)
        events = self._events(
            envelope=envelope,
            request=request,
            proposal=proposal,
            root_task=root_task,
            root_task_version_before=previous_root_task_version,
            tasks=materialized_tasks,
            snapshot=target_snapshot,
            materialization=materialization,
            plan_transition=plan_transition,
            worker_transitions=worker_transitions,
        )
        for event in events:
            uow.events.append_with_outbox(event)
        return RevisionMaterializationResult(
            root_task_id=root_task.id,
            plan_snapshot_id=target_snapshot.id,
            plan_generation=target_snapshot.generation,
            plan_transition_id=plan_transition.id,
            proposal_revision_id=proposal.id,
            diff_hash=diff.diff_hash,
            materialization=materialization,
            snapshot=target_snapshot,
            plan_transition=plan_transition,
            worker_transitions=worker_transitions,
            tasks=(root_task, *materialized_tasks),
            lineages=lineages,
            event_ids=tuple(event.id for event in events),
        )

    @staticmethod
    def _validate_input(
        *,
        request: PlanningRequest,
        proposal: ProposalRevision,
        profile: PlanningProfile,
        envelope: CommandEnvelope,
    ) -> PlanningRevisionProposalPayloadV3:
        if request.kind is not PlanningRequestKind.REVISION:
            raise ValidationError("ConfirmPlanRevision requires a revision request")
        if proposal.kind is not PlanningRequestKind.REVISION:
            raise ValidationError("ConfirmPlanRevision requires a revision proposal")
        if proposal.planning_request_id != request.id:
            raise ValidationError("proposal does not belong to the planning request")
        payload = proposal.payload
        if not isinstance(payload, PlanningRevisionProposalPayloadV3):
            raise ValidationError("revision proposal schema v3 is required")
        if request.base_descriptor != payload.base_descriptor:
            raise ValidationError("proposal base does not match the planning request")
        if request.current_proposal_revision != proposal.revision:
            raise ValidationError("proposal revision does not match current proposal")
        if request.current_proposal_content_hash != proposal.content_hash:
            raise ValidationError(
                "proposal content hash does not match current proposal"
            )
        if (
            _positive_int(envelope.payload, "expected_proposal_revision")
            != proposal.revision
        ):
            raise ValidationError("proposal revision does not match confirmation")
        if (
            _required_hash(envelope.payload, "expected_proposal_content_hash")
            != proposal.content_hash
        ):
            raise ValidationError("proposal content hash does not match confirmation")
        expected_base_id = _uuid(envelope.payload, "expected_base_snapshot_id")
        expected_base_hash = _required_hash(
            envelope.payload, "expected_base_content_hash"
        )
        if (
            expected_base_id != payload.base_descriptor.plan_snapshot_id
            or expected_base_hash != payload.base_descriptor.plan_content_hash
        ):
            raise ValidationError("revision base does not match confirmation")
        if envelope.workspace_id != request.workspace_id:
            raise ValidationError("planning request workspace does not match command")
        profile.assert_user_selectable()
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
        profile_snapshot = request.profile_snapshot
        if (
            profile_snapshot.profile_id != profile.id
            or profile_snapshot.profile_version != profile.version
            or profile_snapshot.workspace_id != request.workspace_id
            or profile_snapshot.planner_module_instance_id
            != profile.planner_module_instance_id
            or profile_snapshot.planner_module_config_version
            != profile.planner_module_config_version
            or profile_snapshot.proposal_schema_version
            != profile.proposal_schema_version
        ):
            raise ValidationError(
                "planning profile snapshot does not match confirmation"
            )
        if (
            proposal.profile_descriptor.profile_id != profile.id
            or proposal.profile_descriptor.profile_version != profile.version
            or proposal.proposal_schema_version
            != profile_snapshot.proposal_schema_version
        ):
            raise ValidationError("proposal profile does not match confirmation")
        return payload

    @staticmethod
    def _validate_previous_transitions(
        uow: ControlPlaneUnitOfWork,
        *,
        root_task_id: UUID,
        workspace_id: UUID,
        worker_ids: tuple[UUID, ...],
        request_id: UUID,
    ) -> None:
        active_revision = uow.planning_requests.get_active_revision_for_root(
            root_task_id, workspace_id
        )
        if active_revision is not None and active_revision.id != request_id:
            # The caller's request is the only expected active revision. The
            # command handler has already locked it; another row is a real
            # root-level confirmation conflict.
            raise RevisionConfirmationBlocked(
                "another plan revision is active",
                details={
                    "reason": "active_revision",
                    "request_id": str(active_revision.id),
                },
            )
        handoff_transitions = getattr(uow, "handoff_transitions", None)
        get_active_handoffs = getattr(
            handoff_transitions, "get_active_for_worker_items", None
        )
        if callable(get_active_handoffs):
            active_handoffs = cast(
                tuple[Any, ...],
                get_active_handoffs(
                    tuple(sorted(set(worker_ids), key=str)),
                    workspace_id,
                    for_update=True,
                ),
            )
            if active_handoffs:
                raise HandoffInProgress(active_handoffs[0])
        for worker_id in worker_ids:
            transition = uow.worker_transitions.get_active_for_worker_item(
                worker_id,
                workspace_id,
                for_update=True,
            )
            if transition is not None:
                raise RevisionConfirmationBlocked(
                    "WorkerItem is fenced by another transition",
                    details={
                        "reason": (
                            "unknown_transition"
                            if transition.status
                            is WorkerTransitionStatus.BLOCKED_UNKNOWN
                            else "active_transition"
                        ),
                        "worker_item_id": str(worker_id),
                        "worker_transition_id": str(transition.id),
                        "status": transition.status.value,
                        "writer_state": transition.writer_state.value,
                    },
                )

    @staticmethod
    def _validate_worker_identities(
        payload: PlanningRevisionProposalPayloadV3,
        *,
        base_snapshot: TaskPlanSnapshot,
        workers_by_id: Mapping[UUID, WorkerItem],
    ) -> None:
        base_ids = {
            item.worker_item_id
            for item in base_snapshot.worker_item_plans
            if item.worker_item_id is not None
        }
        retired = set(payload.retired_worker_item_ids)
        for decision in payload.worker_item_identity_decisions:
            if decision.existing_worker_item_id is not None:
                item = workers_by_id.get(decision.existing_worker_item_id)
                if item is None or decision.existing_worker_item_id not in base_ids:
                    raise ValidationError("reused WorkerItem is outside the base plan")
                if item.root_task_id != base_snapshot.root_task_id:
                    raise ValidationError("reused WorkerItem belongs to another root")
                if item.status is WorkerItemStatus.RETIRED:
                    raise ValidationError("retired WorkerItem cannot be reused")
            if set(decision.derived_from_worker_item_ids).intersection(retired):
                raise ValidationError("retired WorkerItem cannot be a derive source")

    def _materialize_tasks(
        self,
        uow: ControlPlaneUnitOfWork,
        *,
        payload: PlanningRevisionProposalPayloadV3,
        base_snapshot: TaskPlanSnapshot,
        tasks_by_id: Mapping[UUID, Task],
        root_task: Task,
        actor_id: str,
        now: datetime,
        plan_transition_id: UUID,
    ) -> tuple[dict[str, UUID], tuple[Task, ...], tuple[PlanEntityLineage, ...]]:
        decisions = {
            item.target_subtask_key: item for item in payload.subtask_identity_decisions
        }
        base_subtasks = {
            item.task_id: item
            for item in base_snapshot.subtasks
            if item.task_id is not None
        }
        previous_root_version = root_task.version
        root_task.update_specification(
            title=payload.root_task.title,
            description=payload.root_task.goal_summary,
            acceptance_criteria=payload.root_task.acceptance_refs,
        )
        if root_task.version != previous_root_version:
            uow.tasks.save(root_task, previous_root_version)

        mapping: dict[str, UUID] = {}
        created: list[Task] = []
        lineages: list[PlanEntityLineage] = []
        for item in payload.subtasks:
            decision = decisions[item.key]
            if decision.strategy is IdentityStrategy.REUSE:
                task_id = decision.existing_subtask_task_id
                if task_id is None:
                    raise ValidationError("reused subtask has no identity")
                task = tasks_by_id[task_id]
                base_item = base_subtasks.get(task_id)
                if base_item is None:
                    raise ValidationError("reused subtask is outside the base plan")
                target_spec = (item.title, item.goal_summary, item.acceptance_refs)
                current_spec = (
                    task.title,
                    task.description,
                    task.acceptance_criteria,
                )
                if (
                    task.status in TERMINAL_TASK_STATUSES
                    and target_spec != current_spec
                ):
                    raise ValidationError(
                        "terminal subtask requires a derived identity for changed work"
                    )
                previous_version = task.version
                task.update_specification(
                    title=item.title,
                    description=item.goal_summary,
                    acceptance_criteria=item.acceptance_refs,
                )
                if task.version != previous_version:
                    uow.tasks.save(task, previous_version)
                lineages.append(
                    _lineage(
                        plan_transition_id=plan_transition_id,
                        workspace_id=base_snapshot.workspace_id,
                        target_type="subtask",
                        target_id=task.id,
                        source_type="subtask",
                        source_id=task.id,
                        reason=decision.reason,
                    )
                )
                mapping[item.key] = task.id
                continue

            task = Task.create(
                task_id=uuid4(),
                workspace_id=base_snapshot.workspace_id,
                title=item.title,
                description=item.goal_summary,
                acceptance_criteria=item.acceptance_refs,
                priority=root_task.priority,
                requested_by=actor_id,
            )
            uow.tasks.add(task)
            created.append(task)
            mapping[item.key] = task.id
            for source_id in decision.derived_from_subtask_task_ids:
                lineages.append(
                    _lineage(
                        plan_transition_id=plan_transition_id,
                        workspace_id=base_snapshot.workspace_id,
                        target_type="subtask",
                        target_id=task.id,
                        source_type="subtask",
                        source_id=source_id,
                        reason=decision.reason,
                    )
                )
        return mapping, tuple(created), tuple(lineages)

    def _materialize_workers(
        self,
        uow: ControlPlaneUnitOfWork,
        *,
        payload: PlanningRevisionProposalPayloadV3,
        base_snapshot: TaskPlanSnapshot,
        workers_by_id: Mapping[UUID, WorkerItem],
        root_task_id: UUID,
        now: datetime,
        plan_transition_id: UUID,
    ) -> tuple[
        dict[str, UUID], Mapping[str, WorkerItem], tuple[PlanEntityLineage, ...]
    ]:
        decisions = {
            item.target_worker_item_key: item
            for item in payload.worker_item_identity_decisions
        }
        target_workers_by_key: dict[str, WorkerItem] = {}
        target_ids: dict[str, UUID] = {}
        lineages: list[PlanEntityLineage] = []
        for item in payload.worker_items:
            decision = decisions[item.key]
            if decision.strategy is IdentityStrategy.REUSE:
                worker_id = decision.existing_worker_item_id
                if worker_id is None:
                    raise ValidationError("reused WorkerItem has no identity")
                worker = workers_by_id[worker_id]
                previous_version = worker.version
                worker.update_specification(
                    title=item.title,
                    responsibility=item.responsibility,
                    now=now,
                )
                if decision.context_disposition is ContextDisposition.PATCH_REQUIRED:
                    worker.advance_context(now=now)
                if worker.version != previous_version:
                    uow.worker_items.save(worker, previous_version)
                target_workers_by_key[item.key] = worker
                target_ids[item.key] = worker.id
                lineages.append(
                    _lineage(
                        plan_transition_id=plan_transition_id,
                        workspace_id=base_snapshot.workspace_id,
                        target_type="worker_item",
                        target_id=worker.id,
                        source_type="worker_item",
                        source_id=worker.id,
                        reason="reuse stable WorkerItem identity",
                    )
                )
                continue

            worker = WorkerItem.create(
                item_id=uuid4(),
                workspace_id=base_snapshot.workspace_id,
                root_task_id=root_task_id,
                title=item.title,
                responsibility=item.responsibility,
                now=now,
                derived_from_worker_item_ids=decision.derived_from_worker_item_ids,
            )
            uow.worker_items.add(worker)
            target_workers_by_key[item.key] = worker
            target_ids[item.key] = worker.id
            for source_id in decision.derived_from_worker_item_ids:
                lineages.append(
                    _lineage(
                        plan_transition_id=plan_transition_id,
                        workspace_id=base_snapshot.workspace_id,
                        target_type="worker_item",
                        target_id=worker.id,
                        source_type="worker_item",
                        source_id=source_id,
                        reason="derive WorkerItem identity",
                    )
                )
        return target_ids, target_workers_by_key, tuple(lineages)

    @staticmethod
    def _target_snapshot(
        *,
        request: PlanningRequest,
        proposal: ProposalRevision,
        payload: PlanningRevisionProposalPayloadV3,
        subtask_ids: Mapping[str, UUID],
        worker_ids: Mapping[str, UUID],
        published_by: str,
        now: datetime,
    ) -> TaskPlanSnapshot:
        subtasks = tuple(
            SubtaskPlanItem(
                key=item.key,
                task_id=subtask_ids[item.key],
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
                worker_item_id=worker_ids[item.key],
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
            schema_version=2,
            workspace_id=request.workspace_id,
            root_task_id=payload.base_descriptor.root_task_id,
            generation=payload.base_descriptor.plan_generation + 1,
            parent_snapshot_id=payload.base_descriptor.plan_snapshot_id,
            source_type="planning_revision",
            source_ref=(
                f"planning_request:{request.id}/proposal:{proposal.revision}/"
                f"hash:{proposal.content_hash}"
            ),
            title=payload.root_task.title,
            goal_summary=payload.root_task.goal_summary,
            acceptance_refs=payload.root_task.acceptance_refs,
            published_by=published_by,
            published_at=now,
            subtasks=subtasks,
            worker_item_plans=workers,
            subtask_milestones=payload.subtask_milestones,
            subtask_dependencies=payload.subtask_dependencies,
            worker_item_dependencies=payload.worker_item_dependencies,
        )

    @staticmethod
    def _source_executions(
        uow: ControlPlaneUnitOfWork,
        *,
        tasks_by_id: Mapping[UUID, Task],
        workspace_id: UUID,
    ) -> Mapping[UUID, Execution]:
        executions = uow.executions.list_for_tasks(tuple(tasks_by_id), workspace_id)
        result: dict[UUID, Execution] = {}
        for execution in executions:
            if execution.worker_item_id is not None:
                result[execution.worker_item_id] = execution
        return result

    @staticmethod
    def _worker_transitions(
        uow: ControlPlaneUnitOfWork,
        *,
        payload: PlanningRevisionProposalPayloadV3,
        diff: NormalizedRevisionDiff,
        base_snapshot: TaskPlanSnapshot,
        target_snapshot: TaskPlanSnapshot,
        target_workers: Mapping[str, WorkerItem],
        source_workers: Mapping[UUID, WorkerItem],
        source_executions: Mapping[UUID, Execution],
        plan_transition_id: UUID,
        now: datetime,
    ) -> tuple[WorkerTransition, ...]:
        del uow
        previews = {
            item.target_worker_item_key: item for item in diff.transition_preview
        }
        base_workers = {
            item.worker_item_id: item
            for item in base_snapshot.worker_item_plans
            if item.worker_item_id is not None
        }
        base_subtasks = {item.key: item.task_id for item in base_snapshot.subtasks}
        target_worker_plans = {item.key: item for item in payload.worker_items}
        target_subtasks = {item.key: item for item in target_snapshot.subtasks}
        result: list[WorkerTransition] = []
        for key, worker_id in sorted(
            ((item.key, target_workers[item.key].id) for item in payload.worker_items),
            key=lambda item: item[0],
        ):
            preview = previews[key]
            worker_plan = target_worker_plans[key]
            action = _actual_action(preview)
            source = preview.worker_item_id
            source_execution = source_executions.get(source) if source else None
            from_subtask_id = (
                base_subtasks.get(base_workers[source].subtask_key)
                if source is not None
                else None
            )
            to_subtask_id = target_subtasks[worker_plan.subtask_key].task_id
            worker = target_workers[key]
            terminal = action in {
                WorkerTransitionAction.PRESERVE,
                WorkerTransitionAction.MOVE,
            }
            result.append(
                _worker_transition(
                    plan_transition_id=plan_transition_id,
                    workspace_id=target_snapshot.workspace_id,
                    root_task_id=target_snapshot.root_task_id,
                    worker_item_id=worker.id,
                    from_subtask_id=from_subtask_id,
                    to_subtask_id=to_subtask_id,
                    action=action,
                    fallback_action=preview.fallback_action,
                    target_context_version=worker.context_version,
                    source_execution=source_execution,
                    status=(
                        WorkerTransitionStatus.CONVERGED
                        if terminal
                        else WorkerTransitionStatus.PENDING
                    ),
                    writer_state=WriterState.CLOSED if terminal else WriterState.FENCED,
                    now=now,
                )
            )
        for worker_id in sorted(payload.retired_worker_item_ids, key=str):
            base_worker = base_workers.get(worker_id)
            worker = source_workers.get(worker_id)
            if worker is None:
                raise ValidationError("retired WorkerItem is not available")
            source_execution = source_executions.get(worker_id)
            result.append(
                _worker_transition(
                    plan_transition_id=plan_transition_id,
                    workspace_id=target_snapshot.workspace_id,
                    root_task_id=target_snapshot.root_task_id,
                    worker_item_id=worker_id,
                    from_subtask_id=(
                        base_subtasks.get(base_worker.subtask_key)
                        if base_worker is not None
                        else None
                    ),
                    to_subtask_id=None,
                    action=WorkerTransitionAction.RETIRE,
                    fallback_action=None,
                    target_context_version=worker.context_version,
                    source_execution=source_execution,
                    status=WorkerTransitionStatus.PENDING,
                    writer_state=WriterState.FENCED,
                    now=now,
                )
            )
        return tuple(result)

    @staticmethod
    def _events(
        *,
        envelope: CommandEnvelope,
        request: PlanningRequest,
        proposal: ProposalRevision,
        root_task: Task,
        root_task_version_before: int,
        tasks: tuple[Task, ...],
        snapshot: TaskPlanSnapshot,
        materialization: ProposalMaterialization,
        plan_transition: PlanTransition,
        worker_transitions: tuple[WorkerTransition, ...],
    ) -> tuple[EventData, ...]:
        events: list[EventData] = []
        if root_task.version != root_task_version_before:
            events.append(
                _event(
                    envelope,
                    aggregate_type="task",
                    aggregate_id=root_task.id,
                    aggregate_version=root_task.version,
                    event_type="TaskSpecificationUpdated",
                    payload={
                        "task_id": str(root_task.id),
                        "root_task_id": str(root_task.id),
                    },
                )
            )
        events.extend(
            _event(
                envelope,
                aggregate_type="task",
                aggregate_id=task.id,
                aggregate_version=task.version,
                event_type="TaskCreated",
                payload={
                    "task_id": str(task.id),
                    "root_task_id": str(root_task.id),
                    "task_kind": "subtask",
                    "title": task.title,
                    "description": task.description,
                    "acceptance_criteria": list(task.acceptance_criteria),
                },
            )
            for task in tasks
        )
        events.append(
            _event(
                envelope,
                aggregate_type="task_plan",
                aggregate_id=snapshot.id,
                aggregate_version=snapshot.generation,
                event_type="task.plan_snapshot_published",
                payload={
                    "snapshot_id": str(snapshot.id),
                    "generation": snapshot.generation,
                    "root_task_id": str(root_task.id),
                    "planning_request_id": str(request.id),
                },
            )
        )
        events.append(
            _event(
                envelope,
                aggregate_type="plan_transition",
                aggregate_id=plan_transition.id,
                aggregate_version=plan_transition.version,
                event_type="PlanTransitionCreated",
                payload={
                    "root_task_id": str(root_task.id),
                    "from_generation": plan_transition.from_generation,
                    "to_generation": plan_transition.to_generation,
                    "status": plan_transition.status.value,
                },
            )
        )
        events.extend(
            _event(
                envelope,
                aggregate_type="worker_transition",
                aggregate_id=item.id,
                aggregate_version=item.version,
                event_type="WorkerTransitionCreated",
                payload={
                    "plan_transition_id": str(plan_transition.id),
                    "worker_item_id": str(item.worker_item_id),
                    "action": item.action.value,
                    "status": item.status.value,
                    "writer_state": item.writer_state.value,
                    "target_context_version": item.target_context_version,
                },
            )
            for item in worker_transitions
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
                        "plan_snapshot_id": str(snapshot.id),
                        "plan_transition_id": str(plan_transition.id),
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
                        "plan_snapshot_id": str(snapshot.id),
                        "plan_transition_id": str(plan_transition.id),
                        "materialization_id": str(materialization.id),
                        "status": request.status.value,
                    },
                ),
            )
        )
        return tuple(events)


def _task_lock_ids(
    base_snapshot: TaskPlanSnapshot,
    payload: PlanningRevisionProposalPayloadV3,
) -> tuple[UUID, ...]:
    ids: set[UUID] = {payload.base_descriptor.root_task_id}
    ids.update(
        item.task_id for item in base_snapshot.subtasks if item.task_id is not None
    )
    ids.update(
        item.existing_subtask_task_id
        for item in payload.subtask_identity_decisions
        if item.existing_subtask_task_id is not None
    )
    ids.update(payload.retired_subtask_ids)
    ids.update(
        source_id
        for item in payload.subtask_identity_decisions
        for source_id in item.derived_from_subtask_task_ids
    )
    return tuple(sorted(ids, key=str))


def _worker_lock_ids(
    base_snapshot: TaskPlanSnapshot,
    payload: PlanningRevisionProposalPayloadV3,
) -> tuple[UUID, ...]:
    ids: set[UUID] = {
        item.worker_item_id
        for item in base_snapshot.worker_item_plans
        if item.worker_item_id is not None
    }
    ids.update(
        item.existing_worker_item_id
        for item in payload.worker_item_identity_decisions
        if item.existing_worker_item_id is not None
    )
    ids.update(payload.retired_worker_item_ids)
    ids.update(
        source_id
        for item in payload.worker_item_identity_decisions
        for source_id in item.derived_from_worker_item_ids
    )
    return tuple(sorted(ids, key=str))


def _head_matches_base(
    head: RootTaskPlanHead,
    payload: PlanningRevisionProposalPayloadV3,
    snapshot: TaskPlanSnapshot,
) -> bool:
    return (
        head.root_task_id == payload.base_descriptor.root_task_id
        and head.active_snapshot_id == payload.base_descriptor.plan_snapshot_id
        and head.active_generation == payload.base_descriptor.plan_generation
        and head.active_snapshot_hash == plan_snapshot_content_hash(snapshot)
    )


def _actual_action(preview: WorkerTransitionPreview) -> WorkerTransitionAction:
    if preview.worker_item_id is None:
        return WorkerTransitionAction.CREATE
    if WorkerTransitionAction.PATCH in preview.allowed_actions:
        return WorkerTransitionAction.PATCH
    if WorkerTransitionAction.MOVE in preview.allowed_actions:
        return WorkerTransitionAction.MOVE
    return WorkerTransitionAction.PRESERVE


def _worker_transition(
    *,
    plan_transition_id: UUID,
    workspace_id: UUID,
    root_task_id: UUID,
    worker_item_id: UUID,
    from_subtask_id: UUID | None,
    to_subtask_id: UUID | None,
    action: WorkerTransitionAction,
    fallback_action: WorkerTransitionAction | None,
    target_context_version: int,
    source_execution: Execution | None,
    status: WorkerTransitionStatus,
    writer_state: WriterState,
    now: datetime,
) -> WorkerTransition:
    terminal = status is WorkerTransitionStatus.CONVERGED
    return WorkerTransition(
        id=uuid4(),
        plan_transition_id=plan_transition_id,
        workspace_id=workspace_id,
        root_task_id=root_task_id,
        worker_item_id=worker_item_id,
        from_subtask_id=from_subtask_id,
        to_subtask_id=to_subtask_id,
        action=action,
        fallback_action=fallback_action,
        target_context_version=target_context_version,
        source_execution_id=source_execution.id if source_execution else None,
        source_session_id=None,
        target_execution_id=None,
        target_session_id=None,
        writer_state=writer_state,
        status=status,
        failure_code=None,
        lease_id=None,
        fencing_token=0,
        attempt_count=0,
        next_attempt_at=None,
        started_at=None,
        completed_at=now if terminal else None,
        version=1,
    )


def _lineage(
    *,
    plan_transition_id: UUID,
    workspace_id: UUID,
    target_type: str,
    target_id: UUID,
    source_type: str,
    source_id: UUID,
    reason: str,
) -> PlanEntityLineage:
    return PlanEntityLineage(
        id=uuid4(),
        plan_transition_id=plan_transition_id,
        workspace_id=workspace_id,
        target_type=target_type,
        target_id=target_id,
        source_type=source_type,
        source_id=source_id,
        reason=reason,
    )


def _event(
    envelope: CommandEnvelope,
    *,
    aggregate_type: str,
    aggregate_id: UUID,
    aggregate_version: int,
    event_type: str,
    payload: dict[str, object],
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


def _positive_int(payload: Mapping[str, object], key: str) -> int:
    value = payload.get(key)
    if not isinstance(value, int) or isinstance(value, bool) or value < 1:
        raise ValidationError(f"{key} must be positive")
    return value


def _uuid(payload: Mapping[str, object], key: str) -> UUID:
    value = payload.get(key)
    try:
        return value if isinstance(value, UUID) else UUID(str(value))
    except (TypeError, ValueError) as exc:
        raise ValidationError(f"{key} must be a UUID") from exc


def _required_hash(payload: Mapping[str, object], key: str) -> str:
    value = payload.get(key)
    if not isinstance(value, str) or re.fullmatch(r"[0-9a-f]{64}", value) is None:
        raise ValidationError(f"{key} must be a lowercase SHA-256 value")
    return value
