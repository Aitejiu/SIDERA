from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from uuid import NAMESPACE_URL, UUID, uuid4, uuid5

from sidera_control_plane.application.command_service import hash_request
from sidera_control_plane.application.ports import ControlPlaneUnitOfWork
from sidera_control_plane.domain.commands import CommandEnvelope, CommandResult
from sidera_control_plane.domain.errors import InvalidTransition
from sidera_control_plane.domain.events import EventData
from sidera_control_plane.domain.execution import ExecutionSessionStatus
from sidera_control_plane.domain.execution_requests import (
    DependencyEnforcement,
    ExecutionRequest,
    ExecutionRequestDependency,
    ExecutionRequestStatus,
    collaboration_gate_mode_for_policy,
)
from sidera_control_plane.domain.plan_revisions import (
    WorkerTransitionAction,
    plan_snapshot_content_hash,
)
from sidera_control_plane.domain.plan_transitions import (
    PlanTransition,
    WorkerTransition,
)
from sidera_control_plane.domain.planning import (
    PlanningRequest,
    PlanningRequestKind,
    PlanningRequestStatus,
    ProposalMaterialization,
)
from sidera_control_plane.domain.planning_proposals import (
    ExecutionIntent,
    ProposalRevision,
)
from sidera_control_plane.domain.plans import TaskPlanSnapshot, WorkerItemPlanItem


@dataclass(frozen=True, slots=True)
class _TargetAuthority:
    plan: PlanTransition
    snapshot: TaskPlanSnapshot
    planning_request: PlanningRequest
    proposal: ProposalRevision
    materialization: ProposalMaterialization | None


@dataclass(frozen=True, slots=True)
class _PreservedRequestCandidate:
    transition: WorkerTransition
    worker_plan: WorkerItemPlanItem
    intent: ExecutionIntent
    subtask_id: UUID
    request_id: UUID


class TransitionExecutionRequestProducer:
    """Produce ordinary requests for confirmed plan-transition targets."""

    def __init__(self, *, owner_id: str) -> None:
        if not owner_id.strip():
            raise ValueError("owner_id is required")
        self._owner_id = owner_id.strip()

    def ensure_for_transition(
        self,
        uow: ControlPlaneUnitOfWork,
        transition: WorkerTransition,
        *,
        now: datetime,
    ) -> ExecutionRequest:
        plan = uow.plan_transitions.get(
            transition.plan_transition_id, transition.workspace_id
        )
        authority = (
            self._target_authority(
                uow,
                plan,
                workspace_id=transition.workspace_id,
                require_materialization=True,
            )
            if plan is not None
            else None
        )
        if authority is None:
            raise InvalidTransition(
                "transition target planning records are unavailable"
            )
        worker_plan = self._worker_plan(authority.snapshot, transition)
        intent = self._intent(authority.proposal, worker_plan.key)
        if intent is None:
            raise InvalidTransition("transition target execution intent is missing")
        request, _created = self._ensure_request(
            uow,
            authority,
            transition,
            worker_plan,
            intent,
            now=now,
            allow_preserved_first_execution=False,
            defer_dependencies=False,
        )
        return request

    def ensure_preserved_pending_for_plan(
        self,
        uow: ControlPlaneUnitOfWork,
        plan: PlanTransition,
        *,
        now: datetime,
        require_materialization: bool,
    ) -> dict[str, UUID]:
        authority = self._target_authority(
            uow,
            plan,
            workspace_id=plan.workspace_id,
            require_materialization=require_materialization,
        )
        if authority is None:
            return {}
        base_snapshot = uow.plans.get(plan.from_snapshot_id, plan.workspace_id)
        if base_snapshot is None:
            return {}

        transitions = uow.worker_transitions.list_for_plan_transition(
            plan.id, plan.workspace_id
        )
        candidates: dict[str, _PreservedRequestCandidate] = {}
        for transition in transitions:
            if transition.action is not WorkerTransitionAction.PRESERVE:
                continue
            if (
                transition.plan_transition_id != plan.id
                or transition.workspace_id != plan.workspace_id
                or transition.root_task_id != plan.root_task_id
            ):
                return {}
            if uow.executions.list_for_worker_item(
                transition.worker_item_id, transition.workspace_id
            ):
                continue

            worker_plan = self._worker_plan(authority.snapshot, transition)
            intent = self._intent(authority.proposal, worker_plan.key)
            if intent is None:
                continue
            self._assert_base_pending_request_authority(
                uow,
                plan=plan,
                base_snapshot=base_snapshot,
                transition=transition,
                worker_plan=worker_plan,
            )
            subtask = next(
                item
                for item in authority.snapshot.subtasks
                if item.key == worker_plan.subtask_key
            )
            if subtask.task_id is None:
                raise InvalidTransition("transition target subtask is missing")
            existing = uow.execution_requests.get_by_proposal_worker_item(
                plan.proposal_revision_id,
                worker_plan.key,
                plan.workspace_id,
                for_update=True,
            )
            request_id = self._transition_request_id(transition, worker_plan.key)
            if existing is not None:
                self._assert_request_matches_target(
                    existing,
                    authority=authority,
                    transition=transition,
                    worker_plan=worker_plan,
                    intent=intent,
                    subtask_id=subtask.task_id,
                    source_execution_id=None,
                    source_session_id=None,
                )
                if (
                    existing.status is ExecutionRequestStatus.MATERIALIZED
                    or existing.first_execution_id is not None
                    or existing.execution_lineage_id is not None
                ):
                    raise InvalidTransition(
                        "preserved first-execution request has execution lineage"
                    )
                request_id = existing.id
            if authority.materialization is not None:
                mapped_request_id = (
                    authority.materialization.execution_request_mapping.get(
                        worker_plan.key
                    )
                )
                if mapped_request_id is not None and mapped_request_id != request_id:
                    raise InvalidTransition(
                        "materialization request mapping does not match preserved request"
                    )
            if worker_plan.key in candidates:
                raise InvalidTransition(
                    "preserved target has duplicate WorkerItem request authority"
                )
            candidates[worker_plan.key] = _PreservedRequestCandidate(
                transition=transition,
                worker_plan=worker_plan,
                intent=intent,
                subtask_id=subtask.task_id,
                request_id=request_id,
            )

        dependencies_by_key = {
            worker_plan_key: self._resolve_preserved_dependencies(
                uow,
                authority=authority,
                base_snapshot=base_snapshot,
                transitions=transitions,
                candidates=candidates,
                worker_plan_key=worker_plan_key,
            )
            for worker_plan_key in candidates
        }

        requests: dict[str, UUID] = {}
        created: list[tuple[ExecutionRequest, WorkerTransition]] = []
        for worker_plan_key, candidate in candidates.items():
            request, was_created = self._ensure_request(
                uow,
                authority,
                candidate.transition,
                candidate.worker_plan,
                candidate.intent,
                now=now,
                allow_preserved_first_execution=True,
                defer_dependencies=True,
                defer_event=True,
            )
            if request.id != candidate.request_id:
                raise InvalidTransition(
                    "preserved request identity changed during production"
                )
            requests[worker_plan_key] = request.id
            if was_created:
                created.append((request, candidate.transition))

        uow.flush()
        for worker_plan_key, request_id in requests.items():
            expected_dependencies = dependencies_by_key[worker_plan_key]
            request = uow.execution_requests.get(request_id, plan.workspace_id)
            if request is None:
                raise InvalidTransition(
                    "preserved request disappeared during production"
                )
            missing_dependencies = self._missing_dependencies(
                uow,
                request,
                expected_dependencies,
            )
            for dependency in missing_dependencies:
                uow.execution_requests.add_dependency(dependency)

        uow.flush()
        for request, transition in created:
            self.append_transition_event(
                uow,
                transition,
                aggregate_type="execution_request",
                aggregate_id=request.id,
                aggregate_version=request.version,
                event_type="ExecutionRequestCreatedByPlanTransition",
                payload={
                    "execution_request_id": str(request.id),
                    "worker_item_id": str(request.worker_item_id),
                    "worker_item_key": request.worker_item_key,
                    "plan_snapshot_id": str(request.plan_snapshot_id),
                    "plan_generation": request.plan_generation,
                    "source_execution_id": None,
                    "source_session_id": None,
                },
                now=now,
            )
        return requests

    def _target_authority(
        self,
        uow: ControlPlaneUnitOfWork,
        plan: PlanTransition,
        *,
        workspace_id: UUID,
        require_materialization: bool,
    ) -> _TargetAuthority | None:
        if (
            plan.workspace_id != workspace_id
            or plan.to_generation != plan.from_generation + 1
        ):
            return None
        snapshot = uow.plans.get(plan.to_snapshot_id, workspace_id)
        planning_request = uow.planning_requests.get(
            plan.planning_request_id, workspace_id
        )
        proposal = uow.planning_records.get_proposal(
            plan.proposal_revision_id, workspace_id
        )
        if snapshot is None or planning_request is None or proposal is None:
            return None
        if (
            snapshot.workspace_id != workspace_id
            or snapshot.root_task_id != plan.root_task_id
            or snapshot.id != plan.to_snapshot_id
            or snapshot.generation != plan.to_generation
            or snapshot.parent_snapshot_id != plan.from_snapshot_id
            or planning_request.id != plan.planning_request_id
            or planning_request.workspace_id != workspace_id
            or planning_request.root_task_id != plan.root_task_id
            or planning_request.kind is not PlanningRequestKind.REVISION
            or planning_request.status is not PlanningRequestStatus.MATERIALIZED
            or proposal.id != plan.proposal_revision_id
            or proposal.planning_request_id != plan.planning_request_id
        ):
            return None
        head = uow.plans.get_head(plan.root_task_id, workspace_id)
        if (
            head is None
            or head.workspace_id != workspace_id
            or head.root_task_id != plan.root_task_id
            or head.active_snapshot_id != snapshot.id
            or head.active_generation != snapshot.generation
            or head.active_snapshot_hash != plan_snapshot_content_hash(snapshot)
        ):
            return None

        materialization = uow.proposal_materializations.get_by_proposal(
            proposal.id, workspace_id
        )
        if require_materialization and materialization is None:
            return None
        if materialization is not None and (
            materialization.workspace_id != workspace_id
            or materialization.planning_request_id != plan.planning_request_id
            or materialization.proposal_revision_id != proposal.id
            or materialization.root_task_id != plan.root_task_id
            or materialization.plan_snapshot_id != snapshot.id
            or materialization.plan_generation != snapshot.generation
            or materialization.plan_transition_id != plan.id
        ):
            return None
        return _TargetAuthority(
            plan=plan,
            snapshot=snapshot,
            planning_request=planning_request,
            proposal=proposal,
            materialization=materialization,
        )

    @staticmethod
    def _worker_plan(
        snapshot: TaskPlanSnapshot, transition: WorkerTransition
    ) -> WorkerItemPlanItem:
        matches = tuple(
            item
            for item in snapshot.worker_item_plans
            if item.worker_item_id == transition.worker_item_id
        )
        if len(matches) != 1:
            raise InvalidTransition("transition target WorkerItem plan is missing")
        worker_plan = matches[0]
        subtask = next(
            (item for item in snapshot.subtasks if item.key == worker_plan.subtask_key),
            None,
        )
        if (
            subtask is None
            or subtask.task_id is None
            or transition.to_subtask_id != subtask.task_id
        ):
            raise InvalidTransition("transition target subtask placement is invalid")
        return worker_plan

    @staticmethod
    def _intent(
        proposal: ProposalRevision, worker_plan_key: str
    ) -> ExecutionIntent | None:
        matches = tuple(
            item
            for item in proposal.payload.execution_intents
            if item.worker_item_key == worker_plan_key
        )
        return matches[0] if len(matches) == 1 else None

    def _resolve_preserved_dependencies(
        self,
        uow: ControlPlaneUnitOfWork,
        *,
        authority: _TargetAuthority,
        base_snapshot: TaskPlanSnapshot,
        transitions: tuple[WorkerTransition, ...],
        candidates: dict[str, _PreservedRequestCandidate],
        worker_plan_key: str,
    ) -> tuple[ExecutionRequestDependency, ...]:
        downstream = candidates[worker_plan_key]
        resolved: list[ExecutionRequestDependency] = []
        for edge in authority.snapshot.worker_item_dependencies:
            if edge.downstream_worker_item_key != worker_plan_key:
                continue
            upstream_plans = tuple(
                item
                for item in authority.snapshot.worker_item_plans
                if item.key == edge.upstream_worker_item_key
            )
            if len(upstream_plans) != 1 or upstream_plans[0].worker_item_id is None:
                raise InvalidTransition(
                    "WorkerItem dependency upstream target is ambiguous"
                )
            upstream_plan = upstream_plans[0]
            upstream_transitions = tuple(
                item
                for item in transitions
                if item.worker_item_id == upstream_plan.worker_item_id
            )
            if len(upstream_transitions) != 1:
                raise InvalidTransition(
                    "WorkerItem dependency upstream transition is unavailable"
                )
            upstream_transition = upstream_transitions[0]
            if (
                upstream_transition.plan_transition_id != authority.plan.id
                or upstream_transition.workspace_id != authority.plan.workspace_id
                or upstream_transition.root_task_id != authority.plan.root_task_id
            ):
                raise InvalidTransition(
                    "WorkerItem dependency upstream transition scope is inconsistent"
                )
            upstream_intent = self._intent(authority.proposal, upstream_plan.key)
            if upstream_intent is None:
                raise InvalidTransition(
                    "WorkerItem dependency upstream target intent is missing"
                )

            staged_upstream = candidates.get(upstream_plan.key)
            if staged_upstream is not None:
                upstream_request_id = staged_upstream.request_id
            elif upstream_transition.action is WorkerTransitionAction.PRESERVE:
                history = uow.executions.list_for_worker_item(
                    upstream_transition.worker_item_id,
                    upstream_transition.workspace_id,
                )
                if not history:
                    raise InvalidTransition(
                        "WorkerItem dependency upstream preserved request is unavailable"
                    )
                upstream_request_id = self._preserved_source_request_id(
                    uow,
                    authority=authority,
                    base_snapshot=base_snapshot,
                    transition=upstream_transition,
                    worker_plan=upstream_plan,
                    intent=upstream_intent,
                )
            else:
                upstream_subtask = next(
                    (
                        item
                        for item in authority.snapshot.subtasks
                        if item.key == upstream_plan.subtask_key
                    ),
                    None,
                )
                if upstream_subtask is None or upstream_subtask.task_id is None:
                    raise InvalidTransition(
                        "WorkerItem dependency upstream target placement is missing"
                    )
                upstream_request = uow.execution_requests.get_by_proposal_worker_item(
                    authority.proposal.id,
                    upstream_plan.key,
                    authority.plan.workspace_id,
                    for_update=True,
                )
                if upstream_request is None:
                    raise InvalidTransition(
                        "WorkerItem dependency upstream target request is unavailable"
                    )
                source_execution_id = (
                    upstream_transition.source_execution_id
                    if upstream_transition.action is WorkerTransitionAction.CONTINUE
                    else None
                )
                source_session_id = (
                    upstream_transition.source_session_id
                    if source_execution_id is not None
                    else None
                )
                self._assert_request_matches_target(
                    upstream_request,
                    authority=authority,
                    transition=upstream_transition,
                    worker_plan=upstream_plan,
                    intent=upstream_intent,
                    subtask_id=upstream_subtask.task_id,
                    source_execution_id=source_execution_id,
                    source_session_id=source_session_id,
                )
                upstream_request_id = upstream_request.id

            dependency = ExecutionRequestDependency(
                downstream_request_id=downstream.request_id,
                upstream_request_id=upstream_request_id,
                enforcement=(
                    DependencyEnforcement.HARD
                    if edge.kind == "dependency"
                    else DependencyEnforcement.INFORMATIONAL
                ),
                required_artifact_refs=edge.required_artifact_refs,
            )
            if any(
                item.upstream_request_id == dependency.upstream_request_id
                for item in resolved
            ):
                raise InvalidTransition(
                    "WorkerItem dependency graph has duplicate request edges"
                )
            resolved.append(dependency)
        return tuple(resolved)

    @staticmethod
    def _preserved_source_request_id(
        uow: ControlPlaneUnitOfWork,
        *,
        authority: _TargetAuthority,
        base_snapshot: TaskPlanSnapshot,
        transition: WorkerTransition,
        worker_plan: WorkerItemPlanItem,
        intent: ExecutionIntent,
    ) -> UUID:
        source_execution_id = transition.source_execution_id
        source_session_id = transition.source_session_id
        if source_execution_id is None:
            raise InvalidTransition(
                "preserved WorkerItem dependency has no scoped source execution"
            )
        base_workers = tuple(
            item
            for item in base_snapshot.worker_item_plans
            if item.key == worker_plan.key
            and item.worker_item_id == transition.worker_item_id
        )
        if len(base_workers) != 1:
            raise InvalidTransition(
                "preserved WorkerItem dependency has no unique base placement"
            )
        base_worker = base_workers[0]
        base_subtask = next(
            (
                item
                for item in base_snapshot.subtasks
                if item.key == base_worker.subtask_key
            ),
            None,
        )
        if (
            base_subtask is None
            or base_subtask.task_id is None
            or worker_plan.subtask_key != base_worker.subtask_key
            or transition.from_subtask_id != base_subtask.task_id
            or transition.to_subtask_id != base_subtask.task_id
        ):
            raise InvalidTransition("preserved WorkerItem dependency placement changed")

        worker = uow.worker_items.get(
            transition.worker_item_id, authority.plan.workspace_id
        )
        execution = uow.executions.get(source_execution_id, authority.plan.workspace_id)
        session = (
            uow.sessions.get_any(source_session_id, authority.plan.workspace_id)
            if source_session_id is not None
            else None
        )
        if (
            worker is None
            or worker.workspace_id != authority.plan.workspace_id
            or worker.root_task_id != authority.plan.root_task_id
            or worker.context_version != transition.target_context_version
            or worker.acknowledged_context_version != transition.target_context_version
            or execution is None
            or execution.workspace_id != authority.plan.workspace_id
            or execution.worker_item_id != transition.worker_item_id
            or execution.task_id != base_subtask.task_id
            or not frozenset(intent.capability_requirements).issubset(
                execution.capability_requirements
            )
            or (
                intent.adapter_preferences
                and execution.adapter_preferences != tuple(intent.adapter_preferences)
            )
            or (
                source_session_id is not None
                and (
                    session is None
                    or session.workspace_id != authority.plan.workspace_id
                    or session.execution_id != source_execution_id
                    or session.id != source_session_id
                    or session.status is ExecutionSessionStatus.LOST
                )
            )
        ):
            raise InvalidTransition(
                "preserved WorkerItem dependency source authority is inconsistent"
            )

        request = uow.execution_requests.get_by_execution_lineage(
            execution.lineage_id, authority.plan.workspace_id
        )
        if (
            request is None
            or request.status is not ExecutionRequestStatus.MATERIALIZED
            or request.execution_lineage_id != execution.lineage_id
            or request.first_execution_id is None
            or request.workspace_id != authority.plan.workspace_id
            or request.root_task_id != authority.plan.root_task_id
            or request.worker_item_id != transition.worker_item_id
            or request.worker_item_key != worker_plan.key
            or request.subtask_id != base_subtask.task_id
        ):
            raise InvalidTransition(
                "preserved WorkerItem dependency source request is inconsistent"
            )
        materialization = uow.proposal_materializations.get_by_proposal(
            request.proposal_revision_id, authority.plan.workspace_id
        )
        if (
            materialization is None
            or materialization.workspace_id != authority.plan.workspace_id
            or materialization.root_task_id != authority.plan.root_task_id
            or materialization.planning_request_id != request.planning_request_id
            or materialization.proposal_revision_id != request.proposal_revision_id
            or materialization.plan_snapshot_id != request.plan_snapshot_id
            or materialization.plan_generation != request.plan_generation
            or request.plan_generation > authority.plan.from_generation
            or materialization.subtask_task_mapping.get(base_worker.subtask_key)
            != base_subtask.task_id
            or materialization.worker_item_mapping.get(worker_plan.key)
            != transition.worker_item_id
            or materialization.execution_request_mapping.get(worker_plan.key)
            not in {None, request.id}
        ):
            raise InvalidTransition(
                "preserved WorkerItem dependency source materialization is inconsistent"
            )
        return request.id

    @staticmethod
    def _missing_dependencies(
        uow: ControlPlaneUnitOfWork,
        request: ExecutionRequest,
        expected: tuple[ExecutionRequestDependency, ...],
    ) -> tuple[ExecutionRequestDependency, ...]:
        expected_by_upstream = {item.upstream_request_id: item for item in expected}
        actual = uow.execution_requests.list_dependencies(
            request.id, request.workspace_id
        )
        actual_by_upstream = {item.upstream_request_id: item for item in actual}
        if any(
            upstream_id not in expected_by_upstream
            or expected_by_upstream[upstream_id] != dependency
            for upstream_id, dependency in actual_by_upstream.items()
        ):
            raise InvalidTransition(
                "existing preserved request has a contradictory dependency graph"
            )
        return tuple(
            dependency
            for upstream_id, dependency in expected_by_upstream.items()
            if upstream_id not in actual_by_upstream
        )

    def _ensure_request(
        self,
        uow: ControlPlaneUnitOfWork,
        authority: _TargetAuthority,
        transition: WorkerTransition,
        worker_plan: WorkerItemPlanItem,
        intent: ExecutionIntent,
        *,
        now: datetime,
        allow_preserved_first_execution: bool,
        defer_dependencies: bool,
        defer_event: bool = False,
    ) -> tuple[ExecutionRequest, bool]:
        plan = authority.plan
        snapshot = authority.snapshot
        planning_request = authority.planning_request
        if (
            transition.plan_transition_id != plan.id
            or transition.workspace_id != plan.workspace_id
            or transition.root_task_id != plan.root_task_id
            or transition.worker_item_id != worker_plan.worker_item_id
            or worker_plan.worker_item_id is None
        ):
            raise InvalidTransition("transition target identity is inconsistent")
        if (
            transition.action is WorkerTransitionAction.PRESERVE
            and not allow_preserved_first_execution
        ):
            raise InvalidTransition("preserved request requires first-execution scope")
        if (
            transition.action is WorkerTransitionAction.PRESERVE
            and uow.executions.list_for_worker_item(
                transition.worker_item_id, transition.workspace_id
            )
        ):
            raise InvalidTransition(
                "preserved WorkerItem already has execution history"
            )

        subtask = next(
            (item for item in snapshot.subtasks if item.key == worker_plan.subtask_key),
            None,
        )
        if subtask is None or subtask.task_id is None:
            raise InvalidTransition("transition target subtask is missing")
        source_execution_id = (
            transition.source_execution_id
            if transition.action is WorkerTransitionAction.CONTINUE
            else None
        )
        source_session_id = (
            transition.source_session_id if source_execution_id is not None else None
        )
        request = uow.execution_requests.get_by_proposal_worker_item(
            plan.proposal_revision_id,
            worker_plan.key,
            transition.workspace_id,
            for_update=True,
        )
        if request is not None:
            self._assert_request_matches_target(
                request,
                authority=authority,
                transition=transition,
                worker_plan=worker_plan,
                intent=intent,
                subtask_id=subtask.task_id,
                source_execution_id=source_execution_id,
                source_session_id=source_session_id,
            )
            if transition.action is WorkerTransitionAction.PRESERVE and (
                request.status is ExecutionRequestStatus.MATERIALIZED
                or request.first_execution_id is not None
                or request.execution_lineage_id is not None
            ):
                raise InvalidTransition(
                    "preserved first-execution request has execution lineage"
                )
            return request, False

        request_id = self._transition_request_id(transition, worker_plan.key)
        request = ExecutionRequest.create(
            request_id=request_id,
            workspace_id=transition.workspace_id,
            planning_request_id=plan.planning_request_id,
            proposal_revision_id=plan.proposal_revision_id,
            plan_snapshot_id=plan.to_snapshot_id,
            plan_generation=plan.to_generation,
            root_task_id=plan.root_task_id,
            subtask_id=subtask.task_id,
            worker_item_id=transition.worker_item_id,
            worker_item_key=worker_plan.key,
            intent_key=worker_plan.key,
            purpose=intent.purpose,
            capability_requirements=frozenset(intent.capability_requirements),
            adapter_preferences=intent.adapter_preferences,
            adapter_forbidden=planning_request.profile_snapshot.adapter_forbidden,
            budget_class=intent.budget_class,
            not_before=now + timedelta(seconds=intent.not_before_seconds or 0),
            max_attempts=intent.max_attempts,
            expected_artifact_refs=intent.expected_artifact_refs,
            expected_evidence_refs=intent.required_evidence_refs,
            expected_outputs=subtask.expected_outputs,
            collaboration_gate_mode=collaboration_gate_mode_for_policy(
                snapshot.collaboration_policy,
                subtask_key=worker_plan.subtask_key,
            ),
            now=now,
            source_execution_id=source_execution_id,
            source_session_id=source_session_id,
        )
        uow.execution_requests.add(request)
        uow.flush()
        if not defer_dependencies:
            self._add_request_dependencies(
                uow,
                request,
                snapshot=snapshot,
                worker_plan_key=worker_plan.key,
            )
        if not defer_event:
            self.append_transition_event(
                uow,
                transition,
                aggregate_type="execution_request",
                aggregate_id=request.id,
                aggregate_version=request.version,
                event_type="ExecutionRequestCreatedByPlanTransition",
                payload={
                    "execution_request_id": str(request.id),
                    "worker_item_id": str(request.worker_item_id),
                    "worker_item_key": request.worker_item_key,
                    "plan_snapshot_id": str(request.plan_snapshot_id),
                    "plan_generation": request.plan_generation,
                    "source_execution_id": str(source_execution_id)
                    if source_execution_id is not None
                    else None,
                    "source_session_id": str(source_session_id)
                    if source_session_id is not None
                    else None,
                },
                now=now,
            )
        return request, True

    @staticmethod
    def _transition_request_id(
        transition: WorkerTransition, worker_plan_key: str
    ) -> UUID:
        return uuid5(
            NAMESPACE_URL,
            f"sidera.plan-transition.execution-request:{transition.id}:{worker_plan_key}",
        )

    @staticmethod
    def _assert_request_matches_target(
        request: ExecutionRequest,
        *,
        authority: _TargetAuthority,
        transition: WorkerTransition,
        worker_plan: WorkerItemPlanItem,
        intent: ExecutionIntent,
        subtask_id: UUID,
        source_execution_id: UUID | None,
        source_session_id: UUID | None,
    ) -> None:
        plan = authority.plan
        snapshot = authority.snapshot
        planning_request = authority.planning_request
        if (
            request.workspace_id != transition.workspace_id
            or request.planning_request_id != plan.planning_request_id
            or request.proposal_revision_id != plan.proposal_revision_id
            or request.plan_snapshot_id != snapshot.id
            or request.plan_generation != snapshot.generation
            or request.root_task_id != plan.root_task_id
            or request.worker_item_id != transition.worker_item_id
            or request.worker_item_key != worker_plan.key
            or request.intent_key != worker_plan.key
            or request.subtask_id != subtask_id
            or request.purpose != intent.purpose
            or request.capability_requirements
            != frozenset(intent.capability_requirements)
            or request.adapter_preferences != tuple(intent.adapter_preferences)
            or request.adapter_forbidden
            != frozenset(planning_request.profile_snapshot.adapter_forbidden)
            or request.budget_class != intent.budget_class
            or request.max_attempts != intent.max_attempts
            or request.expected_artifact_refs != tuple(intent.expected_artifact_refs)
            or request.expected_evidence_refs != tuple(intent.required_evidence_refs)
            or request.expected_outputs
            != next(
                item.expected_outputs
                for item in snapshot.subtasks
                if item.key == worker_plan.subtask_key
            )
            or request.collaboration_gate_mode
            != collaboration_gate_mode_for_policy(
                snapshot.collaboration_policy,
                subtask_key=worker_plan.subtask_key,
            )
            or request.source_execution_id != source_execution_id
            or request.source_session_id != source_session_id
        ):
            raise InvalidTransition(
                "existing transition request has different authority"
            )
        if request.status in {
            ExecutionRequestStatus.CANCELLED,
            ExecutionRequestStatus.OBSOLETE,
        }:
            raise InvalidTransition("existing transition request is closed")
        materialization = authority.materialization
        if materialization is not None:
            mapped_request_id = materialization.execution_request_mapping.get(
                worker_plan.key
            )
            if mapped_request_id is not None and mapped_request_id != request.id:
                raise InvalidTransition(
                    "materialization request mapping does not match transition"
                )

    @staticmethod
    def _assert_base_pending_request_authority(
        uow: ControlPlaneUnitOfWork,
        *,
        plan: PlanTransition,
        base_snapshot: TaskPlanSnapshot,
        transition: WorkerTransition,
        worker_plan: WorkerItemPlanItem,
    ) -> None:
        base_worker_plans = tuple(
            item
            for item in base_snapshot.worker_item_plans
            if item.worker_item_id == transition.worker_item_id
            and item.key == worker_plan.key
        )
        if len(base_worker_plans) != 1:
            raise InvalidTransition("preserved WorkerItem has no base placement")
        base_worker = base_worker_plans[0]
        base_subtask = next(
            (
                item
                for item in base_snapshot.subtasks
                if item.key == base_worker.subtask_key
            ),
            None,
        )
        if base_subtask is None or base_subtask.task_id is None:
            raise InvalidTransition("preserved WorkerItem base subtask is missing")

        old_requests = tuple(
            item
            for item in uow.execution_requests.list_for_root(
                plan.root_task_id, plan.workspace_id
            )
            if item.worker_item_id == transition.worker_item_id
            and item.worker_item_key == worker_plan.key
            and item.plan_generation == plan.from_generation
            and item.handoff_transition_id is None
            and item.result_derivation_kind is None
        )
        if len(old_requests) > 1:
            raise InvalidTransition(
                "base plan has ambiguous pending WorkerItem requests"
            )
        if not old_requests:
            return
        old_request = old_requests[0]
        if (
            old_request.workspace_id != plan.workspace_id
            or old_request.root_task_id != plan.root_task_id
            or old_request.plan_snapshot_id != plan.from_snapshot_id
            or old_request.subtask_id != base_subtask.task_id
            or old_request.source_execution_id is not None
            or old_request.source_session_id is not None
        ):
            raise InvalidTransition("base pending request has contradictory placement")
        if (
            old_request.status is ExecutionRequestStatus.MATERIALIZED
            or old_request.first_execution_id is not None
            or old_request.execution_lineage_id is not None
        ):
            raise InvalidTransition("base pending request was already materialized")
        base_materialization = uow.proposal_materializations.get_by_proposal(
            old_request.proposal_revision_id, plan.workspace_id
        )
        if (
            base_materialization is None
            or base_materialization.workspace_id != plan.workspace_id
            or base_materialization.root_task_id != plan.root_task_id
            or base_materialization.plan_snapshot_id != plan.from_snapshot_id
            or base_materialization.plan_generation != plan.from_generation
        ):
            raise InvalidTransition(
                "base pending request materialization is inconsistent"
            )

    @staticmethod
    def _add_request_dependencies(
        uow: ControlPlaneUnitOfWork,
        request: ExecutionRequest,
        *,
        snapshot: TaskPlanSnapshot,
        worker_plan_key: str,
    ) -> None:
        for dependency in snapshot.worker_item_dependencies:
            if dependency.downstream_worker_item_key != worker_plan_key:
                continue
            upstream = uow.execution_requests.get_by_proposal_worker_item(
                request.proposal_revision_id,
                dependency.upstream_worker_item_key,
                request.workspace_id,
            )
            if upstream is None:
                continue
            uow.execution_requests.add_dependency(
                ExecutionRequestDependency(
                    downstream_request_id=request.id,
                    upstream_request_id=upstream.id,
                    enforcement=(
                        DependencyEnforcement.HARD
                        if dependency.kind == "dependency"
                        else DependencyEnforcement.INFORMATIONAL
                    ),
                    required_artifact_refs=dependency.required_artifact_refs,
                )
            )

    def append_transition_event(
        self,
        uow: ControlPlaneUnitOfWork,
        transition: WorkerTransition,
        *,
        aggregate_type: str,
        aggregate_id: UUID,
        aggregate_version: int,
        event_type: str,
        payload: dict[str, object],
        now: datetime,
    ) -> None:
        del now
        envelope = self.transition_envelope(
            transition,
            event_type=event_type,
            aggregate_id=aggregate_id,
            aggregate_version=aggregate_version,
        )
        event = EventData(
            id=uuid4(),
            workspace_id=transition.workspace_id,
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
        self.record_internal_event(uow, envelope, event)

    def transition_envelope(
        self,
        transition: WorkerTransition,
        *,
        event_type: str,
        aggregate_id: UUID,
        aggregate_version: int,
        command_id: UUID | None = None,
    ) -> CommandEnvelope:
        resolved_command_id = command_id or uuid5(
            NAMESPACE_URL,
            f"sidera.plan-transition:{transition.id}:"
            f"{event_type}:{aggregate_id}:v{aggregate_version}",
        )
        return CommandEnvelope(
            id=resolved_command_id,
            workspace_id=transition.workspace_id,
            command_type="PlanTransitionRuntime",
            actor_type="system",
            actor_id=self._owner_id,
            target_type="worker_transition",
            target_id=transition.id,
            payload={
                "worker_transition_id": str(transition.id),
                "event_type": event_type,
                "aggregate_id": str(aggregate_id),
            },
            expected_version=aggregate_version,
            idempotency_key=(
                f"plan-transition:{transition.id}:{event_type}:"
                f"{aggregate_id}:v{aggregate_version}"
            ),
            correlation_id=transition.plan_transition_id,
        )

    @staticmethod
    def record_internal_event(
        uow: ControlPlaneUnitOfWork,
        envelope: CommandEnvelope,
        event: EventData,
    ) -> None:
        if uow.commands.find_by_id(envelope.workspace_id, envelope.id) is not None:
            return
        uow.commands.record_applied(
            envelope,
            hash_request(envelope),
            CommandResult(
                command_id=envelope.id,
                aggregate_id=event.aggregate_id,
                aggregate_version=event.aggregate_version,
                event_ids=(event.id,),
            ),
        )
        uow.events.append_with_outbox(event)
