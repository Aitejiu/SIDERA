from dataclasses import replace
from uuid import UUID

from sidera_agents.contracts import (
    CredentialRef,
    ExecutionWorkspace,
    ProfileRef,
    ResourceRef,
    RunStart,
    RuntimeRef,
    SourceRef,
    StartReceipt,
    ToolBinding,
)
from sqlalchemy import and_, or_, select, true, update
from sqlalchemy.orm import Session

from sidera_control_plane.domain.agent_bindings import AssignmentDenied
from sidera_control_plane.domain.planning_authority import (
    PlanningAgentLink,
    PlanningAuthority,
)
from sidera_control_plane.persistence.models import (
    PlanningAgentLinkModel,
    PlanningAuthorityModel,
    PlanningRunModel,
)


class PlanningAuthorityRepository:
    def __init__(self, session: Session):
        self._session = session

    def add(self, authority: PlanningAuthority):
        policy = {
            "planning_profile_id": str(authority.planning_profile_id),
            "planning_profile_version": authority.planning_profile_version,
            "operation_id": str(authority.operation_id),
            "principal_id": authority.principal_id,
            "upstream_grant_id": str(authority.upstream_grant_id),
            "owner_id": authority.owner_id,
            "fencing_token": authority.fencing_token,
            "execution_workspace": authority.execution_workspace.model_dump(
                mode="json"
            ),
            "profile_ref": authority.profile_ref.model_dump(mode="json"),
            "runtime_ref": authority.runtime_ref.model_dump(mode="json"),
            "actions": list(authority.actions),
            **{
                field: [
                    item.model_dump(mode="json") for item in getattr(authority, field)
                ]
                for field in ("sources", "credentials", "tools", "resources")
            },
        }
        self._session.add(
            PlanningAuthorityModel(
                id=authority.id,
                workspace_id=authority.workspace_id,
                planning_request_id=authority.planning_request_id,
                planning_run_id=authority.planning_run_id,
                lease_id=authority.lease_id,
                revoked=authority.revoked,
                policy=policy,
            )
        )

    def get(self, authority_id, *, workspace_id=None):
        statement = select(PlanningAuthorityModel).where(
            PlanningAuthorityModel.id == authority_id
        )
        if workspace_id is not None:
            statement = statement.where(
                PlanningAuthorityModel.workspace_id == workspace_id
            )
        row = self._session.scalar(statement)
        if row is None:
            return None
        policy = row.policy
        return PlanningAuthority(
            id=row.id,
            workspace_id=row.workspace_id,
            planning_request_id=row.planning_request_id,
            planning_run_id=row.planning_run_id,
            planning_profile_id=UUID(policy["planning_profile_id"]),
            planning_profile_version=policy["planning_profile_version"],
            operation_id=UUID(policy["operation_id"]),
            principal_id=policy["principal_id"],
            upstream_grant_id=UUID(policy["upstream_grant_id"]),
            owner_id=policy["owner_id"],
            profile_ref=ProfileRef.model_validate(policy["profile_ref"]),
            runtime_ref=RuntimeRef.model_validate(policy["runtime_ref"]),
            lease_id=row.lease_id,
            fencing_token=policy["fencing_token"],
            execution_workspace=ExecutionWorkspace.model_validate(
                policy["execution_workspace"]
            ),
            actions=tuple(policy["actions"]),
            revoked=row.revoked,
            **{
                field: tuple(contract.model_validate(item) for item in policy[field])
                for field, contract in (
                    ("sources", SourceRef),
                    ("credentials", CredentialRef),
                    ("tools", ToolBinding),
                    ("resources", ResourceRef),
                )
            },
        )

    def revoke(self, authority_id, workspace_id):
        self._session.execute(
            update(PlanningAuthorityModel)
            .where(
                PlanningAuthorityModel.id == authority_id,
                PlanningAuthorityModel.workspace_id == workspace_id,
            )
            .values(revoked=True)
        )

    def add_link(self, link: PlanningAgentLink):
        authority = self.get(link.authority_id, workspace_id=link.workspace_id)
        if authority is None or (
            authority.planning_run_id,
            authority.operation_id,
            authority.profile_ref,
            authority.execution_workspace,
        ) != (
            link.planning_run_id,
            link.start_operation_id,
            link.profile_ref,
            link.start_request.workspace,
        ):
            raise AssignmentDenied("planning link authority mismatch")
        self._session.add(PlanningAgentLinkModel(**self._link_values(link)))

    def get_link(self, workspace_id, planning_run_id, *, for_update=False):
        statement = select(PlanningAgentLinkModel).where(
            PlanningAgentLinkModel.workspace_id == workspace_id,
            PlanningAgentLinkModel.planning_run_id == planning_run_id,
        )
        if for_update:
            statement = statement.with_for_update()
        row = self._session.scalar(statement)
        if row is None:
            return None
        return PlanningAgentLink(
            workspace_id=row.workspace_id,
            planning_run_id=row.planning_run_id,
            authority_id=row.authority_id,
            start_operation_id=row.start_operation_id,
            profile_ref=ProfileRef.model_validate(row.start_request["profile_ref"]),
            start_request=RunStart.model_validate(row.start_request),
            request_sha256=row.request_sha256,
            status=row.status,
            run_id=row.run_id,
            start_receipt=None
            if row.start_receipt is None
            else StartReceipt.model_validate(row.start_receipt),
            event_cursor=row.event_cursor,
            response_id=row.response_id,
            closed=row.closed,
            reason_code=row.reason_code,
        )

    def list_reconciliation_links(
        self, workspace_id, *, limit=100, after_planning_run_id=None
    ):
        rows = self._session.scalars(
            select(PlanningAgentLinkModel)
            .join(
                PlanningRunModel,
                and_(
                    PlanningRunModel.id == PlanningAgentLinkModel.planning_run_id,
                    PlanningRunModel.workspace_id
                    == PlanningAgentLinkModel.workspace_id,
                ),
            )
            .where(
                PlanningAgentLinkModel.workspace_id == workspace_id,
                or_(
                    PlanningRunModel.status == "running",
                    and_(
                        PlanningRunModel.status.in_(("blocked", "failed", "cancelled")),
                        PlanningAgentLinkModel.status.in_(
                            ("cleanup_pending", "unknown")
                        ),
                        PlanningAgentLinkModel.closed.is_(False),
                    ),
                ),
                true()
                if after_planning_run_id is None
                else PlanningAgentLinkModel.planning_run_id > after_planning_run_id,
            )
            .order_by(PlanningAgentLinkModel.planning_run_id)
            .limit(limit)
        )
        return tuple(
            link
            for row in rows
            if (link := self.get_link(workspace_id, row.planning_run_id)) is not None
        )

    def save_original_observation(
        self, expected: PlanningAgentLink, *, event_cursor, response_id, closed
    ):
        current = self.get_link(
            expected.workspace_id, expected.planning_run_id, for_update=True
        )
        if (
            current is None
            or current != expected
            or event_cursor < current.event_cursor
            or (current.response_id is not None and response_id != current.response_id)
        ):
            raise AssignmentDenied("original planning observation changed")
        if current.closed:
            if not closed or event_cursor != current.event_cursor:
                raise AssignmentDenied("closed original planning evidence changed")
            return current
        return self.save_link(
            current,
            event_cursor=event_cursor,
            response_id=response_id,
            status="closed" if closed else current.status,
            closed=closed,
        )

    def save_link(self, expected: PlanningAgentLink, **changes):
        current = self.get_link(
            expected.workspace_id, expected.planning_run_id, for_update=True
        )
        if current != expected:
            raise AssignmentDenied("planning link observation is stale")
        if set(changes) - {
            "status",
            "run_id",
            "start_receipt",
            "event_cursor",
            "response_id",
            "closed",
            "reason_code",
        }:
            raise AssignmentDenied("planning intention pins are immutable")
        replacement = replace(current, **changes)
        if (
            current.closed
            or (current.run_id is not None and replacement.run_id != current.run_id)
            or replacement.event_cursor < current.event_cursor
        ):
            raise AssignmentDenied("original planning Run cannot be replaced")
        values = self._link_values(replacement)
        self._session.execute(
            update(PlanningAgentLinkModel)
            .where(
                PlanningAgentLinkModel.workspace_id == current.workspace_id,
                PlanningAgentLinkModel.planning_run_id == current.planning_run_id,
            )
            .values(**values)
        )
        return replacement

    @staticmethod
    def _link_values(link):
        return {
            "workspace_id": link.workspace_id,
            "planning_run_id": link.planning_run_id,
            "authority_id": link.authority_id,
            "start_operation_id": link.start_operation_id,
            "start_request": link.start_request.model_dump(mode="json"),
            "request_sha256": link.request_sha256,
            "status": link.status,
            "run_id": link.run_id,
            "start_receipt": None
            if link.start_receipt is None
            else link.start_receipt.model_dump(mode="json"),
            "event_cursor": link.event_cursor,
            "response_id": link.response_id,
            "closed": link.closed,
            "reason_code": link.reason_code,
        }
