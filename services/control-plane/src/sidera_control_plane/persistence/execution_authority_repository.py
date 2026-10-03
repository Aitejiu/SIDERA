from dataclasses import replace
from uuid import UUID

from sidera_agents.contracts import (
    CredentialRef,
    ExecutionWorkspace,
    ProfileRef,
    ResourceRef,
    RuntimeRef,
    SourceRef,
    ToolBinding,
)
from sqlalchemy import select, update
from sqlalchemy.orm import Session

from sidera_control_plane.domain.agent_bindings import (
    AgentExecutionLink,
    AssignmentDenied,
    ExecutionAuthority,
)
from sidera_control_plane.persistence.models import (
    AgentExecutionLinkModel,
    ExecutionAuthorityModel,
)
from sidera_control_plane.persistence.workspace_repository import WorkspaceRepository


class ExecutionAuthorityRepository:
    def __init__(self, session: Session):
        self._session = session

    def add(self, authority: ExecutionAuthority):
        manifest = WorkspaceRepository(self._session).get(
            authority.workspace_ref, authority.workspace_id, for_update=True
        )
        if (
            manifest is None
            or manifest.execution_id != authority.execution_id
            or manifest.fencing_token != authority.fencing_token
        ):
            raise AssignmentDenied("authority prepared workspace unavailable")
        self._session.add(
            ExecutionAuthorityModel(
                id=authority.id,
                workspace_id=authority.workspace_id,
                execution_id=authority.execution_id,
                target_type=authority.target_type,
                target_id=authority.target_id,
                binding_version=authority.binding_version,
                revoked=authority.revoked,
                policy={
                    "principal_id": authority.principal_id,
                    "profile_ref": authority.profile_ref.model_dump(mode="json"),
                    "runtime_ref": authority.runtime_ref.model_dump(mode="json"),
                    "lease_id": str(authority.lease_id),
                    "fencing_token": authority.fencing_token,
                    "workspace_ref": str(authority.workspace_ref),
                    "workspace_location_ref": manifest.location_ref,
                    "execution_workspace": authority.execution_workspace.model_dump(
                        mode="json"
                    ),
                    "actions": list(authority.actions),
                    "sources": [v.model_dump(mode="json") for v in authority.sources],
                    "credentials": [
                        v.model_dump(mode="json") for v in authority.credentials
                    ],
                    "tools": [v.model_dump(mode="json") for v in authority.tools],
                    "resources": [
                        v.model_dump(mode="json") for v in authority.resources
                    ],
                },
            )
        )

    def get(self, authority_id, *, workspace_id=None):
        statement = select(ExecutionAuthorityModel).where(
            ExecutionAuthorityModel.id == authority_id
        )
        if workspace_id is not None:
            statement = statement.where(
                ExecutionAuthorityModel.workspace_id == workspace_id
            )
        row = self._session.scalar(statement)
        if row is None:
            return None
        policy = row.policy
        return ExecutionAuthority(
            id=row.id,
            workspace_id=row.workspace_id,
            execution_id=row.execution_id,
            target_type=row.target_type,
            target_id=row.target_id,
            binding_version=row.binding_version,
            principal_id=policy["principal_id"],
            profile_ref=ProfileRef.model_validate(policy["profile_ref"]),
            runtime_ref=RuntimeRef.model_validate(policy["runtime_ref"]),
            lease_id=UUID(policy["lease_id"]),
            fencing_token=policy["fencing_token"],
            workspace_ref=UUID(policy["workspace_ref"]),
            execution_workspace=ExecutionWorkspace.model_validate(
                policy["execution_workspace"]
            ),
            actions=tuple(policy["actions"]),
            revoked=row.revoked,
            sources=tuple(SourceRef.model_validate(v) for v in policy["sources"]),
            credentials=tuple(
                CredentialRef.model_validate(v) for v in policy["credentials"]
            ),
            tools=tuple(ToolBinding.model_validate(v) for v in policy["tools"]),
            resources=tuple(ResourceRef.model_validate(v) for v in policy["resources"]),
        )

    def revoke(self, authority_id, workspace_id):
        self._session.execute(
            update(ExecutionAuthorityModel)
            .where(
                ExecutionAuthorityModel.id == authority_id,
                ExecutionAuthorityModel.workspace_id == workspace_id,
            )
            .values(revoked=True)
        )

    def prepared_location_ref(self, authority_id, workspace_id):
        row = self._session.scalar(
            select(ExecutionAuthorityModel).where(
                ExecutionAuthorityModel.id == authority_id,
                ExecutionAuthorityModel.workspace_id == workspace_id,
            )
        )
        return None if row is None else row.policy["workspace_location_ref"]

    def add_link(self, link: AgentExecutionLink):
        authority = self.get(link.authority_id, workspace_id=link.workspace_id)
        if authority is None or (
            authority.execution_id,
            authority.profile_ref,
            authority.binding_version,
        ) != (link.execution_id, link.profile_ref, link.binding_version):
            raise AssignmentDenied("execution link authority mismatch")
        self._session.add(
            AgentExecutionLinkModel(
                workspace_id=link.workspace_id,
                execution_id=link.execution_id,
                start_operation_id=link.start_operation_id,
                profile_ref=link.profile_ref.model_dump(mode="json"),
                run_id=link.run_id,
                event_cursor=link.event_cursor,
                status=link.status,
                authority_id=link.authority_id,
                binding_version=link.binding_version,
            )
        )

    def get_link(self, workspace_id, execution_id, *, for_update=False):
        statement = select(AgentExecutionLinkModel).where(
            AgentExecutionLinkModel.workspace_id == workspace_id,
            AgentExecutionLinkModel.execution_id == execution_id,
        )
        if for_update:
            statement = statement.with_for_update()
        row = self._session.scalar(statement)
        if row is None:
            return None
        return AgentExecutionLink(
            workspace_id=row.workspace_id,
            execution_id=row.execution_id,
            start_operation_id=row.start_operation_id,
            profile_ref=ProfileRef.model_validate(row.profile_ref),
            run_id=row.run_id,
            event_cursor=row.event_cursor,
            status=row.status,
            authority_id=row.authority_id,
            binding_version=row.binding_version,
        )

    def advance_link(
        self, *, workspace_id, execution_id, authority_id, run_id, event_cursor, status
    ):
        link = self.get_link(workspace_id, execution_id, for_update=True)
        authority = self.get(authority_id, workspace_id=workspace_id)
        if (
            link is None
            or authority is None
            or link.authority_id != authority_id
            or authority.revoked
        ):
            raise AssignmentDenied("execution link unavailable")
        from sidera_control_plane.persistence.agent_binding_repository import (
            AgentBindingRepository,
        )

        binding = AgentBindingRepository(self._session).get(
            workspace_id, authority.target_type, authority.target_id, for_update=True
        )
        if (
            binding is None
            or binding.version != link.binding_version
            or binding.profile_ref != link.profile_ref
        ):
            raise AssignmentDenied("execution link binding is stale")
        if link.run_id is not None and link.run_id != run_id:
            raise AssignmentDenied("execution run mismatch")
        if (
            link.status in {"succeeded", "failed", "stopped", "cancelled"}
            or event_cursor <= link.event_cursor
        ):
            raise AssignmentDenied("execution event is stale")
        replacement = replace(
            link, run_id=run_id, event_cursor=event_cursor, status=status
        )
        self._session.execute(
            update(AgentExecutionLinkModel)
            .where(
                AgentExecutionLinkModel.workspace_id == workspace_id,
                AgentExecutionLinkModel.execution_id == execution_id,
                AgentExecutionLinkModel.event_cursor == link.event_cursor,
            )
            .values(run_id=run_id, event_cursor=event_cursor, status=status)
        )
        return replacement
