from datetime import UTC, datetime
from uuid import UUID, uuid4

import pytest

from sidera_control_plane.application.command_service import CommandService
from sidera_control_plane.application.notification_commands import (
    NotificationCommandHandler,
)
from sidera_control_plane.domain.commands import CommandEnvelope
from sidera_control_plane.domain.modules import (
    NOTIFICATION_CHANNEL_MODULE_TYPE,
    CancellationCapability,
    IdempotencyCapability,
    ModuleDefinition,
    ModuleInstance,
    ModuleLifecycle,
    NotificationChannelCapabilityProfile,
    NotificationReceiptCapability,
    PlannerAdapterCapabilityProfile,
    StabilityLevel,
    StructuredOutputCapability,
    UsageReportingCapability,
)
from sidera_control_plane.domain.notifications import NotificationBindingKind
from sidera_control_plane.domain.planning import PlanningRequest
from sidera_control_plane.domain.planning_profiles import (
    InvalidOutputPolicy,
    PlanningBudgetPolicy,
    PlanningProfile,
    PlanningProfileStatus,
    PlanningRetryPolicy,
    UnknownOutcomePolicy,
)
from sidera_control_plane.persistence.unit_of_work import SqlAlchemyUnitOfWork

pytestmark = pytest.mark.integration

NOW = datetime(2026, 9, 4, 12, 0, tzinfo=UTC)


def notification_capabilities() -> NotificationChannelCapabilityProfile:
    return NotificationChannelCapabilityProfile(
        channel_types=("akashic_private",),
        delivery_contract_versions=(1,),
        idempotency=IdempotencyCapability.OPERATION_ID,
        receipt=NotificationReceiptCapability.PROVIDER_REF,
        max_message_bytes=16_384,
        supports_deep_link=True,
        stability=StabilityLevel.EXPERIMENTAL,
    )


def planner_capabilities() -> PlannerAdapterCapabilityProfile:
    return PlannerAdapterCapabilityProfile(
        structured_output=StructuredOutputCapability.JSON_SCHEMA,
        clarification=True,
        idempotency=IdempotencyCapability.OPERATION_ID,
        cancellation=CancellationCapability.RUN,
        usage_reporting=UsageReportingCapability.TOKENS,
        max_context_bytes=128_000,
        stability=StabilityLevel.STABLE,
        interface_version="sidera.planner.v1",
        proposal_schema_versions=(2,),
    )


def planning_profile(workspace_id: UUID, instance_id: UUID) -> PlanningProfile:
    return PlanningProfile(
        id=uuid4(),
        workspace_id=workspace_id,
        profile_key=f"notification-origin-{uuid4().hex[:8]}",
        version=1,
        display_name="Notification origin planner",
        planner_module_instance_id=instance_id,
        planner_module_config_version=1,
        planner_interface_version="sidera.planner.v1",
        prompt_policy_version="prompt.v1",
        system_policy={"approval": {"required": True}},
        proposal_schema_version=2,
        allowed_capabilities=("code",),
        adapter_preferences=("opencode",),
        adapter_forbidden=(),
        budget=PlanningBudgetPolicy(
            max_context_bytes=64_000,
            max_tokens=10_000,
            max_cost_microunits=1_000_000,
            max_runtime_seconds=600,
            max_revisions=3,
            max_nodes=20,
            max_concurrency=2,
        ),
        retry_policy=PlanningRetryPolicy(
            max_retries=1,
            backoff_seconds=(5,),
            unknown_outcome_policy=UnknownOutcomePolicy.BLOCK,
            invalid_output_policy=InvalidOutputPolicy.RETRY,
        ),
        default_priority=50,
        execution_max_attempts=2,
        execution_budget_level="standard",
        user_selectable=True,
        status=PlanningProfileStatus.ACTIVE,
        planner_secret_refs=(),
    )


def registration_command(
    workspace_id: UUID, module_instance_id: UUID, source_ref: str
) -> CommandEnvelope:
    return CommandEnvelope(
        id=uuid4(),
        workspace_id=workspace_id,
        command_type="RegisterNotificationRoute",
        target_type="notification_route",
        actor_type="local",
        actor_id="operator",
        target_id=None,
        payload={
            "channel_module_instance_id": str(module_instance_id),
            "channel_type": "akashic_private",
            "recipient_type": "workspace_operator",
            "recipient_id": "local",
            "source_ref": source_ref,
        },
        expected_version=None,
        idempotency_key=uuid4().hex,
        correlation_id=uuid4(),
    )


def test_late_route_registration_repairs_exact_persisted_private_request(
    database, workspace_id: UUID
) -> None:
    notification_definition_id = uuid4()
    notification_instance_id = uuid4()
    planner_definition_id = uuid4()
    planner_instance_id = uuid4()
    source_ref = "src_" + "integration" * 4
    profile = planning_profile(workspace_id, planner_instance_id)
    request = PlanningRequest.create(
        request_id=uuid4(),
        workspace_id=workspace_id,
        profile_snapshot=profile.snapshot(planner_capabilities()),
        objective="Preserve notification origin",
        constraints=("Bind only exact source references",),
        acceptance_criteria=("Late registration repairs the request",),
        context_refs=(),
        requested_outputs=("proposal",),
        source_type="akashic_private",
        source_ref=source_ref,
        actor="operator",
        correlation_id=uuid4(),
        run_id=uuid4(),
        now=NOW,
    )

    with SqlAlchemyUnitOfWork(database) as uow:
        uow.modules.add_definition(
            ModuleDefinition(
                id=notification_definition_id,
                workspace_id=workspace_id,
                module_type=NOTIFICATION_CHANNEL_MODULE_TYPE,
                provider="akashic",
                interface_version="sidera.notification.v1",
                config_schema_version=1,
                capabilities=notification_capabilities(),
                lifecycle=ModuleLifecycle.ACTIVE,
            )
        )
        uow.modules.add_instance(
            ModuleInstance(
                id=notification_instance_id,
                workspace_id=workspace_id,
                definition_id=notification_definition_id,
                instance_name="akashic-private",
                config_version=1,
                config_namespace="notification",
                config={"notification.mode": "integration"},
                secret_refs=(),
                lifecycle=ModuleLifecycle.ACTIVE,
            )
        )
        uow.modules.add_definition(
            ModuleDefinition(
                id=planner_definition_id,
                workspace_id=workspace_id,
                module_type="planner_adapter",
                provider="fake",
                interface_version="sidera.planner.v1",
                config_schema_version=1,
                capabilities=planner_capabilities(),
                lifecycle=ModuleLifecycle.ACTIVE,
            )
        )
        uow.modules.add_instance(
            ModuleInstance(
                id=planner_instance_id,
                workspace_id=workspace_id,
                definition_id=planner_definition_id,
                instance_name="planning",
                config_version=1,
                config_namespace="planning",
                config={"planning.mode": "integration"},
                secret_refs=(),
                lifecycle=ModuleLifecycle.ACTIVE,
            )
        )
        uow.planning_profiles.add(profile)
        uow.planning_requests.add(request)

    result = CommandService(
        lambda: SqlAlchemyUnitOfWork(database),
        (
            NotificationCommandHandler(
                local_actor_type="local", local_actor_id="operator", clock=lambda: NOW
            ),
        ),
    ).handle(registration_command(workspace_id, notification_instance_id, source_ref))
    assert result.data["request_bindings_created"] == 1
    assert result.data["root_bindings_created"] == 0

    with SqlAlchemyUnitOfWork(database) as uow:
        binding = uow.notifications.get_binding(
            workspace_id,
            "planning_request",
            request.id,
            NotificationBindingKind.ORIGIN.value,
        )
        assert binding is not None
        assert binding.notification_route_id == UUID(
            result.data["notification_route_id"]
        )
