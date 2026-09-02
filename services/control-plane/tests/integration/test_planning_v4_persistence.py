from datetime import UTC, datetime
from uuid import UUID, uuid4

import pytest

from sidera_control_plane.domain.collaboration import (
    CollaborationRuntimeBindingSnapshot,
)
from sidera_control_plane.domain.modules import (
    CancellationCapability,
    CollaborationRuntimeCapabilityProfile,
    IdempotencyCapability,
    PlannerAdapterCapabilityProfile,
    StabilityLevel,
    StructuredOutputCapability,
    UsageReportingCapability,
)
from sidera_control_plane.domain.planning import PlanningRequest
from sidera_control_plane.domain.planning_profiles import (
    InvalidOutputPolicy,
    PlanningBudgetPolicy,
    PlanningProfile,
    PlanningProfileStatus,
    PlanningRetryPolicy,
    UnknownOutcomePolicy,
)
from sidera_control_plane.domain.planning_proposals import (
    PlanningProfileDescriptor,
    PlanningProposalPayloadV4,
    ProposalRevision,
)
from sidera_control_plane.persistence.models import (
    ModuleDefinitionModel,
    ModuleInstanceModel,
)
from sidera_control_plane.persistence.planning_repository import (
    PlanningProfileRepository,
    PlanningRecordRepository,
    PlanningRequestRepository,
)
from tests.unit.test_planning_proposals_v4 import proposal_v4

pytestmark = pytest.mark.integration

NOW = datetime(2026, 9, 3, 12, 0, tzinfo=UTC)


def _planner_capabilities() -> PlannerAdapterCapabilityProfile:
    return PlannerAdapterCapabilityProfile(
        structured_output=StructuredOutputCapability.JSON_SCHEMA,
        clarification=True,
        idempotency=IdempotencyCapability.OPERATION_ID,
        cancellation=CancellationCapability.RUN,
        usage_reporting=UsageReportingCapability.TOKENS,
        max_context_bytes=128_000,
        stability=StabilityLevel.STABLE,
        interface_version="sidera.planner.v1",
        proposal_schema_versions=(4,),
    )


def _runtime_binding() -> CollaborationRuntimeBindingSnapshot:
    capability = CollaborationRuntimeCapabilityProfile(
        interface_version="collaboration-runtime-v1",
        projection_schema_versions=(1,),
        decision_schema_versions=(1,),
        strategy_ids=("all_required",),
        idempotency=IdempotencyCapability.OPERATION_ID,
        max_projection_bytes=262_144,
        max_decision_bytes=65_536,
        max_intents_per_decision=128,
        max_strategy_state_bytes=16_384,
        stability=StabilityLevel.EXPERIMENTAL,
    )
    return CollaborationRuntimeBindingSnapshot(
        runtime_definition_id=uuid4(),
        runtime_instance_id=uuid4(),
        runtime_interface_version=capability.interface_version,
        runtime_config_version=3,
        runtime_capability_snapshot=capability,
    )


def _profile(workspace_id: UUID, planner_instance_id: UUID) -> PlanningProfile:
    return PlanningProfile(
        id=uuid4(),
        workspace_id=workspace_id,
        profile_key="v4-integration",
        version=1,
        display_name="v4 integration planner",
        planner_module_instance_id=planner_instance_id,
        planner_module_config_version=1,
        planner_interface_version="sidera.planner.v1",
        prompt_policy_version="prompt.v4",
        system_policy={"approval": {"required": True}},
        proposal_schema_version=4,
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
        planner_secret_refs=("secret://planner/key",),
        collaboration_runtime_binding=_runtime_binding(),
    )


def test_v4_profile_snapshot_and_proposal_round_trip_without_runtime_config_body(
    database, workspace_id: UUID
) -> None:
    planner_definition_id = uuid4()
    planner_instance_id = uuid4()
    profile = _profile(workspace_id, planner_instance_id)
    planner_capabilities = _planner_capabilities()
    profile_snapshot = profile.snapshot(planner_capabilities)
    request = PlanningRequest.create(
        request_id=uuid4(),
        workspace_id=workspace_id,
        profile_snapshot=profile_snapshot,
        objective="Persist a v4 proposal",
        constraints=(),
        acceptance_criteria=("The v4 proposal is recoverable",),
        context_refs=(),
        requested_outputs=("proposal",),
        source_type="integration",
        source_ref="test:planning-v4",
        actor="integration",
        correlation_id=uuid4(),
        run_id=uuid4(),
        now=NOW,
    )

    with database.session() as session:
        session.add(
            ModuleDefinitionModel(
                id=planner_definition_id,
                workspace_id=workspace_id,
                module_type="planner_adapter",
                provider="fake",
                interface_version="sidera.planner.v1",
                config_schema_version=1,
                capabilities={},
                lifecycle="active",
            )
        )
        session.flush()
        session.add(
            ModuleInstanceModel(
                id=planner_instance_id,
                workspace_id=workspace_id,
                definition_id=planner_definition_id,
                instance_name="v4-planner",
                config_version=1,
                config_namespace="planning",
                config={},
                secret_refs=[],
                lifecycle="active",
            )
        )
        PlanningProfileRepository(session).add(profile)
        session.flush()
        PlanningRequestRepository(session).add(request)
        session.commit()

    with database.session() as session:
        stored_profile = PlanningProfileRepository(session).get(
            profile.id, workspace_id, version=1
        )
        stored_request = PlanningRequestRepository(session).get(
            request.id, workspace_id
        )

        assert stored_profile is not None
        assert stored_request is not None
        assert stored_profile.collaboration_runtime_binding == (
            profile.collaboration_runtime_binding
        )
        assert stored_request.profile_snapshot.collaboration_runtime_binding == (
            profile.collaboration_runtime_binding
        )
        assert "runtime.mode" not in repr(stored_profile.collaboration_runtime_binding)
        assert "secret://runtime" not in repr(stored_profile.collaboration_runtime_binding)

        payload = proposal_v4()
        proposal = ProposalRevision.create(
            planning_request_id=request.id,
            revision=1,
            brief_revision=1,
            planning_run_id=request.current_run_id,
            kind=request.kind,
            payload=payload,
            profile_descriptor=PlanningProfileDescriptor.from_snapshot(
                profile_snapshot
            ),
            profile_snapshot=profile_snapshot,
            created_at=NOW,
        )
        PlanningRecordRepository(session).add_proposal(proposal, workspace_id)
        session.commit()

    with database.session() as session:
        restored = PlanningRecordRepository(session).get_proposal_revision(
            request.id, workspace_id, 1
        )

    assert restored is not None
    assert isinstance(restored.payload, PlanningProposalPayloadV4)
    assert restored.payload.collaboration_policy_request == (
        proposal_v4().collaboration_policy_request
    )
    assert restored.content_hash == proposal.content_hash
