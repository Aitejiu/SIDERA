from __future__ import annotations

from collections.abc import Callable
from contextlib import contextmanager
from dataclasses import dataclass, replace
from datetime import timedelta
from types import SimpleNamespace
from uuid import uuid4

import pytest
from sqlalchemy.dialects import postgresql

from sidera_control_plane.application.activation_reconciler import ActivationReconciler
from sidera_control_plane.application.ports import (
    ControlPlaneUnitOfWork,
    WorkerItemPlacement,
)
from sidera_control_plane.domain.execution_requests import (
    ExecutionRequest,
    ExecutionRequestStatus,
)
from sidera_control_plane.domain.plan_revisions import (
    RootTaskPlanHead,
    plan_snapshot_content_hash,
)
from sidera_control_plane.domain.planning import PlanningRequest
from sidera_control_plane.domain.planning_proposals import ProposalRevision
from sidera_control_plane.domain.plans import TaskPlanSnapshot
from sidera_control_plane.domain.task import Task
from sidera_control_plane.domain.work_context import (
    WorkContext,
    WorkContextRevision,
    WorkContextRevisionReason,
    WorkContextSnapshotV1,
)
from sidera_control_plane.domain.worker_item import WorkerItem
from sidera_control_plane.persistence.execution_request_repository import (
    ExecutionRequestRepository,
    _request_model,
)

from .test_native_planner_confirmation import NOW, confirmation_case


@dataclass
class ActivationCase:
    request: ExecutionRequest
    planning: PlanningRequest
    proposal: ProposalRevision
    plan: TaskPlanSnapshot
    head: RootTaskPlanHead
    root: Task
    task: Task
    worker: WorkerItem
    placement: WorkerItemPlacement
    context: WorkContext
    revision: WorkContextRevision


def activation_case(*, ready: bool = False) -> ActivationCase:
    confirmed = confirmation_case("initial")
    planning, proposal = confirmed.request, confirmed.proposal
    planning.materialize(
        proposal_revision=proposal.revision,
        content_hash=proposal.content_hash,
        now=NOW,
    )
    workspace = planning.workspace_id
    root, task = (
        Task.create(
            task_id=uuid4(),
            workspace_id=workspace,
            title=title,
            description=title,
            acceptance_criteria=("Original work preserved",),
            priority=50,
            requested_by="operator",
        )
        for title in ("Root", "Original subtask")
    )
    worker = WorkerItem.create(
        item_id=uuid4(),
        workspace_id=workspace,
        root_task_id=root.id,
        title="Original worker",
        responsibility="Implement original subtask",
        now=NOW,
    )
    plan = TaskPlanSnapshot.create(
        snapshot_id=uuid4(),
        schema_version=1,
        workspace_id=workspace,
        root_task_id=root.id,
        generation=1,
        parent_snapshot_id=None,
        source_type="test",
        source_ref="test:activation-locking",
        title="Original plan",
        goal_summary="Original work",
        acceptance_refs=("done",),
        published_by="operator",
        published_at=NOW,
    )
    request = ExecutionRequest.create(
        request_id=uuid4(),
        workspace_id=workspace,
        planning_request_id=planning.id,
        proposal_revision_id=proposal.id,
        plan_snapshot_id=plan.id,
        plan_generation=1,
        root_task_id=root.id,
        subtask_id=task.id,
        worker_item_id=worker.id,
        worker_item_key="worker",
        intent_key="original",
        purpose="implement",
        capability_requirements=("code",),
        adapter_preferences=("controlled",),
        budget_class="standard",
        not_before=NOW,
        max_attempts=1,
        expected_artifact_refs=("artifact_type:diff",),
        expected_evidence_refs=("acceptance:done",),
        now=NOW,
    )
    if ready:
        request.mark_ready(now=NOW)
    revision = WorkContextRevision.create(
        revision_id=uuid4(),
        work_context_id=uuid4(),
        workspace_id=workspace,
        revision_number=1,
        brief_planning_request_id=planning.id,
        brief_revision=1,
        proposal_revision_id=proposal.id,
        plan_snapshot_id=plan.id,
        reason=WorkContextRevisionReason.REQUEST_CREATED,
        snapshot=WorkContextSnapshotV1.create(
            objective="Original work",
            constraints=(),
            acceptance_criteria=(),
            requested_outputs=(),
            clarification_decisions=(),
            accepted_decisions=(),
            unresolved_questions=(),
            knowledge_refs=(),
            artifact_refs=(),
            evidence_refs=(),
            checkpoint_refs=(),
        ),
        created_by="operator",
        created_at=NOW,
    )
    return ActivationCase(
        request,
        planning,
        proposal,
        plan,
        RootTaskPlanHead(
            workspace, root.id, plan.id, 1, plan_snapshot_content_hash(plan), 1, NOW
        ),
        root,
        task,
        worker,
        WorkerItemPlacement(
            plan.id,
            1,
            workspace,
            root.id,
            worker.id,
            "worker",
            "subtask",
            task.id,
            "implement",
            ("code",),
            None,
            None,
            NOW,
        ),
        WorkContext.create(
            work_context_id=revision.work_context_id,
            workspace_id=workspace,
            planning_request_id=planning.id,
            first_revision=revision,
            created_at=NOW,
        ),
        revision,
    )


class RecordingStore:
    def __init__(self, uow, name, rows):
        self.uow, self.name, self.rows = uow, name, rows

    def get(self, row_id, workspace_id, *, for_update=False):
        self.uow.record(self.name, row_id, workspace_id, for_update)
        row = self.rows().get(row_id)
        return row if row is not None and row.workspace_id == workspace_id else None

    def save(self, row, previous_version):
        assert row.version == previous_version + 1
        self.uow.record("write", row.id, row.workspace_id, False)
        self.uow.db.writes.append((self.name, row.id, previous_version))


class RecordingRequests(RecordingStore):
    def __init__(self, uow):
        super().__init__(uow, "request", lambda: uow.db.requests)

    def list_activation_candidates(self, *, status, now, limit):
        self.uow.discovery_status = status
        self.uow.record("discover", status, now, False)
        override = self.uow.db.discovery_override.get(status)
        rows = override if override is not None else tuple(self.rows().values())
        return tuple(
            replace(row)
            for row in rows
            if row.status is status and row.not_before <= now
        )[:limit]

    def claim_blocked(self, *, now, limit):
        return self._claim(ExecutionRequestStatus.BLOCKED, now, limit)

    def claim_ready(self, *, now, limit):
        return self._claim(ExecutionRequestStatus.READY, now, limit)

    def _claim(self, status, now, limit):
        self.uow.record("claim", status, now, True)
        return tuple(
            row
            for row in self.rows().values()
            if row.status is status and row.not_before <= now
        )[:limit]

    def save(self, row, previous_version):
        super().save(row, previous_version)
        if row.id in self.uow.db.fail_save:
            raise RuntimeError("controlled candidate save failure")

    def list_dependencies(self, *_args):
        return ()


class RecordingUow(ControlPlaneUnitOfWork):
    def __init__(self, db):
        self.db, self.index = db, len(db.uows)
        self.discovery_status = None
        db.uows.append(self)
        self.execution_requests = RecordingRequests(self)
        self.tasks = RecordingStore(self, "subtask", lambda: db.tasks)
        self.worker_items = RecordingStore(self, "worker_item", lambda: db.workers)
        self.worker_items.get_current_placement = lambda row_id, _workspace: (
            db.placements.get(row_id)
        )
        self.plans = SimpleNamespace(
            get_head=self.get_head,
            get_current_for_root=lambda root, _workspace: db.plans.get(root),
        )
        self.planning_requests = RecordingStore(self, "planning", lambda: db.planning)
        self.planning_records = SimpleNamespace(
            get_current_proposal=lambda request_id, _workspace: db.proposals.get(
                request_id
            )
        )
        self.results = SimpleNamespace(get_head=lambda *_args: None)
        self.work_contexts = SimpleNamespace(
            get_by_planning_request=lambda request_id, _workspace: db.contexts.get(
                request_id
            ),
            get_by_root_task=lambda *_args: None,
            get_revision=lambda revision_id, _workspace: db.revisions.get(revision_id),
        )
        self.executions = SimpleNamespace(add=lambda value: db.executions.append(value))
        self.context_assemblies = SimpleNamespace(
            add=lambda value: db.assemblies.append(value)
        )
        self.events = SimpleNamespace(
            append_with_outbox=lambda value: db.events.append(value)
        )
        self.commands = SimpleNamespace(
            record_applied=lambda *values: db.commands.append(values)
        )

    def record(self, name, row_id, workspace_id, for_update):
        self.db.calls.append((self.index, name, row_id, workspace_id, for_update))

    def get_head(self, root_id, workspace_id, *, for_update=False):
        self.record("plan_head", root_id, workspace_id, for_update)
        return self.db.heads.get(root_id)

    def __enter__(self):
        self.before = self.db.snapshot()
        self.record("enter", None, None, False)
        return self

    def __exit__(self, exc_type, exc, traceback):
        locked_ids = {
            call[2]
            for call in self.db.calls
            if call[0] == self.index and call[1] == "request" and call[4]
        }
        commit_failure = bool(locked_ids & self.db.fail_commit)
        if exc_type is not None or commit_failure:
            self.db.restore(self.before)
            self.record("rollback", None, None, False)
        else:
            self.record("commit", None, None, False)
        if commit_failure:
            raise RuntimeError("controlled commit failure")
        if self.discovery_status is not None:
            self.db.after_discovery(self.discovery_status)

    @contextmanager
    def savepoint(self):
        before = self.db.snapshot()
        try:
            yield
        except Exception:
            self.db.restore(before)
            raise

    def flush(self):
        return None


class RecordingDatabase:
    def __init__(self, *cases):
        self.requests = {case.request.id: replace(case.request) for case in cases}
        self.tasks = {
            task.id: replace(task) for case in cases for task in (case.root, case.task)
        }
        self.workers = {case.worker.id: case.worker for case in cases}
        self.placements = {case.worker.id: case.placement for case in cases}
        self.plans = {case.root.id: case.plan for case in cases}
        self.heads = {case.root.id: case.head for case in cases}
        self.planning = {case.planning.id: case.planning for case in cases}
        self.proposals = {case.planning.id: case.proposal for case in cases}
        self.contexts = {case.planning.id: case.context for case in cases}
        self.revisions = {case.revision.revision_id: case.revision for case in cases}
        self.executions, self.assemblies, self.events, self.commands, self.writes = (
            [],
            [],
            [],
            [],
            [],
        )
        self.calls, self.uows = [], []
        self.fail_save, self.fail_commit = set(), set()
        self.discovery_override = {}
        self.after_discovery: Callable[[ExecutionRequestStatus], None] = (
            lambda _status: None
        )

    def snapshot(self):
        return (
            {key: replace(value) for key, value in self.requests.items()},
            {key: replace(value) for key, value in self.tasks.items()},
            *(
                list(getattr(self, name))
                for name in ("executions", "assemblies", "events", "commands", "writes")
            ),
        )

    def restore(self, before):
        (
            self.requests,
            self.tasks,
            self.executions,
            self.assemblies,
            self.events,
            self.commands,
            self.writes,
        ) = before

    def reconciler(self, owner_id="activation-reconciler"):
        return ActivationReconciler(
            uow_factory=lambda: RecordingUow(self), clock=lambda: NOW, owner_id=owner_id
        )


def assert_lock_order(db, case):
    locks = [call for call in db.calls if call[4]]
    assert locks
    expected = (
        ("plan_head", case.root.id),
        ("subtask", case.task.id),
        ("worker_item", case.worker.id),
        ("request", case.request.id),
    )
    for index in {call[0] for call in locks}:
        first = [call for call in locks if call[0] == index][:4]
        assert [(call[1], call[2]) for call in first] == list(expected)
        assert all(call[3] == case.request.workspace_id for call in first)
        first_position = db.calls.index(first[0])
        reader = next(
            call
            for call in reversed(db.calls[:first_position])
            if call[1] == "discover"
        )
        reader_commit = next(
            position
            for position, call in enumerate(db.calls)
            if call[0] == reader[0] and call[1] == "commit"
        )
        writer_enter = next(
            position
            for position, call in enumerate(db.calls)
            if call[0] == index and call[1] == "enter"
        )
        assert reader_commit < writer_enter < first_position
    assert not any(call[1] == "claim" for call in db.calls)


def test_actual_activation_discovers_then_locks_parents_before_request_and_commits():
    case = activation_case()
    db = RecordingDatabase(case)
    result = db.reconciler().run_once(limit=1)
    assert_lock_order(db, case)
    assert (result.examined, result.readied, result.materialized, result.failed) == (
        1,
        1,
        1,
        0,
    )
    assert result.execution_ids == (db.executions[0].id,)
    assert len(db.assemblies) == 1
    assert db.requests[case.request.id].status is ExecutionRequestStatus.MATERIALIZED
    assert [call[1] for call in db.calls if call[1] in {"enter", "commit"}] == [
        "enter",
        "commit",
        "enter",
        "commit",
        "enter",
        "commit",
        "enter",
        "commit",
    ]


IMMUTABLE_FIELDS = (
    "workspace_id",
    "id",
    "planning_request_id",
    "proposal_revision_id",
    "plan_snapshot_id",
    "plan_generation",
    "root_task_id",
    "subtask_id",
    "worker_item_id",
)


@pytest.mark.parametrize("change", ("missing", "status", "future", *IMMUTABLE_FIELDS))
def test_candidate_revalidates_locked_identity_and_eligibility_without_writes(change):
    case = activation_case(ready=True)
    db = RecordingDatabase(case)

    def change_after_discovery(status):
        if status is not ExecutionRequestStatus.READY:
            return
        current = db.requests[case.request.id]
        if change == "missing":
            del db.requests[case.request.id]
        elif change == "status":
            current.status = ExecutionRequestStatus.CANCELLED
        elif change == "future":
            current.not_before = NOW + timedelta(seconds=1)
        else:
            setattr(current, change, 2 if change == "plan_generation" else uuid4())

    db.after_discovery = change_after_discovery
    result = db.reconciler().run_once(limit=1)
    assert (result.examined, result.deferred, result.materialized, result.failed) == (
        1,
        1,
        0,
        0,
    )
    assert db.writes == db.events == db.commands == db.executions == db.assemblies == []
    assert_lock_order(db, case)


def test_current_locked_version_can_advance_without_changing_candidate_identity():
    case = activation_case(ready=True)
    db = RecordingDatabase(case)

    def advance(status):
        if status is ExecutionRequestStatus.READY:
            db.requests[case.request.id].version += 1

    db.after_discovery = advance
    result = db.reconciler().run_once(1)
    assert result.materialized == 1 and result.failed == 0
    assert ("request", case.request.id, case.request.version + 1) in db.writes


def test_two_owners_with_same_discovery_materialize_only_one_original():
    case = activation_case(ready=True)
    db = RecordingDatabase(case)
    db.discovery_override[ExecutionRequestStatus.READY] = (replace(case.request),)
    first = db.reconciler("activation-owner-a").run_once(1)
    second = db.reconciler("activation-owner-b").run_once(1)
    assert (first.materialized, second.materialized, second.deferred) == (1, 0, 1)
    assert len(db.executions) == len(db.assemblies) == 1
    assert second.execution_ids == ()


@pytest.mark.parametrize("failure", ("save", "commit"))
def test_failed_candidate_rolls_back_and_independent_candidate_commits(failure):
    failed, successful = activation_case(ready=True), activation_case(ready=True)
    db = RecordingDatabase(failed, successful)
    getattr(db, f"fail_{failure}").add(failed.request.id)
    result = db.reconciler().run_once(2)
    assert (result.examined, result.materialized, result.failed) == (2, 1, 1)
    assert db.requests[failed.request.id].status is ExecutionRequestStatus.READY
    assert (
        db.requests[successful.request.id].status is ExecutionRequestStatus.MATERIALIZED
    )
    assert result.execution_ids == (db.executions[0].id,)
    assert len(db.executions) == len(db.assemblies) == 1
    assert all(write[1] != failed.request.id for write in db.writes)


@pytest.mark.parametrize("outcome", ("readied", "deferred"))
def test_failed_blocked_commit_does_not_publish_success_counters(outcome):
    case = activation_case()
    db = RecordingDatabase(case)
    if outcome == "deferred":
        del db.planning[case.request.planning_request_id]
    db.fail_commit.add(case.request.id)
    result = db.reconciler().run_once(1)
    assert (
        result.examined,
        result.readied,
        result.materialized,
        result.deferred,
        result.failed,
    ) == (1, 0, 0, 0, 1)
    assert db.requests[case.request.id].status is ExecutionRequestStatus.BLOCKED
    assert db.writes == db.events == db.commands == []


@pytest.mark.parametrize("missing", ("subtask", "worker_item"))
def test_missing_parent_uses_existing_obsolete_policy_after_ordered_request_lock(
    missing,
):
    case = activation_case(ready=True)
    db = RecordingDatabase(case)
    if missing == "subtask":
        del db.tasks[case.task.id]
    else:
        del db.workers[case.worker.id]
    result = db.reconciler().run_once(1)
    assert_lock_order(db, case)
    assert (result.examined, result.deferred, result.materialized, result.failed) == (
        1,
        1,
        0,
        0,
    )
    assert db.requests[case.request.id].status is ExecutionRequestStatus.OBSOLETE
    request_lock = next(
        index for index, call in enumerate(db.calls) if call[1] == "request" and call[4]
    )
    first_write = next(
        index for index, call in enumerate(db.calls) if call[1] == "write"
    )
    assert request_lock < first_write
    assert len(db.commands) == len(db.events) == 1
    assert db.executions == db.assemblies == []


class RecordingSqlSession:
    def __init__(self, row):
        self.row, self.statements = row, []

    def scalars(self, statement):
        self.statements.append(statement)
        return SimpleNamespace(all=lambda: [self.row])


@pytest.mark.parametrize(
    "status", (ExecutionRequestStatus.BLOCKED, ExecutionRequestStatus.READY)
)
def test_repository_discovery_has_real_codec_due_order_limit_and_no_row_locks(status):
    case = activation_case(ready=status is ExecutionRequestStatus.READY)
    row = _request_model(case.request)
    row.created_at = row.updated_at = NOW
    session = RecordingSqlSession(row)
    repository = ExecutionRequestRepository(session)
    result = repository.list_activation_candidates(status=status, now=NOW, limit=7)
    assert result == (case.request,) and result[0] is not case.request
    compiled = session.statements[0].compile(dialect=postgresql.dialect())
    sql = str(compiled)
    assert "FOR UPDATE" not in sql
    assert (
        "execution_requests.status =" in sql
        and "execution_requests.not_before <=" in sql
    )
    assert (
        "ORDER BY execution_requests.not_before ASC, execution_requests.created_at ASC, execution_requests.id ASC"
        in sql
    )
    assert compiled.params == {
        "status_1": status.value,
        "not_before_1": NOW,
        "param_1": 7,
    }
    repository.claim_ready(now=NOW, limit=7)
    repository.claim_blocked(now=NOW, limit=7)
    assert all(
        "FOR UPDATE SKIP LOCKED" in str(statement.compile(dialect=postgresql.dialect()))
        for statement in session.statements[1:]
    )


@pytest.mark.parametrize(
    "status,limit",
    ((ExecutionRequestStatus.MATERIALIZED, 1), (ExecutionRequestStatus.BLOCKED, 0)),
)
def test_repository_rejects_invalid_discovery_without_query(status, limit):
    session = RecordingSqlSession(None)
    with pytest.raises(ValueError):
        ExecutionRequestRepository(session).list_activation_candidates(
            status=status, now=NOW, limit=limit
        )
    assert session.statements == []
