from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping
from datetime import UTC, datetime
from uuid import UUID, uuid4

from sidera_control_plane.application.context_requests import (
    build_planning_context_assembly_request,
)
from sidera_control_plane.application.errors import NotFound
from sidera_control_plane.application.knowledge_candidate_submission import (
    KnowledgeCandidateSubmissionService,
)
from sidera_control_plane.application.notification_commands import (
    bind_planning_request_origin,
    copy_planning_request_origin_to_root,
)
from sidera_control_plane.application.ports import ControlPlaneUnitOfWork
from sidera_control_plane.application.proposal_materializer import (
    ProposalMaterializer,
)
from sidera_control_plane.application.revision_materializer import (
    RevisionMaterializer,
    RevisionStaleBase,
)
from sidera_control_plane.application.work_context_commands import (
    WorkContextRevisionFactory,
    work_context_event,
)
from sidera_control_plane.domain.collaboration import (
    CollaborationRuntimeBindingSnapshot,
)
from sidera_control_plane.domain.commands import CommandEnvelope, CommandResult
from sidera_control_plane.domain.errors import (
    OperatorRequired,
    PlanGenerationConflict,
    PlanningConflict,
    ValidationError,
    VersionConflict,
)
from sidera_control_plane.domain.events import EventData
from sidera_control_plane.domain.modules import (
    COLLABORATION_RUNTIME_MODULE_TYPE,
    PLANNER_ADAPTER_MODULE_TYPE,
    CollaborationRuntimeCapabilityProfile,
    ModuleDefinition,
    ModuleInstance,
    ModuleLifecycle,
    PlannerAdapterCapabilityProfile,
)
from sidera_control_plane.domain.plan_revisions import (
    RevisionBaseDescriptorV1,
    plan_snapshot_content_hash,
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
from sidera_control_plane.domain.work_context import (
    WorkContext,
    WorkContextRevision,
    WorkContextRevisionReason,
)


class PlanningCommandHandler:
    command_types = frozenset(
        {
            "RegisterPlannerModule",
            "RegisterCollaborationRuntimeModule",
            "RegisterPlanningProfile",
            "SubmitPlanningRequest",
            "SubmitPlanRevision",
            "CancelPlanningRequest",
            "AnswerPlanningClarification",
            "RequestProposalRevision",
            "RejectPlanningProposal",
            "ConfirmPlanningProposal",
            "ConfirmPlanRevision",
            "RetryBlockedPlanningRun",
        }
    )

    def __init__(
        self,
        *,
        local_actor_type: str,
        local_actor_id: str,
        clock: Callable[[], datetime] | None = None,
        materializer: ProposalMaterializer | None = None,
        revision_materializer: RevisionMaterializer | None = None,
        work_context_factory: WorkContextRevisionFactory | None = None,
        planning_context_ttl_seconds: int = 1_800,
        candidate_submission_service: KnowledgeCandidateSubmissionService | None = None,
    ) -> None:
        if planning_context_ttl_seconds < 1:
            raise ValueError("planning_context_ttl_seconds must be positive")
        self._local_actor_type = local_actor_type
        self._local_actor_id = local_actor_id
        self._clock = clock or (lambda: datetime.now(UTC))
        self._planning_context_ttl_seconds = planning_context_ttl_seconds
        self._materializer = materializer or ProposalMaterializer()
        self._revision_materializer = revision_materializer or RevisionMaterializer()
        self._work_context_factory = (
            work_context_factory or WorkContextRevisionFactory()
        )
        self._candidate_submission_service = candidate_submission_service

    def handle(
        self, uow: ControlPlaneUnitOfWork, envelope: CommandEnvelope
    ) -> CommandResult:
        if envelope.command_type == "RegisterPlannerModule":
            return self._register_module(uow, envelope)
        if envelope.command_type == "RegisterCollaborationRuntimeModule":
            return self._register_collaboration_runtime_module(uow, envelope)
        if envelope.command_type == "RegisterPlanningProfile":
            return self._register_profile(uow, envelope)
        if envelope.command_type == "SubmitPlanningRequest":
            return self._submit_request(uow, envelope)
        if envelope.command_type == "SubmitPlanRevision":
            return self._submit_plan_revision(uow, envelope)
        if envelope.command_type == "CancelPlanningRequest":
            return self._cancel_request(uow, envelope)
        if envelope.command_type == "AnswerPlanningClarification":
            return self._answer_clarification(uow, envelope)
        if envelope.command_type == "RequestProposalRevision":
            return self._request_proposal_revision(uow, envelope)
        if envelope.command_type == "RejectPlanningProposal":
            return self._reject_proposal(uow, envelope)
        if envelope.command_type == "ConfirmPlanningProposal":
            return self._confirm_proposal(uow, envelope)
        if envelope.command_type == "ConfirmPlanRevision":
            return self._confirm_plan_revision(uow, envelope)
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

    def _register_collaboration_runtime_module(
        self, uow: ControlPlaneUnitOfWork, envelope: CommandEnvelope
    ) -> CommandResult:
        self._require_operator(envelope)
        payload = envelope.payload
        config = _mapping(payload.get("config"), "config")
        _assert_no_secret_values(config)
        secret_refs = _string_tuple(payload.get("secret_refs", ()), "secret_refs")
        _validate_secret_refs(secret_refs)
        capabilities = _collaboration_capabilities(
            _mapping(payload.get("capabilities"), "capabilities")
        )
        definition = ModuleDefinition(
            id=uuid4(),
            workspace_id=envelope.workspace_id,
            module_type=COLLABORATION_RUNTIME_MODULE_TYPE,
            provider=_required_string(payload, "provider"),
            interface_version=_required_string(payload, "interface_version"),
            config_schema_version=_positive_int(payload, "config_schema_version"),
            capabilities=capabilities,
            lifecycle=ModuleLifecycle.ACTIVE,
        )
        if capabilities.interface_version != definition.interface_version:
            raise ValidationError(
                "collaboration capability interface_version must match module "
                "interface_version"
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
            event_type="CollaborationRuntimeModuleRegistered",
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
            event_type="CollaborationRuntimeModuleInstanceRegistered",
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
        collaboration_binding = _resolve_collaboration_runtime_binding(
            uow,
            workspace_id=envelope.workspace_id,
            payload=payload,
        )
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
            execution_max_attempts=_positive_int(payload, "execution_max_attempts"),
            execution_budget_level=_required_string(payload, "execution_budget_level"),
            user_selectable=bool(payload.get("user_selectable", True)),
            status=PlanningProfileStatus.ACTIVE,
            planner_secret_refs=_string_tuple(
                payload.get("planner_secret_refs", ()), "planner_secret_refs"
            ),
            collaboration_runtime_binding=collaboration_binding,
        )
        if profile.planner_interface_version != definition.interface_version:
            raise ValidationError(
                "planning profile interface version does not match module"
            )
        if profile.planner_module_config_version != instance.config_version:
            raise ValidationError(
                "planning profile module config version does not match instance"
            )
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
            context_refs=_string_tuple(payload.get("context_refs", ()), "context_refs"),
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
        work_context_revision = self._work_context_factory.create(
            work_context_id=uuid4(),
            workspace_id=envelope.workspace_id,
            brief=request.briefs[0],
            revision_number=1,
            reason=WorkContextRevisionReason.REQUEST_CREATED,
            actor=envelope.actor_id,
            created_at=request.created_at,
        )
        work_context = WorkContext.create(
            work_context_id=work_context_revision.work_context_id,
            workspace_id=envelope.workspace_id,
            planning_request_id=request.id,
            first_revision=work_context_revision,
            created_at=request.created_at,
        )
        uow.planning_requests.add(request)
        bind_planning_request_origin(uow, request, now=request.created_at)
        uow.work_contexts.add(work_context, work_context_revision)
        uow.context_assemblies.add(
            build_planning_context_assembly_request(
                run=request.runs[-1],
                brief=request.briefs[-1],
                work_context_revision=work_context_revision,
                ttl_seconds=self._planning_context_ttl_seconds,
                created_at=request.created_at,
            )
        )
        context_event = work_context_event(
            envelope,
            context=work_context,
            revision=work_context_revision,
            event_type="WorkContextCreated",
        )
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
                "work_context_id": str(work_context.id),
                "source_type": request.source_type,
                "source_ref": request.source_ref,
            },
        )
        uow.events.append_with_outbox(context_event)
        uow.events.append_with_outbox(event)
        return CommandResult(
            command_id=envelope.id,
            aggregate_id=request.id,
            aggregate_version=request.version,
            event_ids=(context_event.id, event.id),
            data={
                "planning_request_id": str(request.id),
                "planning_run_id": str(request.current_run_id),
                "brief_revision": request.current_brief_revision,
                "work_context_id": str(work_context.id),
                "work_context_revision": work_context.current_revision_number,
                "work_context_content_hash": work_context_revision.content_hash,
            },
        )

    def _submit_plan_revision(
        self, uow: ControlPlaneUnitOfWork, envelope: CommandEnvelope
    ) -> CommandResult:
        if envelope.target_type != "task":
            raise ValidationError("SubmitPlanRevision requires target_type=task")
        if envelope.target_id is None or envelope.expected_version is None:
            raise ValidationError(
                "SubmitPlanRevision requires target_id and expected_version"
            )
        if envelope.expected_version < 1:
            raise ValidationError("expected_version must be positive")
        payload = envelope.payload
        root_task_id = _uuid(payload, "root_task_id")
        if envelope.target_id != root_task_id:
            raise ValidationError(
                "SubmitPlanRevision root_task_id must match target_id"
            )

        root_task = uow.tasks.get(root_task_id, envelope.workspace_id, for_update=True)
        if root_task is None:
            raise NotFound("root task not found")
        if root_task.version != envelope.expected_version:
            raise VersionConflict(envelope.expected_version, root_task.version)

        plan_head = uow.plans.get_head(
            root_task_id, envelope.workspace_id, for_update=True
        )
        if plan_head is None:
            raise NotFound("current plan head not found")

        expected_snapshot_id = _uuid(payload, "expected_base_snapshot_id")
        expected_generation = _positive_int(payload, "expected_base_generation")
        expected_content_hash = _content_hash(
            _required_string(payload, "expected_base_content_hash")
        )
        if plan_head.active_generation != expected_generation:
            raise PlanGenerationConflict(
                expected_generation, plan_head.active_generation
            )
        if plan_head.active_snapshot_id != expected_snapshot_id:
            raise ValidationError(
                "revision base snapshot does not match the current plan head",
                details={
                    "expected_snapshot_id": str(expected_snapshot_id),
                    "current_snapshot_id": str(plan_head.active_snapshot_id),
                    "current_generation": plan_head.active_generation,
                },
            )
        if plan_head.active_snapshot_hash != expected_content_hash:
            raise ValidationError(
                "revision base content hash does not match the current plan head",
                details={
                    "expected_content_hash": expected_content_hash,
                    "current_content_hash": plan_head.active_snapshot_hash,
                },
            )

        base_snapshot = uow.plans.get(expected_snapshot_id, envelope.workspace_id)
        if base_snapshot is None:
            raise NotFound("revision base snapshot not found")
        actual_content_hash = plan_snapshot_content_hash(base_snapshot)
        if (
            base_snapshot.root_task_id != root_task_id
            or base_snapshot.generation != expected_generation
            or actual_content_hash != expected_content_hash
        ):
            raise ValidationError(
                "revision base snapshot content does not match the current plan head",
                details={
                    "snapshot_id": str(base_snapshot.id),
                    "root_task_id": str(base_snapshot.root_task_id),
                    "generation": base_snapshot.generation,
                    "content_hash": actual_content_hash,
                },
            )

        active_revision = uow.planning_requests.get_active_revision_for_root(
            root_task_id, envelope.workspace_id, for_update=True
        )
        if active_revision is not None:
            raise PlanningConflict(
                current_request_version=active_revision.version,
                current_proposal_revision=active_revision.current_proposal_revision,
                current_proposal_content_hash=(
                    active_revision.current_proposal_content_hash
                ),
            )

        profile_id = _uuid(payload, "planning_profile_id")
        profile_version = _positive_int(payload, "planning_profile_version")
        profile = uow.planning_profiles.get(
            profile_id, envelope.workspace_id, version=profile_version
        )
        if profile is None:
            raise NotFound("planning profile not found")
        profile.assert_user_selectable()
        if profile.proposal_schema_version not in {3, 4, 5}:
            raise ValidationError(
                "plan revision requires a planning profile with proposal schema v3, v4, or v5"
            )
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
        if (
            profile.proposal_schema_version
            not in definition.capabilities.proposal_schema_versions
        ):
            raise ValidationError(
                "planner adapter does not support the selected revision proposal schema"
            )
        profile_snapshot = profile.snapshot(
            definition.capabilities,
            module_instance_config_version=instance.config_version,
        )

        work_context = uow.work_contexts.get_by_root_task(
            root_task_id, envelope.workspace_id, for_update=True
        )
        if work_context is None:
            raise NotFound("work context not found for plan revision")
        current_context_revision = uow.work_contexts.get_revision(
            work_context.current_revision_id,
            envelope.workspace_id,
        )
        if current_context_revision is None:
            raise NotFound("work context current revision not found")

        now = self._clock()
        base = RevisionBaseDescriptorV1(
            schema_version=1,
            workspace_id=envelope.workspace_id,
            root_task_id=root_task_id,
            plan_snapshot_id=expected_snapshot_id,
            plan_generation=expected_generation,
            plan_content_hash=expected_content_hash,
            root_task_version=root_task.version,
            captured_at=now,
        )
        request = PlanningRequest.create_revision(
            request_id=uuid4(),
            workspace_id=envelope.workspace_id,
            profile_snapshot=profile_snapshot,
            objective=_required_string(payload, "objective"),
            constraints=_string_tuple(payload.get("constraints", ()), "constraints"),
            acceptance_criteria=_string_tuple(
                payload.get("acceptance_criteria", ()), "acceptance_criteria"
            ),
            context_refs=(),
            requested_outputs=("plan",),
            source_type="plan_revision",
            source_ref=str(envelope.id),
            actor=envelope.actor_id,
            correlation_id=envelope.correlation_id,
            run_id=uuid4(),
            now=now,
            base=base,
        )
        work_context_revision = self._work_context_factory.create(
            work_context_id=work_context.id,
            workspace_id=envelope.workspace_id,
            brief=request.briefs[0],
            revision_number=work_context.current_revision_number + 1,
            reason=WorkContextRevisionReason.REPLANNED,
            actor=envelope.actor_id,
            created_at=now,
            base_snapshot=current_context_revision.snapshot,
            plan_snapshot_id=base_snapshot.id,
        )
        previous_context_version = work_context.version
        work_context.append_external_revision(
            work_context_revision,
            expected_version=previous_context_version,
            now=now,
        )

        uow.planning_requests.add(request)
        uow.work_contexts.append_revision(
            work_context,
            work_context_revision,
            expected_version=previous_context_version,
        )
        uow.context_assemblies.add(
            build_planning_context_assembly_request(
                run=request.runs[-1],
                brief=request.briefs[-1],
                work_context_revision=work_context_revision,
                ttl_seconds=self._planning_context_ttl_seconds,
                created_at=now,
                planning_kind=request.kind,
                revision_base=request.base_descriptor,
            )
        )
        context_event = work_context_event(
            envelope,
            context=work_context,
            revision=work_context_revision,
            event_type="WorkContextRevised",
        )
        event = _event(
            envelope,
            aggregate_type="planning_request",
            aggregate_id=request.id,
            aggregate_version=request.version,
            event_type="PlanRevisionRequested",
            payload={
                "root_task_id": str(root_task_id),
                "planning_request_id": str(request.id),
                "planning_run_id": str(request.current_run_id),
                "work_context_id": str(work_context.id),
                "work_context_revision": work_context_revision.revision_number,
                "base_snapshot_id": str(base.plan_snapshot_id),
                "base_generation": base.plan_generation,
                "base_content_hash": base.plan_content_hash,
            },
        )
        uow.events.append_with_outbox(context_event)
        uow.events.append_with_outbox(event)
        return CommandResult(
            command_id=envelope.id,
            aggregate_id=request.id,
            aggregate_version=request.version,
            event_ids=(context_event.id, event.id),
            data={
                "planning_request_id": str(request.id),
                "planning_run_id": str(request.current_run_id),
                "root_task_id": str(root_task_id),
                "base_snapshot_id": str(base.plan_snapshot_id),
                "base_generation": base.plan_generation,
                "base_content_hash": base.plan_content_hash,
                "work_context_id": str(work_context.id),
                "work_context_revision": work_context_revision.revision_number,
                "work_context_content_hash": work_context_revision.content_hash,
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
            raise _planning_conflict(request)
        previous_request_version = request.version
        previous_run_versions = {
            run.id: run.version
            for run in request.runs
            if run.status
            not in {
                PlanningRunStatus.SUCCEEDED,
                PlanningRunStatus.NEEDS_CLARIFICATION,
                PlanningRunStatus.FAILED,
                PlanningRunStatus.BLOCKED,
                PlanningRunStatus.CANCELLED,
            }
        }
        now = self._clock()
        self._discard_planner_candidates(
            uow,
            request,
            planning_run_ids=(run.id for run in request.runs),
            envelope=envelope,
            now=now,
        )
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
            data={
                "planning_request_id": str(request.id),
                "status": request.status.value,
            },
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
                raise ValidationError(
                    "answer does not reference a pending clarification"
                )
            answer = _required_string(item, "answer")
            _validate_clarification_answer(pending[clarification_id], answer)
            answers[clarification_id] = answer
        if set(answers) != set(pending):
            raise ValidationError(
                "all pending clarifications must be answered together"
            )

        work_context = uow.work_contexts.get_by_planning_request(
            request.id, envelope.workspace_id, for_update=True
        )
        if work_context is None:
            raise NotFound("work context not found")
        current_context_revision = uow.work_contexts.get_revision(
            work_context.current_revision_id,
            envelope.workspace_id,
        )
        if current_context_revision is None:
            raise NotFound("work context current revision not found")

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
        previous_run_id = request.current_run_id
        request.answer_clarifications(
            run_id=request.current_run_id,
            answers=answers,
            brief=brief,
            new_run_id=uuid4(),
            actor=envelope.actor_id,
            now=brief.created_at,
        )
        self._discard_planner_candidates(
            uow,
            request,
            planning_run_ids=(previous_run_id,),
            envelope=envelope,
            now=brief.created_at,
        )
        previous_context_version = work_context.version
        new_decisions = self._work_context_factory.clarification_decisions(
            tuple(pending.values()),
            answers,
            actor=envelope.actor_id,
            ordinal_offset=len(
                current_context_revision.snapshot.clarification_decisions
            ),
        )
        context_revision = self._work_context_factory.create(
            work_context_id=work_context.id,
            workspace_id=envelope.workspace_id,
            brief=brief,
            revision_number=work_context.current_revision_number + 1,
            reason=WorkContextRevisionReason.CLARIFICATION_ACCEPTED,
            actor=envelope.actor_id,
            created_at=brief.created_at,
            base_snapshot=current_context_revision.snapshot,
            clarification_decisions=(
                *current_context_revision.snapshot.clarification_decisions,
                *new_decisions,
            ),
        )
        work_context.append_revision(
            context_revision,
            expected_version=previous_context_version,
            now=brief.created_at,
        )
        self._persist_appended_revision(
            uow,
            request,
            brief=brief,
            previous_request_version=previous_request_version,
            previous_clarification_versions=previous_clarification_versions,
            work_context=work_context,
            work_context_revision=context_revision,
            previous_context_version=previous_context_version,
        )
        context_event = work_context_event(
            envelope,
            context=work_context,
            revision=context_revision,
            event_type="WorkContextRevised",
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
        uow.events.append_with_outbox(context_event)
        uow.events.append_with_outbox(event)
        return CommandResult(
            command_id=envelope.id,
            aggregate_id=request.id,
            aggregate_version=request.version,
            event_ids=(context_event.id, event.id),
            data={
                "planning_request_id": str(request.id),
                "planning_run_id": str(request.current_run_id),
                "brief_revision": brief.revision,
                "work_context_id": str(work_context.id),
                "work_context_revision": context_revision.revision_number,
                "work_context_content_hash": context_revision.content_hash,
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
        previous_run_id = request.current_run_id
        request.request_revision(
            expected_proposal_revision=_positive_int(
                envelope.payload, "current_proposal_revision"
            ),
            expected_content_hash=_content_hash(
                _required_string(envelope.payload, "current_proposal_content_hash")
            ),
            feedback=feedback,
            brief=brief,
            new_run_id=uuid4(),
            actor=envelope.actor_id,
            now=now,
        )
        self._discard_planner_candidates(
            uow,
            request,
            planning_run_ids=(previous_run_id,),
            envelope=envelope,
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
                    _required_string(envelope.payload, "current_proposal_content_hash")
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
        proposal = uow.planning_records.get_current_proposal(
            request.id, request.workspace_id
        )
        if proposal is None:
            raise NotFound("current planning proposal not found")
        reason = _required_string(envelope.payload, "reason")
        previous_request_version = request.version
        previous_run_versions = _active_run_versions(request)
        request.reject(now=self._clock())
        if self._candidate_submission_service is not None:
            self._candidate_submission_service.discard_planner_submissions(
                uow,
                workspace_id=request.workspace_id,
                planning_run_id=proposal.planning_run_id,
                now=self._clock(),
                correlation_id=envelope.correlation_id,
                actor_id=envelope.actor_id,
                causation_id=envelope.id,
                actor_type=envelope.actor_type,
            )
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
                    _required_string(envelope.payload, "current_proposal_content_hash")
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

    def _confirm_proposal(
        self, uow: ControlPlaneUnitOfWork, envelope: CommandEnvelope
    ) -> CommandResult:
        request = self._request_for_mutation(uow, envelope)
        self._assert_current_proposal(
            uow,
            request,
            envelope.payload,
            revision_key="proposal_revision",
            content_hash_key="proposal_content_hash",
        )
        proposal = uow.planning_records.get_current_proposal(
            request.id, request.workspace_id
        )
        if proposal is None:
            raise NotFound("current planning proposal not found")
        profile_id = _uuid(envelope.payload, "profile_id")
        profile_version = _positive_int(envelope.payload, "profile_version")
        profile = uow.planning_profiles.get(
            profile_id, envelope.workspace_id, version=profile_version
        )
        if profile is None:
            raise NotFound("planning profile not found")
        result = self._materializer.materialize(
            uow,
            request=request,
            proposal=proposal,
            profile=profile,
            envelope=envelope,
            now=self._clock(),
        )
        materialization = result.materialization
        copy_planning_request_origin_to_root(
            uow,
            request_id=request.id,
            root_task_id=materialization.root_task_id,
            workspace_id=request.workspace_id,
        )
        if self._candidate_submission_service is not None:
            self._candidate_submission_service.activate_planner_submissions(
                uow,
                workspace_id=request.workspace_id,
                planning_run_id=proposal.planning_run_id,
                root_task_id=materialization.root_task_id,
                now=self._clock(),
                correlation_id=envelope.correlation_id,
                actor_id=envelope.actor_id,
                causation_id=envelope.id,
                actor_type=envelope.actor_type,
            )
        return CommandResult(
            command_id=envelope.id,
            aggregate_id=request.id,
            aggregate_version=request.version,
            event_ids=result.event_ids,
            data={
                "planning_request_id": str(request.id),
                "proposal_revision": proposal.revision,
                "proposal_content_hash": proposal.content_hash,
                "profile_id": str(profile.id),
                "profile_version": profile.version,
                "root_task_id": str(materialization.root_task_id),
                "plan_snapshot_id": str(materialization.plan_snapshot_id),
                "plan_generation": materialization.plan_generation,
                "subtask_task_mapping": {
                    key: str(value)
                    for key, value in materialization.subtask_task_mapping.items()
                },
                "worker_item_mapping": {
                    key: str(value)
                    for key, value in materialization.worker_item_mapping.items()
                },
                "execution_request_mapping": {
                    key: str(value)
                    for key, value in materialization.execution_request_mapping.items()
                },
                "deep_link": materialization.deep_link,
            },
        )

    def _confirm_plan_revision(
        self, uow: ControlPlaneUnitOfWork, envelope: CommandEnvelope
    ) -> CommandResult:
        request = self._request_for_mutation(uow, envelope)
        proposal = uow.planning_records.get_current_proposal(
            request.id, request.workspace_id
        )
        if proposal is None:
            raise NotFound("current planning proposal not found")
        profile_id = _uuid(envelope.payload, "profile_id")
        profile_version = _positive_int(envelope.payload, "profile_version")
        profile = uow.planning_profiles.get(
            profile_id, envelope.workspace_id, version=profile_version
        )
        if profile is None:
            raise NotFound("planning profile not found")
        now = self._clock()
        try:
            result = self._revision_materializer.materialize(
                uow,
                request=request,
                proposal=proposal,
                profile=profile,
                envelope=envelope,
                now=now,
            )
        except RevisionStaleBase as exc:
            previous_request_version = request.version
            request.mark_conflicted(now=now, reason="stale_base")
            uow.planning_requests.save(request, previous_request_version)
            event = _event(
                envelope,
                aggregate_type="planning_request",
                aggregate_id=request.id,
                aggregate_version=request.version,
                event_type="PlanningProposalConflicted",
                payload={
                    "planning_request_id": str(request.id),
                    "proposal_revision": proposal.revision,
                    "proposal_content_hash": proposal.content_hash,
                    "reason": "stale_base",
                    "details": dict(exc.details),
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
                    "reason": "stale_base",
                    "proposal_revision": proposal.revision,
                    "proposal_content_hash": proposal.content_hash,
                    "details": dict(exc.details),
                },
            )

        materialization = result.materialization
        return CommandResult(
            command_id=envelope.id,
            aggregate_id=request.id,
            aggregate_version=request.version,
            event_ids=result.event_ids,
            data={
                "planning_request_id": str(request.id),
                "proposal_revision": proposal.revision,
                "proposal_content_hash": proposal.content_hash,
                "profile_id": str(profile.id),
                "profile_version": profile.version,
                "root_task_id": str(result.root_task_id),
                "plan_snapshot_id": str(result.plan_snapshot_id),
                "plan_generation": result.plan_generation,
                "plan_transition_id": str(result.plan_transition_id),
                "proposal_revision_id": str(result.proposal_revision_id),
                "diff_hash": result.diff_hash,
                "subtask_task_mapping": {
                    key: str(value)
                    for key, value in materialization.subtask_task_mapping.items()
                },
                "worker_item_mapping": {
                    key: str(value)
                    for key, value in materialization.worker_item_mapping.items()
                },
                "execution_request_mapping": {},
                "deep_link": materialization.deep_link,
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
        self._add_planning_context_assembly(
            uow, request, run=new_run, brief=request.briefs[-1]
        )
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
            raise ValidationError(
                "planning request mutation requires target_id and expected_version"
            )
        request = uow.planning_requests.get(
            envelope.target_id, envelope.workspace_id, for_update=True
        )
        if request is None:
            raise NotFound("planning request not found")
        if request.version != envelope.expected_version:
            raise _planning_conflict(request)
        return request

    def _discard_planner_candidates(
        self,
        uow: ControlPlaneUnitOfWork,
        request: PlanningRequest,
        *,
        planning_run_ids: Iterable[UUID],
        envelope: CommandEnvelope,
        now: datetime,
    ) -> None:
        if self._candidate_submission_service is None:
            return
        seen: set[UUID] = set()
        for planning_run_id in planning_run_ids:
            if planning_run_id in seen:
                continue
            seen.add(planning_run_id)
            self._candidate_submission_service.discard_planner_submissions(
                uow,
                workspace_id=request.workspace_id,
                planning_run_id=planning_run_id,
                now=now,
                correlation_id=envelope.correlation_id,
                actor_id=envelope.actor_id,
                causation_id=envelope.id,
                actor_type=envelope.actor_type,
            )

    def _run_for_mutation(
        self, uow: ControlPlaneUnitOfWork, envelope: CommandEnvelope
    ) -> PlanningRun:
        if envelope.target_id is None or envelope.expected_version is None:
            raise ValidationError(
                "planning run mutation requires target_id and expected_version"
            )
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
        *,
        revision_key: str = "current_proposal_revision",
        content_hash_key: str = "current_proposal_content_hash",
    ) -> None:
        proposal = uow.planning_records.get_current_proposal(
            request.id, request.workspace_id
        )
        if proposal is None:
            raise NotFound("current planning proposal not found")
        revision = _positive_int(payload, revision_key)
        content_hash = _content_hash(_required_string(payload, content_hash_key))
        if proposal.revision != revision:
            raise _planning_conflict(request)
        if proposal.content_hash != content_hash:
            raise _planning_conflict(request)

    def _persist_appended_revision(
        self,
        uow: ControlPlaneUnitOfWork,
        request: PlanningRequest,
        *,
        brief: PlanningBriefRevision,
        previous_request_version: int,
        previous_clarification_versions: Mapping[UUID, int] | None = None,
        work_context: WorkContext | None = None,
        work_context_revision: WorkContextRevision | None = None,
        previous_context_version: int | None = None,
    ) -> None:
        uow.planning_records.add_brief(brief)
        if work_context is not None:
            if work_context_revision is None or previous_context_version is None:
                raise ValidationError(
                    "WorkContext revision persistence requires revision and version"
                )
            uow.work_contexts.append_revision(
                work_context,
                work_context_revision,
                expected_version=previous_context_version,
            )
        run = request.runs[-1]
        uow.planning_runs.add(run)
        self._add_planning_context_assembly(
            uow,
            request,
            run=run,
            brief=brief,
            work_context_revision=work_context_revision,
        )
        uow.planning_requests.save(request, previous_request_version)
        previous_clarification_versions = previous_clarification_versions or {}
        for clarification in request.clarifications:
            previous_version = previous_clarification_versions.get(clarification.id)
            if (
                previous_version is not None
                and clarification.version != previous_version
            ):
                uow.planning_records.save_clarification(clarification, previous_version)

    def _add_planning_context_assembly(
        self,
        uow: ControlPlaneUnitOfWork,
        request: PlanningRequest,
        *,
        run: PlanningRun,
        brief: PlanningBriefRevision,
        work_context_revision: WorkContextRevision | None = None,
    ) -> None:
        revision = work_context_revision
        if revision is None:
            context = uow.work_contexts.get_by_planning_request(
                request.id, request.workspace_id
            )
            if context is None and request.root_task_id is not None:
                context = uow.work_contexts.get_by_root_task(
                    request.root_task_id, request.workspace_id
                )
            if context is None:
                raise NotFound("work context not found for planning context assembly")
            revision = uow.work_contexts.get_revision(
                context.current_revision_id, request.workspace_id
            )
            if revision is None:
                raise NotFound(
                    "work context revision not found for planning context assembly"
                )
        uow.context_assemblies.add(
            build_planning_context_assembly_request(
                run=run,
                brief=brief,
                work_context_revision=revision,
                ttl_seconds=self._planning_context_ttl_seconds,
                created_at=run.queued_at,
                planning_kind=request.kind,
                revision_base=request.base_descriptor,
            )
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
            _positive_ints(
                value.get("proposal_schema_versions"), "proposal_schema_versions"
            )
        ),
        context_package_versions=tuple(
            _positive_ints(
                value.get("context_package_versions", ()),
                "context_package_versions",
            )
        ),
    )


def _collaboration_capabilities(
    value: Mapping[str, object],
) -> CollaborationRuntimeCapabilityProfile:
    return CollaborationRuntimeCapabilityProfile(
        interface_version=_required_string(value, "interface_version"),
        projection_schema_versions=tuple(
            _positive_ints(
                value.get("projection_schema_versions"),
                "projection_schema_versions",
            )
        ),
        decision_schema_versions=tuple(
            _positive_ints(
                value.get("decision_schema_versions"),
                "decision_schema_versions",
            )
        ),
        strategy_ids=_string_tuple(value.get("strategy_ids"), "strategy_ids"),
        idempotency=_enum_string(value, "idempotency"),  # type: ignore[arg-type]
        max_projection_bytes=_positive_int(value, "max_projection_bytes"),
        max_decision_bytes=_positive_int(value, "max_decision_bytes"),
        max_intents_per_decision=_positive_int(value, "max_intents_per_decision"),
        max_strategy_state_bytes=_positive_int(value, "max_strategy_state_bytes"),
        stability=_enum_string(value, "stability"),  # type: ignore[arg-type]
    )


def _resolve_collaboration_runtime_binding(
    uow: ControlPlaneUnitOfWork,
    *,
    workspace_id: UUID,
    payload: Mapping[str, object],
) -> CollaborationRuntimeBindingSnapshot | None:
    definition_value = payload.get("collaboration_runtime_definition_id")
    instance_value = payload.get("collaboration_runtime_instance_id")
    config_value = payload.get("collaboration_runtime_config_version")
    supplied = tuple(
        value is not None for value in (definition_value, instance_value, config_value)
    )
    if not any(supplied):
        return None
    if not all(supplied):
        raise ValidationError(
            "collaboration runtime references must be supplied together"
        )
    if _positive_int(payload, "proposal_schema_version") not in {4, 5}:
        raise ValidationError(
            "collaboration runtime binding requires proposal schema version 4 or 5"
        )

    definition_id = _uuid_value(definition_value, "collaboration_runtime_definition_id")
    instance_id = _uuid_value(instance_value, "collaboration_runtime_instance_id")
    config_version = _positive_value(
        config_value, "collaboration_runtime_config_version"
    )
    instance = uow.modules.get_instance(instance_id, workspace_id)
    if instance is None:
        raise NotFound("collaboration runtime instance not found")
    if instance.definition_id != definition_id:
        raise ValidationError(
            "collaboration runtime instance does not belong to the definition"
        )
    definition = uow.modules.get_definition(definition_id, workspace_id)
    if definition is None:
        raise NotFound("collaboration runtime definition not found")
    if definition.module_type != COLLABORATION_RUNTIME_MODULE_TYPE:
        raise ValidationError("module is not a collaboration runtime")
    if (
        definition.lifecycle is not ModuleLifecycle.ACTIVE
        or instance.lifecycle is not ModuleLifecycle.ACTIVE
    ):
        raise ValidationError("collaboration runtime binding requires an active module")
    if instance.config_version != config_version:
        raise ValidationError(
            "collaboration runtime config version does not match instance"
        )
    capabilities = definition.capabilities
    if not isinstance(capabilities, CollaborationRuntimeCapabilityProfile):
        raise ValidationError("collaboration runtime capabilities are invalid")
    if definition.interface_version != capabilities.interface_version:
        raise ValidationError(
            "collaboration runtime interface version does not match capabilities"
        )
    return CollaborationRuntimeBindingSnapshot(
        runtime_definition_id=definition.id,
        runtime_instance_id=instance.id,
        runtime_interface_version=definition.interface_version,
        runtime_config_version=instance.config_version,
        runtime_capability_snapshot=capabilities,
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
    if len(value) != 64 or any(
        character not in "0123456789abcdef" for character in value
    ):
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


def _positive_value(value: object, name: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 1:
        raise ValidationError(f"{name} must be positive")
    return value


def _planning_conflict(request: PlanningRequest) -> PlanningConflict:
    return PlanningConflict(
        current_request_version=request.version,
        current_proposal_revision=request.current_proposal_revision,
        current_proposal_content_hash=request.current_proposal_content_hash,
    )


def _non_negative_int(value: Mapping[str, object], name: str) -> int:
    item = value.get(name)
    if not isinstance(item, int) or isinstance(item, bool) or item < 0:
        raise ValidationError(f"{name} must be zero or positive")
    return item


def _bounded_int(value: Mapping[str, object], name: str, lower: int, upper: int) -> int:
    item = value.get(name)
    if (
        not isinstance(item, int)
        or isinstance(item, bool)
        or not lower <= item <= upper
    ):
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


def _uuid_value(value: object, name: str) -> UUID:
    try:
        return value if isinstance(value, UUID) else UUID(str(value))
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
