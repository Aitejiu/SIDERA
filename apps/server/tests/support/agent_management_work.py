from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID, uuid4

from sidera_agents.contracts import ProfileRef
from sidera_control_plane.persistence.models import (
    AgentBindingModel,
    AgentExecutionLinkModel,
    ExecutionAuthorityModel,
    ExecutionModel,
    TaskModel,
    WorkspaceModel,
)
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session
from sqlalchemy.pool import NullPool

from .agent_management_inventory import read_private_json, write_private_json
from .product_targets import ProductTargetError, TargetGuard


class WorkPinFixtureError(RuntimeError):
    pass


@dataclass(frozen=True)
class WorkPinIds:
    workspace_id: UUID
    task_id: UUID
    execution_id: UUID
    authority_id: UUID
    start_operation_id: UUID


@dataclass(frozen=True)
class WorkPinCheckpoint:
    ids: WorkPinIds
    principal_id: str
    profile_ref: ProfileRef
    checkpoint_path: Path

    def facts(self) -> dict[str, object]:
        return {
            "workspace_id": str(self.ids.workspace_id),
            "task_id": str(self.ids.task_id),
            "execution_id": str(self.ids.execution_id),
            "authority_id": str(self.ids.authority_id),
            "start_operation_id": str(self.ids.start_operation_id),
            "principal_id": self.principal_id,
            "profile_ref": self.profile_ref.model_dump(mode="json"),
        }


def _checkpoint_path(evidence_directory: Path, workspace_id: UUID) -> Path:
    return evidence_directory / "work-pin" / f"{workspace_id}.json"


def _new_checkpoint(
    *,
    path: Path,
    workspace_id: UUID,
    principal_id: str,
    profile_ref: ProfileRef,
) -> WorkPinCheckpoint:
    record = WorkPinCheckpoint(
        ids=WorkPinIds(
            workspace_id=workspace_id,
            task_id=uuid4(),
            execution_id=uuid4(),
            authority_id=uuid4(),
            start_operation_id=uuid4(),
        ),
        principal_id=principal_id,
        profile_ref=profile_ref,
        checkpoint_path=path,
    )
    write_private_json(path, record.facts())
    return record


def _load_checkpoint(
    *,
    path: Path,
    workspace_id: UUID,
    principal_id: str,
    profile_ref: ProfileRef,
) -> WorkPinCheckpoint:
    try:
        value = read_private_json(path)
        if not isinstance(value, dict):
            raise ValueError
        if (
            value.get("workspace_id") != str(workspace_id)
            or value.get("principal_id") != principal_id
            or value.get("profile_ref") != profile_ref.model_dump(mode="json")
        ):
            raise ValueError
        ids = WorkPinIds(
            workspace_id=workspace_id,
            task_id=UUID(value["task_id"]),
            execution_id=UUID(value["execution_id"]),
            authority_id=UUID(value["authority_id"]),
            start_operation_id=UUID(value["start_operation_id"]),
        )
    except (KeyError, TypeError, ValueError, ProductTargetError):
        raise WorkPinFixtureError("work_pin_checkpoint_invalid") from None
    return WorkPinCheckpoint(ids, principal_id, profile_ref, path)


def _models(checkpoint: WorkPinCheckpoint):
    ids = checkpoint.ids
    return (
        (WorkspaceModel, ids.workspace_id),
        (TaskModel, ids.task_id),
        (AgentBindingModel, (ids.workspace_id, "subtask", ids.task_id, 1)),
        (ExecutionModel, ids.execution_id),
        (ExecutionAuthorityModel, ids.authority_id),
        (AgentExecutionLinkModel, (ids.workspace_id, ids.execution_id)),
    )


def _existing_rows(session: Session, checkpoint: WorkPinCheckpoint):
    return tuple(session.get(model, key) for model, key in _models(checkpoint))


def _assert_recorded_rows(session: Session, checkpoint: WorkPinCheckpoint) -> bool:
    rows = _existing_rows(session, checkpoint)
    present = tuple(row is not None for row in rows)
    if not any(present):
        return False
    if not all(present):
        raise WorkPinFixtureError("work_pin_checkpoint_partial")
    workspace, task, binding, execution, authority, link = rows
    profile_json = checkpoint.profile_ref.model_dump(mode="json")
    if not (
        workspace.name == "N20 owned Work pin"
        and workspace.status == "active"
        and workspace.version == 1
        and task.workspace_id == checkpoint.ids.workspace_id
        and task.title == "Unadmitted configuration pin"
        and task.description == "N20 read fixture only"
        and task.acceptance_criteria == []
        and task.status == "pending"
        and task.priority == 50
        and task.requested_by == checkpoint.principal_id
        and task.version == 1
        and binding.workspace_id == checkpoint.ids.workspace_id
        and binding.target_type == "subtask"
        and binding.target_id == checkpoint.ids.task_id
        and binding.version == 1
        and binding.is_current is True
        and binding.assignee_kind == "profile"
        and binding.assignee_id == str(checkpoint.profile_ref.profile_id)
        and binding.profile_ref == profile_json
        and binding.runnable is False
        and binding.not_runnable_reasons == ["n20_read_fixture"]
        and execution.workspace_id == checkpoint.ids.workspace_id
        and execution.task_id == checkpoint.ids.task_id
        and execution.lineage_id == checkpoint.ids.execution_id
        and execution.attempt == 1
        and execution.max_attempts == 1
        and execution.purpose == "N20 Work read fixture"
        and execution.status == "queued"
        and execution.capability_requirements == []
        and execution.adapter_preferences == []
        and execution.adapter_forbidden == []
        and execution.worker_item_id is None
        and execution.last_fencing_token == 0
        and execution.version == 1
        and execution.stop_requested is False
        and authority.workspace_id == checkpoint.ids.workspace_id
        and authority.execution_id == checkpoint.ids.execution_id
        and authority.target_type == "subtask"
        and authority.target_id == checkpoint.ids.task_id
        and authority.binding_version == 1
        and authority.policy == {"profile_ref": profile_json}
        and authority.revoked is False
        and link.workspace_id == checkpoint.ids.workspace_id
        and link.execution_id == checkpoint.ids.execution_id
        and link.start_operation_id == checkpoint.ids.start_operation_id
        and link.profile_ref == profile_json
        and link.run_id is None
        and link.event_cursor == 0
        and link.status == "pending"
        and link.authority_id == checkpoint.ids.authority_id
        and link.binding_version == 1
        and link.request_sha256 is None
        and link.start_receipt is None
        and link.preparation is None
    ):
        raise WorkPinFixtureError("work_pin_checkpoint_rows_changed")
    return True


def _insert_six_rows(
    session: Session,
    checkpoint: WorkPinCheckpoint,
) -> None:
    ids = checkpoint.ids
    profile_json = checkpoint.profile_ref.model_dump(mode="json")
    now = datetime.now(UTC)
    session.add(
        WorkspaceModel(
            id=ids.workspace_id,
            name="N20 owned Work pin",
            status="active",
            version=1,
        )
    )
    session.flush()
    session.add(
        TaskModel(
            id=ids.task_id,
            workspace_id=ids.workspace_id,
            title="Unadmitted configuration pin",
            description="N20 read fixture only",
            acceptance_criteria=[],
            status="pending",
            priority=50,
            requested_by=checkpoint.principal_id,
            version=1,
        )
    )
    session.flush()
    session.add(
        AgentBindingModel(
            workspace_id=ids.workspace_id,
            target_type="subtask",
            target_id=ids.task_id,
            version=1,
            is_current=True,
            assignee_kind="profile",
            assignee_id=str(checkpoint.profile_ref.profile_id),
            profile_ref=profile_json,
            runnable=False,
            not_runnable_reasons=["n20_read_fixture"],
            created_at=now,
            changed_at=now,
        )
    )
    session.flush()
    session.add(
        ExecutionModel(
            id=ids.execution_id,
            workspace_id=ids.workspace_id,
            task_id=ids.task_id,
            lineage_id=ids.execution_id,
            attempt=1,
            max_attempts=1,
            purpose="N20 Work read fixture",
            status="queued",
            capability_requirements=[],
            adapter_preferences=[],
            adapter_forbidden=[],
            worker_item_id=None,
            last_fencing_token=0,
            not_before=now,
            version=1,
            stop_requested=False,
        )
    )
    session.flush()
    session.add(
        ExecutionAuthorityModel(
            id=ids.authority_id,
            workspace_id=ids.workspace_id,
            execution_id=ids.execution_id,
            target_type="subtask",
            target_id=ids.task_id,
            binding_version=1,
            policy={"profile_ref": profile_json},
            revoked=False,
        )
    )
    session.flush()
    session.add(
        AgentExecutionLinkModel(
            workspace_id=ids.workspace_id,
            execution_id=ids.execution_id,
            start_operation_id=ids.start_operation_id,
            profile_ref=profile_json,
            run_id=None,
            event_cursor=0,
            status="pending",
            authority_id=ids.authority_id,
            binding_version=1,
            request_sha256=None,
            start_receipt=None,
            preparation=None,
        )
    )
    session.flush()


def write_work_pin(
    guard: TargetGuard,
    *,
    workspace_id: UUID,
    principal_id: str,
    profile_ref: ProfileRef,
    evidence_directory: Path,
) -> WorkPinCheckpoint:
    if not guard.active or guard.target.component != "cp":
        raise WorkPinFixtureError("work_pin_cp_guard_required")
    if (
        type(workspace_id) is not UUID
        or workspace_id != profile_ref.workspace_id
        or type(principal_id) is not str
        or not principal_id
    ):
        raise WorkPinFixtureError("work_pin_fixture_scope_invalid")
    path = _checkpoint_path(evidence_directory, workspace_id)
    checkpoint = (
        _load_checkpoint(
            path=path,
            workspace_id=workspace_id,
            principal_id=principal_id,
            profile_ref=profile_ref,
        )
        if path.exists()
        else _new_checkpoint(
            path=path,
            workspace_id=workspace_id,
            principal_id=principal_id,
            profile_ref=profile_ref,
        )
    )
    engine = create_engine(
        guard.target.url,
        poolclass=NullPool,
        echo=False,
        hide_parameters=True,
    )
    try:
        with Session(engine) as session, session.begin():
            if _assert_recorded_rows(session, checkpoint):
                return checkpoint
            for model, identity in _models(checkpoint):
                if session.get(model, identity) is not None:
                    raise WorkPinFixtureError("work_pin_uncheckpointed_row_exists")
            operation_row = session.scalar(
                select(AgentExecutionLinkModel.execution_id).where(
                    AgentExecutionLinkModel.workspace_id == workspace_id,
                    AgentExecutionLinkModel.start_operation_id
                    == checkpoint.ids.start_operation_id,
                )
            )
            if operation_row is not None:
                raise WorkPinFixtureError("work_pin_operation_id_exists")
            _insert_six_rows(session, checkpoint)
            if not _assert_recorded_rows(session, checkpoint):
                raise WorkPinFixtureError("work_pin_seed_missing")
        return checkpoint
    finally:
        engine.dispose()
