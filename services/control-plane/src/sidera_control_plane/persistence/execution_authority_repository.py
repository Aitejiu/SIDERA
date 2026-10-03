from dataclasses import replace
from datetime import datetime
from uuid import UUID

from sidera_agents.contracts import (
    CredentialRef,
    ExecutionWorkspace,
    ProfileRef,
    ResourceRef,
    RuntimeRef,
    SourceRef,
    StartReceipt,
    ToolBinding,
)
from sqlalchemy import delete, select, update
from sqlalchemy.orm import Session

from sidera_control_plane.domain.agent_bindings import (
    AgentExecutionLink,
    AssignmentDenied,
    ExecutionAuthority,
    NotAdmittedProof,
    RetiredAdmissionPin,
)
from sidera_control_plane.persistence.execution_repository import ExecutionRepository
from sidera_control_plane.persistence.lease_store import LeaseStore
from sidera_control_plane.persistence.models import (
    AgentAdmissionPinHistoryModel,
    AgentExecutionLinkModel,
    ExecutionAuthorityModel,
    ExecutionSessionModel,
    TaskModel,
    WorkerItemModel,
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
                request_sha256=link.request_sha256,
                start_receipt=None
                if link.start_receipt is None
                else link.start_receipt.model_dump(mode="json"),
                preparation=None
                if link.preparation is None
                else dict(link.preparation),
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
            request_sha256=row.request_sha256,
            start_receipt=None
            if row.start_receipt is None
            else StartReceipt.model_validate(row.start_receipt),
            preparation=row.preparation,
        )

    def lock_intention(
        self, expected: AgentExecutionLink, *, now: datetime | None = None
    ):
        # Match assignment's aggregate -> execution -> lease -> intention lock order.
        authority = self.get(expected.authority_id, workspace_id=expected.workspace_id)
        if authority is None:
            raise AssignmentDenied("native intention authority unavailable")
        model = TaskModel if authority.target_type == "subtask" else WorkerItemModel
        self._session.scalar(
            select(model)
            .where(
                model.id == authority.target_id,
                model.workspace_id == expected.workspace_id,
            )
            .with_for_update()
        )
        execution = ExecutionRepository(self._session).get(
            expected.execution_id, expected.workspace_id, for_update=True
        )
        lease = LeaseStore(self._session).get(
            authority.lease_id, expected.workspace_id, for_update=True
        )
        link = self.get_link(
            expected.workspace_id, expected.execution_id, for_update=True
        )
        from sidera_control_plane.persistence.agent_binding_repository import (
            AgentBindingRepository,
        )

        binding = AgentBindingRepository(self._session).get(
            expected.workspace_id,
            authority.target_type,
            authority.target_id,
            for_update=True,
        )
        authority = self.get(expected.authority_id, workspace_id=expected.workspace_id)
        if (
            link != expected
            or execution is None
            or lease is None
            or authority is None
            or authority.revoked
            or binding is None
            or binding.version != link.binding_version
            or binding.profile_ref != link.profile_ref
            or authority.binding_version != link.binding_version
            or authority.profile_ref != link.profile_ref
            or authority.execution_id != execution.id
            or execution.active_lease_id != lease.id
            or execution.last_fencing_token != authority.fencing_token
            or execution.assigned_worker_id != lease.worker_id
            or lease.execution_id != execution.id
            or lease.fencing_token != authority.fencing_token
            or lease.status.value != "active"
        ):
            raise AssignmentDenied("native intention pin or fence changed")
        if now is not None:
            lease.assert_owner(lease.worker_id, authority.fencing_token, now)
        return execution, lease, link, authority

    def begin_start(self, expected, *, request_sha256, now):
        execution, _, link, _ = self.lock_intention(expected, now=now)
        if (
            execution.status.value != "leased"
            or execution.started_at is not None
            or execution.stop_requested
            or link.run_id is not None
            or link.event_cursor != 0
            or link.status not in {"pending", "waiting_capacity"}
            or (
                link.request_sha256 is not None
                and link.request_sha256 != request_sha256
            )
            or self.has_session(link.workspace_id, link.execution_id)
        ):
            raise AssignmentDenied("native intention cannot begin or request changed")
        replacement = replace(
            link, status="starting", request_sha256=request_sha256, start_receipt=None
        )
        self._write_start(replacement)
        return replacement

    def record_start(self, expected, receipt: StartReceipt, *, now):
        _, _, link, _ = self.lock_intention(expected, now=now)
        if (
            receipt.operation_id != link.start_operation_id
            or link.request_sha256 is None
        ):
            raise AssignmentDenied("native admission identity mismatch")
        if link.start_receipt == receipt:
            return link
        if link.status not in {"starting", "unknown"}:
            raise AssignmentDenied("native admission receipt is stale")
        if link.run_id is not None and receipt.run_id != link.run_id:
            raise AssignmentDenied("native admission Run changed")
        status = receipt.status.value
        if status == "busy" and (
            receipt.run_id is not None
            or link.event_cursor != 0
            or self.has_session(link.workspace_id, link.execution_id)
        ):
            raise AssignmentDenied("BUSY receipt contradicts admitted evidence")
        replacement = replace(
            link,
            status={
                "busy": "waiting_capacity",
                "accepted": "starting",
                "rejected": "failed",
                "unknown": "unknown",
            }[status],
            run_id=receipt.run_id,
            start_receipt=receipt,
        )
        self._write_start(replacement)
        return replacement

    def _write_start(self, link):
        self._session.execute(
            update(AgentExecutionLinkModel)
            .where(
                AgentExecutionLinkModel.workspace_id == link.workspace_id,
                AgentExecutionLinkModel.execution_id == link.execution_id,
                AgentExecutionLinkModel.start_operation_id == link.start_operation_id,
                AgentExecutionLinkModel.authority_id == link.authority_id,
                AgentExecutionLinkModel.binding_version == link.binding_version,
            )
            .values(
                status=link.status,
                run_id=link.run_id,
                request_sha256=link.request_sha256,
                start_receipt=None
                if link.start_receipt is None
                else link.start_receipt.model_dump(mode="json"),
            )
        )

    def save_preparation(self, expected, preparation, *, now):
        _, _, link, _ = self.lock_intention(expected, now=now)
        if link.status != "pending" or link.run_id is not None:
            raise AssignmentDenied("native preparation is no longer pending")
        if link.preparation is not None and link.preparation != preparation:
            raise AssignmentDenied("native preparation identity changed")
        self._session.execute(
            update(AgentExecutionLinkModel)
            .where(
                AgentExecutionLinkModel.workspace_id == link.workspace_id,
                AgentExecutionLinkModel.execution_id == link.execution_id,
            )
            .values(preparation=preparation)
        )
        return replace(link, preparation=preparation)

    def has_session(self, workspace_id, execution_id):
        return (
            self._session.scalar(
                select(ExecutionSessionModel.id)
                .where(
                    ExecutionSessionModel.workspace_id == workspace_id,
                    ExecutionSessionModel.execution_id == execution_id,
                )
                .limit(1)
            )
            is not None
        )

    def retirement_proof(self, workspace_id, execution_id):
        link = self.get_link(workspace_id, execution_id)
        if link is None or not link.not_admitted:
            return None
        execution, lease, link, authority = self.lock_intention(link)
        if (
            execution.status.value != "leased"
            or execution.started_at is not None
            or execution.stop_requested
            or self.has_session(workspace_id, execution_id)
        ):
            return None
        return NotAdmittedProof(link, lease.id, authority.fencing_token)

    def append_admission_history(self, pin: RetiredAdmissionPin):
        link = pin.link
        self._session.add(
            AgentAdmissionPinHistoryModel(
                workspace_id=link.workspace_id,
                execution_id=link.execution_id,
                start_operation_id=link.start_operation_id,
                authority_id=link.authority_id,
                binding_version=link.binding_version,
                profile_ref=link.profile_ref.model_dump(mode="json"),
                request_sha256=link.request_sha256,
                start_receipt=link.start_receipt.model_dump(mode="json"),
                status=link.status,
                run_id=link.run_id,
                event_cursor=link.event_cursor,
                lease_id=pin.lease_id,
                fencing_token=pin.fencing_token,
                retired_at=pin.retired_at,
                cause_command_id=pin.cause_command_id,
                preparation=None
                if link.preparation is None
                else dict(link.preparation),
                execution_binding_snapshot=None
                if pin.execution_binding_snapshot is None
                else dict(pin.execution_binding_snapshot),
                workspace_snapshot=None
                if pin.workspace_snapshot is None
                else dict(pin.workspace_snapshot),
            )
        )
        self._session.flush()

    def remove_archived_link(self, pin: RetiredAdmissionPin):
        if pin not in self.admission_history(
            pin.link.workspace_id, pin.link.execution_id
        ):
            raise AssignmentDenied("native intention must be archived before removal")
        link = self.get_link(
            pin.link.workspace_id, pin.link.execution_id, for_update=True
        )
        if link != pin.link:
            raise AssignmentDenied("archived intention changed")
        self._session.execute(
            delete(AgentExecutionLinkModel).where(
                AgentExecutionLinkModel.workspace_id == link.workspace_id,
                AgentExecutionLinkModel.execution_id == link.execution_id,
                AgentExecutionLinkModel.start_operation_id == link.start_operation_id,
            )
        )

    def admission_history(self, workspace_id, execution_id):
        rows = self._session.scalars(
            select(AgentAdmissionPinHistoryModel)
            .where(
                AgentAdmissionPinHistoryModel.workspace_id == workspace_id,
                AgentAdmissionPinHistoryModel.execution_id == execution_id,
            )
            .order_by(
                AgentAdmissionPinHistoryModel.retired_at,
                AgentAdmissionPinHistoryModel.start_operation_id,
            )
        )
        return tuple(
            RetiredAdmissionPin(
                AgentExecutionLink(
                    row.workspace_id,
                    row.execution_id,
                    row.start_operation_id,
                    ProfileRef.model_validate(row.profile_ref),
                    row.run_id,
                    row.event_cursor,
                    row.status,
                    row.authority_id,
                    row.binding_version,
                    row.request_sha256,
                    StartReceipt.model_validate(row.start_receipt),
                    row.preparation,
                ),
                row.lease_id,
                row.fencing_token,
                row.retired_at,
                row.cause_command_id,
                row.execution_binding_snapshot,
                row.workspace_snapshot,
            )
            for row in rows
        )

    def list_intentions(self, workspace_id, *, limit=100):
        ids = self._session.scalars(
            select(AgentExecutionLinkModel.execution_id)
            .where(
                AgentExecutionLinkModel.workspace_id == workspace_id,
                AgentExecutionLinkModel.status.in_(
                    {"waiting_capacity", "starting", "unknown"}
                ),
            )
            .order_by(AgentExecutionLinkModel.execution_id)
            .limit(limit)
        )
        return tuple(self.get_link(workspace_id, execution_id) for execution_id in ids)

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
