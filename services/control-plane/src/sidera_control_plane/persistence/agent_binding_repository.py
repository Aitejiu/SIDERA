from uuid import UUID

from sidera_agents.contracts import ProfileRef
from sqlalchemy import or_, select, update
from sqlalchemy.orm import Session

from sidera_control_plane.domain.agent_bindings import AgentBinding, AssigneeRef
from sidera_control_plane.domain.errors import VersionConflict
from sidera_control_plane.persistence.models import (
    AgentBindingModel,
    AgentExecutionLinkModel,
    AssignmentLeaseModel,
    ExecutionModel,
    ExecutionSessionModel,
)


class AgentBindingRepository:
    def __init__(self, session: Session):
        self._session = session

    def get(self, workspace_id, target_type, target_id, *, for_update=False):
        statement = select(AgentBindingModel).where(
            AgentBindingModel.workspace_id == workspace_id,
            AgentBindingModel.target_type == target_type,
            AgentBindingModel.target_id == target_id,
            AgentBindingModel.is_current,
        )
        if for_update:
            statement = statement.with_for_update()
        row = self._session.scalar(statement)
        return _binding(row) if row is not None else None

    def history(self, workspace_id, target_type, target_id):
        rows = self._session.scalars(
            select(AgentBindingModel)
            .where(
                AgentBindingModel.workspace_id == workspace_id,
                AgentBindingModel.target_type == target_type,
                AgentBindingModel.target_id == target_id,
            )
            .order_by(AgentBindingModel.version)
        )
        return tuple(_binding(row) for row in rows)

    def append(self, binding: AgentBinding):
        current = self.get(
            binding.workspace_id,
            binding.target_type,
            binding.target_id,
            for_update=True,
        )
        expected = 1 if current is None else current.version + 1
        if binding.version != expected:
            raise VersionConflict(binding.version - 1, expected - 1)
        self._session.execute(
            update(AgentBindingModel)
            .where(
                AgentBindingModel.workspace_id == binding.workspace_id,
                AgentBindingModel.target_type == binding.target_type,
                AgentBindingModel.target_id == binding.target_id,
                AgentBindingModel.is_current,
            )
            .values(is_current=False)
        )
        self._session.add(
            AgentBindingModel(
                workspace_id=binding.workspace_id,
                target_type=binding.target_type,
                target_id=binding.target_id,
                version=binding.version,
                is_current=True,
                assignee_kind=binding.assignee.kind,
                assignee_id=str(binding.assignee.id),
                profile_ref=None
                if binding.profile_ref is None
                else binding.profile_ref.model_dump(mode="json"),
                runnable=binding.runnable,
                not_runnable_reasons=list(binding.not_runnable_reasons),
                created_at=binding.created_at,
                changed_at=binding.changed_at,
            )
        )

    def execution_facts(self, workspace_id: UUID, target_type: str, target_id: UUID):
        target = (
            ExecutionModel.worker_item_id == target_id
            if target_type == "worker_item"
            else (ExecutionModel.task_id == target_id)
            & ExecutionModel.worker_item_id.is_(None)
        )
        executions = tuple(
            self._session.scalars(
                select(ExecutionModel)
                .where(
                    ExecutionModel.workspace_id == workspace_id,
                    target,
                )
                .with_for_update()
            )
        )
        ids = [row.id for row in executions]
        if not ids:
            return False, frozenset()
        # A pending start has uncertain dispatch even before the first Run receipt.
        active = any(
            row.status in {"leased", "running", "lost"}
            or row.active_lease_id is not None
            for row in executions
        )
        active = (
            active
            or self._session.scalar(
                select(AssignmentLeaseModel.id)
                .where(
                    AssignmentLeaseModel.workspace_id == workspace_id,
                    AssignmentLeaseModel.execution_id.in_(ids),
                    AssignmentLeaseModel.status == "active",
                )
                .limit(1)
            )
            is not None
        )
        active = (
            active
            or self._session.scalar(
                select(ExecutionSessionModel.id)
                .where(
                    ExecutionSessionModel.workspace_id == workspace_id,
                    ExecutionSessionModel.execution_id.in_(ids),
                    ExecutionSessionModel.status != "closed",
                )
                .limit(1)
            )
            is not None
        )
        active = (
            active
            or self._session.scalar(
                select(AgentExecutionLinkModel.execution_id)
                .where(
                    AgentExecutionLinkModel.workspace_id == workspace_id,
                    AgentExecutionLinkModel.execution_id.in_(ids),
                    or_(
                        AgentExecutionLinkModel.status.not_in(
                            {"succeeded", "failed", "stopped", "cancelled"}
                        ),
                        AgentExecutionLinkModel.status.is_(None),
                    ),
                )
                .limit(1)
            )
            is not None
        )
        requirements = frozenset(
            cap
            for row in executions
            if row.status == "queued"
            for cap in row.capability_requirements
        )
        return active, requirements


def _binding(row):
    return AgentBinding(
        workspace_id=row.workspace_id,
        target_type=row.target_type,
        target_id=row.target_id,
        assignee=AssigneeRef(row.assignee_kind, row.assignee_id),
        profile_ref=None
        if row.profile_ref is None
        else ProfileRef.model_validate(row.profile_ref),
        version=row.version,
        created_at=row.created_at,
        changed_at=row.changed_at,
        runnable=row.runnable,
        not_runnable_reasons=tuple(row.not_runnable_reasons),
    )
