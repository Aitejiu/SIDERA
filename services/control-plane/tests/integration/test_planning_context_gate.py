from __future__ import annotations

from collections.abc import Sequence
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest
from sqlalchemy import select

from sidera_control_plane.adapters.fake_planner import (
    FakePlannerAdapter,
    FakePlannerConfig,
)
from sidera_control_plane.application.context_materialization import ContextMaterializer
from sidera_control_plane.application.context_requests import (
    build_planning_context_assembly_request,
)
from sidera_control_plane.application.planning_runner import PlanningRunner
from sidera_control_plane.application.ports import (
    ExactRevisionManifest,
    ExactSectionBody,
)
from sidera_control_plane.domain.commands import CommandEnvelope, CommandResult
from sidera_control_plane.domain.context_packages import (
    ContextBudgetV1,
    ContextConsumerType,
    ContextPackageManifestV1,
    ContextPurpose,
)
from sidera_control_plane.domain.modules import (
    CancellationCapability,
    IdempotencyCapability,
    ModuleDefinition,
    ModuleInstance,
    ModuleLifecycle,
    PlannerAdapterCapabilityProfile,
    StabilityLevel,
    StructuredOutputCapability,
    UsageReportingCapability,
)
from sidera_control_plane.domain.planning import PlanningRequest, PlanningRunStatus
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
    WorkContextSnapshotV1,
)
from sidera_control_plane.persistence.models import (
    ContextPackageAcknowledgementModel,
    ContextPackageMaterializationAuditModel,
)

pytestmark = pytest.mark.integration

NOW = datetime(2026, 8, 26, 12, 0, tzinfo=UTC)


class EmptyKnowledgeCatalog:
    def search(self, *args, **kwargs):
        raise NotImplementedError

    def get_revision_manifest(
        self,
        workspace_id: UUID,
        page_id: UUID,
        revision_id: UUID,
        *,
        correlation_id: UUID,
        caller_request_id: UUID,
    ) -> ExactRevisionManifest:
        raise NotImplementedError

    def materialize(
        self,
        workspace_id: UUID,
        sections: Sequence[object],
        *,
        correlation_id: UUID,
        caller_request_id: UUID,
    ) -> tuple[ExactSectionBody, ...]:
        return ()


class RecordingPlanner:
    def __init__(self, *, mismatch: bool = False) -> None:
        self.events: list[str] = []
        self.mismatch = mismatch
        self.delegate = FakePlannerAdapter(FakePlannerConfig(scenario="proposal"))

    def probe(self, config):
        return self.delegate.probe(config)

    def accept_context(self, materialization, *, operation_id):
        self.events.append("accept")
        acknowledgement = self.delegate.accept_context(
            materialization,
            operation_id=operation_id,
        )
        if self.mismatch:
            return replace(acknowledgement, package_hash="0" * 64)
        return acknowledgement

    def plan(self, planner_input, control):
        self.events.append("plan")
        return self.delegate.plan(planner_input, control)

    def cancel(self, operation_id):
        return self.delegate.cancel(operation_id)


class StaticFactory:
    def __init__(self, adapter: RecordingPlanner) -> None:
        self.adapter = adapter

    def create(self, definition, instance, profile_snapshot):
        return self.adapter


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
        context_package_versions=(1,),
    )


def planning_profile(instance_id: UUID, workspace_id: UUID) -> PlanningProfile:
    return PlanningProfile(
        id=uuid4(),
        workspace_id=workspace_id,
        profile_key=f"context-gate-{instance_id.hex[:8]}",
        version=1,
        display_name="Context gate planner",
        planner_module_instance_id=instance_id,
        planner_module_config_version=1,
        planner_interface_version="sidera.planner.v1",
        prompt_policy_version="prompt.v1",
        system_policy={},
        proposal_schema_version=2,
        allowed_capabilities=("code",),
        adapter_preferences=("fake",),
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
            backoff_seconds=(1,),
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


def _snapshot() -> WorkContextSnapshotV1:
    return WorkContextSnapshotV1.create(
        objective="Build a context-gated planner",
        constraints=(),
        acceptance_criteria=(),
        requested_outputs=(),
        clarification_decisions=(),
        accepted_decisions=(),
        unresolved_questions=(),
        knowledge_refs=(),
        artifact_refs=(),
        evidence_refs=(),
        checkpoint_refs=(),
    )


def seed_request(uow_factory, workspace_id: UUID) -> tuple[UUID, UUID]:
    definition_id = uuid4()
    instance_id = uuid4()
    profile = planning_profile(instance_id, workspace_id)
    definition = ModuleDefinition(
        id=definition_id,
        workspace_id=workspace_id,
        module_type="planner_adapter",
        provider="fake",
        interface_version="sidera.planner.v1",
        config_schema_version=1,
        capabilities=planner_capabilities(),
        lifecycle=ModuleLifecycle.ACTIVE,
    )
    instance = ModuleInstance(
        id=instance_id,
        workspace_id=workspace_id,
        definition_id=definition_id,
        instance_name="context-gate-planner",
        config_version=1,
        config_namespace="planning",
        config={"planning.scenario": "proposal"},
        secret_refs=(),
        lifecycle=ModuleLifecycle.ACTIVE,
    )
    snapshot = profile.snapshot(planner_capabilities())
    request = PlanningRequest.create(
        request_id=uuid4(),
        workspace_id=workspace_id,
        profile_snapshot=snapshot,
        objective="Build a context-gated planner",
        constraints=(),
        acceptance_criteria=("The planner receives an acknowledged package",),
        context_refs=(),
        requested_outputs=("proposal",),
        source_type="integration",
        source_ref="test:planning-context-gate",
        actor="integration",
        correlation_id=uuid4(),
        run_id=uuid4(),
        now=NOW,
    )
    context_id = uuid4()
    revision_id = uuid4()
    revision = WorkContextRevision.create(
        revision_id=revision_id,
        work_context_id=context_id,
        workspace_id=workspace_id,
        revision_number=1,
        brief_planning_request_id=request.id,
        brief_revision=request.briefs[-1].revision,
        proposal_revision_id=None,
        plan_snapshot_id=None,
        reason=WorkContextRevisionReason.REQUEST_CREATED,
        snapshot=_snapshot(),
        created_by="integration",
        created_at=NOW,
    )
    context = WorkContext.create(
        work_context_id=context_id,
        workspace_id=workspace_id,
        planning_request_id=request.id,
        first_revision=revision,
        created_at=NOW,
    )
    with uow_factory() as uow:
        uow.modules.add_definition(definition)
        uow.modules.add_instance(instance)
        uow.planning_profiles.add(profile)
        uow.planning_requests.add(request)
        uow.work_contexts.add(context, revision)
        assembly = build_planning_context_assembly_request(
            run=request.runs[-1],
            brief=request.briefs[-1],
            work_context_revision=revision,
            ttl_seconds=3600,
            created_at=NOW,
        )
        uow.context_assemblies.add(assembly)
    with uow_factory() as uow:
        claimed = uow.context_assemblies.claim_next(
            workspace_id=workspace_id,
            owner="context-package-worker:test",
            now=NOW,
            lease_ttl=timedelta(minutes=1),
            limit=1,
        )[0]
        manifest = ContextPackageManifestV1.create(
            package_id=uuid4(),
            workspace_id=workspace_id,
            purpose=ContextPurpose.PLANNING_RUN,
            consumer_type=ContextConsumerType.PLANNER,
            consumer_id=request.runs[-1].id,
            worker_item_id=None,
            work_context_revision_id=revision_id,
            context_version=None,
            control_plane_fact_snapshot={"planning_request_id": str(request.id)},
            knowledge_refs=(),
            artifact_refs=(),
            evidence_refs=(),
            budget=ContextBudgetV1(
                profile_id=uuid4(),
                profile_version=1,
                max_sections=16,
                max_chars=64_000,
                max_input_tokens=10_000,
            ),
            expires_at=NOW + timedelta(hours=1),
            idempotency_key=claimed.idempotency_key,
            created_at=NOW,
        )
        command_id = uuid4()
        command = CommandEnvelope(
            id=command_id,
            workspace_id=workspace_id,
            command_type="TestAssembleContextPackage",
            target_type="context_package",
            actor_type="system",
            actor_id="context-package-worker:test",
            target_id=manifest.package_id,
            payload={},
            expected_version=None,
            idempotency_key=f"test-context-package:{manifest.package_id}",
            correlation_id=request.correlation_id,
        )
        uow.commands.record_applied(
            command,
            "a" * 64,
            CommandResult(
                command_id=command_id,
                aggregate_id=manifest.package_id,
                aggregate_version=1,
                event_ids=(),
            ),
        )
        uow.context_assemblies.complete_with_package(
            claimed,
            manifest,
            owner="context-package-worker:test",
            expected_version=claimed.version,
            actor_type="system",
            actor_id="context-package-worker:test",
            causation_id=command_id,
            correlation_id=request.correlation_id,
        )
    return request.id, request.runs[-1].id


def runner(uow_factory, adapter, *, context_materializer) -> PlanningRunner:
    return PlanningRunner(
        uow_factory=uow_factory,
        adapter_factory=StaticFactory(adapter),
        owner_id="planning-worker:context-gate-test",
        lease_ttl_seconds=60,
        lease_renew_interval_seconds=10,
        clock=lambda: NOW,
        context_materializer=context_materializer,
    )


def test_planner_acknowledges_context_before_model_call(
    uow_factory, workspace_id: UUID
) -> None:
    request_id, run_id = seed_request(uow_factory, workspace_id)
    adapter = RecordingPlanner()

    result = runner(
        uow_factory,
        adapter,
        context_materializer=ContextMaterializer(EmptyKnowledgeCatalog()),
    ).run_once(1)

    assert result.succeeded == 1
    assert adapter.events == ["accept", "plan"]
    with uow_factory() as uow:
        request = uow.planning_requests.get(request_id, workspace_id)
        assert request is not None
        assert request.run(run_id).status is PlanningRunStatus.SUCCEEDED
        package = uow.context_packages.get_for_consumer(
            ContextPurpose.PLANNING_RUN, run_id, workspace_id
        )
        assert package is not None
        acknowledgements = uow.session.scalars(
            select(ContextPackageAcknowledgementModel).where(
                ContextPackageAcknowledgementModel.package_id == package.package_id
            )
        ).all()
        audits = uow.session.scalars(
            select(ContextPackageMaterializationAuditModel).where(
                ContextPackageMaterializationAuditModel.package_id == package.package_id
            )
        ).all()
        assert len(acknowledgements) == 1
        assert len(audits) == 1
        assert audits[0].outcome == "succeeded"


def test_context_ack_mismatch_blocks_without_model_call(
    uow_factory, workspace_id: UUID
) -> None:
    request_id, run_id = seed_request(uow_factory, workspace_id)
    adapter = RecordingPlanner(mismatch=True)

    result = runner(
        uow_factory,
        adapter,
        context_materializer=ContextMaterializer(EmptyKnowledgeCatalog()),
    ).run_once(1)

    assert result.blocked == 1
    assert adapter.events == ["accept"]
    with uow_factory() as uow:
        request = uow.planning_requests.get(request_id, workspace_id)
        assert request is not None
        assert request.run(run_id).status is PlanningRunStatus.BLOCKED
        package = uow.context_packages.get_for_consumer(
            ContextPurpose.PLANNING_RUN, run_id, workspace_id
        )
        assert package is not None
        assert uow.session.scalar(
            select(ContextPackageAcknowledgementModel).where(
                ContextPackageAcknowledgementModel.package_id == package.package_id
            )
        ) is None
