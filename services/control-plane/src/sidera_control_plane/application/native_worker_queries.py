from __future__ import annotations

from dataclasses import dataclass
from typing import Literal
from uuid import NAMESPACE_URL, UUID, uuid5

from pydantic import ValidationError
from sidera_agents.api import AgentExecution, AgentObservations
from sidera_agents.contracts import EventPage, ProfileRef, RunEvent, RunSnapshot
from sidera_agents.errors import AgentError
from sidera_agents.ports import ExecutionAuthorizer

from sidera_control_plane.application.assignment_queries import AssignmentReadStore
from sidera_control_plane.application.assignments import AssignmentTarget
from sidera_control_plane.application.request_authority import (
    ControlPlaneIdentity,
    ControlPlaneRequestAuthorizer,
)
from sidera_control_plane.application.worker_workspace_queries import (
    WorkerWorkspaceQueryService,
    WorkerWorkspaceQuerySnapshot,
    WorkerWorkspaceQueryStore,
)
from sidera_control_plane.domain.assignment_views import (
    AgentBindingView,
    AgentExecutionView,
    NativeRunView,
    OriginalRunEvidence,
)
from sidera_control_plane.domain.native_worker_views import (
    NativeWorkerContextAddress,
    NativeWorkerContextView,
    NativeWorkerTraceView,
)
from sidera_control_plane.domain.worker_item_projections import (
    BindingFact,
    ExecutionFact,
    SessionFact,
    WorkerWorkspaceProjection,
)


def _invalid() -> AgentError:
    return AgentError("configuration_invalid", "Invalid native worker read request.")


def _unavailable() -> AgentError:
    return AgentError("query_unavailable", "Native worker reads are unavailable.")


@dataclass(frozen=True, slots=True)
class _WorkspaceFacts:
    snapshot: WorkerWorkspaceQuerySnapshot
    projection: WorkerWorkspaceProjection
    execution: ExecutionFact | None
    session: SessionFact | None
    binding: BindingFact | None


class NativeWorkerQueryService:
    def __init__(
        self,
        *,
        workspace_store: WorkerWorkspaceQueryStore,
        assignment_store: AssignmentReadStore,
        execution: AgentExecution,
        observations: AgentObservations | None,
        input_authorizer: ExecutionAuthorizer,
        authorizer: ControlPlaneRequestAuthorizer,
        identity: ControlPlaneIdentity,
        principal_id: str,
        workspace_id: UUID,
    ) -> None:
        self._workspace_store = workspace_store
        self._assignment_store = assignment_store
        self._execution = execution
        self._observations = observations
        self._input_authorizer = input_authorizer
        self._authorizer = authorizer
        self._identity = identity
        self._principal_id = principal_id
        self._workspace_id = workspace_id

    def read_context(
        self, workspace_id: UUID, worker_item_id: UUID
    ) -> NativeWorkerContextView:
        self._validate_scope(workspace_id, worker_item_id)
        self._require_item(workspace_id, worker_item_id)
        facts = self._load_workspace(workspace_id, worker_item_id)
        binding: AgentBindingView | None
        execution_view: AgentExecutionView | None = None
        evidence: OriginalRunEvidence | None = None

        if facts.execution is None:
            binding = self._read_assignment(workspace_id, worker_item_id)
            mode: Literal["native_profile", "legacy", "unassigned"] = (
                "native_profile"
                if binding is not None and binding.profile_ref is not None
                else "unassigned"
            )
            return NativeWorkerContextView(
                workspace_id=workspace_id,
                worker_item_id=worker_item_id,
                execution_mode=mode,
                worker_item_version=facts.snapshot.worker_item.version,
                binding=binding,
                execution=None,
                execution_version=None,
                context=None,
                input_enabled=False,
                read_only_reason=facts.projection.read_only_reason,
                run=None,
                as_of=facts.projection.as_of,
            )

        execution_id = facts.execution.id
        self._require_execution(workspace_id, execution_id)
        binding = self._read_assignment(workspace_id, worker_item_id)
        execution_view = self._read_execution(workspace_id, execution_id)
        evidence = self._read_original(workspace_id, execution_id)
        if not self._execution_matches_facts(
            execution_view, facts.execution, worker_item_id, workspace_id
        ):
            raise _unavailable()

        if not self._native_mode(facts, binding, execution_view, evidence):
            return NativeWorkerContextView(
                workspace_id=workspace_id,
                worker_item_id=worker_item_id,
                execution_mode="legacy",
                worker_item_version=facts.snapshot.worker_item.version,
                binding=binding,
                execution=execution_view,
                execution_version=facts.execution.version,
                context=None,
                input_enabled=False,
                read_only_reason="execution_mode_legacy",
                run=None,
                as_of=facts.projection.as_of,
            )

        if evidence is not None and self._known_unadmitted(
            evidence, execution_view, workspace_id, worker_item_id
        ):
            return NativeWorkerContextView(
                workspace_id=workspace_id,
                worker_item_id=worker_item_id,
                execution_mode="native_profile",
                worker_item_version=facts.snapshot.worker_item.version,
                binding=binding,
                execution=execution_view,
                execution_version=facts.execution.version,
                context=None,
                input_enabled=False,
                read_only_reason="run_not_admitted",
                run=None,
                as_of=facts.projection.as_of,
            )

        if not self._verified_native_link(
            evidence, execution_view, workspace_id, worker_item_id
        ):
            return self._native_context_result(
                workspace_id,
                worker_item_id,
                facts,
                binding,
                execution_view,
                None,
                None,
                "native_run_mismatch",
            )

        assert evidence is not None
        assert execution_view.profile_ref is not None
        assert execution_view.run_id is not None
        assert execution_view.binding_version is not None
        address, mismatch = self._native_address(
            facts, evidence, execution_view, worker_item_id
        )
        if mismatch or address is None:
            reason = (
                facts.projection.read_only_reason
                if facts.session is None
                and facts.projection.read_only_reason is not None
                else "native_context_mismatch"
            )
            return self._native_context_result(
                workspace_id,
                worker_item_id,
                facts,
                binding,
                execution_view,
                None,
                None,
                reason,
            )

        pin_matches = self._current_binding_matches(binding, execution_view)
        stale_before_read = not pin_matches
        canonical_reason = facts.projection.read_only_reason
        latest_read_only_reason = canonical_reason
        input_candidate = canonical_reason is None and pin_matches
        input_denied = False
        if input_candidate:
            input_denied = not self._require_original_input(evidence, workspace_id)

        observation_failed = False
        try:
            run_snapshot = self._execution.get_run(
                workspace_id, evidence.run_id, grant_id=evidence.grant_id
            )
        except Exception:  # noqa: BLE001 - Original Run failures stay private.
            observation_failed = True
            run_snapshot = None

        post_input_denied = False
        if input_candidate:
            post_input_denied = not self._require_original_input(evidence, workspace_id)

        current_binding = binding
        changed_link = False
        stale = stale_before_read
        try:
            self._require_item(workspace_id, worker_item_id)
            latest = self._load_workspace(workspace_id, worker_item_id)
            latest_read_only_reason = latest.projection.read_only_reason
            self._require_execution(workspace_id, execution_id)
            current_binding = self._read_assignment(workspace_id, worker_item_id)
            current_execution = self._read_execution(workspace_id, execution_id)
            current_evidence = self._read_original(workspace_id, execution_id)
            changed_link = not self._same_original_link(
                execution_view, evidence, current_execution, current_evidence
            )
            stale = stale or not self._same_workspace_pins(facts, latest)
            latest_address, latest_mismatch = self._native_address(
                latest, evidence, execution_view, worker_item_id
            )
            stale = stale or latest_mismatch or latest_address != address
            stale = stale or not self._current_binding_matches(
                current_binding, execution_view
            )
        except AgentError as error:
            if error.code == "permission_denied":
                raise
            stale = True
        except Exception:  # noqa: BLE001 - Revalidation failure disables input.
            stale = True

        if changed_link:
            return self._native_context_result(
                workspace_id,
                worker_item_id,
                facts,
                current_binding,
                execution_view,
                None,
                None,
                "native_run_mismatch",
            )

        if observation_failed:
            reason = latest_read_only_reason or canonical_reason
            if reason is None:
                reason = (
                    "stale_execution_binding" if stale else "native_run_unavailable"
                )
            return self._native_context_result(
                workspace_id,
                worker_item_id,
                facts,
                current_binding,
                execution_view,
                address,
                None,
                reason,
            )

        run_view = self._run_view(
            run_snapshot, workspace_id, evidence.run_id, evidence.profile_ref
        )
        if run_view is None:
            return self._native_context_result(
                workspace_id,
                worker_item_id,
                facts,
                current_binding,
                execution_view,
                None,
                None,
                "native_run_mismatch",
            )

        readonly_reason = latest_read_only_reason or canonical_reason
        input_enabled = False
        if readonly_reason is None:
            if stale:
                readonly_reason = "stale_execution_binding"
            elif input_denied or post_input_denied:
                readonly_reason = "native_input_denied"
            elif not run_view.input_open or run_view.state not in {"running", "idle"}:
                readonly_reason = "native_input_closed"
            else:
                input_enabled = True

        return NativeWorkerContextView(
            workspace_id=workspace_id,
            worker_item_id=worker_item_id,
            execution_mode="native_profile",
            worker_item_version=facts.snapshot.worker_item.version,
            binding=current_binding,
            execution=execution_view,
            execution_version=facts.execution.version,
            context=address,
            input_enabled=input_enabled,
            read_only_reason=readonly_reason,
            run=run_view,
            as_of=facts.projection.as_of,
        )

    def read_trace(
        self,
        workspace_id: UUID,
        worker_item_id: UUID,
        execution_id: UUID,
        *,
        after_sequence: int = 0,
        limit: int = 100,
    ) -> NativeWorkerTraceView:
        self._validate_scope(workspace_id, worker_item_id)
        if type(execution_id) is not UUID:
            raise _invalid()
        if (
            type(after_sequence) is not int
            or after_sequence < 0
            or type(limit) is not int
            or not 1 <= limit <= 500
        ):
            raise _invalid()
        self._require_item(workspace_id, worker_item_id)
        if self._observations is None:
            raise _unavailable()
        self._require_execution(workspace_id, execution_id)
        execution = self._read_execution(workspace_id, execution_id)
        if execution is None:
            raise AgentError("not_found", "Execution is unavailable.")
        if (
            execution.workspace_id != workspace_id
            or execution.execution_id != execution_id
            or execution.worker_item_id != worker_item_id
        ):
            raise AgentError("not_found", "Execution is unavailable.")
        evidence = self._read_original(workspace_id, execution_id)
        if not self._verified_native_link(
            evidence, execution, workspace_id, worker_item_id
        ):
            raise _unavailable()
        assert evidence is not None
        assert evidence.run_id is not None
        assert evidence.profile_ref is not None
        assert evidence.grant_id is not None

        unsupported_trace = False
        page = None
        try:
            page = self._observations.read_trace(
                workspace_id,
                evidence.run_id,
                after_sequence,
                limit,
                grant_id=evidence.grant_id,
            )
        except AgentError as error:
            if error.code == "unsupported_capability":
                unsupported_trace = True
            else:
                raise _unavailable() from None
        except Exception:  # noqa: BLE001 - Hidden authority and adapter errors.
            raise _unavailable() from None
        if not unsupported_trace:
            self._validate_page(page, evidence.run_id, after_sequence, limit)

        self._require_item(workspace_id, worker_item_id)
        self._require_execution(workspace_id, execution_id)
        current_execution = self._read_execution(workspace_id, execution_id)
        current_evidence = self._read_original(workspace_id, execution_id)
        if not self._same_original_link(
            execution, evidence, current_execution, current_evidence
        ):
            raise _unavailable()
        if unsupported_trace:
            raise AgentError("native_trace_unsupported", "Native trace is unsupported.")

        return NativeWorkerTraceView(
            workspace_id=workspace_id,
            worker_item_id=worker_item_id,
            execution_id=execution_id,
            run_id=evidence.run_id,
            profile_ref=evidence.profile_ref,
            events=page.events,
            next_sequence=page.next_sequence,
            has_more=page.has_more,
        )

    def _validate_scope(self, workspace_id: UUID, worker_item_id: UUID) -> None:
        if type(workspace_id) is not UUID or type(worker_item_id) is not UUID:
            raise _invalid()
        if workspace_id != self._workspace_id:
            raise AgentError("permission_denied", "Native worker read denied.")

    def _require_item(self, workspace_id: UUID, worker_item_id: UUID) -> None:
        self._require_current_human(workspace_id)
        self._require_assignment(workspace_id, "worker_item", worker_item_id)

    def _require_execution(self, workspace_id: UUID, execution_id: UUID) -> None:
        self._require_current_human(workspace_id)
        self._require_assignment(workspace_id, "execution", execution_id)

    def _require_current_human(self, workspace_id: UUID) -> None:
        identity = self._identity
        if (
            not isinstance(identity, ControlPlaneIdentity)
            or type(identity.grant_id) is not UUID
            or identity.actor_type != "user"
            or not isinstance(identity.principal_id, str)
            or not identity.principal_id.strip()
            or identity.principal_id != self._principal_id
            or workspace_id != self._workspace_id
        ):
            raise AgentError("permission_denied", "Native worker read denied.")
        try:
            current = self._authorizer.identity_for_grant(identity.grant_id)
        except AgentError as error:
            if error.code == "permission_denied":
                raise
            raise _unavailable() from None
        except Exception:  # noqa: BLE001 - Failed grant lookup is private.
            raise _unavailable() from None
        if current != identity:
            raise AgentError("permission_denied", "Native worker read denied.")

    def _require_assignment(
        self, workspace_id: UUID, target_type: str, target_id: UUID
    ) -> None:
        target = AssignmentTarget(
            workspace_id, target_type, target_id, self._principal_id
        )
        try:
            self._authorizer.require(self._identity.grant_id, "assignment.read", target)
        except AgentError:
            raise
        except Exception:  # noqa: BLE001 - Do not expose authorizer details.
            raise _unavailable() from None

    def _load_workspace(
        self, workspace_id: UUID, worker_item_id: UUID
    ) -> _WorkspaceFacts:
        try:
            snapshot = self._workspace_store.load(workspace_id, worker_item_id)
        except AgentError:
            raise
        except Exception:  # noqa: BLE001 - Do not expose store details.
            raise _unavailable() from None
        if snapshot is None:
            raise AgentError("not_found", "WorkerItem is unavailable.")
        if (
            snapshot.worker_item.workspace_id != workspace_id
            or snapshot.worker_item.id != worker_item_id
        ):
            raise _unavailable()
        try:
            projection = (
                WorkerWorkspaceQueryService(None)
                ._composer(snapshot)
                .compose_workspace()
            )
        except Exception:  # noqa: BLE001 - Canonical projection is authoritative.
            raise _unavailable() from None

        runtime = projection.current_runtime
        execution_id = None if runtime is None else runtime.execution_id
        execution_matches = tuple(
            item for item in snapshot.executions if item.id == execution_id
        )
        if execution_id is None:
            execution = None
        elif len(execution_matches) == 1:
            execution = execution_matches[0]
        else:
            raise _unavailable()

        session_id = None if runtime is None else runtime.session_id
        sessions = tuple(
            item
            for item in snapshot.sessions
            if item.id == session_id and item.execution_id == execution_id
        )
        if session_id is not None and len(sessions) != 1:
            raise _unavailable()
        session = sessions[0] if sessions else None
        bindings = tuple(
            item for item in snapshot.bindings if item.execution_id == execution_id
        )
        if execution_id is not None and len(bindings) > 1:
            raise _unavailable()
        return _WorkspaceFacts(
            snapshot,
            projection,
            execution,
            session,
            bindings[0] if bindings else None,
        )

    def _read_assignment(
        self, workspace_id: UUID, worker_item_id: UUID
    ) -> AgentBindingView | None:
        try:
            binding = self._assignment_store.read_assignment(
                workspace_id, "worker_item", worker_item_id
            )
        except AgentError:
            raise
        except Exception:  # noqa: BLE001 - Do not expose store details.
            raise _unavailable() from None
        if binding is not None and (
            binding.workspace_id != workspace_id
            or binding.target_type != "worker_item"
            or binding.target_id != worker_item_id
        ):
            raise _unavailable()
        return binding

    def _read_execution(
        self, workspace_id: UUID, execution_id: UUID
    ) -> AgentExecutionView | None:
        try:
            return self._assignment_store.read_execution(workspace_id, execution_id)
        except AgentError:
            raise
        except Exception:  # noqa: BLE001 - Do not expose store details.
            raise _unavailable() from None

    def _read_original(
        self, workspace_id: UUID, execution_id: UUID
    ) -> OriginalRunEvidence | None:
        try:
            return self._assignment_store.read_original_run(
                workspace_id, "execution", execution_id
            )
        except AgentError:
            raise
        except Exception:  # noqa: BLE001 - Do not expose store details.
            raise _unavailable() from None

    @staticmethod
    def _execution_matches_facts(
        execution: AgentExecutionView | None,
        fact: ExecutionFact,
        worker_item_id: UUID,
        workspace_id: UUID,
    ) -> bool:
        return bool(
            execution is not None
            and execution.workspace_id == workspace_id
            and execution.execution_id == fact.id
            and execution.worker_item_id == worker_item_id
            and fact.worker_item_id == worker_item_id
            and execution.subtask_id == fact.task_id
        )

    @staticmethod
    def _native_mode(
        facts: _WorkspaceFacts,
        binding: AgentBindingView | None,
        execution: AgentExecutionView,
        evidence: OriginalRunEvidence | None,
    ) -> bool:
        session_id = None if facts.session is None else facts.session.adapter_session_id
        return bool(
            facts.execution is not None
            and (
                facts.execution.native_binding_version is not None
                or (session_id is not None and session_id.startswith("agent-run:"))
                or execution.profile_ref is not None
                or execution.binding_version is not None
                or execution.run_id is not None
                or (binding is not None and binding.profile_ref is not None)
                or (
                    evidence is not None
                    and (
                        evidence.profile_ref is not None
                        or evidence.run_id is not None
                        or evidence.grant_id is not None
                    )
                )
            )
        )

    @staticmethod
    def _known_unadmitted(
        evidence: OriginalRunEvidence,
        execution: AgentExecutionView,
        workspace_id: UUID,
        worker_item_id: UUID,
    ) -> bool:
        return bool(
            evidence.reason_code == "run_not_admitted"
            and not evidence.verified
            and evidence.workspace_id == workspace_id
            and evidence.target_type == "worker_item"
            and evidence.target_id == worker_item_id
            and evidence.run_id is None
            and execution.run_id is None
        )

    @staticmethod
    def _verified_native_link(
        evidence: OriginalRunEvidence | None,
        execution: AgentExecutionView,
        workspace_id: UUID,
        worker_item_id: UUID,
    ) -> bool:
        return bool(
            evidence is not None
            and evidence.verified
            and evidence.workspace_id == workspace_id
            and evidence.target_type == "worker_item"
            and evidence.target_id == worker_item_id
            and evidence.run_id is not None
            and evidence.run_id == execution.run_id
            and evidence.profile_ref is not None
            and evidence.profile_ref == execution.profile_ref
            and evidence.profile_ref.workspace_id == workspace_id
            and evidence.grant_id is not None
            and execution.workspace_id == workspace_id
            and execution.worker_item_id == worker_item_id
            and execution.binding_version is not None
            and execution.binding_version >= 1
            and execution.profile_ref is not None
        )

    def _native_address(
        self,
        facts: _WorkspaceFacts,
        evidence: OriginalRunEvidence,
        execution: AgentExecutionView,
        worker_item_id: UUID,
    ) -> tuple[NativeWorkerContextAddress | None, bool]:
        context = facts.projection.intervention_context
        runtime = facts.projection.current_runtime
        selected = facts.execution
        session = facts.session
        binding = facts.binding
        if (
            context is None
            or runtime is None
            or selected is None
            or session is None
            or binding is None
            or execution.profile_ref is None
            or execution.binding_version is None
            or execution.run_id is None
            or evidence.run_id is None
        ):
            return None, True

        worker_ids = (
            selected.assigned_worker_id,
            session.worker_id,
            binding.worker_id,
        )
        if (
            any(worker_id is None for worker_id in worker_ids)
            or len(set(worker_ids)) != 1
        ):
            return None, True
        worker_id = worker_ids[0]
        assert worker_id is not None
        if not any(worker.id == worker_id for worker in facts.snapshot.workers):
            return None, True

        placements = tuple(
            placement
            for placement in facts.snapshot.placements
            if placement.plan_snapshot_id
            == facts.projection.current_placement.plan_snapshot_id
            and placement.plan_generation
            == facts.projection.current_placement.plan_generation
        )
        if (
            len(placements) != 1
            or selected.worker_item_id != worker_item_id
            or session.execution_id != selected.id
            or runtime.execution_id != selected.id
            or runtime.session_id != session.id
            or runtime.session_version != session.version
            or runtime.worker_id != worker_id
            or context.worker_item_id != worker_item_id
            or context.execution_id != selected.id
            or context.session_id != session.id
            or context.session_version != session.version
            or context.worker_id != worker_id
            or context.plan_generation != placements[0].plan_generation
            or context.context_version != facts.snapshot.worker_item.context_version
            or selected.native_binding_version != execution.binding_version
            or session.id
            != uuid5(NAMESPACE_URL, f"sidera.native.session:{evidence.run_id}")
            or session.adapter_session_id != f"agent-run:{evidence.run_id}"
        ):
            return None, True

        try:
            address = NativeWorkerContextAddress(
                worker_item_id=context.worker_item_id,
                plan_generation=context.plan_generation,
                context_version=context.context_version,
                execution_id=context.execution_id,
                session_id=context.session_id,
                session_version=context.session_version,
                worker_id=context.worker_id,
                execution_mode="native_profile",
                binding_version=execution.binding_version,
                run_id=evidence.run_id,
            )
        except ValidationError:
            return None, True
        return address, False

    @staticmethod
    def _current_binding_matches(
        binding: AgentBindingView | None, execution: AgentExecutionView
    ) -> bool:
        return bool(
            binding is not None
            and binding.runnable
            and binding.assignee.kind == "profile"
            and execution.profile_ref is not None
            and binding.profile_ref == execution.profile_ref
            and binding.version == execution.binding_version
            and binding.assignee.id == str(execution.profile_ref.profile_id)
        )

    def _require_original_input(
        self, evidence: OriginalRunEvidence, workspace_id: UUID
    ) -> bool:
        try:
            self._input_authorizer.require(evidence.grant_id, "run.input", workspace_id)
            return True
        except Exception:  # noqa: BLE001 - Original authority remains internal.
            return False

    @staticmethod
    def _run_view(
        run: RunSnapshot,
        workspace_id: UUID,
        run_id: UUID,
        profile_ref: ProfileRef,
    ) -> NativeRunView | None:
        if (
            not isinstance(run, RunSnapshot)
            or run.workspace_id != workspace_id
            or run.run_id != run_id
            or run.profile_ref != profile_ref
            or run.effective_config.profile_ref != profile_ref
            or run.effective_config.runtime_ref.workspace_id != workspace_id
        ):
            return None
        try:
            return NativeRunView(
                state=run.state,
                input_open=run.input_open,
                profile_ref=run.profile_ref,
                config_hash=run.effective_config.config_hash,
                runtime_ref=run.effective_config.runtime_ref,
                actual_runtime_version=run.effective_config.actual_runtime_version,
                selected_model=run.effective_config.selected_model,
                observed_model=run.effective_config.observed_model,
                applied_fields=run.effective_config.applied_fields,
                reason_code=run.reason_code,
            )
        except ValidationError:
            return None

    @staticmethod
    def _native_context_result(
        workspace_id: UUID,
        worker_item_id: UUID,
        facts: _WorkspaceFacts,
        binding: AgentBindingView | None,
        execution: AgentExecutionView,
        context: NativeWorkerContextAddress | None,
        run: NativeRunView | None,
        reason: str,
    ) -> NativeWorkerContextView:
        return NativeWorkerContextView(
            workspace_id=workspace_id,
            worker_item_id=worker_item_id,
            execution_mode="native_profile",
            worker_item_version=facts.snapshot.worker_item.version,
            binding=binding,
            execution=execution,
            execution_version=facts.execution.version
            if facts.execution is not None
            else None,
            context=context,
            input_enabled=False,
            read_only_reason=reason,
            run=run,
            as_of=facts.projection.as_of,
        )

    @staticmethod
    def _same_original_link(
        original_execution: AgentExecutionView,
        original_evidence: OriginalRunEvidence,
        current_execution: AgentExecutionView | None,
        current_evidence: OriginalRunEvidence | None,
    ) -> bool:
        return bool(
            current_execution is not None
            and current_evidence is not None
            and current_execution.workspace_id == original_execution.workspace_id
            and current_execution.execution_id == original_execution.execution_id
            and current_execution.worker_item_id == original_execution.worker_item_id
            and current_execution.profile_ref == original_execution.profile_ref
            and current_execution.binding_version == original_execution.binding_version
            and current_execution.run_id == original_execution.run_id
            and current_evidence.workspace_id == original_evidence.workspace_id
            and current_evidence.run_id == original_evidence.run_id
            and current_evidence.profile_ref == original_evidence.profile_ref
            and current_evidence.operation_id == original_evidence.operation_id
            and current_evidence.grant_id == original_evidence.grant_id
            and current_evidence.target_type == original_evidence.target_type
            and current_evidence.target_id == original_evidence.target_id
            and current_evidence.verified == original_evidence.verified
        )

    @staticmethod
    def _same_workspace_pins(before: _WorkspaceFacts, after: _WorkspaceFacts) -> bool:
        if before.execution is None or after.execution is None:
            return before.execution is after.execution
        old_runtime = before.projection.current_runtime
        new_runtime = after.projection.current_runtime
        old_session, new_session = before.session, after.session
        old_binding, new_binding = before.binding, after.binding
        old_placement = before.projection.current_placement
        new_placement = after.projection.current_placement
        return bool(
            before.snapshot.worker_item.id == after.snapshot.worker_item.id
            and before.snapshot.worker_item.version
            == after.snapshot.worker_item.version
            and before.snapshot.worker_item.context_version
            == after.snapshot.worker_item.context_version
            and before.snapshot.worker_item.acknowledged_context_version
            == after.snapshot.worker_item.acknowledged_context_version
            and old_placement.plan_snapshot_id == new_placement.plan_snapshot_id
            and old_placement.plan_generation == new_placement.plan_generation
            and before.execution.id == after.execution.id
            and before.execution.task_id == after.execution.task_id
            and before.execution.worker_item_id == after.execution.worker_item_id
            and before.execution.version == after.execution.version
            and before.execution.status == after.execution.status
            and before.execution.assigned_worker_id
            == after.execution.assigned_worker_id
            and before.execution.pinned_worker_id == after.execution.pinned_worker_id
            and before.execution.active_lease_id == after.execution.active_lease_id
            and before.execution.native_binding_version
            == after.execution.native_binding_version
            and old_runtime is not None
            and new_runtime is not None
            and old_runtime.execution_id == new_runtime.execution_id
            and old_runtime.worker_id == new_runtime.worker_id
            and old_runtime.lease_id == new_runtime.lease_id
            and old_runtime.fencing_token == new_runtime.fencing_token
            and old_session is not None
            and new_session is not None
            and old_session.id == new_session.id
            and old_session.execution_id == new_session.execution_id
            and old_session.version == new_session.version
            and old_session.worker_id == new_session.worker_id
            and old_session.adapter_session_id == new_session.adapter_session_id
            and old_session.status == new_session.status
            and old_session.input_mode == new_session.input_mode
            and old_binding is not None
            and new_binding is not None
            and old_binding.id == new_binding.id
            and old_binding.worker_id == new_binding.worker_id
            and old_binding.version == new_binding.version
            and old_binding.lease_id == new_binding.lease_id
            and old_binding.fencing_token == new_binding.fencing_token
        )

    @staticmethod
    def _validate_page(
        page: EventPage, run_id: UUID, after_sequence: int, limit: int
    ) -> None:
        if (
            not isinstance(page, EventPage)
            or type(page.has_more) is not bool
            or len(page.events) > limit
            or (page.has_more and len(page.events) != limit)
        ):
            raise _unavailable()
        previous = after_sequence
        for event in page.events:
            if (
                not isinstance(event, RunEvent)
                or event.run_id != run_id
                or event.sequence <= previous
                or "_native" in event.data
            ):
                raise _unavailable()
            previous = event.sequence
        expected_cursor = page.events[-1].sequence if page.events else after_sequence
        if page.next_sequence != expected_cursor:
            raise _unavailable()
