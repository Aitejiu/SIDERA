from __future__ import annotations

from copy import deepcopy
from uuid import UUID, uuid4

import pytest
from sqlalchemy import delete, or_, select

from sidera_control_plane.application.command_service import CommandService
from sidera_control_plane.application.plan_transition_coordinator import (
    PlanTransitionCoordinator,
)
from sidera_control_plane.application.planning_commands import PlanningCommandHandler
from sidera_control_plane.application.transition_execution_requests import (
    TransitionExecutionRequestProducer,
)
from sidera_control_plane.domain.collaboration import resolve_collaboration_policy_v2
from sidera_control_plane.domain.commands import CommandEnvelope
from sidera_control_plane.domain.errors import InvalidTransition
from sidera_control_plane.domain.execution import Execution, ExecutionStatus
from sidera_control_plane.domain.execution_requests import (
    DependencyEnforcement,
    ExecutionRequestStatus,
)
from sidera_control_plane.domain.plan_revisions import (
    ContextDisposition,
    WorkerTransitionAction,
    plan_snapshot_content_hash,
)
from sidera_control_plane.domain.plan_transitions import WorkerTransitionStatus
from sidera_control_plane.domain.planning import PlanningRequestKind
from sidera_control_plane.domain.planning_proposals import (
    PlanningProfileDescriptor,
    ProposalRevision,
)
from sidera_control_plane.domain.planning_revision_proposals import (
    PlanningRevisionProposalPayloadV5,
)
from sidera_control_plane.domain.revision_diff import RevisionDiffNormalizer
from sidera_control_plane.persistence.database import Database
from sidera_control_plane.persistence.models import (
    EventModel,
    ExecutionRequestDependencyModel,
    ExecutionRequestModel,
)
from tests.integration.result_input_scenario import ResultInputScenario

pytestmark = pytest.mark.integration


@pytest.fixture
def scenario(database: Database, workspace_id: UUID, tmp_path) -> ResultInputScenario:
    return ResultInputScenario(database, workspace_id, tmp_path)


def test_revision_preserves_successful_test_and_pending_integration_request(
    scenario: ResultInputScenario,
) -> None:
    scenario.confirm_plan(
        groups=(
            {
                "group_key": "research",
                "title": "Command and test results",
                "consumer_worker_item_key": "summary",
                "candidates": [
                    {"worker_item_key": "a", "priority": 1},
                    {"worker_item_key": "b", "priority": 2},
                ],
                "requirements": {
                    "execution_status": "succeeded",
                    "required_artifact_refs": ["artifact_type:build"],
                    "required_evidence_refs": ["acceptance:build"],
                    "evidence_status": "candidate",
                },
                "mode": "at_least",
                "minimum_count": 2,
                "selection_strategy": "plan_order",
            },
        ),
        source_worker_keys=("a", "b", "c"),
    )
    scenario.complete_source("b", body=b"test branch result\n")
    test_execution_id = scenario.latest_execution_id("b")
    integration_request_id = scenario.request_ids["summary"]
    integration_worker_id = scenario.worker_item_ids["summary"]
    test_worker_id = scenario.worker_item_ids["b"]
    with scenario.uow_factory() as uow:
        test_worker = uow.worker_items.get(test_worker_id, scenario.workspace_id)
        assert test_worker is not None
        test_context_version = test_worker.context_version

    revision = _confirm_command_input_revision(scenario)
    transition_id = UUID(str(revision.data["plan_transition_id"]))
    proposal_id: UUID

    with scenario.uow_factory() as uow:
        plan = uow.plan_transitions.get(transition_id, scenario.workspace_id)
        assert plan is not None
        proposal_id = plan.proposal_revision_id
        atomic_request = uow.execution_requests.get_by_proposal_worker_item(
            proposal_id, "summary", scenario.workspace_id
        )
        assert atomic_request is not None, (
            "confirmed preserved integration intent must be persisted atomically"
        )
        assert atomic_request.plan_generation == 2
        assert atomic_request.source_execution_id is None
        assert atomic_request.source_session_id is None
        integration_worker_id = scenario.worker_item_ids["summary"]
        uow.session.execute(
            delete(ExecutionRequestDependencyModel).where(
                or_(
                    ExecutionRequestDependencyModel.downstream_request_id
                    == atomic_request.id,
                    ExecutionRequestDependencyModel.upstream_request_id
                    == atomic_request.id,
                )
            )
        )
        uow.session.execute(
            delete(ExecutionRequestModel).where(
                ExecutionRequestModel.id == atomic_request.id,
                ExecutionRequestModel.workspace_id == scenario.workspace_id,
            )
        )

    # Model the already-confirmed legacy plan whose request row was never
    # produced. Reconciliation must recreate its deterministic request once.
    for _ in range(2):
        PlanTransitionCoordinator(
            uow_factory=scenario.uow_factory,
            owner_id="preserved-pending-request-test",
            clock=scenario.clock,
        ).run_once(limit=20)
    scenario.activate()

    with scenario.uow_factory() as uow:
        plan = uow.plan_transitions.get(transition_id, scenario.workspace_id)
        assert plan is not None
        proposal_id = plan.proposal_revision_id
        transitions = uow.worker_transitions.list_for_plan_transition(
            transition_id, scenario.workspace_id
        )
        integration_transition = next(
            item for item in transitions if item.worker_item_id == integration_worker_id
        )
        test_transition = next(
            item for item in transitions if item.worker_item_id == test_worker_id
        )
        assert integration_transition.action is WorkerTransitionAction.PRESERVE
        assert integration_transition.status is WorkerTransitionStatus.CONVERGED
        assert test_transition.action is WorkerTransitionAction.PRESERVE
        assert test_transition.source_execution_id == test_execution_id

        old_integration_request = uow.execution_requests.get(
            integration_request_id, scenario.workspace_id
        )
        assert old_integration_request is not None
        assert old_integration_request.status is ExecutionRequestStatus.OBSOLETE
        assert old_integration_request.gate_reason == "stale_plan"
        assert not uow.executions.list_for_worker_item(
            integration_worker_id, scenario.workspace_id
        )
        test_executions = uow.executions.list_for_worker_item(
            test_worker_id, scenario.workspace_id
        )
        assert tuple(item.id for item in test_executions) == (test_execution_id,)
        test_worker = uow.worker_items.get(test_worker_id, scenario.workspace_id)
        assert test_worker is not None
        assert test_worker.context_version == test_context_version

        current_integration_request = (
            uow.execution_requests.get_by_proposal_worker_item(
                proposal_id, "summary", scenario.workspace_id
            )
        )
        assert current_integration_request is not None, (
            "confirmed preserved integration intent must have a current request"
        )
        assert current_integration_request.plan_generation == 2
        assert current_integration_request.source_execution_id is None
        assert current_integration_request.source_session_id is None
        request_created_events = uow.session.scalars(
            select(EventModel).where(
                EventModel.aggregate_type == "execution_request",
                EventModel.aggregate_id == current_integration_request.id,
                EventModel.event_type == "ExecutionRequestCreatedByPlanTransition",
            )
        ).all()
        assert len(request_created_events) == 1

    prepared = scenario._collaboration_reconciler.prepare_next(
        owner_id="preserved-pending-request-test",
        now=scenario.now,
        workspace_id=scenario.workspace_id,
        root_task_id=scenario.ids["root_task"],
    )
    assert prepared is not None
    research = next(
        item
        for item in prepared.projection.result_input_evaluations
        if item.group_key == "research"
    )
    assert research.consumer_execution_request_id == current_integration_request.id
    assert not research.evaluation.satisfied
    assert research.evaluation.ready_count == 1


def test_lost_execution_without_session_is_not_treated_as_never_executed(
    scenario: ResultInputScenario,
) -> None:
    scenario.confirm_plan()
    worker_item_id = scenario.worker_item_ids["summary"]
    with scenario.uow_factory() as uow:
        head = uow.plans.get_head(scenario.ids["root_task"], scenario.workspace_id)
        assert head is not None
        snapshot = uow.plans.get(head.active_snapshot_id, scenario.workspace_id)
        assert snapshot is not None
        worker_plan = next(
            item
            for item in snapshot.worker_item_plans
            if item.worker_item_id == worker_item_id
        )
        subtask = next(
            item for item in snapshot.subtasks if item.key == worker_plan.subtask_key
        )
        assert subtask.task_id is not None
        original_proposal = uow.planning_records.get_current_proposal(
            scenario.ids["planning_request"], scenario.workspace_id
        )
        assert original_proposal is not None
        pending_request = uow.execution_requests.get_by_proposal_worker_item(
            original_proposal.id,
            worker_plan.key,
            scenario.workspace_id,
        )
        assert pending_request is not None
        lost_execution_id = uuid4()
        lost_execution = Execution.create(
            execution_id=lost_execution_id,
            workspace_id=scenario.workspace_id,
            task_id=subtask.task_id,
            worker_item_id=worker_item_id,
            lineage_id=lost_execution_id,
            attempt=1,
            max_attempts=pending_request.max_attempts,
            purpose=pending_request.purpose,
            capability_requirements=pending_request.capability_requirements,
            pinned_worker_id=None,
            not_before=scenario.now,
            adapter_preferences=pending_request.adapter_preferences,
            adapter_forbidden=pending_request.adapter_forbidden,
        )
        lost_execution.status = ExecutionStatus.LOST
        lost_execution.finished_at = scenario.now
        uow.executions.add(lost_execution)

    revision = _confirm_command_input_revision(scenario)
    transition_id = UUID(str(revision.data["plan_transition_id"]))
    PlanTransitionCoordinator(
        uow_factory=scenario.uow_factory,
        owner_id="lost-preserved-request-test",
        clock=scenario.clock,
    ).run_once(limit=20)

    with scenario.uow_factory() as uow:
        plan = uow.plan_transitions.get(transition_id, scenario.workspace_id)
        assert plan is not None
        transitions = uow.worker_transitions.list_for_plan_transition(
            transition_id, scenario.workspace_id
        )
        summary_transition = next(
            item for item in transitions if item.worker_item_id == worker_item_id
        )
        assert summary_transition.action is WorkerTransitionAction.PRESERVE
        assert summary_transition.source_execution_id is None
        assert uow.executions.get(lost_execution.id, scenario.workspace_id) is not None
        assert (
            uow.sessions.get_latest_for_execution(
                lost_execution.id, scenario.workspace_id
            )
            is None
        )
        assert (
            uow.execution_requests.get_by_proposal_worker_item(
                plan.proposal_revision_id, "summary", scenario.workspace_id
            )
            is None
        )


def test_preserved_hard_dependency_links_its_exact_source_request(
    scenario: ResultInputScenario,
) -> None:
    scenario.confirm_plan(groups=(), hard_edges=(("a", "summary"),))
    scenario.complete_source("a", body=b"successful preserved source\n")
    source_execution_id = scenario.latest_execution_id("a")
    revision = _confirm_command_input_revision(
        scenario,
        hard_dependency=("a", "summary"),
        changed_worker_key="c",
    )
    transition_id = UUID(str(revision.data["plan_transition_id"]))
    consumer_request_id: UUID
    source_request_id: UUID

    with scenario.uow_factory() as uow:
        plan = uow.plan_transitions.get(transition_id, scenario.workspace_id)
        assert plan is not None
        consumer = uow.execution_requests.get_by_proposal_worker_item(
            plan.proposal_revision_id, "summary", scenario.workspace_id
        )
        assert consumer is not None
        source_execution = uow.executions.get(
            source_execution_id, scenario.workspace_id
        )
        assert source_execution is not None
        source_request = uow.execution_requests.get_by_execution_lineage(
            source_execution.lineage_id, scenario.workspace_id
        )
        assert source_request is not None
        assert source_request.first_execution_id == source_execution_id
        assert source_request.root_task_id == plan.root_task_id
        assert source_request.workspace_id == plan.workspace_id
        upstream_transition = next(
            item
            for item in uow.worker_transitions.list_for_plan_transition(
                transition_id, scenario.workspace_id
            )
            if item.worker_item_id == scenario.worker_item_ids["a"]
        )
        assert upstream_transition.action is WorkerTransitionAction.PRESERVE
        assert upstream_transition.source_execution_id == source_execution_id
        assert source_request.plan_snapshot_id == plan.from_snapshot_id
        assert source_request.subtask_id == upstream_transition.to_subtask_id
        upstream_worker = uow.worker_items.get(
            scenario.worker_item_ids["a"], scenario.workspace_id
        )
        assert upstream_worker is not None
        assert (
            upstream_worker.context_version
            == upstream_transition.target_context_version
        )
        dependencies = uow.execution_requests.list_dependencies(
            consumer.id, scenario.workspace_id
        )
        assert len(dependencies) == 1
        assert dependencies[0].upstream_request_id == source_request.id
        assert dependencies[0].enforcement is DependencyEnforcement.HARD
        assert dependencies[0].required_artifact_refs == ("artifact_type:build",)
        consumer_request_id = consumer.id
        source_request_id = source_request.id

    with scenario.uow_factory() as uow:
        result = uow.session.execute(
            delete(ExecutionRequestDependencyModel).where(
                ExecutionRequestDependencyModel.downstream_request_id
                == consumer_request_id,
                ExecutionRequestDependencyModel.upstream_request_id
                == source_request_id,
            )
        )
        assert result.rowcount == 1

    producer = TransitionExecutionRequestProducer(
        owner_id="preserved-hard-dependency-retry"
    )
    for _ in range(2):
        with scenario.uow_factory() as uow:
            plan = uow.plan_transitions.get(transition_id, scenario.workspace_id)
            assert plan is not None
            requests = producer.ensure_preserved_pending_for_plan(
                uow,
                plan,
                now=scenario.now,
                require_materialization=True,
            )
            assert requests["summary"] == consumer_request_id

    with scenario.uow_factory() as uow:
        dependencies = uow.execution_requests.list_dependencies(
            consumer_request_id, scenario.workspace_id
        )
        assert len(dependencies) == 1
        assert dependencies[0].upstream_request_id == source_request_id
        assert dependencies[0].enforcement is DependencyEnforcement.HARD
        request_events = uow.session.scalars(
            select(EventModel).where(
                EventModel.aggregate_type == "execution_request",
                EventModel.aggregate_id == consumer_request_id,
                EventModel.event_type == "ExecutionRequestCreatedByPlanTransition",
            )
        ).all()
        assert len(request_events) == 1


def test_changed_upstream_without_target_request_refuses_atomically(
    scenario: ResultInputScenario,
) -> None:
    scenario.confirm_plan(groups=(), hard_edges=(("a", "summary"),))
    with scenario.uow_factory() as uow:
        before_requests = {
            request.id
            for request in uow.execution_requests.list_for_root(
                scenario.ids["root_task"], scenario.workspace_id
            )
        }
        before_request_events = uow.session.scalar(
            select(EventModel.id).where(
                EventModel.event_type == "ExecutionRequestCreatedByPlanTransition"
            )
        )

    with pytest.raises(InvalidTransition, match="dependency"):
        _confirm_command_input_revision(scenario, hard_dependency=("a", "summary"))

    with scenario.uow_factory() as uow:
        head = uow.plans.get_head(scenario.ids["root_task"], scenario.workspace_id)
        assert head is not None
        assert head.active_generation == 1
        assert (
            uow.plan_transitions.list_for_root(
                scenario.ids["root_task"], scenario.workspace_id
            )
            == ()
        )
        after_requests = {
            request.id
            for request in uow.execution_requests.list_for_root(
                scenario.ids["root_task"], scenario.workspace_id
            )
        }
        assert after_requests == before_requests
        assert (
            uow.session.scalar(
                select(EventModel.id).where(
                    EventModel.event_type == "ExecutionRequestCreatedByPlanTransition"
                )
            )
            == before_request_events
        )


def _confirm_command_input_revision(
    scenario: ResultInputScenario,
    *,
    hard_dependency: tuple[str, str] | None = None,
    changed_worker_key: str = "a",
):
    with scenario.uow_factory() as uow:
        root_task = uow.tasks.get(
            scenario.ids["root_task"], scenario.workspace_id, for_update=True
        )
        head = uow.plans.get_head(
            scenario.ids["root_task"], scenario.workspace_id, for_update=True
        )
        original_request = uow.planning_requests.get(
            scenario.ids["planning_request"], scenario.workspace_id
        )
        original_proposal = uow.planning_records.get_current_proposal(
            scenario.ids["planning_request"], scenario.workspace_id
        )
        assert root_task is not None
        assert head is not None
        base_snapshot = uow.plans.get(head.active_snapshot_id, scenario.workspace_id)
        assert base_snapshot is not None
        assert original_request is not None
        assert original_proposal is not None
        expected_root_version = root_task.version
        expected_profile_id = original_request.profile_snapshot.profile_id
        expected_profile_version = original_request.profile_snapshot.profile_version
        raw = deepcopy(original_proposal.payload.to_mapping())

    raw["kind"] = PlanningRequestKind.REVISION.value
    raw["base_descriptor"] = {
        "schema_version": 1,
        "workspace_id": str(scenario.workspace_id),
        "root_task_id": str(base_snapshot.root_task_id),
        "plan_snapshot_id": str(base_snapshot.id),
        "plan_generation": base_snapshot.generation,
        "plan_content_hash": plan_snapshot_content_hash(base_snapshot),
        "root_task_version": expected_root_version,
        "captured_at": scenario.now.isoformat(),
    }
    raw["subtask_identity_decisions"] = [
        {
            "target_subtask_key": item.key,
            "strategy": "reuse",
            "existing_subtask_task_id": str(item.task_id),
            "derived_from_subtask_task_ids": [],
            "reason": "reuse stable Task identity",
        }
        for item in base_snapshot.subtasks
    ]
    raw["worker_item_identity_decisions"] = [
        {
            "target_worker_item_key": item.key,
            "strategy": "reuse",
            "existing_worker_item_id": str(item.worker_item_id),
            "derived_from_worker_item_ids": [],
            "target_subtask_key": item.subtask_key,
            "context_disposition": (
                ContextDisposition.PATCH_REQUIRED.value
                if item.key == changed_worker_key
                else ContextDisposition.UNCHANGED.value
            ),
            "change_reasons": (
                ["command input group changed"]
                if item.key == changed_worker_key
                else []
            ),
        }
        for item in base_snapshot.worker_item_plans
    ]
    raw["retired_subtask_ids"] = []
    raw["retired_worker_item_ids"] = []
    if hard_dependency is not None:
        upstream_worker_item_key, downstream_worker_item_key = hard_dependency
        raw["worker_item_dependencies"] = [
            {
                "upstream_worker_item_key": upstream_worker_item_key,
                "downstream_worker_item_key": downstream_worker_item_key,
                "kind": "dependency",
                "required_artifact_refs": ["artifact_type:build"],
            }
        ]
    impact_policy = raw["impact_policy"]
    assert isinstance(impact_policy, dict)
    impact_policy["revision_operation"] = "revise"
    policy = raw["collaboration_policy_request"]
    assert isinstance(policy, dict)
    input_groups = policy["input_groups"]
    assert isinstance(input_groups, list)
    input_groups.append(
        {
            "group_key": "command-inputs",
            "title": "Command prerequisites",
            "consumer_worker_item_key": changed_worker_key,
            "candidates": [
                {
                    "worker_item_key": "c" if changed_worker_key == "a" else "a",
                    "priority": 1,
                }
            ],
            "requirements": {
                "execution_status": "succeeded",
                "required_artifact_refs": ["artifact_type:build"],
                "required_evidence_refs": ["acceptance:build"],
                "evidence_status": "candidate",
            },
            "mode": "at_least",
            "minimum_count": 1,
            "selection_strategy": "plan_order",
        }
    )
    payload = PlanningRevisionProposalPayloadV5.from_mapping(raw)

    submit = CommandService(
        scenario.uow_factory,
        (
            PlanningCommandHandler(
                local_actor_type="local",
                local_actor_id="operator",
                clock=scenario.clock,
                result_inputs_enabled=True,
            ),
        ),
    ).handle(
        CommandEnvelope(
            id=uuid4(),
            workspace_id=scenario.workspace_id,
            command_type="SubmitPlanRevision",
            target_type="task",
            actor_type="local",
            actor_id="operator",
            target_id=scenario.ids["root_task"],
            payload={
                "root_task_id": str(scenario.ids["root_task"]),
                "expected_base_snapshot_id": str(base_snapshot.id),
                "expected_base_generation": base_snapshot.generation,
                "expected_base_content_hash": plan_snapshot_content_hash(base_snapshot),
                "objective": "Revise the command input contract",
                "constraints": ["Preserve successful test output"],
                "acceptance_criteria": ["Keep integration input gates authoritative"],
                "planning_profile_id": str(expected_profile_id),
                "planning_profile_version": expected_profile_version,
            },
            expected_version=expected_root_version,
            idempotency_key=f"preserved-pending-submit:{scenario.ids['root_task']}",
            correlation_id=scenario.ids["root_task"],
        )
    )
    revision_request_id = submit.aggregate_id

    with scenario.uow_factory() as uow:
        revision_request = uow.planning_requests.get(
            revision_request_id, scenario.workspace_id, for_update=True
        )
        assert revision_request is not None
        run = revision_request.runs[0]
        previous_request_version = revision_request.version
        previous_run_version = run.version
        lease_id = uuid4()
        revision_request.start_planning(
            run_id=run.id,
            lease_id=lease_id,
            fencing_token=1,
            now=scenario.now,
        )
        revision_request.succeed_run(
            run_id=run.id,
            lease_id=lease_id,
            fencing_token=1,
            provider_ref="fake:preserved-pending-revision",
            structured_result_type="planning.proposal.v5",
            usage=run.usage,
            now=scenario.now,
        )
        resolved_policy = resolve_collaboration_policy_v2(
            payload.collaboration_policy_request,
            revision_request.profile_snapshot.collaboration_runtime_binding,
            subtask_keys=frozenset(item.key for item in payload.subtasks),
        )
        diff = RevisionDiffNormalizer().normalize(
            base_snapshot,
            payload,
            collaboration_policy=resolved_policy,
        )
        proposal = ProposalRevision.create(
            planning_request_id=revision_request.id,
            revision=1,
            brief_revision=1,
            planning_run_id=run.id,
            kind=revision_request.kind,
            payload=payload,
            profile_descriptor=PlanningProfileDescriptor.from_snapshot(
                revision_request.profile_snapshot
            ),
            profile_snapshot=revision_request.profile_snapshot,
            created_at=scenario.now,
            derived_diff=diff,
        )
        revision_request.mark_proposal_ready(
            run_id=run.id,
            proposal_revision=proposal.revision,
            content_hash=proposal.content_hash,
            now=scenario.now,
        )
        uow.planning_records.add_proposal(proposal, scenario.workspace_id)
        uow.planning_runs.save(run, previous_run_version)
        uow.planning_requests.save(revision_request, previous_request_version)
        expected_request_version = revision_request.version

    return CommandService(
        scenario.uow_factory,
        (
            PlanningCommandHandler(
                local_actor_type="local",
                local_actor_id="operator",
                clock=scenario.clock,
                result_inputs_enabled=True,
            ),
        ),
    ).handle(
        CommandEnvelope(
            id=uuid4(),
            workspace_id=scenario.workspace_id,
            command_type="ConfirmPlanRevision",
            target_type="planning_request",
            actor_type="local",
            actor_id="operator",
            target_id=revision_request_id,
            payload={
                "expected_proposal_revision": proposal.revision,
                "expected_proposal_content_hash": proposal.content_hash,
                "expected_diff_hash": diff.diff_hash,
                "expected_base_snapshot_id": str(base_snapshot.id),
                "expected_base_content_hash": plan_snapshot_content_hash(base_snapshot),
                "profile_id": str(expected_profile_id),
                "profile_version": expected_profile_version,
            },
            expected_version=expected_request_version,
            idempotency_key=f"preserved-pending-confirm:{revision_request_id}",
            correlation_id=scenario.ids["root_task"],
        )
    )
