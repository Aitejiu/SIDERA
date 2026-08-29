"""Seed an isolated R5.1 Planning workspace for browser tests."""

from __future__ import annotations

import argparse
import json
import os
from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Any, cast
from uuid import UUID

from sqlalchemy import delete, select, text, update

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
from sidera_control_plane.domain.planning_profiles import (
    InvalidOutputPolicy,
    PlanningBudgetPolicy,
    PlanningProfile,
    PlanningProfileStatus,
    PlanningRetryPolicy,
    UnknownOutcomePolicy,
)
from sidera_control_plane.persistence.database import Database
from sidera_control_plane.persistence.models import (
    ArtifactModel,
    AssignmentLeaseModel,
    AttentionAcknowledgementModel,
    CommandModel,
    ContextAssemblyRequestModel,
    ContextPackageAcknowledgementModel,
    ContextPackageKnowledgeRefModel,
    ContextPackageMaterializationAuditModel,
    ContextPackageModel,
    ContextPatchAcknowledgementModel,
    ContextPatchModel,
    EventModel,
    EvidenceClaimModel,
    ExecutionBindingModel,
    ExecutionEventModel,
    ExecutionInputModel,
    ExecutionModel,
    ExecutionRequestDependencyModel,
    ExecutionRequestModel,
    ExecutionSessionModel,
    ExecutionWorkspaceModel,
    GatewayOperationModel,
    KnowledgeCandidateSubmissionModel,
    ModuleDefinitionModel,
    ModuleInstanceModel,
    OutboxMessageModel,
    PlanEntityLineageModel,
    PlanningBriefRevisionModel,
    PlanningClarificationModel,
    PlanningProfileModel,
    PlanningProposalRevisionModel,
    PlanningRequestModel,
    PlanningRunLeaseModel,
    PlanningRunModel,
    PlanSnapshotModel,
    PlanTransitionModel,
    ProcessDescriptorModel,
    ProjectionChangeModel,
    ProposalMaterializationModel,
    RootTaskPlanHeadModel,
    SchedulingBlockModel,
    TaskModel,
    WorkContextModel,
    WorkContextRevisionModel,
    WorkerItemModel,
    WorkerItemPlacementModel,
    WorkerModel,
    WorkerRuntimeConfigurationModel,
    WorkerTransitionModel,
    WorkspaceModel,
    WorkspaceProjectionCursorModel,
)
from sidera_control_plane.persistence.unit_of_work import SqlAlchemyUnitOfWork

NOW = datetime(2026, 8, 23, 12, 0, tzinfo=UTC)
DEFAULT_WORKSPACE_ID = UUID("20000000-0000-0000-0000-000000000002")
MODULE_DEFINITION_ID = UUID("b2000000-0000-0000-0000-000000000001")
MODULE_INSTANCE_ID = UUID("b3000000-0000-0000-0000-000000000001")
PROFILE_ID = UUID("b4000000-0000-0000-0000-000000000001")
WORKSPACE_NAME = "SIDERA R5.1 Planning E2E Workspace"
SCENARIOS = ("proposal", "clarification", "scale", "real")


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--scenario", choices=SCENARIOS, default="proposal")
    parser.add_argument("--workspace-id", default=str(DEFAULT_WORKSPACE_ID))
    args = parser.parse_args(argv)

    if os.environ.get("SIDERA_ALLOW_E2E_SEED") != "1":
        raise SystemExit("SIDERA_ALLOW_E2E_SEED=1 is required for the E2E seed")
    if args.scenario == "real" and os.environ.get("SIDERA_RUN_REAL_OPENCODE") != "1":
        raise SystemExit(
            "SIDERA_RUN_REAL_OPENCODE=1 is required for the real planning seed"
        )
    database_url = os.environ.get("SIDERA_DATABASE_URL")
    if not database_url:
        raise SystemExit("SIDERA_DATABASE_URL is required for the E2E seed")
    try:
        workspace_id = UUID(args.workspace_id)
    except ValueError as exc:
        raise SystemExit("--workspace-id must be a UUID") from exc

    database = Database(database_url)
    try:
        with database.session() as session:
            _clean_workspace(session, workspace_id)
            session.add(
                WorkspaceModel(
                    id=workspace_id,
                    name=WORKSPACE_NAME,
                    status="active",
                    version=1,
                    created_at=NOW,
                    updated_at=NOW,
                )
            )
            session.commit()

        definition, instance, profile = _planning_facts(workspace_id, args.scenario)
        with SqlAlchemyUnitOfWork(database) as uow:
            uow.modules.add_definition(definition)
            uow.modules.add_instance(instance)
            uow.planning_profiles.add(profile)
    finally:
        database.dispose()

    print(
        json.dumps(
            {
                "workspace_id": str(workspace_id),
                "module_definition_id": str(MODULE_DEFINITION_ID),
                "module_instance_id": str(MODULE_INSTANCE_ID),
                "profile_id": str(PROFILE_ID),
                "scenario": args.scenario,
            },
            sort_keys=True,
        )
    )
    return 0


def _clean_workspace(session, workspace_id: UUID) -> None:
    existing = session.get(WorkspaceModel, workspace_id)
    if existing is not None and existing.name != WORKSPACE_NAME:
        raise RuntimeError(
            f"refusing to clean workspace {workspace_id}: unexpected name {existing.name!r}"
        )

    session.execute(
        update(PlanningRequestModel)
        .where(PlanningRequestModel.workspace_id == workspace_id)
        .values(
            current_brief_revision=None,
            current_proposal_revision=None,
            current_proposal_content_hash=None,
        )
    )
    session.execute(
        delete(ContextPackageKnowledgeRefModel).where(
            ContextPackageKnowledgeRefModel.package_id.in_(
                select(ContextPackageModel.id).where(
                    ContextPackageModel.workspace_id == workspace_id
                )
            )
        )
    )
    session.execute(
        text(
            "ALTER TABLE work_contexts "
            "DROP CONSTRAINT IF EXISTS fk_work_contexts_current_revision"
        )
    )
    session.execute(
        update(ExecutionModel)
        .where(ExecutionModel.workspace_id == workspace_id)
        .values(
            active_lease_id=None,
            source_execution_id=None,
            source_session_id=None,
            worker_item_id=None,
        )
    )
    models = (
        AttentionAcknowledgementModel,
        SchedulingBlockModel,
        ProjectionChangeModel,
        WorkspaceProjectionCursorModel,
        ContextPackageAcknowledgementModel,
        ContextPackageMaterializationAuditModel,
        ContextPackageModel,
        ContextAssemblyRequestModel,
        ContextPatchAcknowledgementModel,
        ContextPatchModel,
        PlanEntityLineageModel,
        WorkerTransitionModel,
        KnowledgeCandidateSubmissionModel,
        WorkContextRevisionModel,
        WorkContextModel,
        ExecutionRequestDependencyModel,
        ExecutionRequestModel,
        ProposalMaterializationModel,
        PlanTransitionModel,
        PlanningProposalRevisionModel,
        PlanningClarificationModel,
        PlanningRunLeaseModel,
        PlanningRunModel,
        PlanningBriefRevisionModel,
        PlanningRequestModel,
        PlanningProfileModel,
        WorkerItemPlacementModel,
        RootTaskPlanHeadModel,
        PlanSnapshotModel,
        ExecutionInputModel,
        EvidenceClaimModel,
        ArtifactModel,
        ExecutionEventModel,
        GatewayOperationModel,
        ProcessDescriptorModel,
        ExecutionWorkspaceModel,
        ExecutionBindingModel,
        ExecutionSessionModel,
        AssignmentLeaseModel,
        ExecutionModel,
        WorkerRuntimeConfigurationModel,
        ModuleInstanceModel,
        ModuleDefinitionModel,
        WorkerItemModel,
        WorkerModel,
        OutboxMessageModel,
        EventModel,
        CommandModel,
        TaskModel,
    )
    try:
        for model in models:
            if model is OutboxMessageModel:
                session.execute(
                    delete(model).where(
                        model.event_id.in_(
                            select(EventModel.id).where(
                                EventModel.workspace_id == workspace_id
                            )
                        )
                    )
                )
            else:
                session.execute(
                    delete(model).where(cast(Any, model).workspace_id == workspace_id)
                )
    finally:
        session.execute(
            text(
                "ALTER TABLE work_contexts "
                "ADD CONSTRAINT fk_work_contexts_current_revision "
                "FOREIGN KEY (current_revision_id) REFERENCES work_context_revisions (id) "
                "ON DELETE RESTRICT DEFERRABLE INITIALLY DEFERRED"
            )
        )
    if existing is not None:
        session.execute(delete(WorkspaceModel).where(WorkspaceModel.id == workspace_id))
    session.flush()


def _planning_facts(
    workspace_id: UUID, scenario: str
) -> tuple[ModuleDefinition, ModuleInstance, PlanningProfile]:
    is_real = scenario == "real"
    capabilities = PlannerAdapterCapabilityProfile(
        structured_output=(
            StructuredOutputCapability.JSON
            if is_real
            else StructuredOutputCapability.JSON_SCHEMA
        ),
        clarification=True,
        idempotency=(
            IdempotencyCapability.NONE
            if is_real
            else IdempotencyCapability.OPERATION_ID
        ),
        cancellation=(
            CancellationCapability.PROCESS
            if is_real
            else CancellationCapability.RUN
        ),
        usage_reporting=UsageReportingCapability.TOKENS,
        max_context_bytes=128_000,
        stability=StabilityLevel.EXPERIMENTAL if is_real else StabilityLevel.STABLE,
        interface_version="sidera.planner.v1",
        proposal_schema_versions=(2, 3) if not is_real else (2,),
        context_package_versions=(1,),
    )
    executable = os.environ.get("SIDERA_OPENCODE_EXECUTABLE", "opencode")
    model = os.environ.get("SIDERA_OPENCODE_MODEL", "deepseek/deepseek-chat")
    definition = ModuleDefinition(
        id=MODULE_DEFINITION_ID,
        workspace_id=workspace_id,
        module_type="planner_adapter",
        provider="opencode-acp-planner" if is_real else "fake",
        interface_version="sidera.planner.v1",
        config_schema_version=1,
        capabilities=capabilities,
        lifecycle=ModuleLifecycle.ACTIVE,
    )
    instance = ModuleInstance(
        id=MODULE_INSTANCE_ID,
        workspace_id=workspace_id,
        definition_id=definition.id,
        instance_name="opencode-r5-planner" if is_real else "fake-r5-planner",
        config_version=1,
        config_namespace="planning",
        config=(
            {"planning.executable": executable, "planning.model": model}
            if is_real
            else {"planning.scenario": scenario}
        ),
        secret_refs=(),
        lifecycle=ModuleLifecycle.ACTIVE,
    )
    profile = PlanningProfile(
        id=PROFILE_ID,
        workspace_id=workspace_id,
        profile_key="r5-e2e",
        version=1,
        display_name="R5.1 OpenCode Planner" if is_real else "R5.1 Fake Planner",
        planner_module_instance_id=instance.id,
        planner_module_config_version=1,
        planner_interface_version="sidera.planner.v1",
        prompt_policy_version="r5.1-e2e",
        system_policy={},
        proposal_schema_version=2,
        allowed_capabilities=("code",),
        adapter_preferences=("opencode",) if is_real else ("fake",),
        adapter_forbidden=(),
        budget=PlanningBudgetPolicy(
            max_context_bytes=64_000,
            max_tokens=100_000,
            max_cost_microunits=1_000_000_000,
            max_runtime_seconds=3_600,
            max_revisions=20,
            max_nodes=500,
            max_concurrency=4,
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
    if scenario not in SCENARIOS:
        raise ValueError(f"unsupported R5.1 seed scenario: {scenario}")
    return definition, instance, profile


if __name__ == "__main__":
    raise SystemExit(main())
