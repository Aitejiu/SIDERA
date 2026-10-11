from collections.abc import Callable, Iterator
from contextlib import AbstractContextManager, contextmanager
from dataclasses import replace
from uuid import UUID

from pydantic import BaseModel, ConfigDict, ValidationError
from sidera_agents.contracts import ProfileRef, RunStart, StartReceipt, StartStatus
from sidera_agents.errors import AgentError
from sqlalchemy import (
    String,
    and_,
    case,
    cast,
    func,
    literal,
    not_,
    null,
    or_,
    select,
    union_all,
)
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session
from sqlalchemy.sql import Select

from sidera_control_plane.domain.agent_bindings import AgentExecutionLink
from sidera_control_plane.domain.assignment_views import (
    AgentBindingView,
    AgentExecutionView,
    AssignmentKind,
    OriginalRunEvidence,
    ProfileWorkKind,
    ProfileWorkRow,
    WorkCursor,
)
from sidera_control_plane.domain.errors import ValidationError as DomainValidationError
from sidera_control_plane.domain.planning_authority import PlanningAgentLink
from sidera_control_plane.persistence.models import (
    AgentBindingModel,
    AgentExecutionLinkModel,
    ExecutionAuthorityModel,
    ExecutionModel,
    ExecutionRequestModel,
    PlanningAgentLinkModel,
    PlanningAuthorityModel,
    PlanningBriefRevisionModel,
    PlanningRequestModel,
    PlanningRunModel,
    ProposalMaterializationModel,
    TaskGoalSubmissionModel,
    TaskModel,
    WorkerItemModel,
)


class _ExecutionPolicyPins(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True)
    profile_ref: ProfileRef


class _PlanningPolicyPins(_ExecutionPolicyPins):
    operation_id: UUID


def profile_work_statement(
    workspace_id: UUID, profile_id: UUID, limit: int, after: WorkCursor | None
) -> Select:
    root_for_assignment = (
        select(ExecutionRequestModel.root_task_id)
        .where(
            ExecutionRequestModel.workspace_id == workspace_id,
            ExecutionRequestModel.subtask_task_id == AgentBindingModel.target_id,
        )
        .order_by(
            ExecutionRequestModel.plan_generation.desc(), ExecutionRequestModel.id
        )
        .limit(1)
        .correlate(AgentBindingModel)
        .scalar_subquery()
    )
    assignment = select(
        AgentBindingModel.workspace_id.label("workspace_id"),
        AgentBindingModel.target_id.label("work_id"),
        literal("assignment").label("kind"),
        func.coalesce(TaskModel.title, WorkerItemModel.title).label("title"),
        func.coalesce(TaskModel.status, WorkerItemModel.status).label("status"),
        AgentBindingModel.changed_at.label("updated_at"),
        AgentBindingModel.profile_ref.label("profile_ref"),
        case(
            (AgentBindingModel.target_type == "subtask", root_for_assignment),
            else_=WorkerItemModel.root_task_id,
        ).label("root_task_id"),
        case(
            (AgentBindingModel.target_type == "subtask", AgentBindingModel.target_id),
            else_=null(),
        ).label("subtask_id"),
        case(
            (
                AgentBindingModel.target_type == "worker_item",
                AgentBindingModel.target_id,
            ),
            else_=null(),
        ).label("worker_item_id"),
        cast(null(), PlanningRunModel.planning_request_id.type).label(
            "planning_request_id"
        ),
        cast(null(), PlanningRunModel.id.type).label("planning_run_id"),
        null().label("execution_id"),
        AgentBindingModel.target_type.label("target_type"),
        AgentBindingModel.version.label("binding_version"),
        null().label("run_id"),
        null().label("link_status"),
        null().label("event_cursor"),
    ).outerjoin(
        TaskModel,
        and_(
            AgentBindingModel.target_type == "subtask",
            TaskModel.workspace_id == workspace_id,
            TaskModel.id == AgentBindingModel.target_id,
        ),
    )
    assignment = assignment.outerjoin(
        WorkerItemModel,
        and_(
            AgentBindingModel.target_type == "worker_item",
            WorkerItemModel.workspace_id == workspace_id,
            WorkerItemModel.id == AgentBindingModel.target_id,
        ),
    ).where(
        AgentBindingModel.workspace_id == workspace_id,
        AgentBindingModel.is_current,
        AgentBindingModel.assignee_kind == "profile",
        AgentBindingModel.profile_ref["profile_id"].astext == str(profile_id),
        or_(TaskModel.id.is_not(None), WorkerItemModel.id.is_not(None)),
    )
    root_for_execution = (
        select(ExecutionRequestModel.root_task_id)
        .where(
            ExecutionRequestModel.workspace_id == workspace_id,
            ExecutionRequestModel.first_execution_id == ExecutionModel.lineage_id,
        )
        .order_by(
            ExecutionRequestModel.plan_generation.desc(), ExecutionRequestModel.id
        )
        .limit(1)
        .correlate(ExecutionModel)
        .scalar_subquery()
    )
    execution = select(
        ExecutionModel.workspace_id.label("workspace_id"),
        ExecutionModel.id.label("work_id"),
        literal("execution").label("kind"),
        TaskModel.title.label("title"),
        ExecutionModel.status.label("status"),
        ExecutionModel.updated_at.label("updated_at"),
        AgentExecutionLinkModel.profile_ref.label("profile_ref"),
        func.coalesce(WorkerItemModel.root_task_id, root_for_execution).label(
            "root_task_id"
        ),
        ExecutionModel.task_id.label("subtask_id"),
        ExecutionModel.worker_item_id.label("worker_item_id"),
        cast(null(), PlanningRunModel.planning_request_id.type).label(
            "planning_request_id"
        ),
        cast(null(), PlanningRunModel.id.type).label("planning_run_id"),
        ExecutionModel.id.label("execution_id"),
        null().label("target_type"),
        AgentExecutionLinkModel.binding_version.label("binding_version"),
        AgentExecutionLinkModel.run_id.label("run_id"),
        AgentExecutionLinkModel.status.label("link_status"),
        AgentExecutionLinkModel.event_cursor.label("event_cursor"),
    ).join(
        AgentExecutionLinkModel,
        and_(
            AgentExecutionLinkModel.workspace_id == workspace_id,
            AgentExecutionLinkModel.execution_id == ExecutionModel.id,
        ),
    )
    execution = (
        execution.join(
            TaskModel,
            and_(
                TaskModel.workspace_id == workspace_id,
                TaskModel.id == ExecutionModel.task_id,
            ),
        )
        .outerjoin(
            WorkerItemModel,
            and_(
                WorkerItemModel.workspace_id == workspace_id,
                WorkerItemModel.id == ExecutionModel.worker_item_id,
            ),
        )
        .where(
            ExecutionModel.workspace_id == workspace_id,
            AgentExecutionLinkModel.profile_ref["profile_id"].astext == str(profile_id),
        )
    )
    planning_ref = func.coalesce(
        PlanningAgentLinkModel.start_request["profile_ref"],
        PlanningRunModel.planner_agent_ref,
    )
    planning = select(
        PlanningRunModel.workspace_id.label("workspace_id"),
        PlanningRunModel.id.label("work_id"),
        literal("planning").label("kind"),
        PlanningBriefRevisionModel.payload["objective"].astext.label("title"),
        PlanningRunModel.status.label("status"),
        PlanningRunModel.updated_at.label("updated_at"),
        planning_ref.label("profile_ref"),
        func.coalesce(
            PlanningRequestModel.root_task_id,
            TaskGoalSubmissionModel.root_task_id,
            ProposalMaterializationModel.root_task_id,
        ).label("root_task_id"),
        null().label("subtask_id"),
        null().label("worker_item_id"),
        PlanningRunModel.planning_request_id.label("planning_request_id"),
        PlanningRunModel.id.label("planning_run_id"),
        null().label("execution_id"),
        null().label("target_type"),
        null().label("binding_version"),
        PlanningAgentLinkModel.run_id.label("run_id"),
        PlanningAgentLinkModel.status.label("link_status"),
        PlanningAgentLinkModel.event_cursor.label("event_cursor"),
    ).join(
        PlanningRequestModel,
        and_(
            PlanningRequestModel.workspace_id == workspace_id,
            PlanningRequestModel.id == PlanningRunModel.planning_request_id,
        ),
    )
    planning = (
        planning.join(
            PlanningBriefRevisionModel,
            and_(
                PlanningBriefRevisionModel.workspace_id == workspace_id,
                PlanningBriefRevisionModel.planning_request_id
                == PlanningRunModel.planning_request_id,
                PlanningBriefRevisionModel.revision == PlanningRunModel.brief_revision,
            ),
        )
        .outerjoin(
            TaskGoalSubmissionModel,
            and_(
                TaskGoalSubmissionModel.workspace_id == workspace_id,
                TaskGoalSubmissionModel.planning_request_id
                == PlanningRunModel.planning_request_id,
            ),
        )
        .outerjoin(
            ProposalMaterializationModel,
            and_(
                ProposalMaterializationModel.workspace_id == workspace_id,
                ProposalMaterializationModel.planning_request_id
                == PlanningRunModel.planning_request_id,
            ),
        )
        .outerjoin(
            PlanningAgentLinkModel,
            and_(
                PlanningAgentLinkModel.workspace_id == workspace_id,
                PlanningAgentLinkModel.planning_run_id == PlanningRunModel.id,
            ),
        )
        .where(
            PlanningRunModel.workspace_id == workspace_id,
            PlanningRunModel.execution_mode == "native_profile",
            planning_ref["profile_id"].astext == str(profile_id),
        )
    )
    work = union_all(assignment, execution, planning).subquery("profile_work")
    discriminator = func.coalesce(work.c.target_type, "")
    statement = select(work)
    if after is not None:
        statement = statement.where(
            or_(
                work.c.updated_at < after.updated_at,
                and_(work.c.updated_at == after.updated_at, work.c.kind > after.kind),
                and_(
                    work.c.updated_at == after.updated_at,
                    work.c.kind == after.kind,
                    work.c.work_id > after.work_id,
                ),
                and_(
                    work.c.updated_at == after.updated_at,
                    work.c.kind == after.kind,
                    work.c.work_id == after.work_id,
                    discriminator > after.target_type,
                ),
            )
        )
    return statement.order_by(
        work.c.updated_at.desc(), work.c.kind, work.c.work_id, discriminator
    ).limit(limit)


def _binding(row: AgentBindingModel) -> AgentBindingView:
    return AgentBindingView.model_validate(
        {
            "workspace_id": row.workspace_id,
            "target_type": row.target_type,
            "target_id": row.target_id,
            "assignee": {"kind": row.assignee_kind, "id": row.assignee_id},
            "profile_ref": row.profile_ref,
            "version": row.version,
            "created_at": row.created_at,
            "changed_at": row.changed_at,
            "runnable": row.runnable,
            "not_runnable_reasons": row.not_runnable_reasons,
        }
    )


def _root(
    session: Session, workspace_id: UUID, execution: ExecutionModel
) -> UUID | None:
    if execution.worker_item_id is not None:
        return session.scalar(
            select(WorkerItemModel.root_task_id).where(
                WorkerItemModel.workspace_id == workspace_id,
                WorkerItemModel.id == execution.worker_item_id,
            )
        )
    return session.scalar(
        select(ExecutionRequestModel.root_task_id)
        .where(
            ExecutionRequestModel.workspace_id == workspace_id,
            ExecutionRequestModel.first_execution_id == execution.lineage_id,
        )
        .order_by(
            ExecutionRequestModel.plan_generation.desc(), ExecutionRequestModel.id
        )
        .limit(1)
    )


class PostgresAssignmentReadStore:
    def __init__(self, sessions: Callable[[], AbstractContextManager[Session]]) -> None:
        self._sessions = sessions

    @contextmanager
    def _read(self) -> Iterator[Session]:
        try:
            with self._sessions() as session:
                try:
                    yield session
                finally:
                    session.rollback()
        except (SQLAlchemyError, ValidationError):
            raise AgentError("query_unavailable", "Work read is unavailable.") from None

    def read_assignment(
        self, workspace_id: UUID, target_type: AssignmentKind, target_id: UUID
    ) -> AgentBindingView | None:
        with self._read() as session:
            model = TaskModel if target_type == "subtask" else WorkerItemModel
            if (
                session.scalar(
                    select(model.id).where(
                        model.workspace_id == workspace_id, model.id == target_id
                    )
                )
                is None
            ):
                raise AgentError("not_found", "Assignment target is unavailable.")
            row = session.scalar(
                select(AgentBindingModel).where(
                    AgentBindingModel.workspace_id == workspace_id,
                    AgentBindingModel.target_type == target_type,
                    AgentBindingModel.target_id == target_id,
                    AgentBindingModel.is_current,
                )
            )
            return None if row is None else _binding(row)

    def read_execution(
        self, workspace_id: UUID, execution_id: UUID
    ) -> AgentExecutionView | None:
        with self._read() as session:
            execution = session.scalar(
                select(ExecutionModel).where(
                    ExecutionModel.workspace_id == workspace_id,
                    ExecutionModel.id == execution_id,
                )
            )
            if execution is None:
                return None
            link = session.scalar(
                select(AgentExecutionLinkModel).where(
                    AgentExecutionLinkModel.workspace_id == workspace_id,
                    AgentExecutionLinkModel.execution_id == execution_id,
                )
            )
            return AgentExecutionView(
                workspace_id=workspace_id,
                execution_id=execution_id,
                root_task_id=_root(session, workspace_id, execution),
                subtask_id=execution.task_id,
                worker_item_id=execution.worker_item_id,
                status=execution.status,
                updated_at=execution.updated_at,
                profile_ref=None
                if link is None
                else ProfileRef.model_validate(link.profile_ref),
                binding_version=None if link is None else link.binding_version,
                run_id=None if link is None else link.run_id,
                link_status=None if link is None else link.status,
                event_cursor=None if link is None else link.event_cursor,
            )

    def list_profile_work(
        self, workspace_id: UUID, profile_id: UUID, limit: int, after: WorkCursor | None
    ) -> tuple[ProfileWorkRow, ...]:
        with self._read() as session:
            return tuple(
                ProfileWorkRow.model_validate(dict(row))
                for row in session.execute(
                    profile_work_statement(workspace_id, profile_id, limit, after),
                ).mappings()
            )

    def read_original_run(
        self,
        workspace_id: UUID,
        kind: ProfileWorkKind,
        work_id: UUID,
        *,
        target_type: AssignmentKind | None = None,
    ) -> OriginalRunEvidence | None:
        with self._read() as session:
            if kind == "execution":
                return self._execution_evidence(session, workspace_id, work_id)
            if kind == "planning":
                return self._planning_evidence(session, workspace_id, work_id)
            if target_type not in {"subtask", "worker_item"}:
                raise AgentError("configuration_invalid", "Invalid assignment read.")
            model = TaskModel if target_type == "subtask" else WorkerItemModel
            if (
                session.scalar(
                    select(model.id).where(
                        model.workspace_id == workspace_id, model.id == work_id
                    )
                )
                is None
            ):
                raise AgentError("not_found", "Assignment target is unavailable.")
            binding = session.scalar(
                select(AgentBindingModel).where(
                    AgentBindingModel.workspace_id == workspace_id,
                    AgentBindingModel.target_type == target_type,
                    AgentBindingModel.target_id == work_id,
                    AgentBindingModel.is_current,
                )
            )
            if binding is None:
                return None
            receipt_matches = and_(
                func.jsonb_typeof(AgentExecutionLinkModel.start_receipt) == "object",
                AgentExecutionLinkModel.start_receipt["operation_id"].astext
                == cast(AgentExecutionLinkModel.start_operation_id, String),
                AgentExecutionLinkModel.start_receipt["run_id"].is_not(None),
                AgentExecutionLinkModel.start_receipt["run_id"].astext.is_(None),
                AgentExecutionLinkModel.start_receipt["reason_code"].is_not(None),
                func.jsonb_typeof(
                    AgentExecutionLinkModel.start_receipt["reason_code"]
                ).in_(("string", "null")),
            )
            known_unadmitted = and_(
                AgentExecutionLinkModel.run_id.is_(None),
                AgentExecutionLinkModel.event_cursor == 0,
                or_(
                    and_(
                        AgentExecutionLinkModel.status == "pending",
                        AgentExecutionLinkModel.request_sha256.is_(None),
                        AgentExecutionLinkModel.start_receipt.is_(None),
                    ),
                    and_(
                        AgentExecutionLinkModel.status == "waiting_capacity",
                        AgentExecutionLinkModel.request_sha256.is_not(None),
                        receipt_matches,
                        AgentExecutionLinkModel.start_receipt["status"].astext
                        == "busy",
                    ),
                    and_(
                        AgentExecutionLinkModel.status == "failed",
                        AgentExecutionLinkModel.request_sha256.is_not(None),
                        receipt_matches,
                        AgentExecutionLinkModel.start_receipt["status"].astext
                        == "rejected",
                    ),
                ),
            )
            execution_ids = session.scalars(
                select(AgentExecutionLinkModel.execution_id)
                .join(
                    ExecutionAuthorityModel,
                    (ExecutionAuthorityModel.id == AgentExecutionLinkModel.authority_id)
                    & (ExecutionAuthorityModel.workspace_id == workspace_id),
                )
                .join(
                    ExecutionModel,
                    (ExecutionModel.id == AgentExecutionLinkModel.execution_id)
                    & (ExecutionModel.workspace_id == workspace_id),
                )
                .where(
                    AgentExecutionLinkModel.workspace_id == workspace_id,
                    ExecutionAuthorityModel.target_type == binding.target_type,
                    ExecutionAuthorityModel.target_id == binding.target_id,
                    ExecutionAuthorityModel.binding_version == binding.version,
                    AgentExecutionLinkModel.binding_version == binding.version,
                    or_(
                        AgentExecutionLinkModel.run_id.is_not(None),
                        not_(func.coalesce(known_unadmitted, False)),
                    ),
                )
                .order_by(ExecutionModel.id)
                .limit(2)
            ).all()
            if not execution_ids:
                return None
            if len(execution_ids) > 1:
                return OriginalRunEvidence(
                    workspace_id,
                    None,
                    None,
                    None,
                    None,
                    reason_code="native_run_ambiguous",
                )
            evidence = self._execution_evidence(session, workspace_id, execution_ids[0])
            if evidence is None:
                return None
            try:
                current_ref = ProfileRef.model_validate(binding.profile_ref)
            except ValidationError:
                return replace(evidence, verified=False)
            return replace(
                evidence,
                verified=evidence.verified
                and evidence.target_type == target_type
                and evidence.target_id == work_id
                and evidence.profile_ref == current_ref,
            )

    @staticmethod
    def _execution_evidence(
        session: Session, workspace_id: UUID, execution_id: UUID
    ) -> OriginalRunEvidence | None:
        execution = session.scalar(
            select(ExecutionModel).where(
                ExecutionModel.workspace_id == workspace_id,
                ExecutionModel.id == execution_id,
            )
        )
        if execution is None:
            raise AgentError("not_found", "Execution is unavailable.")
        link = session.scalar(
            select(AgentExecutionLinkModel).where(
                AgentExecutionLinkModel.workspace_id == workspace_id,
                AgentExecutionLinkModel.execution_id == execution_id,
            )
        )
        if link is None:
            return None
        authority = session.scalar(
            select(ExecutionAuthorityModel).where(
                ExecutionAuthorityModel.workspace_id == workspace_id,
                ExecutionAuthorityModel.execution_id == execution_id,
                ExecutionAuthorityModel.id == link.authority_id,
            )
        )
        binding = (
            None
            if authority is None
            else session.scalar(
                select(AgentBindingModel).where(
                    AgentBindingModel.workspace_id == workspace_id,
                    AgentBindingModel.target_type == authority.target_type,
                    AgentBindingModel.target_id == authority.target_id,
                    AgentBindingModel.version == link.binding_version,
                )
            )
        )
        target_type: AssignmentKind = (
            "worker_item" if execution.worker_item_id is not None else "subtask"
        )
        target_id = (
            execution.worker_item_id
            if execution.worker_item_id is not None
            else execution.task_id
        )
        try:
            ref = ProfileRef.model_validate(link.profile_ref)
            policy = (
                None
                if authority is None
                else _ExecutionPolicyPins.model_validate(authority.policy)
            )
            binding_ref = (
                None
                if binding is None
                else ProfileRef.model_validate(binding.profile_ref)
            )
            receipt = (
                None
                if link.start_receipt is None
                else StartReceipt.model_validate(link.start_receipt)
            )
        except ValidationError:
            return OriginalRunEvidence(
                workspace_id,
                link.run_id,
                None,
                link.start_operation_id,
                link.authority_id,
                target_type,
                target_id,
                reason_code="native_run_unavailable",
            )
        pins_match = (
            authority is not None
            and authority.binding_version == link.binding_version
            and authority.target_type == target_type
            and authority.target_id == target_id
            and binding is not None
            and binding.version == link.binding_version
            and binding_ref == ref
            and policy is not None
            and policy.profile_ref == ref
            and ref.workspace_id == workspace_id
        )
        if link.run_id is None:
            reason = "native_run_unavailable"
            if pins_match:
                try:
                    proof = AgentExecutionLink(
                        workspace_id=workspace_id,
                        execution_id=execution_id,
                        start_operation_id=link.start_operation_id,
                        profile_ref=ref,
                        run_id=None,
                        event_cursor=link.event_cursor,
                        status=link.status,
                        authority_id=link.authority_id,
                        binding_version=link.binding_version,
                        request_sha256=link.request_sha256,
                        start_receipt=receipt,
                        preparation=link.preparation,
                    )
                    reason = (
                        "run_not_admitted"
                        if proof.known_unadmitted
                        else "run_admission_unconfirmed"
                    )
                except DomainValidationError:
                    pass
            return OriginalRunEvidence(
                workspace_id,
                None,
                ref,
                link.start_operation_id,
                link.authority_id,
                target_type,
                target_id,
                reason_code=reason,
            )
        verified = (
            pins_match
            and receipt is not None
            and receipt.status is StartStatus.ACCEPTED
            and receipt.operation_id == link.start_operation_id
            and receipt.run_id == link.run_id
        )
        return OriginalRunEvidence(
            workspace_id,
            link.run_id,
            ref,
            link.start_operation_id,
            link.authority_id,
            target_type,
            target_id,
            verified,
        )

    @staticmethod
    def _planning_evidence(
        session: Session, workspace_id: UUID, planning_run_id: UUID
    ) -> OriginalRunEvidence | None:
        run = session.scalar(
            select(PlanningRunModel).where(
                PlanningRunModel.workspace_id == workspace_id,
                PlanningRunModel.id == planning_run_id,
            )
        )
        if run is None:
            raise AgentError("not_found", "Planning Run is unavailable.")
        link = session.scalar(
            select(PlanningAgentLinkModel).where(
                PlanningAgentLinkModel.workspace_id == workspace_id,
                PlanningAgentLinkModel.planning_run_id == planning_run_id,
            )
        )
        if link is None:
            return None
        authority = session.scalar(
            select(PlanningAuthorityModel).where(
                PlanningAuthorityModel.workspace_id == workspace_id,
                PlanningAuthorityModel.planning_run_id == planning_run_id,
                PlanningAuthorityModel.id == link.authority_id,
            )
        )
        try:
            request = RunStart.model_validate(link.start_request)
            policy = (
                None
                if authority is None
                else _PlanningPolicyPins.model_validate(authority.policy)
            )
            run_ref = ProfileRef.model_validate(run.planner_agent_ref)
            receipt = (
                None
                if link.start_receipt is None
                else StartReceipt.model_validate(link.start_receipt)
            )
        except ValidationError:
            return OriginalRunEvidence(
                workspace_id,
                link.run_id,
                None,
                link.start_operation_id,
                link.authority_id,
                reason_code="native_run_unavailable",
            )
        pins_match = (
            authority is not None
            and authority.planning_request_id == run.planning_request_id
            and run.execution_mode == "native_profile"
            and policy is not None
            and policy.profile_ref == request.profile_ref == run_ref
            and request.profile_ref.workspace_id == workspace_id
            and policy.operation_id
            == request.operation_id
            == run.operation_id
            == link.start_operation_id
            and request.grant_id == link.authority_id
        )
        if link.run_id is None:
            reason = "native_run_unavailable"
            if pins_match:
                try:
                    proof = PlanningAgentLink(
                        workspace_id=workspace_id,
                        planning_run_id=planning_run_id,
                        authority_id=link.authority_id,
                        start_operation_id=link.start_operation_id,
                        profile_ref=request.profile_ref,
                        start_request=request,
                        request_sha256=link.request_sha256,
                        status=link.status,
                        start_receipt=receipt,
                        event_cursor=link.event_cursor,
                        response_id=link.response_id,
                        closed=link.closed,
                        reason_code=link.reason_code,
                    )
                    pending = (
                        proof.status == "pending"
                        and proof.event_cursor == 0
                        and proof.start_receipt is None
                    )
                    reason = (
                        "run_not_admitted"
                        if pending or proof.not_admitted
                        else "run_admission_unconfirmed"
                    )
                except DomainValidationError:
                    pass
            return OriginalRunEvidence(
                workspace_id,
                None,
                request.profile_ref,
                link.start_operation_id,
                link.authority_id,
                reason_code=reason,
            )
        verified = (
            pins_match
            and receipt is not None
            and receipt.status is StartStatus.ACCEPTED
            and receipt.operation_id == link.start_operation_id
            and receipt.run_id == link.run_id
        )
        return OriginalRunEvidence(
            workspace_id,
            link.run_id,
            request.profile_ref,
            link.start_operation_id,
            link.authority_id,
            verified=verified,
        )
