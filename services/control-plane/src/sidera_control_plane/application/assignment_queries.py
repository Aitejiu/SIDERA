import base64
from binascii import Error as Base64Error
from typing import Protocol
from uuid import UUID

from pydantic import ValidationError
from sidera_agents.api import AgentExecution
from sidera_agents.contracts import RunSnapshot
from sidera_agents.errors import AgentError

from sidera_control_plane.application.assignments import AssignmentTarget
from sidera_control_plane.application.request_authority import (
    ControlPlaneIdentity,
    ControlPlaneRequestAuthorizer,
)
from sidera_control_plane.domain.assignment_views import (
    AgentBindingView,
    AgentExecutionView,
    AssignmentKind,
    NativeRunSummary,
    NativeRunView,
    OriginalRunEvidence,
    ProfileWorkKind,
    ProfileWorkPage,
    ProfileWorkRow,
    ProfileWorkRunTarget,
    ProfileWorkTarget,
    WorkCursor,
)


class AssignmentReadStore(Protocol):
    def read_assignment(
        self, workspace_id: UUID, target_type: AssignmentKind, target_id: UUID
    ) -> AgentBindingView | None: ...

    def read_execution(
        self, workspace_id: UUID, execution_id: UUID
    ) -> AgentExecutionView | None: ...

    def list_profile_work(
        self, workspace_id: UUID, profile_id: UUID, limit: int, after: WorkCursor | None
    ) -> tuple[ProfileWorkRow, ...]: ...

    def read_original_run(
        self,
        workspace_id: UUID,
        kind: ProfileWorkKind,
        work_id: UUID,
        *,
        target_type: AssignmentKind | None = None,
    ) -> OriginalRunEvidence | None: ...


def _invalid() -> AgentError:
    return AgentError("configuration_invalid", "Invalid work read request.")


def _decode_cursor(after: str, workspace_id: UUID, profile_id: UUID) -> WorkCursor:
    if type(after) is not str or not after or len(after) > 2048:
        raise _invalid()
    try:
        raw = base64.b64decode(after.encode("ascii"), altchars=b"-_", validate=True)
        cursor = WorkCursor.model_validate_json(raw)
    except (ValueError, UnicodeError, ValidationError, Base64Error):
        raise _invalid() from None
    if cursor.workspace_id != workspace_id or cursor.profile_id != profile_id:
        raise _invalid()
    return cursor


class AssignmentQueryService:
    def __init__(
        self,
        *,
        store: AssignmentReadStore,
        execution: AgentExecution,
        authorizer: ControlPlaneRequestAuthorizer,
        identity: ControlPlaneIdentity,
        principal_id: str,
        workspace_id: UUID,
    ) -> None:
        self._store, self._execution, self._authorizer = store, execution, authorizer
        self._identity, self._principal_id, self._workspace_id = (
            identity,
            principal_id,
            workspace_id,
        )

    def _authorize(
        self,
        workspace_id: UUID,
        target: AssignmentTarget | ProfileWorkTarget | ProfileWorkRunTarget,
    ) -> None:
        identity = self._identity
        if (
            type(workspace_id) is not UUID
            or workspace_id != self._workspace_id
            or identity.actor_type != "user"
            or identity.principal_id != self._principal_id
            or type(identity.grant_id) is not UUID
            or self._authorizer.identity_for_grant(identity.grant_id) != identity
        ):
            raise AgentError("permission_denied", "Work read denied.")
        self._authorizer.require(identity.grant_id, "assignment.read", target)

    def read_assignment(
        self, workspace_id: UUID, target_type: AssignmentKind, target_id: UUID
    ) -> AgentBindingView | None:
        if target_type not in {"subtask", "worker_item"} or type(target_id) is not UUID:
            raise _invalid()
        self._authorize(
            workspace_id,
            AssignmentTarget(
                workspace_id, target_type, target_id, self._identity.principal_id
            ),
        )
        return self._store.read_assignment(workspace_id, target_type, target_id)

    def read_agent_execution(
        self, workspace_id: UUID, execution_id: UUID
    ) -> AgentExecutionView:
        if type(execution_id) is not UUID:
            raise _invalid()
        self._authorize(
            workspace_id,
            AssignmentTarget(
                workspace_id, "execution", execution_id, self._identity.principal_id
            ),
        )
        view = self._store.read_execution(workspace_id, execution_id)
        if view is None:
            raise AgentError("not_found", "Execution is unavailable.")
        kind, target_id = (
            ("worker_item", view.worker_item_id)
            if view.worker_item_id is not None
            else ("subtask", view.subtask_id)
        )
        self._authorize(
            workspace_id,
            AssignmentTarget(
                workspace_id, kind, target_id, self._identity.principal_id
            ),
        )
        if view.workspace_id != workspace_id or view.execution_id != execution_id:
            raise AgentError("query_unavailable", "Execution evidence is unavailable.")
        return view

    def list_profile_work(
        self,
        workspace_id: UUID,
        profile_id: UUID,
        *,
        after: str | None = None,
        limit: int = 50,
    ) -> ProfileWorkPage:
        if (
            type(profile_id) is not UUID
            or type(limit) is not int
            or not 1 <= limit <= 100
        ):
            raise _invalid()
        self._authorize(
            workspace_id,
            ProfileWorkTarget(workspace_id, profile_id, self._identity.principal_id),
        )
        cursor = (
            None if after is None else _decode_cursor(after, workspace_id, profile_id)
        )
        rows = self._store.list_profile_work(
            workspace_id, profile_id, limit + 1, cursor
        )
        if len(rows) > limit + 1 or any(
            row.workspace_id != workspace_id
            or row.profile_ref.workspace_id != workspace_id
            or row.profile_ref.profile_id != profile_id
            for row in rows
        ):
            raise AgentError("query_unavailable", "Work evidence is unavailable.")
        items, next_cursor = rows[:limit], None
        if len(rows) > limit:
            last = items[-1]
            next_cursor = base64.urlsafe_b64encode(
                WorkCursor(
                    workspace_id=workspace_id,
                    profile_id=profile_id,
                    updated_at=last.updated_at,
                    kind=last.kind,
                    work_id=last.work_id,
                    target_type=last.target_type or "",
                )
                .model_dump_json()
                .encode()
            ).decode("ascii")
        return ProfileWorkPage(items=items, next_cursor=next_cursor)

    def read_profile_work_run(
        self,
        workspace_id: UUID,
        kind: ProfileWorkKind,
        work_id: UUID,
        *,
        target_type: AssignmentKind | None = None,
    ) -> NativeRunSummary:
        if (
            kind not in {"assignment", "execution", "planning"}
            or type(work_id) is not UUID
            or (
                target_type not in {"subtask", "worker_item"}
                if kind == "assignment"
                else target_type is not None
            )
        ):
            raise _invalid()
        self._authorize(
            workspace_id,
            ProfileWorkRunTarget(
                workspace_id, kind, work_id, self._identity.principal_id, target_type
            ),
        )
        if kind == "execution":
            self._authorize(
                workspace_id,
                AssignmentTarget(
                    workspace_id, "execution", work_id, self._identity.principal_id
                ),
            )
        elif target_type is not None:
            self._authorize(
                workspace_id,
                AssignmentTarget(
                    workspace_id, target_type, work_id, self._identity.principal_id
                ),
            )
        evidence = self._store.read_original_run(
            workspace_id, kind, work_id, target_type=target_type
        )
        if (
            evidence is not None
            and evidence.target_type is not None
            and evidence.target_id is not None
        ):
            self._authorize(
                workspace_id,
                AssignmentTarget(
                    workspace_id,
                    evidence.target_type,
                    evidence.target_id,
                    self._identity.principal_id,
                ),
            )
        run_id = None if evidence is None else evidence.run_id
        if run_id is None:
            return NativeRunSummary(
                workspace_id=workspace_id,
                kind=kind,
                work_id=work_id,
                run_id=None,
                available=False,
                reason_code=evidence.reason_code
                if evidence is not None and evidence.reason_code is not None
                else "run_not_admitted",
            )
        if (
            evidence is None
            or not evidence.verified
            or evidence.workspace_id != workspace_id
            or evidence.profile_ref is None
            or evidence.profile_ref.workspace_id != workspace_id
            or evidence.grant_id is None
        ):
            return NativeRunSummary(
                workspace_id=workspace_id,
                kind=kind,
                work_id=work_id,
                run_id=run_id,
                available=False,
                reason_code="native_run_unavailable",
            )
        if kind == "planning":
            self._authorize(
                workspace_id,
                ProfileWorkTarget(
                    workspace_id,
                    evidence.profile_ref.profile_id,
                    self._identity.principal_id,
                ),
            )
        try:
            run = self._execution.get_run(
                workspace_id, run_id, grant_id=evidence.grant_id
            )
        except AgentError:
            return NativeRunSummary(
                workspace_id=workspace_id,
                kind=kind,
                work_id=work_id,
                run_id=run_id,
                available=False,
                reason_code="native_run_unavailable",
            )
        if (
            not isinstance(run, RunSnapshot)
            or run.workspace_id != workspace_id
            or run.run_id != run_id
            or run.profile_ref != evidence.profile_ref
            or run.effective_config is None
            or run.effective_config.profile_ref != evidence.profile_ref
        ):
            return NativeRunSummary(
                workspace_id=workspace_id,
                kind=kind,
                work_id=work_id,
                run_id=run_id,
                available=False,
                reason_code="native_run_mismatch",
            )
        effective = run.effective_config
        return NativeRunSummary(
            workspace_id=workspace_id,
            kind=kind,
            work_id=work_id,
            run_id=run_id,
            available=True,
            reason_code=None,
            run=NativeRunView(
                state=run.state,
                input_open=run.input_open,
                profile_ref=run.profile_ref,
                config_hash=effective.config_hash,
                runtime_ref=effective.runtime_ref,
                actual_runtime_version=effective.actual_runtime_version,
                selected_model=effective.selected_model,
                observed_model=effective.observed_model,
                applied_fields=effective.applied_fields,
                reason_code=run.reason_code,
            ),
        )
