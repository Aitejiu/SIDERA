from __future__ import annotations

from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from uuid import UUID, uuid4

from sidera_control_plane.application.errors import NotFound
from sidera_control_plane.application.ports import ControlPlaneUnitOfWork
from sidera_control_plane.domain.commands import CommandEnvelope, CommandResult
from sidera_control_plane.domain.errors import (
    OperatorRequired,
    ValidationError,
    VersionConflict,
)
from sidera_control_plane.domain.events import EventData
from sidera_control_plane.domain.modules import (
    PLANNER_ADAPTER_MODULE_TYPE,
    ModuleDefinition,
    ModuleInstance,
    ModuleLifecycle,
    PlannerAdapterCapabilityProfile,
)
from sidera_control_plane.domain.planning import (
    RUN_TERMINAL_STATUSES,
    PlanningBriefRevision,
    PlanningClarification,
    PlanningRequest,
    PlanningRun,
    PlanningRunLeaseStatus,
    PlanningRunStatus,
)
from sidera_control_plane.domain.planning_profiles import (
    InvalidOutputPolicy,
    PlanningBudgetPolicy,
    PlanningProfile,
    PlanningProfileStatus,
    PlanningRetryPolicy,
    UnknownOutcomePolicy,
)


class PlanningCommandHandler:
    command_types = frozenset(
        {
            "RegisterPlannerModule",
            "RegisterPlanningProfile",
            "SubmitPlanningRequest",
            "CancelPlanningRequest",
            "AnswerPlanningClarification",
            "RequestProposalRevision",
            "RejectPlanningProposal",
            "RetryBlockedPlanningRun",
        }
    )

    def __init__(
        self,
        *,
        local_actor_type: str,
        local_actor_id: str,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._local_actor_type = local_actor_type
        self._local_actor_id = local_actor_id
        self._clock = clock or (lambda: datetime.now(UTC))

    def handle(
        self, uow: ControlPlaneUnitOfWork, envelope: CommandEnvelope
    ) -> CommandResult:
        if envelope.command_type == "RegisterPlannerModule":
            return self._register_module(uow, envelope)
        if envelope.command_type == "RegisterPlanningProfile":
            return self._register_profile(uow, envelope)
        if envelope.command_type == "SubmitPlanningRequest":
            return self._submit_request(uow, envelope)
        if envelope.command_type == "CancelPlanningRequest":
            return self._cancel_request(uow, envelope)
        if envelope.command_type == "AnswerPlanningClarification":
            return self._answer_clarification(uow, envelope)
        if envelope.command_type == "RequestProposalRevision":
            return self._request_proposal_revision(uow, envelope)
        if envelope.command_type == "RejectPlanningProposal":
            return self._reject_proposal(uow, envelope)
        if envelope.command_type == "RetryBlockedPlanningRun":
            return self._retry_blocked_run(uow, envelope)
        raise ValidationError(f"unsupported planning command: {envelope.command_type}")

    def _register_module(
        self, uow: ControlPlaneUnitOfWork, envelope: CommandEnvelope
    ) -> CommandResult:
        self._require_operator(envelope)
        payload = envelope.payload
        config = _mapping(payload.get("config"), "config")
        _assert_no_secret_values(config)
        secret_refs = _string_tuple(payload.get("secret_refs", ()), "secret_refs")
        _validate_secret_refs(secret_refs)
        capabilities = _planner_capabilities(
            _mapping(payload.get("capabilities"), "capabilities")
        )
        definition = ModuleDefinition(
            id=uuid4(),
            workspace_id=envelope.workspace_id,
            module_type=PLANNER_ADAPTER_MODULE_TYPE,
            provider=_required_string(payload, "provider"),
            interface_version=_required_string(payload, "interface_version"),
            config_schema_version=_positive_int(payload, "config_schema_version"),
            capabilities=capabilities,
            lifecycle=ModuleLifecycle.ACTIVE,
        )
        if capabilities.interface_version != definition.interface_version:
            raise ValidationError(
                "planner capability interface_version must match module interface_version"
            )
        instance = ModuleInstance(
            id=uuid4(),
            workspace_id=envelope.workspace_id,
            definition_id=definition.id,
            instance_name=_required_string(payload, "instance_name"),
            config_version=1,
            config_namespace=_required_string(payload, "config_namespace"),
            config=config,
            secret_refs=secret_refs,
            lifecycle=ModuleLifecycle.ACTIVE,
        )
        uow.modules.add_definition(definition)
        uow.modules.add_instance(instance)
        definition_event = _event(
            envelope,
            aggregate_type="module_definition",
            aggregate_id=definition.id,
            event_type="PlannerModuleRegistered",
            payload={
                "module_type": definition.module_type,
                "provider": definition.provider,
                "interface_version": definition.interface_version,
                "config_schema_version": definition.config_schema_version,
            },
        )
        instance_event = _event(
            envelope,
            aggregate_type="module_instance",
            aggregate_id=instance.id,
            event_type="PlannerModuleInstanceRegistered",
            payload={
                "definition_id": str(definition.id),
                "instance_name": instance.instance_name,
                "config_version": instance.config_version,
            },
        )
        uow.events.append_with_outbox(definition_event)
        uow.events.append_with_outbox(instance_event)
        return CommandResult(
            command_id=envelope.id,
            aggregate_id=instance.id,
            aggregate_version=1,
            event_ids=(definition_event.id, instance_event.id),
            data={
                "module_definition_id": str(definition.id),
                "module_instance_id": str(instance.id),
            },
        )

    def _register_profile(
        self, uow: ControlPlaneUnitOfWork, envelope: CommandEnvelope
    ) -> CommandResult:
        self._require_operator(envelope)
        payload = envelope.payload
        instance_id = _uuid(payload, "planner_module_instance_id")
        instance = uow.modules.get_instance(instance_id, envelope.workspace_id)
        if instance is None:
            raise NotFound("planner module instance not found")
        definition = uow.modules.get_definition(
            instance.definition_id, envelope.workspace_id
        )
        if definition is None or definition.module_type != PLANNER_ADAPTER_MODULE_TYPE:
            raise ValidationError("planner module instance is not a planner adapter")
        if not isinstance(definition.capabilities, PlannerAdapterCapabilityProfile):
            raise ValidationError("planner module capabilities are invalid")
        profile = PlanningProfile(
            id=uuid4(),
            workspace_id=envelope.workspace_id,
            profile_key=_required_string(payload, "profile_key"),
            version=_positive_int(payload, "version"),
            display_name=_required_string(payload, "display_name"),
            planner_module_instance_id=instance.id,
            planner_module_config_version=_positive_int(
                payload, "planner_module_config_version"
            ),
            planner_interface_version=_required_string(
                payload, "planner_interface_version"
            ),
            prompt_policy_version=_required_string(payload, "prompt_policy_version"),
            system_policy=_mapping(payload.get("system_policy", {}), "system_policy"),
            proposal_schema_version=_positive_int(payload, "proposal_schema_version"),
            allowed_capabilities=_string_tuple(
                payload.get("allowed_capabilities", ()), "allowed_capabilities"
            ),
            adapter_preferences=_string_tuple(
                payload.get("adapter_preferences", ()), "adapter_preferences"
            ),
            adapter_forbidden=_string_tuple(
                payload.get("adapter_forbidden", ()), "adapter_forbidden"
            ),
            budget=_budget(payload.get("budget")),
            retry_policy=_retry_policy(payload.get("retry_policy")),
            default_priority=_bounded_int(payload, "default_priority", 0, 100),
            execution_max_attempts=_positive_int(
                payload, "execution_max_attempts"
            ),
            execution_budget_level=_required_string(
                payload, "execution_budget_level"
            ),
            user_selectable=bool(payload.get("user_selectable", True)),
            status=PlanningProfileStatus.ACTIVE,
            planner_secret_refs=_string_tuple(
                payload.get("planner_secret_refs", ()), "planner_secret_refs"
            ),
        )
        if profile.planner_interface_version != definition.interface_version:
            raise ValidationError("planning profile interface version does not match module")
        if profile.planner_module_config_version != instance.config_version:
            raise ValidationError("planning profile module config version does not match instance")
        _validate_secret_refs(profile.planner_secret_refs)
        profile.snapshot(definition.capabilities)
        uow.planning_profiles.add(profile)
        event = _event(
            envelope,
            aggregate_type="planning_profile",
            aggregate_id=profile.id,
            event_type="PlanningProfileRegistered",
            payload={
                "profile_key": profile.profile_key,
                "profile_version": profile.version,
                "planner_module_instance_id": str(profile.planner_module_instance_id),
                "proposal_schema_version": profile.proposal_schema_version,
                "user_selectable": profile.user_selectable,
            },
        )
        uow.events.append_with_outbox(event)
        return CommandResult(
            command_id=envelope.id,
            aggregate_id=profile.id,
            aggregate_version=profile.version,
            event_ids=(event.id,),
            data={
                "planning_profile_id": str(profile.id),
                "profile_version": profile.version,
            },
        )

    def _submit_request(
        self, uow: ControlPlaneUnitOfWork, envelope: CommandEnvelope
    ) -> CommandResult:
        payload = envelope.payload
        profile_id = _uuid(payload, "profile_id")
        profile_version = _positive_int(payload, "profile_version")
        profile = uow.planning_profiles.get(
            profile_id, envelope.workspace_id, version=profile_version
        )
        if profile is None:
            raise NotFound("planning profile not found")
        profile.assert_user_selectable()
        instance = uow.modules.get_instance(
            profile.planner_module_instance_id, envelope.workspace_id
        )
        if instance is None:
            raise ValidationError("planning profile module instance is unavailable")
        definition = uow.modules.get_definition(
            instance.definition_id, envelope.workspace_id
        )
        if definition is None or not isinstance(
            definition.capabilities, PlannerAdapterCapabilityProfile
        ):
            raise ValidationError("planning profile planner capability is unavailable")
        snapshot = profile.snapshot(
            definition.capabilities,
            module_instance_config_version=instance.config_version,
        )
        request = PlanningRequest.create(
            request_id=uuid4(),
            workspace_id=envelope.workspace_id,
            profile_snapshot=snapshot,
            objective=_required_string(payload, "objective"),
            constraints=_string_tuple(payload.get("constraints", ()), "constraints"),
            acceptance_criteria=_string_tuple(
                payload.get("acceptance_criteria", ()), "acceptance_criteria"
            ),
            context_refs=_string_tuple(
                payload.get("context_refs", ()), "context_refs"
            ),
            requested_outputs=_string_tuple(
                payload.get("requested_outputs", ()), "requested_outputs"
            ),
            source_type=_required_string(payload, "source_type"),
            source_ref=_optional_string(payload.get("source_ref")),
            actor=envelope.actor_id,
            correlation_id=envelope.correlation_id,
            run_id=uuid4(),
            now=self._clock(),
        )
        uow.planning_requests.add(request)
        event = _event(
            envelope,
            aggregate_type="planning_request",
            aggregate_id=request.id,
            aggregate_version=request.version,
            event_type="PlanningRequestSubmitted",
            payload={
                "planning_profile_id": str(profile.id),
                "planning_profile_version": profile.version,
                "brief_revision": request.current_brief_revision,
                "planning_run_id": str(request.current_run_id),
                "source_type": request.source_type,
                "source_ref": request.source_ref,
            },
        )
        uow.events.append_with_outbox(event)
        return CommandResult(
            command_id=envelope.id,
            aggregate_id=request.id,
            aggregate_version=request.version,
            event_ids=(event.id,),
            data={
                "planning_request_id": str(request.id),
                "planning_run_id": str(request.current_run_id),
                "brief_revision": request.current_brief_revision,
            },
        )

    def _cancel_request(
        self, uow: ControlPlaneUnitOfWork, envelope: CommandEnvelope
    ) -> CommandResult:
        if envelope.target_id is None or envelope.expected_version is None:
            raise ValidationError(
                "CancelPlanningRequest requires target_id and expected_version"
            )
        request = uow.planning_requests.get(
            envelope.target_id, envelope.workspace_id, for_update=True
        )
        if request is None:
            raise NotFound("planning request not found")
        if request.version != envelope.expected_version:
            raise VersionConflict(envelope.expected_version, request.version)
        previous_request_version = request.version
        previous_run_versions = {
            run.id: run.version
            for run in request.runs
            if run.status not in {
                PlanningRunStatus.SUCCEEDED,
                PlanningRunStatus.NEEDS_CLARIFICATION,
                PlanningRunStatus.FAILED,
                PlanningRunStatus.BLOCKED,
                PlanningRunStatus.CANCELLED,
            }
        }
        now = self._clock()
        request.cancel(now=now)
        uow.planning_requests.save(request, previous_request_version)
        for run in request.runs:
            previous_version = previous_run_versions.get(run.id)
            if previous_version is not None:
                uow.planning_runs.save(run, previous_version)
                if run.lease_id is not None:
                    lease = uow.planning_leases.get(
                        run.lease_id, envelope.workspace_id, for_update=True
                    )
                    if lease is not None and lease.status.value == "active":
                        lease.status = type(lease.status).EXPIRED
                        lease.released_at = now
                        lease.version += 1
                        uow.planning_leases.save(lease, lease.version - 1)
        event = _event(
            envelope,
            aggregate_type="planning_request",
            aggregate_id=request.id,
            aggregate_version=request.version,
            event_type="PlanningRequestCancelled",
            payload={"status": request.status.value},
        )
        uow.events.append_with_outbox(event)
        return CommandResult(
            command_id=envelope.id,
            aggregate_id=request.id,
            aggregate_version=request.version,
            event_ids=(event.id,),
            data={"planning_request_id": str(request.id), "status": request.status.value},
        )

    def _answer_clarification(
        self, uow: ControlPlaneUnitOfWork, envelope: CommandEnvelope
    ) -> CommandResult:
        request = self._request_for_mutation(uow, envelope)
        raw_answers = envelope.payload.get("answers")
        if not isinstance(raw_answers, (list, tuple)) or not raw_answers:
            raise ValidationError("answers must contain at least one clarification")

        pending = {
            item.id: item
            for item in request.clarifications
            if item.planning_run_id == request.current_run_id
            and item.blocking
            and item.answer is None
        }
        if not pending:
            raise ValidationError("planning request has no pending clarifications")

        answers: dict[UUID, str] = {}
        for raw_answer in raw_answers:
            item = _mapping(raw_answer, "answer")
            clarification_id = _uuid(item, "clarification_id")
            if clarification_id in answers:
                raise ValidationError("answers cannot contain duplicate clarifications")
            if clarification_id not in pending:
                raise ValidationError("answer does not reference a pending clarification")
            answer = _required_string(item, "answer")
            _validate_clarification_answer(pending[clarification_id], answer)
            answers[clarification_id] = answer
        if set(answers) != set(pending):
            raise ValidationError("all pending clarifications must be answered together")

        current_brief = _current_brief(request)
        feedback = _clarification_feedback(answers)
        brief = _next_brief(
            request,
            current_brief=current_brief,
            user_feedback=_append_feedback(current_brief.user_feedback, feedback),
            actor=envelope.actor_id,
            correlation_id=envelope.correlation_id,
            now=self._clock(),
        )
        previous_request_version = request.version
        previous_clarification_versions = {
            item.id: item.version for item in request.clarifications
        }
        request.answer_clarifications(
            run_id=request.current_run_id,
            answers=answers,
            brief=brief,
            new_run_id=uuid4(),
            actor=envelope.actor_id,
            now=brief.created_at,
        )
        self._persist_appended_revision(
            uow,
            request,
            brief=brief,
            previous_request_version=previous_request_version,
            previous_clarification_versions=previous_clarification_versions,
        )
        event = _event(
            envelope,
            aggregate_type="planning_request",
            aggregate_id=request.id,
            aggregate_version=request.version,
            event_type="PlanningClarificationAnswered",
            payload={
                "clarification_ids": [str(item) for item in answers],
                "brief_revision": brief.revision,
                "planning_run_id": str(request.current_run_id),
            },
        )
        uow.events.append_with_outbox(event)
        return CommandResult(
            command_id=envelope.id,
            aggregate_id=request.id,
            aggregate_version=request.version,
            event_ids=(event.id,),
            data={
                "planning_request_id": str(request.id),
                "planning_run_id": str(request.current_run_id),
                "brief_revision": brief.revision,
            },
        )

    def _request_proposal_revision(
        self, uow: ControlPlaneUnitOfWork, envelope: CommandEnvelope
    ) -> CommandResult:
        request = self._request_for_mutation(uow, envelope)
        self._assert_current_proposal(uow, request, envelope.payload)
        current_brief = _current_brief(request)
        feedback = _required_string(envelope.payload, "feedback")
        now = self._clock()
        brief = _next_brief(
            request,
            current_brief=current_brief,
            user_feedback=_append_feedback(current_brief.user_feedback, feedback),
            actor=envelope.actor_id,
            correlation_id=envelope.correlation_id,
            now=now,
        )
        previous_request_version = request.version
        request.request_revision(
            expected_proposal_revision=_positive_int(
                envelope.payload, "current_proposal_revision"
            ),
            expected_content_hash=_content_hash(
                _required_string(
                    envelope.payload, "current_proposal_content_hash"
                )
            ),
            feedback=feedback,
            brief=brief,
            new_run_id=uuid4(),
            actor=envelope.actor_id,
            now=now,
        )
        self._persist_appended_revision(
            uow,
            request,
            brief=brief,
            previous_request_version=previous_request_version,
        )
        event = _event(
            envelope,
            aggregate_type="planning_request",
            aggregate_id=request.id,
            aggregate_version=request.version,
            event_type="PlanningProposalRevisionRequested",
            payload={
                "previous_proposal_revision": _positive_int(
                    envelope.payload, "current_proposal_revision"
                ),
                "previous_proposal_content_hash": _content_hash(
                    _required_string(
                        envelope.payload, "current_proposal_content_hash"
                    )
                ),
                "brief_revision": brief.revision,
                "planning_run_id": str(request.current_run_id),
            },
        )
        uow.events.append_with_outbox(event)
        return CommandResult(
            command_id=envelope.id,
            aggregate_id=request.id,
            aggregate_version=request.version,
            event_ids=(event.id,),
            data={
                "planning_request_id": str(request.id),
                "planning_run_id": str(request.current_run_id),
                "brief_revision": brief.revision,
            },
        )

    def _reject_proposal(
        self, uow: ControlPlaneUnitOfWork, envelope: CommandEnvelope
    ) -> CommandResult:
        request = self._request_for_mutation(uow, envelope)
        self._assert_current_proposal(uow, request, envelope.payload)
        reason = _required_string(envelope.payload, "reason")
        previous_request_version = request.version
        previous_run_versions = _active_run_versions(request)
        request.reject(now=self._clock())
        uow.planning_requests.save(request, previous_request_version)
        self._persist_run_changes(uow, request, previous_run_versions)
        event = _event(
            envelope,
            aggregate_type="planning_request",
            aggregate_id=request.id,
            aggregate_version=request.version,
            event_type="PlanningProposalRejected",
            payload={
                "proposal_revision": _positive_int(
                    envelope.payload, "current_proposal_revision"
                ),
                "proposal_content_hash": _content_hash(
                    _required_string(
                        envelope.payload, "current_proposal_content_hash"
                    )
                ),
                "reason": reason,
                "status": request.status.value,
            },
        )
        uow.events.append_with_outbox(event)
        return CommandResult(
            command_id=envelope.id,
            aggregate_id=request.id,
            aggregate_version=request.version,
            event_ids=(event.id,),
            data={
                "planning_request_id": str(request.id),
                "status": request.status.value,
            },
        )

    def _retry_blocked_run(
        self, uow: ControlPlaneUnitOfWork, envelope: CommandEnvelope
    ) -> CommandResult:
        self._require_operator(envelope)
        run = self._run_for_mutation(uow, envelope)
        if run.status is not PlanningRunStatus.BLOCKED:
            raise ValidationError("planning run is not blocked")
        decision = _required_string(envelope.payload, "unknown_outcome_decision")
        if decision != "retry":
            raise ValidationError("unknown_outcome_decision must be retry")
        request = uow.planning_requests.get(
            run.planning_request_id, envelope.workspace_id, for_update=True
        )
        if request is None:
            raise NotFound("planning request not found")
        if request.runs[-1].id != run.id:
            raise ValidationError("blocked run is not the current planning run")
        previous_request_version = request.version
        request.retry_blocked(new_run_id=uuid4(), now=self._clock())
        new_run = request.runs[-1]
        uow.planning_runs.add(new_run)
        uow.planning_requests.save(request, previous_request_version)
        event = _event(
            envelope,
            aggregate_type="planning_request",
            aggregate_id=request.id,
            aggregate_version=request.version,
            event_type="PlanningBlockedRunRetried",
            payload={
                "blocked_run_id": str(run.id),
                "planning_run_id": str(new_run.id),
                "attempt": new_run.attempt,
                "unknown_outcome_decision": decision,
            },
        )
        uow.events.append_with_outbox(event)
        return CommandResult(
            command_id=envelope.id,
            aggregate_id=request.id,
            aggregate_version=request.version,
            event_ids=(event.id,),
            data={
                "planning_request_id": str(request.id),
                "planning_run_id": str(new_run.id),
                "attempt": new_run.attempt,
                "unknown_outcome_decision": decision,
            },
        )

    def _request_for_mutation(
        self, uow: ControlPlaneUnitOfWork, envelope: CommandEnvelope
    ) -> PlanningRequest:
        if envelope.target_id is None or envelope.expected_version is None:
            raise ValidationError("planning request mutation requires target_id and expected_version")
        request = uow.planning_requests.get(
            envelope.target_id, envelope.workspace_id, for_update=True
        )
        if request is None:
            raise NotFound("planning request not found")
        if request.version != envelope.expected_version:
            raise VersionConflict(envelope.expected_version, request.version)
        return request

    def _run_for_mutation(
        self, uow: ControlPlaneUnitOfWork, envelope: CommandEnvelope
    ) -> PlanningRun:
        if envelope.target_id is None or envelope.expected_version is None:
            raise ValidationError("planning run mutation requires target_id and expected_version")
        run = uow.planning_runs.get(
            envelope.target_id, envelope.workspace_id, for_update=True
        )
        if run is None:
            raise NotFound("planning run not found")
        if run.version != envelope.expected_version:
            raise VersionConflict(envelope.expected_version, run.version)
        return run

    def _assert_current_proposal(
        self,
        uow: ControlPlaneUnitOfWork,
        request: PlanningRequest,
        payload: Mapping[str, object],
    ) -> None:
        proposal = uow.planning_records.get_current_proposal(
            request.id, request.workspace_id
        )
        if proposal is None:
            raise NotFound("current planning proposal not found")
        revision = _positive_int(payload, "current_proposal_revision")
        content_hash = _content_hash(
            _required_string(payload, "current_proposal_content_hash")
        )
        if proposal.revision != revision:
            raise ValidationError("proposal revision does not match current proposal")
        if proposal.content_hash != content_hash:
            raise ValidationError("proposal content hash does not match current proposal")

    def _persist_appended_revision(
        self,
        uow: ControlPlaneUnitOfWork,
        request: PlanningRequest,
        *,
        brief: PlanningBriefRevision,
        previous_request_version: int,
        previous_clarification_versions: Mapping[UUID, int] | None = None,
    ) -> None:
        uow.planning_records.add_brief(brief)
        uow.planning_runs.add(request.runs[-1])
        uow.planning_requests.save(request, previous_request_version)
        previous_clarification_versions = previous_clarification_versions or {}
        for clarification in request.clarifications:
            previous_version = previous_clarification_versions.get(clarification.id)
            if previous_version is not None and clarification.version != previous_version:
                uow.planning_records.save_clarification(
                    clarification, previous_version
                )

    def _persist_run_changes(
        self,
        uow: ControlPlaneUnitOfWork,
        request: PlanningRequest,
        previous_run_versions: Mapping[UUID, int],
    ) -> None:
        now = self._clock()
        for run in request.runs:
            previous_version = previous_run_versions.get(run.id)
            if previous_version is None:
                continue
            uow.planning_runs.save(run, previous_version)
            if run.lease_id is None:
                continue
            lease = uow.planning_leases.get(
                run.lease_id, request.workspace_id, for_update=True
            )
            if lease is not None and lease.status is PlanningRunLeaseStatus.ACTIVE:
                lease.status = PlanningRunLeaseStatus.EXPIRED
                lease.released_at = now
                lease.version += 1
                uow.planning_leases.save(lease, lease.version - 1)

    def _require_operator(self, envelope: CommandEnvelope) -> None:
        if (
            envelope.actor_type != self._local_actor_type
            or envelope.actor_id != self._local_actor_id
        ):
            raise OperatorRequired(
                "planning module and profile registration require the configured operator"
            )


def _event(
    envelope: CommandEnvelope,
    *,
    aggregate_type: str,
    aggregate_id: UUID,
    event_type: str,
    payload: dict[str, object],
    aggregate_version: int = 1,
) -> EventData:
    return EventData(
        id=uuid4(),
        workspace_id=envelope.workspace_id,
        aggregate_type=aggregate_type,
        aggregate_id=aggregate_id,
        aggregate_version=aggregate_version,
        event_type=event_type,
        payload=payload,
        actor_type=envelope.actor_type,
        actor_id=envelope.actor_id,
        causation_id=envelope.id,
        correlation_id=envelope.correlation_id,
    )


def _planner_capabilities(
    value: Mapping[str, object],
) -> PlannerAdapterCapabilityProfile:
    return PlannerAdapterCapabilityProfile(
        structured_output=_enum_string(value, "structured_output"),  # type: ignore[arg-type]
        clarification=_bool(value, "clarification"),
        idempotency=_enum_string(value, "idempotency"),  # type: ignore[arg-type]
        cancellation=_enum_string(value, "cancellation"),  # type: ignore[arg-type]
        usage_reporting=_enum_string(value, "usage_reporting"),  # type: ignore[arg-type]
        max_context_bytes=_positive_int(value, "max_context_bytes"),
        stability=_enum_string(value, "stability"),  # type: ignore[arg-type]
        interface_version=_required_string(value, "interface_version"),
        proposal_schema_versions=tuple(
            _positive_ints(value.get("proposal_schema_versions"), "proposal_schema_versions")
        ),
    )


def _current_brief(request: PlanningRequest) -> PlanningBriefRevision:
    if not request.briefs:
        raise ValidationError("planning request has no current brief")
    return request.briefs[-1]


def _next_brief(
    request: PlanningRequest,
    *,
    current_brief: PlanningBriefRevision,
    user_feedback: str,
    actor: str,
    correlation_id: UUID,
    now: datetime,
) -> PlanningBriefRevision:
    return PlanningBriefRevision.create(
        planning_request_id=request.id,
        revision=request.current_brief_revision + 1,
        objective=current_brief.objective,
        constraints=current_brief.constraints,
        acceptance_criteria=current_brief.acceptance_criteria,
        context_refs=current_brief.context_refs,
        requested_outputs=current_brief.requested_outputs,
        user_feedback=user_feedback,
        source_correlation=correlation_id,
        created_by=actor,
        created_at=now,
    )


def _append_feedback(previous: str | None, addition: str) -> str:
    if previous is None:
        return addition
    return f"{previous}\n\n{addition}"


def _clarification_feedback(answers: Mapping[UUID, str]) -> str:
    entries = "; ".join(
        f"{clarification_id}={answers[clarification_id]}"
        for clarification_id in sorted(answers, key=str)
    )
    return f"clarification answers: {entries}"


def _validate_clarification_answer(
    clarification: PlanningClarification, answer: str
) -> None:
    if clarification.options and answer not in clarification.options:
        raise ValidationError("clarification answer is not one of the options")
    answer_type = clarification.answer_type.strip().lower()
    if answer_type in {"boolean", "bool"} and answer.lower() not in {
        "true",
        "false",
    }:
        raise ValidationError("clarification answer must be a boolean")
    if answer_type in {"integer", "number", "float"}:
        try:
            float(answer)
        except ValueError as exc:
            raise ValidationError("clarification answer must be numeric") from exc


def _active_run_versions(request: PlanningRequest) -> dict[UUID, int]:
    return {
        run.id: run.version
        for run in request.runs
        if run.status not in RUN_TERMINAL_STATUSES
        and run.status is not PlanningRunStatus.NEEDS_CLARIFICATION
    }


def _content_hash(value: str) -> str:
    if len(value) != 64 or any(character not in "0123456789abcdef" for character in value):
        raise ValidationError("content hash must be a lowercase SHA-256 value")
    return value


def _budget(value: object) -> PlanningBudgetPolicy:
    item = _mapping(value, "budget")
    return PlanningBudgetPolicy(
        max_context_bytes=_positive_int(item, "max_context_bytes"),
        max_tokens=_positive_int(item, "max_tokens"),
        max_cost_microunits=_positive_int(item, "max_cost_microunits"),
        max_runtime_seconds=_positive_int(item, "max_runtime_seconds"),
        max_revisions=_positive_int(item, "max_revisions"),
        max_nodes=_positive_int(item, "max_nodes"),
        max_concurrency=_positive_int(item, "max_concurrency"),
    )


def _retry_policy(value: object) -> PlanningRetryPolicy:
    item = _mapping(value, "retry_policy")
    max_retries = _non_negative_int(item, "max_retries")
    backoff = _positive_ints(item.get("backoff_seconds"), "backoff_seconds")
    return PlanningRetryPolicy(
        max_retries=max_retries,
        backoff_seconds=backoff,
        unknown_outcome_policy=UnknownOutcomePolicy(
            _required_string(item, "unknown_outcome_policy")
        ),
        invalid_output_policy=InvalidOutputPolicy(
            _required_string(item, "invalid_output_policy")
        ),
    )


def _mapping(value: object, name: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise ValidationError(f"{name} must be an object")
    return value


def _required_string(value: Mapping[str, object], name: str) -> str:
    item = value.get(name)
    if not isinstance(item, str) or not item.strip():
        raise ValidationError(f"{name} is required")
    return item.strip()


def _optional_string(value: object) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValidationError("optional string must be a string")
    return value.strip() or None


def _string_tuple(value: object, name: str) -> tuple[str, ...]:
    if not isinstance(value, (list, tuple)):
        raise ValidationError(f"{name} must be an array of strings")
    result: list[str] = []
    for item in value:
        if not isinstance(item, str) or not item.strip():
            raise ValidationError(f"{name} must contain non-empty strings")
        clean = item.strip()
        if clean not in result:
            result.append(clean)
    return tuple(result)


def _positive_int(value: Mapping[str, object], name: str) -> int:
    item = value.get(name)
    if not isinstance(item, int) or isinstance(item, bool) or item < 1:
        raise ValidationError(f"{name} must be positive")
    return item


def _non_negative_int(value: Mapping[str, object], name: str) -> int:
    item = value.get(name)
    if not isinstance(item, int) or isinstance(item, bool) or item < 0:
        raise ValidationError(f"{name} must be zero or positive")
    return item


def _bounded_int(value: Mapping[str, object], name: str, lower: int, upper: int) -> int:
    item = value.get(name)
    if not isinstance(item, int) or isinstance(item, bool) or not lower <= item <= upper:
        raise ValidationError(f"{name} must be between {lower} and {upper}")
    return item


def _positive_ints(value: object, name: str) -> tuple[int, ...]:
    if not isinstance(value, (list, tuple)):
        raise ValidationError(f"{name} must be an array of positive integers")
    result: list[int] = []
    for item in value:
        if not isinstance(item, int) or isinstance(item, bool) or item < 1:
            raise ValidationError(f"{name} must be an array of positive integers")
        if item not in result:
            result.append(item)
    return tuple(result)


def _enum_string(value: Mapping[str, object], name: str) -> str:
    return _required_string(value, name)


def _uuid(value: Mapping[str, object], name: str) -> UUID:
    item = value.get(name)
    try:
        return item if isinstance(item, UUID) else UUID(str(item))
    except (TypeError, ValueError) as exc:
        raise ValidationError(f"{name} must be a UUID") from exc


def _bool(value: Mapping[str, object], name: str) -> bool:
    item = value.get(name)
    if not isinstance(item, bool):
        raise ValidationError(f"{name} must be a boolean")
    return item


def _validate_secret_refs(refs: tuple[str, ...]) -> None:
    if any(not ref.startswith("secret://") for ref in refs):
        raise ValidationError("secret_refs must contain secret:// references")


def _assert_no_secret_values(value: Mapping[str, object]) -> None:
    secret_markers = ("secret", "password", "token", "api_key", "apikey")
    for key, item in value.items():
        lowered = str(key).lower()
        if any(marker in lowered for marker in secret_markers):
            raise ValidationError("secret values must be supplied as secret_refs")
        if isinstance(item, Mapping):
            _assert_no_secret_values(item)
        elif isinstance(item, (list, tuple)):
            for nested in item:
                if isinstance(nested, Mapping):
                    _assert_no_secret_values(nested)
