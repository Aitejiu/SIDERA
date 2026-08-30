from dataclasses import replace
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest
from sqlalchemy import func, select, update

from sidera_control_plane.application.command_service import CommandService
from sidera_control_plane.application.execution_commands import (
    ExecutionInputCommandHandler,
)
from sidera_control_plane.application.handoff_gate import (
    HandoffGateOperation,
    HandoffOperationGate,
)
from sidera_control_plane.application.ports import WorkerItemPlacement
from sidera_control_plane.domain.commands import CommandEnvelope
from sidera_control_plane.domain.errors import (
    ExecutionContextChanged,
    InvalidTransition,
)
from sidera_control_plane.domain.plan_revisions import plan_snapshot_content_hash
from sidera_control_plane.domain.plans import (
    SubtaskPlanItem,
    TaskPlanSnapshot,
    WorkerItemPlanItem,
    WorkItemKind,
)
from sidera_control_plane.domain.worker_item import WorkerItem
from sidera_control_plane.persistence.database import Database
from sidera_control_plane.persistence.models import (
    AssignmentLeaseModel,
    CommandModel,
    ContextPackageModel,
    ExecutionInputModel,
    ExecutionModel,
    ExecutionSessionModel,
    OutboxMessageModel,
    PlanSnapshotModel,
    RootTaskPlanHeadModel,
    TaskModel,
    WorkContextRevisionModel,
    WorkerItemModel,
    WorkerModel,
)
from sidera_control_plane.persistence.unit_of_work import SqlAlchemyUnitOfWork

pytestmark = pytest.mark.integration

NOW = datetime(2026, 8, 20, 12, 0, tzinfo=UTC)


def _seed_session(database: Database, workspace_id: UUID) -> dict[str, UUID]:
    task_id = uuid4()
    plan_snapshot_id = uuid4()
    worker_id = uuid4()
    worker_item_id = uuid4()
    execution_id = uuid4()
    lease_id = uuid4()
    session_id = uuid4()
    with database.session() as db_session:
        snapshot = TaskPlanSnapshot.create(
            snapshot_id=plan_snapshot_id,
            schema_version=2,
            workspace_id=workspace_id,
            root_task_id=task_id,
            generation=1,
            parent_snapshot_id=None,
            source_type="test",
            source_ref="integration:input",
            title="Input task",
            goal_summary="Accept direct user input",
            acceptance_refs=("input is delivered",),
            subtasks=(
                SubtaskPlanItem(
                    key="input",
                    task_id=task_id,
                    kind=WorkItemKind.CHILD,
                    title="Input task",
                    goal_summary="Accept direct user input",
                    acceptance_refs=("input is delivered",),
                    milestone_key=None,
                    expected_outputs=(),
                    source_refs=(),
                ),
            ),
            worker_item_plans=(
                WorkerItemPlanItem(
                    key="input:worker",
                    worker_item_id=worker_item_id,
                    subtask_key="input",
                    title="Input WorkerItem",
                    role="implementation",
                    responsibility="Accept direct user input",
                    capability_requirements=("python",),
                    worker_policy_ref=None,
                    configuration_ref=None,
                    concurrency_limit=1,
                    source_refs=(),
                ),
            ),
            published_by="integration",
            published_at=NOW,
        )
        db_session.add(
            TaskModel(
                id=task_id,
                workspace_id=workspace_id,
                title="Input task",
                description="Accept direct user input",
                acceptance_criteria=["input is delivered"],
                status="running",
                priority=50,
                requested_by="integration",
                version=1,
            )
        )
        db_session.flush()
        db_session.add(
            PlanSnapshotModel(
                id=snapshot.id,
                schema_version=snapshot.schema_version,
                workspace_id=snapshot.workspace_id,
                root_task_id=snapshot.root_task_id,
                generation=snapshot.generation,
                parent_snapshot_id=None,
                source_type=snapshot.source_type,
                source_ref=snapshot.source_ref,
                title=snapshot.title,
                goal_summary=snapshot.goal_summary,
                payload=snapshot.to_payload(),
                published_by=snapshot.published_by,
                published_at=snapshot.published_at,
            )
        )
        db_session.add(
            RootTaskPlanHeadModel(
                workspace_id=workspace_id,
                root_task_id=task_id,
                active_snapshot_id=snapshot.id,
                active_generation=snapshot.generation,
                active_snapshot_hash=plan_snapshot_content_hash(snapshot),
                version=1,
                updated_at=snapshot.published_at,
            )
        )
        db_session.add(
            WorkerModel(
                id=worker_id,
                workspace_id=workspace_id,
                adapter_type="fake",
                display_name="Input Worker",
                status="busy",
                desired_state="active",
                capabilities=["python"],
                capacity=1,
                current_load=1,
                cost_per_hour_microunits=1,
                startup_latency_ms=1,
                last_heartbeat_at=NOW,
                version=1,
            )
        )
        db_session.commit()
    worker_item = WorkerItem.create(
        item_id=worker_item_id,
        workspace_id=workspace_id,
        root_task_id=task_id,
        title="Input WorkerItem",
        responsibility="Accept direct user input",
        now=NOW,
    )
    placement = WorkerItemPlacement(
        plan_snapshot_id=plan_snapshot_id,
        plan_generation=1,
        workspace_id=workspace_id,
        root_task_id=task_id,
        worker_item_id=worker_item_id,
        worker_item_key="input:worker",
        subtask_key="input",
        subtask_task_id=task_id,
        role="implementation",
        capability_requirements=("python",),
        worker_policy_ref=None,
        configuration_ref=None,
        created_at=NOW,
    )
    with SqlAlchemyUnitOfWork(database) as uow:
        uow.worker_items.add(worker_item)
        uow.worker_items.add_placement(placement)
    with database.session() as db_session:
        execution = ExecutionModel(
            id=execution_id,
            workspace_id=workspace_id,
            task_id=task_id,
            worker_item_id=worker_item_id,
            lineage_id=execution_id,
            attempt=1,
            max_attempts=2,
            purpose="Receive input",
            status="running",
            capability_requirements=["python"],
            pinned_worker_id=None,
            assigned_worker_id=worker_id,
            active_lease_id=None,
            last_fencing_token=1,
            not_before=NOW,
            failure_code=None,
            started_at=NOW,
            finished_at=None,
            version=1,
        )
        db_session.add(execution)
        db_session.flush([execution])
        db_session.add(
            AssignmentLeaseModel(
                id=lease_id,
                workspace_id=workspace_id,
                execution_id=execution_id,
                worker_id=worker_id,
                status="active",
                issued_at=NOW,
                expires_at=NOW + timedelta(minutes=5),
                renewed_at=None,
                ended_at=None,
                fencing_token=1,
                assignment_reason={"source": "integration"},
                version=1,
            )
        )
        db_session.add(
            ExecutionSessionModel(
                id=session_id,
                workspace_id=workspace_id,
                execution_id=execution_id,
                worker_id=worker_id,
                adapter_session_id="input-session",
                status="active",
                last_sequence=0,
                last_activity_at=NOW,
                input_mode="queued",
                adapter_module_id=None,
                adapter_config_version=None,
                workspace_module_id=None,
                workspace_config_version=None,
                capability_snapshot=None,
                process_ref="fake://input-session",
                workspace_ref="fake://input-workspace",
                version=1,
            )
        )
        db_session.flush()
        db_session.execute(
            update(ExecutionModel)
            .where(
                ExecutionModel.id == execution_id,
                ExecutionModel.workspace_id == workspace_id,
            )
            .values(active_lease_id=lease_id)
        )
        db_session.commit()
    return {
        "task_id": task_id,
        "plan_snapshot_id": plan_snapshot_id,
        "worker_item_id": worker_item_id,
        "worker_id": worker_id,
        "execution_id": execution_id,
        "lease_id": lease_id,
        "session_id": session_id,
    }


def _envelope(
    workspace_id: UUID,
    session_id: UUID,
    execution_id: UUID,
    command_type: str,
    client_key: str,
    *,
    payload: dict[str, object] | None = None,
    context: dict[str, object] | None = None,
) -> CommandEnvelope:
    command_payload: dict[str, object] = {
        "execution_id": str(execution_id),
        "client_idempotency_key": client_key,
        **(payload or {}),
    }
    if command_type == "SendExecutionInput":
        if context is None:
            raise AssertionError("SendExecutionInput tests require an exact context")
        command_payload["context"] = context
    return CommandEnvelope(
        id=uuid4(),
        workspace_id=workspace_id,
        command_type=command_type,
        target_type="execution_session",
        actor_type="user",
        actor_id="user:integration",
        target_id=session_id,
        payload=command_payload,
        expected_version=1,
        idempotency_key=f"command:{command_type}:{client_key}:{uuid4()}",
        correlation_id=uuid4(),
    )


def _context(ids: dict[str, UUID]) -> dict[str, object]:
    return {
        "worker_item_id": str(ids["worker_item_id"]),
        "plan_generation": 1,
        "context_version": 1,
        "execution_id": str(ids["execution_id"]),
        "session_id": str(ids["session_id"]),
        "session_version": 1,
        "worker_id": str(ids["worker_id"]),
    }


def _service(database: Database) -> CommandService:
    return CommandService(
        lambda: SqlAlchemyUnitOfWork(database),
        (ExecutionInputCommandHandler(lambda: NOW),),
    )


def test_input_command_persists_queue_outbox_and_client_replay(
    database: Database,
    workspace_id: UUID,
) -> None:
    ids = _seed_session(database, workspace_id)
    service = _service(database)
    with database.session() as db_session:
        before_context_hashes = tuple(
            db_session.scalars(select(WorkContextRevisionModel.content_hash)).all()
        )
        before_package_count = (
            db_session.scalar(select(func.count()).select_from(ContextPackageModel))
            or 0
        )
    envelope = _envelope(
        workspace_id,
        ids["session_id"],
        ids["execution_id"],
        "SendExecutionInput",
        "client-1",
        payload={"text": "  keep this text  ", "attachment_refs": []},
        context=_context(ids),
    )

    first = service.handle(envelope)
    replay = service.handle(replace(envelope, correlation_id=uuid4()))

    assert replay == first
    with database.session() as db_session:
        item = db_session.query(ExecutionInputModel).one()
        assert item.status == "queued"
        assert item.text == "  keep this text  "
        assert item.source_input_id is None
        assert item.handoff_transition_id is None
        assert (
            db_session.query(OutboxMessageModel)
            .filter(
                OutboxMessageModel.topic == "execution_input.ExecutionInputAccepted"
            )
            .count()
            == 1
        )
        assert db_session.query(CommandModel).count() == 1
        assert (
            tuple(
                db_session.scalars(select(WorkContextRevisionModel.content_hash)).all()
            )
            == before_context_hashes
        )
        assert (
            db_session.scalar(select(func.count()).select_from(ContextPackageModel))
            or 0
        ) == before_package_count


def test_input_gate_uses_the_authoritative_handoff_repository(
    database: Database,
    workspace_id: UUID,
) -> None:
    worker_item_id = uuid4()
    with SqlAlchemyUnitOfWork(database) as uow:
        result = HandoffOperationGate(uow).evaluate(
            worker_item_id=worker_item_id,
            workspace_id=workspace_id,
            operation=HandoffGateOperation.USER_INPUT,
        )

    assert result.allowed is True


def test_input_commands_enforce_one_pending_and_allow_update_cancel(
    database: Database,
    workspace_id: UUID,
) -> None:
    ids = _seed_session(database, workspace_id)
    service = _service(database)
    first = _envelope(
        workspace_id,
        ids["session_id"],
        ids["execution_id"],
        "SendExecutionInput",
        "client-1",
        payload={"text": "first", "attachment_refs": []},
        context=_context(ids),
    )
    service.handle(first)

    with pytest.raises(InvalidTransition):
        service.handle(
            _envelope(
                workspace_id,
                ids["session_id"],
                ids["execution_id"],
                "SendExecutionInput",
                "client-2",
                payload={"text": "second", "attachment_refs": []},
                context=_context(ids),
            )
        )

    updated = service.handle(
        _envelope(
            workspace_id,
            ids["session_id"],
            ids["execution_id"],
            "UpdatePendingExecutionInput",
            "client-1",
            payload={"text": "updated"},
        )
    )
    assert updated.aggregate_version == 3
    cancelled = service.handle(
        _envelope(
            workspace_id,
            ids["session_id"],
            ids["execution_id"],
            "CancelExecutionInput",
            "client-1",
        )
    )
    assert cancelled.aggregate_version == 4
    with database.session() as db_session:
        item = db_session.query(ExecutionInputModel).one()
        assert item.status == "cancelled"
        assert item.text == "updated"


@pytest.mark.parametrize(
    "changed_field,changed_value",
    [
        ("worker_item_id", str(uuid4())),
        ("plan_generation", 2),
        ("context_version", 2),
        ("execution_id", str(uuid4())),
        ("session_id", str(uuid4())),
        ("session_version", 2),
        ("worker_id", str(uuid4())),
    ],
)
def test_send_input_revalidates_each_worker_item_context_field(
    database: Database,
    workspace_id: UUID,
    changed_field: str,
    changed_value: object,
) -> None:
    ids = _seed_session(database, workspace_id)
    service = _service(database)
    context = _context(ids)
    context[changed_field] = changed_value
    envelope = _envelope(
        workspace_id,
        ids["session_id"],
        ids["execution_id"],
        "SendExecutionInput",
        f"changed-{changed_field}",
        payload={"text": "fresh context", "attachment_refs": []},
        context=context,
    )

    with pytest.raises(ExecutionContextChanged) as error:
        service.handle(envelope)

    assert changed_field in error.value.changed_fields
    current = error.value.details["current"]
    assert isinstance(current, dict)
    assert current[changed_field] != changed_value


@pytest.mark.parametrize("status", ["paused", "retired"])
def test_send_input_rejects_non_active_worker_item(
    database: Database,
    workspace_id: UUID,
    status: str,
) -> None:
    ids = _seed_session(database, workspace_id)
    with database.session() as session:
        session.execute(
            update(WorkerItemModel)
            .where(WorkerItemModel.id == ids["worker_item_id"])
            .values(status=status)
        )
        session.commit()

    with pytest.raises(InvalidTransition, match="not active"):
        _service(database).handle(
            _envelope(
                workspace_id,
                ids["session_id"],
                ids["execution_id"],
                "SendExecutionInput",
                f"status-{status}",
                payload={"text": "blocked", "attachment_refs": []},
                context=_context(ids),
            )
        )


def test_send_input_rejects_unacknowledged_worker_item_context(
    database: Database,
    workspace_id: UUID,
) -> None:
    ids = _seed_session(database, workspace_id)
    with database.session() as session:
        session.execute(
            update(WorkerItemModel)
            .where(WorkerItemModel.id == ids["worker_item_id"])
            .values(context_version=2, acknowledged_context_version=1)
        )
        session.commit()
    context = _context(ids)
    context["context_version"] = 2

    with pytest.raises(InvalidTransition, match="not acknowledged"):
        _service(database).handle(
            _envelope(
                workspace_id,
                ids["session_id"],
                ids["execution_id"],
                "SendExecutionInput",
                "unacknowledged-context",
                payload={"text": "blocked", "attachment_refs": []},
                context=context,
            )
        )


def test_send_input_rejects_non_active_session(
    database: Database,
    workspace_id: UUID,
) -> None:
    ids = _seed_session(database, workspace_id)
    with database.session() as session:
        session.execute(
            update(ExecutionSessionModel)
            .where(ExecutionSessionModel.id == ids["session_id"])
            .values(status="closed")
        )
        session.commit()

    with pytest.raises(InvalidTransition, match="active Session"):
        _service(database).handle(
            _envelope(
                workspace_id,
                ids["session_id"],
                ids["execution_id"],
                "SendExecutionInput",
                "closed-session",
                payload={"text": "blocked", "attachment_refs": []},
                context=_context(ids),
            )
        )


def test_superseded_input_is_retained_but_leaves_pending_slot(
    database: Database,
    workspace_id: UUID,
) -> None:
    ids = _seed_session(database, workspace_id)
    _service(database).handle(
        _envelope(
            workspace_id,
            ids["session_id"],
            ids["execution_id"],
            "SendExecutionInput",
            "superseded-input",
            payload={"text": "keep this intervention", "attachment_refs": []},
            context=_context(ids),
        )
    )

    with database.session() as db_session:
        stored = db_session.query(ExecutionInputModel).one()
        input_id = stored.id

    with SqlAlchemyUnitOfWork(database) as uow:
        item = uow.execution_inputs.get(input_id, workspace_id, for_update=True)
        assert item is not None
        previous_version = item.version
        item.supersede(
            now=NOW + timedelta(seconds=1),
            reason="plan_generation_changed",
        )
        uow.execution_inputs.save(item, previous_version)

    with SqlAlchemyUnitOfWork(database) as uow:
        item = uow.execution_inputs.get(input_id, workspace_id)
        assert item is not None
        assert item.status.value == "superseded"
        assert item.superseded_reason == "plan_generation_changed"
        assert item.text == "keep this intervention"
        assert (
            uow.execution_inputs.get_pending_for_session(
                ids["session_id"], workspace_id
            )
            is None
        )
