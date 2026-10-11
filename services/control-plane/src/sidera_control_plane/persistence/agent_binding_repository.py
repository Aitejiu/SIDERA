from uuid import UUID

from sidera_agents.contracts import ProfileRef
from sqlalchemy import or_, select, update
from sqlalchemy.orm import Session

from sidera_control_plane.domain.agent_bindings import AgentBinding, AssigneeRef
from sidera_control_plane.domain.errors import DomainError, VersionConflict
from sidera_control_plane.persistence.models import (
    AgentBindingModel,
    AgentExecutionLinkModel,
    AssignmentLeaseModel,
    ExecutionAuthorityModel,
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
        executions = self._target_executions(workspace_id, target_type, target_id)
        proven = {
            p.link.execution_id
            for p in self.retirable_intentions(workspace_id, target_type, target_id)
        }
        ids = [row.id for row in executions if row.id not in proven]
        requirements = frozenset(
            cap
            for row in executions
            if row.status == "queued" or row.id in proven
            for cap in row.capability_requirements
        )
        if not ids:
            return False, requirements
        active = any(
            row.id in ids
            and (
                row.status in {"leased", "running", "lost"}
                or row.active_lease_id is not None
            )
            for row in executions
        )
        for model, condition in (
            (AssignmentLeaseModel, AssignmentLeaseModel.status == "active"),
            (ExecutionSessionModel, ExecutionSessionModel.status != "closed"),
            (
                AgentExecutionLinkModel,
                AgentExecutionLinkModel.status.not_in(
                    {"succeeded", "failed", "stopped", "cancelled"}
                ),
            ),
        ):
            active |= (
                self._session.scalar(
                    select(model.execution_id)
                    .where(
                        model.workspace_id == workspace_id,
                        model.execution_id.in_(ids),
                        condition,
                    )
                    .limit(1)
                )
                is not None
            )
        return active, requirements

    def retirable_intentions(self, workspace_id, target_type, target_id):
        from sidera_control_plane.persistence.execution_authority_repository import (
            ExecutionAuthorityRepository,
        )

        authorities = ExecutionAuthorityRepository(self._session)
        proofs = []
        for row in self._target_executions(workspace_id, target_type, target_id):
            try:
                proof = authorities.retirement_proof(workspace_id, row.id)
            except DomainError:
                proof = None
            if proof is not None:
                authority = authorities.get(
                    proof.link.authority_id, workspace_id=workspace_id
                )
                if (authority.target_type, authority.target_id) == (
                    target_type,
                    target_id,
                ):
                    proofs.append(proof)
        return tuple(proofs)

    def _target_executions(self, workspace_id, target_type, target_id):
        target = (
            ExecutionModel.worker_item_id == target_id
            if target_type == "worker_item"
            else (ExecutionModel.task_id == target_id)
            & ExecutionModel.worker_item_id.is_(None)
        )
        # Inherited defaults affect only intentions actually pinned to this target.
        target = or_(
            target,
            ExecutionModel.id.in_(
                select(ExecutionAuthorityModel.execution_id).where(
                    ExecutionAuthorityModel.workspace_id == workspace_id,
                    ExecutionAuthorityModel.target_type == target_type,
                    ExecutionAuthorityModel.target_id == target_id,
                )
            ),
        )
        return tuple(
            self._session.scalars(
                select(ExecutionModel)
                .where(
                    ExecutionModel.workspace_id == workspace_id,
                    target,
                )
                .order_by(ExecutionModel.id)
                .with_for_update()
            )
        )


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
