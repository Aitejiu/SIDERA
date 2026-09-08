"""Seed a deterministic, isolated R4 workspace for browser tests."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from typing import Any, cast
from uuid import NAMESPACE_URL, UUID, uuid5

from sqlalchemy import delete, insert, select, text, update

from sidera_control_plane.domain.context_packages import (
    ContextAssemblyRequest,
    ContextBudgetV1,
    ContextConsumerType,
    ContextPackageKnowledgeRef,
    ContextPackageManifestV1,
    ContextPurpose,
    ContextSelectionReason,
    ContextTrustLevel,
)
from sidera_control_plane.domain.plan_revisions import plan_snapshot_content_hash
from sidera_control_plane.domain.plans import (
    DependencyEnforcement,
    DependencyKind,
    SubtaskDependency,
    SubtaskMilestone,
    SubtaskPlanItem,
    TaskPlanSnapshot,
    WorkerItemDependency,
    WorkerItemPlanItem,
    WorkItemKind,
)
from sidera_control_plane.domain.work_context import (
    KnowledgeRevisionRef,
    WorkContext,
    WorkContextRevision,
    WorkContextRevisionReason,
    WorkContextSnapshotV1,
)
from sidera_control_plane.persistence.context_package_repository import (
    ContextAssemblyRepository,
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
    PlanningBriefRevisionModel,
    PlanningClarificationModel,
    PlanningProfileModel,
    PlanningProposalRevisionModel,
    PlanningRequestModel,
    PlanningRunLeaseModel,
    PlanningRunModel,
    PlanSnapshotModel,
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
    WorkspaceModel,
    WorkspaceProjectionCursorModel,
)
from sidera_control_plane.persistence.work_context_repository import (
    WorkContextRepository,
)

NOW = datetime(2026, 8, 21, 12, 0, tzinfo=UTC)
DEFAULT_WORKSPACE_ID = UUID("20000000-0000-0000-0000-000000000001")
ROOT_TASK_ID = UUID("30000000-0000-0000-0000-000000000001")
BACKEND_TASK_ID = UUID("30000000-0000-0000-0000-000000000002")
FRONTEND_TASK_ID = UUID("30000000-0000-0000-0000-000000000003")
INTEGRATION_TASK_ID = UUID("30000000-0000-0000-0000-000000000004")
SECOND_ROOT_TASK_ID = UUID("30000000-0000-0000-0000-000000000005")
SECOND_SUBTASK_TASK_ID = UUID("30000000-0000-0000-0000-000000000006")
PREVIOUS_EXECUTION_ID = UUID("60000000-0000-0000-0000-000000000003")
ATTENTION_EXECUTION_ID = UUID("60000000-0000-0000-0000-000000000004")
BACKEND_EXECUTION_ID = UUID("60000000-0000-0000-0000-000000000001")
SCALE_EXECUTION_ID = UUID("60000000-0000-0000-0000-000000000002")
SCALE_WORKER_EXECUTION_ID = UUID("60000000-0000-0000-0000-000000000005")
RUNTIME_EXECUTION_ID = UUID("60000000-0000-0000-0000-000000000006")
PREVIOUS_SESSION_ID = UUID("70000000-0000-0000-0000-000000000003")
ATTENTION_SESSION_ID = UUID("70000000-0000-0000-0000-000000000004")
SESSION_ID = UUID("70000000-0000-0000-0000-000000000001")
SCALE_SESSION_ID = UUID("70000000-0000-0000-0000-000000000002")
SCALE_WORKER_SESSION_ID = UUID("70000000-0000-0000-0000-000000000005")
WORKER_ID = UUID("80000000-0000-0000-0000-000000000001")
SCALE_WORKER_ID = UUID("80000000-0000-0000-0000-000000000002")
RUNTIME_WORKER_ID = UUID("80000000-0000-0000-0000-000000000003")
LEASE_ID = UUID("90000000-0000-0000-0000-000000000001")
ARTIFACT_ID = UUID("a0000000-0000-0000-0000-000000000001")
GENERATION_ONE_ID = UUID("50000000-0000-0000-0000-000000000001")
GENERATION_TWO_ID = UUID("50000000-0000-0000-0000-000000000002")
SECOND_GENERATION_ONE_ID = UUID("50000000-0000-0000-0000-000000000003")
SECOND_GENERATION_TWO_ID = UUID("50000000-0000-0000-0000-000000000004")
IMPLEMENTER_WORKER_ITEM_ID = UUID("a1000000-0000-0000-0000-000000000001")
TESTER_WORKER_ITEM_ID = UUID("a1000000-0000-0000-0000-000000000002")
REVIEWER_WORKER_ITEM_ID = UUID("a1000000-0000-0000-0000-000000000003")
MIGRATED_WORKER_ITEM_ID = UUID("a1000000-0000-0000-0000-000000000004")
LEGACY_WORKER_ITEM_ID = UUID("a1000000-0000-0000-0000-000000000005")
SCALE_WORKER_ITEM_ID = UUID("a1000000-0000-0000-0000-000000000101")
CONTEXT_PLANNING_REQUEST_ID = UUID("b0000000-0000-0000-0000-000000000001")
CONTEXT_ID = UUID("b0000000-0000-0000-0000-000000000002")
CONTEXT_REVISION_ID = UUID("b0000000-0000-0000-0000-000000000003")
CONTEXT_EXECUTION_ASSEMBLY_ID = UUID("b0000000-0000-0000-0000-000000000004")
CONTEXT_EXECUTION_PACKAGE_ID = UUID("b0000000-0000-0000-0000-000000000005")
CONTEXT_PLANNING_ASSEMBLY_ID = UUID("b0000000-0000-0000-0000-000000000006")
CONTEXT_PLANNING_PACKAGE_ID = UUID("b0000000-0000-0000-0000-000000000007")
CONTEXT_PLANNING_RUN_ID = UUID("b0000000-0000-0000-0000-000000000008")
CONTEXT_PLANNING_PROFILE_ID = UUID("b0000000-0000-0000-0000-000000000009")
CONTEXT_SUBMISSION_ID = UUID("b0000000-0000-0000-0000-00000000000a")
KNOWLEDGE_SOURCE_ID = UUID("c0000000-0000-0000-0000-000000000001")
KNOWLEDGE_PAGE_ID = UUID("c0000000-0000-0000-0000-000000000002")
KNOWLEDGE_REVISION_ONE_ID = UUID("c0000000-0000-0000-0000-000000000003")
KNOWLEDGE_REVISION_TWO_ID = UUID("c0000000-0000-0000-0000-000000000004")
KNOWLEDGE_PENDING_CANDIDATE_ID = UUID("c0000000-0000-0000-0000-000000000005")
KNOWLEDGE_STALE_CANDIDATE_ID = UUID("c0000000-0000-0000-0000-000000000006")
WORKSPACE_NAME = "SIDERA R4 E2E Workspace"


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--scenario",
        choices=("task-first", "scale", "runtime-routing", "context-knowledge"),
        default="task-first",
    )
    parser.add_argument("--workspace-id", default=str(DEFAULT_WORKSPACE_ID))
    args = parser.parse_args(argv)

    if os.environ.get("SIDERA_ALLOW_E2E_SEED") != "1":
        raise SystemExit("SIDERA_ALLOW_E2E_SEED=1 is required for the E2E seed")
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
            if args.scenario == "scale":
                _seed_scale(session, workspace_id)
            elif args.scenario == "runtime-routing":
                _seed_runtime_routing(session, workspace_id)
            elif args.scenario == "context-knowledge":
                _seed_context_knowledge(session, workspace_id)
            else:
                _seed_task_first(session, workspace_id)
            session.commit()
    finally:
        database.dispose()

    execution_id = (
        SCALE_EXECUTION_ID
        if args.scenario == "scale"
        else RUNTIME_EXECUTION_ID
        if args.scenario == "runtime-routing"
        else BACKEND_EXECUTION_ID
    )
    session_id = (
        SCALE_SESSION_ID
        if args.scenario == "scale"
        else None
        if args.scenario == "runtime-routing"
        else SESSION_ID
    )
    print(
        json.dumps(
            {
                "workspace_id": str(workspace_id),
                "root_task_id": str(ROOT_TASK_ID),
                "second_root_task_id": str(SECOND_ROOT_TASK_ID),
                "subtask_key": "callback-idempotency",
                "worker_item_ids": {
                    "implementer": str(IMPLEMENTER_WORKER_ITEM_ID),
                    "tester": str(TESTER_WORKER_ITEM_ID),
                    "reviewer": str(REVIEWER_WORKER_ITEM_ID),
                    "legacy": str(LEGACY_WORKER_ITEM_ID),
                },
                "scale_worker_item_id": (
                    str(SCALE_WORKER_ITEM_ID) if args.scenario == "scale" else None
                ),
                "execution_id": str(execution_id),
                "session_id": str(session_id) if session_id is not None else None,
                "runtime_worker_id": (
                    str(RUNTIME_WORKER_ID)
                    if args.scenario == "runtime-routing"
                    else None
                ),
                "context_package_id": (
                    str(CONTEXT_EXECUTION_PACKAGE_ID)
                    if args.scenario == "context-knowledge"
                    else None
                ),
                "context_planning_run_id": (
                    str(CONTEXT_PLANNING_RUN_ID)
                    if args.scenario == "context-knowledge"
                    else None
                ),
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
        update(ExecutionModel)
        .where(ExecutionModel.workspace_id == workspace_id)
        .values(active_lease_id=None, source_execution_id=None, source_session_id=None)
    )
    models = (
        AttentionAcknowledgementModel,
        SchedulingBlockModel,
        ProjectionChangeModel,
        WorkspaceProjectionCursorModel,
        ContextPackageMaterializationAuditModel,
        ContextPackageAcknowledgementModel,
        ContextPackageModel,
        ContextAssemblyRequestModel,
        KnowledgeCandidateSubmissionModel,
        WorkContextRevisionModel,
        WorkContextModel,
        ExecutionRequestDependencyModel,
        ExecutionRequestModel,
        ProposalMaterializationModel,
        PlanningProposalRevisionModel,
        PlanningClarificationModel,
        PlanningRunLeaseModel,
        PlanningRunModel,
        RootTaskPlanHeadModel,
        PlanningBriefRevisionModel,
        PlanningRequestModel,
        PlanningProfileModel,
        WorkerItemPlacementModel,
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
    session.execute(
        text(
            "ALTER TABLE work_contexts "
            "DROP CONSTRAINT IF EXISTS fk_work_contexts_current_revision"
        )
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


def _seed_task_first(session, workspace_id: UUID) -> None:
    first = _snapshot(
        workspace_id,
        generation=1,
        snapshot_id=GENERATION_ONE_ID,
        parent=None,
        include_docs=False,
    )
    second = _snapshot(
        workspace_id,
        generation=2,
        snapshot_id=GENERATION_TWO_ID,
        parent=GENERATION_ONE_ID,
        include_docs=True,
    )
    second_root_first = _secondary_snapshot(
        workspace_id,
        generation=1,
        snapshot_id=SECOND_GENERATION_ONE_ID,
        parent=None,
    )
    second_root_second = _secondary_snapshot(
        workspace_id,
        generation=2,
        snapshot_id=SECOND_GENERATION_TWO_ID,
        parent=SECOND_GENERATION_ONE_ID,
    )
    adapter_definition_id = uuid5(NAMESPACE_URL, f"{workspace_id}:adapter-definition")
    workspace_definition_id = uuid5(
        NAMESPACE_URL, f"{workspace_id}:workspace-definition"
    )
    adapter_instance_id = uuid5(NAMESPACE_URL, f"{workspace_id}:adapter-instance")
    workspace_instance_id = uuid5(NAMESPACE_URL, f"{workspace_id}:workspace-instance")
    capabilities = _capabilities()

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
    session.flush()
    session.add_all(
        [
            TaskModel(
                id=ROOT_TASK_ID,
                workspace_id=workspace_id,
                title="支付重构",
                description="观察并干预支付回调重构的并行 Worker 工作。",
                acceptance_criteria=["回调幂等已实现", "回调测试已通过"],
                status="running",
                priority=50,
                requested_by="e2e",
                version=1,
                created_at=NOW,
                updated_at=NOW,
            ),
            TaskModel(
                id=BACKEND_TASK_ID,
                workspace_id=workspace_id,
                title="回调幂等",
                description="实现回调幂等写入和重试策略。",
                acceptance_criteria=["实现回调幂等"],
                status="running",
                priority=60,
                requested_by="e2e",
                version=2,
                created_at=NOW,
                updated_at=NOW + timedelta(seconds=4),
            ),
            TaskModel(
                id=FRONTEND_TASK_ID,
                workspace_id=workspace_id,
                title="回调测试",
                description="验证重复通知、并发和失败重试。",
                acceptance_criteria=["并发测试通过"],
                status="blocked",
                priority=50,
                requested_by="e2e",
                version=1,
                created_at=NOW,
                updated_at=NOW + timedelta(seconds=2),
            ),
            TaskModel(
                id=INTEGRATION_TASK_ID,
                workspace_id=workspace_id,
                title="集成验收",
                description="汇总回调实现和测试证据。",
                acceptance_criteria=["集成证据齐全"],
                status="pending",
                priority=40,
                requested_by="e2e",
                version=1,
                created_at=NOW,
                updated_at=NOW,
            ),
            TaskModel(
                id=SECOND_ROOT_TASK_ID,
                workspace_id=workspace_id,
                title="发布监控升级",
                description="第二个 Root Task 用于验证多任务标签切换。",
                acceptance_criteria=["监控面板可用"],
                status="running",
                priority=30,
                requested_by="e2e",
                version=1,
                created_at=NOW,
                updated_at=NOW,
            ),
            TaskModel(
                id=SECOND_SUBTASK_TASK_ID,
                workspace_id=workspace_id,
                title="发布检查",
                description="保留历史 WorkerItem 的发布检查。",
                acceptance_criteria=["旧 WorkerItem 可回看"],
                status="completed",
                priority=30,
                requested_by="e2e",
                version=1,
                created_at=NOW,
                updated_at=NOW,
            ),
        ]
    )
    session.add_all(
        [
            WorkerModel(
                id=WORKER_ID,
                workspace_id=workspace_id,
                adapter_type="opencode",
                display_name="OpenCode E2E Worker",
                status="busy",
                desired_state="active",
                capabilities=["python", "typescript"],
                capacity=1,
                current_load=1,
                cost_per_hour_microunits=1,
                startup_latency_ms=10,
                last_heartbeat_at=NOW,
                version=2,
                created_at=NOW,
                updated_at=NOW,
            ),
            ModuleDefinitionModel(
                id=adapter_definition_id,
                workspace_id=workspace_id,
                module_type="worker_adapter",
                provider="opencode",
                interface_version="sidera.worker-adapter.v1",
                config_schema_version=1,
                capabilities=capabilities,
                lifecycle="active",
            ),
            ModuleDefinitionModel(
                id=workspace_definition_id,
                workspace_id=workspace_id,
                module_type="workspace_provider",
                provider="git-worktree",
                interface_version="sidera.workspace-provider.v1",
                config_schema_version=1,
                capabilities=capabilities,
                lifecycle="active",
            ),
            ModuleInstanceModel(
                id=adapter_instance_id,
                workspace_id=workspace_id,
                definition_id=adapter_definition_id,
                instance_name="opencode-r4-e2e",
                config_version=1,
                config_namespace="opencode",
                config={},
                secret_refs=[],
                lifecycle="active",
            ),
            ModuleInstanceModel(
                id=workspace_instance_id,
                workspace_id=workspace_id,
                definition_id=workspace_definition_id,
                instance_name="git-r4-e2e",
                config_version=1,
                config_namespace="workspace",
                config={},
                secret_refs=[],
                lifecycle="active",
            ),
        ]
    )
    session.flush()
    session.add(
        WorkerRuntimeConfigurationModel(
            worker_id=WORKER_ID,
            workspace_id=workspace_id,
            adapter_module_instance_id=adapter_instance_id,
            adapter_module_config_version=1,
            workspace_module_instance_id=workspace_instance_id,
            workspace_module_config_version=1,
            version=1,
            created_at=NOW,
            updated_at=NOW,
        )
    )
    session.flush()
    session.add_all(
        [
            _plan_model(first),
            _plan_model(second),
            _plan_model(second_root_first),
            _plan_model(second_root_second),
        ]
    )
    session.flush()
    session.add_all(
        [
            RootTaskPlanHeadModel(
                workspace_id=workspace_id,
                root_task_id=ROOT_TASK_ID,
                active_snapshot_id=GENERATION_TWO_ID,
                active_generation=2,
                active_snapshot_hash=plan_snapshot_content_hash(second),
                version=1,
                updated_at=NOW,
            ),
            RootTaskPlanHeadModel(
                workspace_id=workspace_id,
                root_task_id=SECOND_ROOT_TASK_ID,
                active_snapshot_id=SECOND_GENERATION_TWO_ID,
                active_generation=2,
                active_snapshot_hash=plan_snapshot_content_hash(second_root_second),
                version=1,
                updated_at=NOW,
            ),
        ]
    )
    session.flush()
    session.add_all(
        [
            _worker_item_model(
                IMPLEMENTER_WORKER_ITEM_ID,
                workspace_id,
                ROOT_TASK_ID,
                "callback-implementer",
                "实现者",
                "实现回调幂等写入和重试策略。",
                status="active",
            ),
            _worker_item_model(
                TESTER_WORKER_ITEM_ID,
                workspace_id,
                ROOT_TASK_ID,
                "callback-tester",
                "测试者",
                "验证重复通知、并发和失败重试。",
                status="active",
            ),
            _worker_item_model(
                REVIEWER_WORKER_ITEM_ID,
                workspace_id,
                ROOT_TASK_ID,
                "callback-reviewer",
                "审查者",
                "审查实现和测试证据是否满足验收。",
                status="active",
            ),
            _worker_item_model(
                MIGRATED_WORKER_ITEM_ID,
                workspace_id,
                ROOT_TASK_ID,
                "migrated-worker",
                "跨代复用者",
                "验证跨计划代的 placement 历史复用。",
                status="active",
            ),
            _worker_item_model(
                LEGACY_WORKER_ITEM_ID,
                workspace_id,
                SECOND_ROOT_TASK_ID,
                "legacy-release-check",
                "历史发布检查",
                "只读保留的旧 WorkerItem 上下文。",
                status="retired",
            ),
        ]
    )
    session.flush()
    session.add_all(
        _worker_item_placements(
            workspace_id,
            root_task_id=ROOT_TASK_ID,
            snapshot_id=GENERATION_ONE_ID,
            generation=1,
            placements=(
                (
                    IMPLEMENTER_WORKER_ITEM_ID,
                    "callback-implementer",
                    "callback-idempotency",
                    BACKEND_TASK_ID,
                    "implementer",
                    ("python",),
                ),
                (
                    TESTER_WORKER_ITEM_ID,
                    "callback-tester",
                    "callback-idempotency",
                    BACKEND_TASK_ID,
                    "tester",
                    ("python",),
                ),
                (
                    REVIEWER_WORKER_ITEM_ID,
                    "callback-reviewer",
                    "callback-idempotency",
                    BACKEND_TASK_ID,
                    "reviewer",
                    ("python",),
                ),
                (
                    MIGRATED_WORKER_ITEM_ID,
                    "migrated-worker",
                    "callback-idempotency",
                    BACKEND_TASK_ID,
                    "implementer",
                    ("python",),
                ),
            ),
        )
        + _worker_item_placements(
            workspace_id,
            root_task_id=ROOT_TASK_ID,
            snapshot_id=GENERATION_TWO_ID,
            generation=2,
            placements=(
                (
                    IMPLEMENTER_WORKER_ITEM_ID,
                    "callback-implementer",
                    "callback-idempotency",
                    BACKEND_TASK_ID,
                    "implementer",
                    ("python",),
                ),
                (
                    TESTER_WORKER_ITEM_ID,
                    "callback-tester",
                    "callback-idempotency",
                    BACKEND_TASK_ID,
                    "tester",
                    ("python",),
                ),
                (
                    REVIEWER_WORKER_ITEM_ID,
                    "callback-reviewer",
                    "callback-idempotency",
                    BACKEND_TASK_ID,
                    "reviewer",
                    ("python",),
                ),
                (
                    MIGRATED_WORKER_ITEM_ID,
                    "migrated-worker",
                    "callback-test",
                    FRONTEND_TASK_ID,
                    "tester",
                    ("python",),
                ),
            ),
        )
        + _worker_item_placements(
            workspace_id,
            root_task_id=SECOND_ROOT_TASK_ID,
            snapshot_id=SECOND_GENERATION_ONE_ID,
            generation=1,
            placements=(
                (
                    LEGACY_WORKER_ITEM_ID,
                    "legacy-release-check",
                    "release-check",
                    SECOND_SUBTASK_TASK_ID,
                    "reviewer",
                    ("typescript",),
                ),
            ),
        )
        + _worker_item_placements(
            workspace_id,
            root_task_id=SECOND_ROOT_TASK_ID,
            snapshot_id=SECOND_GENERATION_TWO_ID,
            generation=2,
            placements=(
                (
                    LEGACY_WORKER_ITEM_ID,
                    "legacy-release-check",
                    "release-check",
                    SECOND_SUBTASK_TASK_ID,
                    "reviewer",
                    ("typescript",),
                ),
            ),
        )
    )
    session.flush()
    session.add_all(
        [
            ExecutionModel(
                id=PREVIOUS_EXECUTION_ID,
                workspace_id=workspace_id,
                task_id=BACKEND_TASK_ID,
                lineage_id=PREVIOUS_EXECUTION_ID,
                attempt=1,
                max_attempts=1,
                purpose="previous callback implementation",
                status="succeeded",
                capability_requirements=["python"],
                pinned_worker_id=WORKER_ID,
                assigned_worker_id=WORKER_ID,
                worker_item_id=IMPLEMENTER_WORKER_ITEM_ID,
                active_lease_id=None,
                last_fencing_token=0,
                not_before=NOW - timedelta(minutes=8),
                failure_code=None,
                started_at=NOW - timedelta(minutes=8),
                finished_at=NOW - timedelta(minutes=6),
                version=2,
                stop_requested=False,
                source_execution_id=None,
                source_session_id=None,
                created_at=NOW - timedelta(minutes=8),
                updated_at=NOW - timedelta(minutes=6),
            ),
            ExecutionModel(
                id=ATTENTION_EXECUTION_ID,
                workspace_id=workspace_id,
                task_id=FRONTEND_TASK_ID,
                lineage_id=ATTENTION_EXECUTION_ID,
                attempt=1,
                max_attempts=2,
                purpose="wait for callback concurrency confirmation",
                status="waiting_user",
                capability_requirements=["python"],
                pinned_worker_id=WORKER_ID,
                assigned_worker_id=WORKER_ID,
                worker_item_id=MIGRATED_WORKER_ITEM_ID,
                active_lease_id=None,
                last_fencing_token=0,
                not_before=NOW,
                failure_code=None,
                started_at=NOW + timedelta(seconds=1),
                finished_at=None,
                version=1,
                stop_requested=False,
                source_execution_id=None,
                source_session_id=None,
                created_at=NOW + timedelta(seconds=1),
                updated_at=NOW + timedelta(seconds=3),
            ),
            ExecutionModel(
                id=BACKEND_EXECUTION_ID,
                workspace_id=workspace_id,
                task_id=BACKEND_TASK_ID,
                lineage_id=BACKEND_EXECUTION_ID,
                attempt=1,
                max_attempts=3,
                purpose="implement backend",
                status="running",
                capability_requirements=["python"],
                pinned_worker_id=WORKER_ID,
                assigned_worker_id=WORKER_ID,
                worker_item_id=IMPLEMENTER_WORKER_ITEM_ID,
                active_lease_id=None,
                last_fencing_token=1,
                not_before=NOW,
                failure_code=None,
                started_at=NOW,
                finished_at=None,
                version=3,
                stop_requested=False,
                source_execution_id=None,
                source_session_id=None,
                created_at=NOW,
                updated_at=NOW + timedelta(seconds=4),
            ),
        ]
    )
    session.flush()
    session.add_all(
        [
            AssignmentLeaseModel(
                id=LEASE_ID,
                workspace_id=workspace_id,
                execution_id=BACKEND_EXECUTION_ID,
                worker_id=WORKER_ID,
                status="active",
                issued_at=NOW,
                expires_at=NOW + timedelta(minutes=5),
                renewed_at=None,
                ended_at=None,
                fencing_token=1,
                assignment_reason={"source": "r4-e2e"},
                version=1,
                created_at=NOW,
                updated_at=NOW,
            ),
            ExecutionSessionModel(
                id=PREVIOUS_SESSION_ID,
                workspace_id=workspace_id,
                execution_id=PREVIOUS_EXECUTION_ID,
                worker_id=WORKER_ID,
                adapter_session_id="r4-previous-session",
                status="closed",
                last_sequence=2,
                last_activity_at=NOW - timedelta(minutes=6),
                input_mode="queued",
                adapter_module_id=adapter_instance_id,
                adapter_config_version=1,
                workspace_module_id=workspace_instance_id,
                workspace_config_version=1,
                capability_snapshot=capabilities,
                process_ref="e2e://opencode-previous-session",
                workspace_ref="e2e://git-previous-worktree",
                version=2,
                created_at=NOW - timedelta(minutes=8),
                updated_at=NOW - timedelta(minutes=6),
            ),
            ExecutionSessionModel(
                id=ATTENTION_SESSION_ID,
                workspace_id=workspace_id,
                execution_id=ATTENTION_EXECUTION_ID,
                worker_id=WORKER_ID,
                adapter_session_id="r4-attention-session",
                status="waiting_user",
                last_sequence=1,
                last_activity_at=NOW + timedelta(seconds=3),
                input_mode="queued",
                adapter_module_id=adapter_instance_id,
                adapter_config_version=1,
                workspace_module_id=workspace_instance_id,
                workspace_config_version=1,
                capability_snapshot=capabilities,
                process_ref="e2e://opencode-attention-session",
                workspace_ref="e2e://git-attention-worktree",
                version=1,
                created_at=NOW + timedelta(seconds=1),
                updated_at=NOW + timedelta(seconds=3),
            ),
            ExecutionSessionModel(
                id=SESSION_ID,
                workspace_id=workspace_id,
                execution_id=BACKEND_EXECUTION_ID,
                worker_id=WORKER_ID,
                adapter_session_id="r4-e2e-session",
                status="active",
                last_sequence=3,
                last_activity_at=NOW + timedelta(seconds=4),
                input_mode="queued",
                adapter_module_id=adapter_instance_id,
                adapter_config_version=1,
                workspace_module_id=workspace_instance_id,
                workspace_config_version=1,
                capability_snapshot=capabilities,
                process_ref="e2e://opencode-session",
                workspace_ref="e2e://git-worktree",
                version=2,
                created_at=NOW,
                updated_at=NOW + timedelta(seconds=4),
            ),
        ]
    )
    session.flush()
    session.add(
        ExecutionInputModel(
            id=uuid5(NAMESPACE_URL, f"{workspace_id}:input:callback-rule"),
            workspace_id=workspace_id,
            execution_id=BACKEND_EXECUTION_ID,
            session_id=SESSION_ID,
            client_idempotency_key="e2e-callback-rule-input",
            text="只修改这个回调重试规则",
            attachment_refs=[],
            status="delivered",
            operation_id=uuid5(
                NAMESPACE_URL, f"{workspace_id}:operation:callback-rule"
            ),
            failure_code=None,
            version=1,
            source_command_id=None,
        )
    )
    session.add(
        ExecutionBindingModel(
            id=uuid5(NAMESPACE_URL, f"{workspace_id}:binding"),
            workspace_id=workspace_id,
            execution_id=BACKEND_EXECUTION_ID,
            worker_id=WORKER_ID,
            adapter_module_id=adapter_instance_id,
            workspace_module_id=workspace_instance_id,
            adapter_config_version=1,
            workspace_config_version=1,
            capability_snapshot=capabilities,
            lease_id=LEASE_ID,
            fencing_token=1,
            version=1,
            created_at=NOW,
            updated_at=NOW,
        )
    )
    session.add_all(
        [
            ExecutionEventModel(
                id=uuid5(NAMESPACE_URL, f"{workspace_id}:trace:1"),
                workspace_id=workspace_id,
                execution_id=BACKEND_EXECUTION_ID,
                session_id=SESSION_ID,
                sequence=1,
                event_type="checkpoint.created",
                payload={
                    "title": "回调 checkpoint",
                    "summary": "Repository baseline captured",
                    "artifact_ids": [str(ARTIFACT_ID)],
                },
                occurred_at=NOW + timedelta(seconds=1),
                raw_ref=None,
            ),
            ExecutionEventModel(
                id=uuid5(NAMESPACE_URL, f"{workspace_id}:previous-trace:1"),
                workspace_id=workspace_id,
                execution_id=PREVIOUS_EXECUTION_ID,
                session_id=PREVIOUS_SESSION_ID,
                sequence=1,
                event_type="checkpoint.created",
                payload={
                    "title": "历史实现 checkpoint",
                    "summary": "上一代 WorkerItem 已完成基础实现。",
                },
                occurred_at=NOW - timedelta(minutes=7),
                raw_ref=None,
            ),
            ExecutionEventModel(
                id=uuid5(NAMESPACE_URL, f"{workspace_id}:previous-trace:2"),
                workspace_id=workspace_id,
                execution_id=PREVIOUS_EXECUTION_ID,
                session_id=PREVIOUS_SESSION_ID,
                sequence=2,
                event_type="assistant.summary",
                payload={"summary": "历史执行已成功完成，当前执行复用了该上下文。"},
                occurred_at=NOW - timedelta(minutes=6),
                raw_ref=None,
            ),
            ExecutionEventModel(
                id=uuid5(NAMESPACE_URL, f"{workspace_id}:attention-trace:1"),
                workspace_id=workspace_id,
                execution_id=ATTENTION_EXECUTION_ID,
                session_id=ATTENTION_SESSION_ID,
                sequence=1,
                event_type="execution.waiting_user",
                payload={"summary": "需要确认并发测试范围。"},
                occurred_at=NOW + timedelta(seconds=3),
                raw_ref=None,
            ),
            ExecutionEventModel(
                id=uuid5(NAMESPACE_URL, f"{workspace_id}:trace:2"),
                workspace_id=workspace_id,
                execution_id=BACKEND_EXECUTION_ID,
                session_id=SESSION_ID,
                sequence=2,
                event_type="tool.call",
                payload={
                    "tool": "pytest",
                    "summary": "Running backend tests",
                    "duration_ms": 320,
                },
                occurred_at=NOW + timedelta(seconds=3),
                raw_ref=None,
            ),
            ExecutionEventModel(
                id=uuid5(NAMESPACE_URL, f"{workspace_id}:trace:3"),
                workspace_id=workspace_id,
                execution_id=BACKEND_EXECUTION_ID,
                session_id=SESSION_ID,
                sequence=3,
                event_type="assistant.summary",
                payload={"summary": "Tests are still running"},
                occurred_at=NOW + timedelta(seconds=4),
                raw_ref=None,
            ),
            ArtifactModel(
                id=ARTIFACT_ID,
                workspace_id=workspace_id,
                task_id=BACKEND_TASK_ID,
                execution_id=BACKEND_EXECUTION_ID,
                session_id=SESSION_ID,
                artifact_type="report",
                storage_ref="artifact://r4-e2e/report",
                content_hash="a" * 64,
                size_bytes=256,
                media_type="text/plain",
                provenance={"producer": "r4-e2e"},
                created_at=NOW + timedelta(seconds=2),
            ),
            EvidenceClaimModel(
                id=uuid5(NAMESPACE_URL, f"{workspace_id}:evidence:backend"),
                workspace_id=workspace_id,
                task_id=BACKEND_TASK_ID,
                execution_id=BACKEND_EXECUTION_ID,
                acceptance_criterion_ref="backend trace exists",
                conclusion="Trace is available for operator inspection.",
                environment_ref="r4-e2e",
                artifact_ids=[str(ARTIFACT_ID)],
                producer="r4-e2e",
                status="candidate",
                created_at=NOW + timedelta(seconds=2),
            ),
        ]
    )
    session.add_all(
        [
            ProjectionChangeModel(
                workspace_id=workspace_id,
                cursor=1,
                schema_version=1,
                projection_type="task_first",
                resource_id=ROOT_TASK_ID,
                change_type="task.plan_snapshot_published",
                resource_version=1,
                occurred_at=NOW,
                correlation_id=GENERATION_ONE_ID,
            ),
            ProjectionChangeModel(
                workspace_id=workspace_id,
                cursor=2,
                schema_version=1,
                projection_type="task_first",
                resource_id=ROOT_TASK_ID,
                change_type="task.plan_snapshot_published",
                resource_version=2,
                occurred_at=NOW + timedelta(minutes=1),
                correlation_id=GENERATION_TWO_ID,
            ),
            ProjectionChangeModel(
                workspace_id=workspace_id,
                cursor=3,
                schema_version=1,
                projection_type="worker_trace",
                resource_id=BACKEND_EXECUTION_ID,
                change_type="execution_event.appended",
                resource_version=3,
                occurred_at=NOW + timedelta(seconds=4),
                correlation_id=uuid5(NAMESPACE_URL, f"{workspace_id}:trace:3"),
            ),
            ProjectionChangeModel(
                workspace_id=workspace_id,
                cursor=4,
                schema_version=1,
                projection_type="worker_item_workspace",
                resource_id=IMPLEMENTER_WORKER_ITEM_ID,
                change_type="worker_item.created",
                resource_version=1,
                occurred_at=NOW + timedelta(seconds=4),
                correlation_id=IMPLEMENTER_WORKER_ITEM_ID,
            ),
            ProjectionChangeModel(
                workspace_id=workspace_id,
                cursor=5,
                schema_version=1,
                projection_type="worker_item_activity",
                resource_id=IMPLEMENTER_WORKER_ITEM_ID,
                change_type="worker_item.activity_appended",
                resource_version=4,
                occurred_at=NOW + timedelta(seconds=4),
                correlation_id=uuid5(
                    NAMESPACE_URL, f"{workspace_id}:input:callback-rule"
                ),
            ),
            ProjectionChangeModel(
                workspace_id=workspace_id,
                cursor=6,
                schema_version=1,
                projection_type="task_first",
                resource_id=ROOT_TASK_ID,
                change_type="execution.waiting_user",
                resource_version=1,
                occurred_at=NOW + timedelta(seconds=3),
                correlation_id=ATTENTION_EXECUTION_ID,
            ),
        ]
    )
    session.add(
        WorkspaceProjectionCursorModel(workspace_id=workspace_id, last_cursor=6)
    )
    session.flush()
    session.execute(
        update(ExecutionModel)
        .where(
            ExecutionModel.workspace_id == workspace_id,
            ExecutionModel.id == BACKEND_EXECUTION_ID,
        )
        .values(active_lease_id=LEASE_ID)
    )


def _seed_context_knowledge(session, workspace_id: UUID) -> None:
    _seed_task_first(session, workspace_id)

    adapter_instance_id = uuid5(NAMESPACE_URL, f"{workspace_id}:adapter-instance")
    profile_snapshot = {
        "profile_id": str(CONTEXT_PLANNING_PROFILE_ID),
        "profile_version": 1,
        "planner_module_instance_id": str(adapter_instance_id),
        "planner_interface_version": "sidera.planner.v1",
        "context_package_versions": [1],
    }
    session.add(
        PlanningProfileModel(
            id=CONTEXT_PLANNING_PROFILE_ID,
            workspace_id=workspace_id,
            profile_key="r5-3-context-e2e",
            version=1,
            display_name="R5.3 Context E2E",
            planner_module_instance_id=adapter_instance_id,
            planner_module_config_version=1,
            planner_interface_version="sidera.planner.v1",
            status="active",
            user_selectable=True,
            policy={},
        )
    )
    session.flush()
    planning_request = PlanningRequestModel(
        id=CONTEXT_PLANNING_REQUEST_ID,
        workspace_id=workspace_id,
        kind="create",
        status="materialized",
        current_brief_revision=None,
        current_proposal_revision=None,
        current_proposal_content_hash=None,
        planning_profile_id=CONTEXT_PLANNING_PROFILE_ID,
        planning_profile_version=1,
        profile_snapshot=profile_snapshot,
        source_type="e2e",
        source_ref="r5.3-context-knowledge",
        actor="local:e2e",
        correlation_id=CONTEXT_PLANNING_REQUEST_ID,
        version=1,
        created_at=NOW,
        updated_at=NOW,
    )
    session.add(planning_request)
    session.flush()
    session.add(
        PlanningBriefRevisionModel(
            planning_request_id=CONTEXT_PLANNING_REQUEST_ID,
            revision=1,
            workspace_id=workspace_id,
            payload={
                "objective": "验证任务上下文与 Knowledge Service 的精确关联。",
                "acceptance_criteria": ["页面正文仅在明确物化后出现"],
                "source_correlation": str(CONTEXT_PLANNING_REQUEST_ID),
            },
            created_by="local:e2e",
            created_at=NOW,
        )
    )
    session.flush()
    planning_request.current_brief_revision = 1
    session.flush()
    session.add(
        PlanningRunModel(
            id=CONTEXT_PLANNING_RUN_ID,
            workspace_id=workspace_id,
            planning_request_id=CONTEXT_PLANNING_REQUEST_ID,
            brief_revision=1,
            planning_profile_id=CONTEXT_PLANNING_PROFILE_ID,
            planning_profile_version=1,
            planner_module_instance_id=adapter_instance_id,
            planner_module_config_version=1,
            attempt=1,
            status="succeeded",
            result_kind="proposal",
            capability_snapshot={"context_package_versions": [1]},
            usage={
                "input_tokens": 240,
                "output_tokens": 180,
                "cost_microunits": 0,
                "runtime_seconds": 4,
                "context_bytes": 512,
            },
            operation_id=uuid5(NAMESPACE_URL, f"{workspace_id}:context-planning-operation"),
            provider_ref="r5.3-context-e2e",
            error_code=None,
            error_message=None,
            diagnostic_refs=[],
            queued_at=NOW,
            started_at=NOW,
            finished_at=NOW + timedelta(seconds=4),
            version=1,
            created_at=NOW,
            updated_at=NOW + timedelta(seconds=4),
        )
    )
    session.flush()

    revision_hash, section_hash = _knowledge_fixture_hashes()
    context_snapshot = WorkContextSnapshotV1.create(
        objective="验证任务上下文与 Knowledge Service 的精确关联。",
        constraints=(),
        acceptance_criteria=(),
        requested_outputs=(),
        clarification_decisions=(),
        accepted_decisions=(),
        unresolved_questions=(),
        knowledge_refs=(
            KnowledgeRevisionRef(
                page_id=KNOWLEDGE_PAGE_ID,
                revision_id=KNOWLEDGE_REVISION_TWO_ID,
                revision_hash=revision_hash,
                section_key="execution",
                section_hash=section_hash,
                source_ids=(KNOWLEDGE_SOURCE_ID,),
                ordinal=0,
            ),
        ),
        artifact_refs=(),
        evidence_refs=(),
        checkpoint_refs=(),
    )
    context_revision = WorkContextRevision.create(
        revision_id=CONTEXT_REVISION_ID,
        work_context_id=CONTEXT_ID,
        workspace_id=workspace_id,
        revision_number=1,
        brief_planning_request_id=CONTEXT_PLANNING_REQUEST_ID,
        brief_revision=1,
        proposal_revision_id=None,
        plan_snapshot_id=None,
        reason=WorkContextRevisionReason.REQUEST_CREATED,
        snapshot=context_snapshot,
        created_by="local:e2e",
        created_at=NOW,
    )
    context = WorkContext.create(
        work_context_id=CONTEXT_ID,
        workspace_id=workspace_id,
        planning_request_id=CONTEXT_PLANNING_REQUEST_ID,
        first_revision=context_revision,
        created_at=NOW,
    )
    context.root_task_id = ROOT_TASK_ID
    context.version = 2
    context.updated_at = NOW
    WorkContextRepository(session).add(context, context_revision)
    session.flush()

    package_ref = ContextPackageKnowledgeRef(
        page_id=KNOWLEDGE_PAGE_ID,
        revision_id=KNOWLEDGE_REVISION_TWO_ID,
        revision_hash=revision_hash,
        section_key="execution",
        section_hash=section_hash,
        source_ids=(KNOWLEDGE_SOURCE_ID,),
        selection_reason=ContextSelectionReason.WORK_CONTEXT,
        trust_level=ContextTrustLevel.WORKSPACE_CONFIRMED,
        ordinal=0,
    )
    _complete_context_package(
        session,
        workspace_id=workspace_id,
        request_id=CONTEXT_EXECUTION_ASSEMBLY_ID,
        package_id=CONTEXT_EXECUTION_PACKAGE_ID,
        purpose=ContextPurpose.EXECUTION,
        consumer_type=ContextConsumerType.WORKER,
        consumer_id=BACKEND_EXECUTION_ID,
        worker_item_id=IMPLEMENTER_WORKER_ITEM_ID,
        context_version=1,
        knowledge_ref=package_ref,
        profile_id=CONTEXT_PLANNING_PROFILE_ID,
        adapter_instance_id=adapter_instance_id,
    )
    _complete_context_package(
        session,
        workspace_id=workspace_id,
        request_id=CONTEXT_PLANNING_ASSEMBLY_ID,
        package_id=CONTEXT_PLANNING_PACKAGE_ID,
        purpose=ContextPurpose.PLANNING_RUN,
        consumer_type=ContextConsumerType.PLANNER,
        consumer_id=CONTEXT_PLANNING_RUN_ID,
        worker_item_id=None,
        context_version=None,
        knowledge_ref=package_ref,
        profile_id=CONTEXT_PLANNING_PROFILE_ID,
        adapter_instance_id=adapter_instance_id,
    )

    session.add(
        KnowledgeCandidateSubmissionModel(
            id=CONTEXT_SUBMISSION_ID,
            workspace_id=workspace_id,
            producer_type="worker",
            producer_id=WORKER_ID,
            planning_run_id=None,
            execution_id=BACKEND_EXECUTION_ID,
            worker_item_id=IMPLEMENTER_WORKER_ITEM_ID,
            task_id=ROOT_TASK_ID,
            subtask_task_id=BACKEND_TASK_ID,
            artifact_refs=[],
            evidence_refs=[],
            proposal_hash="d" * 64,
            content_ref="candidate-spool://r5.3-context-e2e/proposal.json",
            content_bytes=256,
            knowledge_command_id=None,
            status="submitted",
            attempt=1,
            lease_owner=None,
            lease_expires_at=None,
            next_attempt_at=None,
            terminal_candidate_id=KNOWLEDGE_PENDING_CANDIDATE_ID,
            error_code=None,
            version=1,
            created_at=NOW + timedelta(seconds=5),
            updated_at=NOW + timedelta(seconds=5),
        )
    )
    session.add_all(
        [
            ProjectionChangeModel(
                workspace_id=workspace_id,
                cursor=7,
                schema_version=1,
                projection_type="work_context",
                resource_id=CONTEXT_ID,
                change_type="work_context_created",
                resource_version=2,
                occurred_at=NOW,
                correlation_id=CONTEXT_PLANNING_REQUEST_ID,
            ),
            ProjectionChangeModel(
                workspace_id=workspace_id,
                cursor=8,
                schema_version=1,
                projection_type="context_package",
                resource_id=CONTEXT_EXECUTION_PACKAGE_ID,
                change_type="context_package_created",
                resource_version=1,
                occurred_at=NOW,
                correlation_id=CONTEXT_EXECUTION_PACKAGE_ID,
            ),
            ProjectionChangeModel(
                workspace_id=workspace_id,
                cursor=9,
                schema_version=1,
                projection_type="context_package",
                resource_id=CONTEXT_PLANNING_PACKAGE_ID,
                change_type="context_package_created",
                resource_version=1,
                occurred_at=NOW,
                correlation_id=CONTEXT_PLANNING_PACKAGE_ID,
            ),
            ProjectionChangeModel(
                workspace_id=workspace_id,
                cursor=10,
                schema_version=1,
                projection_type="knowledge_candidate_submission",
                resource_id=CONTEXT_SUBMISSION_ID,
                change_type="knowledge_candidate_submission_updated",
                resource_version=1,
                occurred_at=NOW + timedelta(seconds=5),
                correlation_id=CONTEXT_SUBMISSION_ID,
            ),
        ]
    )
    session.execute(
        update(WorkspaceProjectionCursorModel)
        .where(WorkspaceProjectionCursorModel.workspace_id == workspace_id)
        .values(last_cursor=10)
    )


def _complete_context_package(
    session,
    *,
    workspace_id: UUID,
    request_id: UUID,
    package_id: UUID,
    purpose: ContextPurpose,
    consumer_type: ContextConsumerType,
    consumer_id: UUID,
    worker_item_id: UUID | None,
    context_version: int | None,
    knowledge_ref: ContextPackageKnowledgeRef,
    profile_id: UUID,
    adapter_instance_id: UUID,
) -> None:
    assembly = ContextAssemblyRequest.create(
        request_id=request_id,
        workspace_id=workspace_id,
        purpose=purpose,
        consumer_type=consumer_type,
        consumer_id=consumer_id,
        worker_item_id=worker_item_id,
        work_context_revision_id=CONTEXT_REVISION_ID,
        context_version=context_version,
        knowledge_search_scope={},
        knowledge_filter_refs=(),
        fact_scope_refs=(),
        budget={
            "profile_id": str(profile_id),
            "profile_version": 1,
            "max_sections": 8,
            "max_chars": 20_000,
            "max_input_tokens": 4_000,
        },
        expires_at=NOW + timedelta(hours=1),
        request_fingerprint=hashlib.sha256(str(request_id).encode()).hexdigest(),
        idempotency_key=f"r5.3-context-e2e:{request_id}",
        created_at=NOW,
    )
    repository = ContextAssemblyRepository(session)
    repository.add(assembly)
    claimed = repository.claim_next(
        workspace_id=workspace_id,
        owner="r5.3-context-e2e",
        now=NOW,
        lease_ttl=timedelta(minutes=1),
        limit=1,
    )[0]
    budget = ContextBudgetV1(
        profile_id=profile_id,
        profile_version=1,
        max_sections=8,
        max_chars=20_000,
        max_input_tokens=4_000,
        actual_sections=1,
        actual_chars=93,
        actual_input_tokens=24,
    )
    manifest = ContextPackageManifestV1.create(
        package_id=package_id,
        workspace_id=workspace_id,
        purpose=purpose,
        consumer_type=consumer_type,
        consumer_id=consumer_id,
        worker_item_id=worker_item_id,
        work_context_revision_id=CONTEXT_REVISION_ID,
        context_version=context_version,
        control_plane_fact_snapshot={
            "root_task_id": str(ROOT_TASK_ID),
            "consumer_id": str(consumer_id),
        },
        knowledge_refs=(knowledge_ref,),
        artifact_refs=(),
        evidence_refs=(),
        budget=budget,
        expires_at=NOW + timedelta(hours=1),
        idempotency_key=assembly.idempotency_key,
        created_at=NOW,
    )
    repository.complete_with_package(
        claimed,
        manifest,
        owner="r5.3-context-e2e",
        expected_version=claimed.version,
        actor_type="system",
        actor_id="r5.3-context-e2e",
        causation_id=uuid5(NAMESPACE_URL, f"{workspace_id}:context-causation:{package_id}"),
        correlation_id=package_id,
    )
    session.add(
        ContextPackageAcknowledgementModel(
            package_id=package_id,
            workspace_id=workspace_id,
            consumer_type=consumer_type.value,
            consumer_id=consumer_id,
            context_version=context_version,
            acknowledged_package_hash=manifest.package_hash,
            adapter_module_instance_id=adapter_instance_id,
            adapter_config_version=1,
            acknowledged_at=NOW + timedelta(seconds=1),
        )
    )
    session.flush()


def _knowledge_fixture_hashes() -> tuple[str, str]:
    body = (
        "The worker receives an exact immutable context package and keeps the "
        "selected revision hash.\n"
    )
    section = {
        "section_key": "execution",
        "heading": "Execution handoff",
        "summary": "",
        "body_markdown": body,
        "ordinal": 0,
    }
    snapshot = {
        "title": "Worker context reference",
        "summary": "Exact knowledge used by the execution context package.",
        "tags": ["context", "r5"],
        "sections": [section],
        "relations": [],
        "source_refs": [str(KNOWLEDGE_SOURCE_ID)],
    }
    return _sha256_json(snapshot), _sha256_json(section)


def _sha256_json(value: object) -> str:
    return hashlib.sha256(
        json.dumps(
            value, ensure_ascii=False, separators=(",", ":"), sort_keys=True
        ).encode("utf-8")
    ).hexdigest()


def _seed_runtime_routing(session, workspace_id: UUID) -> None:
    _seed_task_first(session, workspace_id)
    runtime_now = datetime.now(UTC)
    execution_now = NOW
    session.add(
        WorkerModel(
            id=RUNTIME_WORKER_ID,
            workspace_id=workspace_id,
            adapter_type="opencode",
            display_name="Runtime Routing Worker",
            status="idle",
            desired_state="active",
            capabilities=["python", "typescript"],
            capacity=1,
            current_load=0,
            cost_per_hour_microunits=1,
            startup_latency_ms=10,
            last_heartbeat_at=runtime_now,
            version=1,
            created_at=runtime_now,
            updated_at=runtime_now,
        )
    )
    session.flush()
    session.add(
        ExecutionModel(
            id=RUNTIME_EXECUTION_ID,
            workspace_id=workspace_id,
            task_id=BACKEND_TASK_ID,
            lineage_id=RUNTIME_EXECUTION_ID,
            attempt=1,
            max_attempts=3,
            purpose="runtime routing e2e",
            status="queued",
            capability_requirements=["python"],
            adapter_preferences=[],
            adapter_forbidden=[],
            pinned_worker_id=None,
            assigned_worker_id=None,
            worker_item_id=IMPLEMENTER_WORKER_ITEM_ID,
            active_lease_id=None,
            last_fencing_token=0,
            not_before=execution_now - timedelta(seconds=1),
            failure_code=None,
            started_at=None,
            finished_at=None,
            version=1,
            stop_requested=False,
            source_execution_id=None,
            source_session_id=None,
            created_at=execution_now,
            updated_at=runtime_now,
        )
    )
    session.flush()


def _seed_scale(session, workspace_id: UUID) -> None:
    subtasks = tuple(_scale_subtask(workspace_id, index) for index in range(1, 201))
    task_ids = tuple(item.task_id for item in subtasks if item.task_id is not None)
    subtask_dependencies = tuple(
        SubtaskDependency(
            upstream_subtask_key=f"work-{index:03d}",
            downstream_subtask_key=f"work-{index + offset:03d}",
            kind=DependencyKind.HARD,
            required_evidence_refs=(),
            enforcement=DependencyEnforcement.INFORMATIONAL,
        )
        for index, offset in (
            *((index, 1) for index in range(1, 199 + 1)),
            *((index, 2) for index in range(1, 198 + 1)),
            *((index, 3) for index in range(1, 3 + 1)),
        )
        if index + offset <= 200
    )
    worker_item_plans = tuple(
        _scale_worker_item_plan(workspace_id, index) for index in range(1, 41)
    )
    worker_item_dependencies = tuple(
        WorkerItemDependency(
            upstream_worker_item_key=f"scale-worker-{index:02d}",
            downstream_worker_item_key=f"scale-worker-{index + offset:02d}",
            kind="review" if offset == 2 else "dependency",
            required_artifact_refs=(),
        )
        for index, offset in (
            *((index, 1) for index in range(1, 40)),
            *((index, 2) for index in range(1, 39)),
            *((1, 4), (2, 5), (3, 6)),
        )
        if index + offset <= 40
    )
    snapshot = TaskPlanSnapshot.create(
        snapshot_id=GENERATION_ONE_ID,
        schema_version=2,
        workspace_id=workspace_id,
        root_task_id=ROOT_TASK_ID,
        generation=1,
        parent_snapshot_id=None,
        source_type="e2e",
        source_ref="e2e:scale",
        title="Scale graph",
        goal_summary=(
            "Render 200 Subtasks with 400 dependency edges and a 40 WorkerItem "
            "graph with 80 edges."
        ),
        acceptance_refs=("scale:graph",),
        subtasks=subtasks,
        worker_item_plans=worker_item_plans,
        subtask_milestones=tuple(
            SubtaskMilestone(
                key=f"m{index}",
                title=f"Scale phase {index}",
                subtask_keys=tuple(
                    f"work-{item:03d}"
                    for item in range((index - 1) * 50 + 1, index * 50 + 1)
                ),
                acceptance_refs=(),
                order_hint=index,
            )
            for index in range(1, 5)
        ),
        subtask_dependencies=subtask_dependencies,
        worker_item_dependencies=worker_item_dependencies,
        published_by="local:e2e",
        published_at=NOW,
    )
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
    session.flush()
    session.add(
        TaskModel(
            id=ROOT_TASK_ID,
            workspace_id=workspace_id,
            title="Scale graph root",
            description="Render the large graph.",
            acceptance_criteria=["scale:graph"],
            status="running",
            priority=50,
            requested_by="e2e",
            version=1,
            created_at=NOW,
            updated_at=NOW,
        )
    )
    session.add_all(
        TaskModel(
            id=task_id,
            workspace_id=workspace_id,
            title=f"Work Item {index:03d}",
            description="Scale fixture work item.",
            acceptance_criteria=["scale:graph"],
            status="pending",
            priority=50,
            requested_by="e2e",
            version=1,
            created_at=NOW,
            updated_at=NOW,
        )
        for index, task_id in enumerate(task_ids, start=1)
    )
    session.flush()
    session.add(_plan_model(snapshot))
    session.flush()
    session.add_all(
        _worker_item_model(
            _scale_worker_item_id(workspace_id, index),
            workspace_id,
            ROOT_TASK_ID,
            f"scale-worker-{index:02d}",
            f"Scale WorkerItem {index:02d}",
            "Scale fixture WorkerItem activity and graph coverage.",
            status="active",
        )
        for index in range(1, 41)
    )
    session.flush()
    session.add_all(
        _worker_item_placements(
            workspace_id,
            root_task_id=ROOT_TASK_ID,
            snapshot_id=GENERATION_ONE_ID,
            generation=1,
            placements=tuple(
                (
                    _scale_worker_item_id(workspace_id, index),
                    f"scale-worker-{index:02d}",
                    "work-001",
                    task_ids[0],
                    "scale",
                    ("python",),
                )
                for index in range(1, 41)
            ),
        )
    )
    session.flush()
    session.add(
        WorkerModel(
            id=SCALE_WORKER_ID,
            workspace_id=workspace_id,
            adapter_type="scale-fixture",
            display_name="Scale Trace Worker",
            status="busy",
            desired_state="active",
            capabilities=["python"],
            capacity=1,
            current_load=1,
            cost_per_hour_microunits=1,
            startup_latency_ms=1,
            last_heartbeat_at=NOW,
            version=1,
            created_at=NOW,
            updated_at=NOW,
        )
    )
    session.flush()
    session.add(
        ExecutionModel(
            id=SCALE_EXECUTION_ID,
            workspace_id=workspace_id,
            task_id=task_ids[0],
            lineage_id=SCALE_EXECUTION_ID,
            attempt=1,
            max_attempts=1,
            purpose="render scale trace",
            status="running",
            capability_requirements=["python"],
            pinned_worker_id=SCALE_WORKER_ID,
            assigned_worker_id=SCALE_WORKER_ID,
            active_lease_id=None,
            last_fencing_token=0,
            not_before=NOW,
            failure_code=None,
            started_at=NOW,
            finished_at=None,
            version=1,
            stop_requested=False,
            source_execution_id=None,
            source_session_id=None,
            created_at=NOW,
            updated_at=NOW,
        )
    )
    session.add(
        ExecutionModel(
            id=SCALE_WORKER_EXECUTION_ID,
            workspace_id=workspace_id,
            task_id=task_ids[0],
            lineage_id=SCALE_WORKER_EXECUTION_ID,
            attempt=1,
            max_attempts=1,
            purpose="render WorkerItem activity",
            status="running",
            capability_requirements=["python"],
            pinned_worker_id=SCALE_WORKER_ID,
            assigned_worker_id=SCALE_WORKER_ID,
            worker_item_id=SCALE_WORKER_ITEM_ID,
            active_lease_id=None,
            last_fencing_token=0,
            not_before=NOW,
            failure_code=None,
            started_at=NOW,
            finished_at=None,
            version=1,
            stop_requested=False,
            source_execution_id=None,
            source_session_id=None,
            created_at=NOW,
            updated_at=NOW + timedelta(minutes=4),
        )
    )
    session.flush()
    session.add(
        ExecutionSessionModel(
            id=SCALE_SESSION_ID,
            workspace_id=workspace_id,
            execution_id=SCALE_EXECUTION_ID,
            worker_id=SCALE_WORKER_ID,
            adapter_session_id="r4-scale-session",
            status="active",
            last_sequence=50_000,
            last_activity_at=NOW + timedelta(minutes=5),
            input_mode="queued",
            adapter_module_id=None,
            adapter_config_version=None,
            workspace_module_id=None,
            workspace_config_version=None,
            capability_snapshot={"fixture": "scale"},
            process_ref="e2e://scale-session",
            workspace_ref="e2e://scale-workspace",
            version=1,
            created_at=NOW,
            updated_at=NOW + timedelta(minutes=5),
        )
    )
    session.add(
        ExecutionSessionModel(
            id=SCALE_WORKER_SESSION_ID,
            workspace_id=workspace_id,
            execution_id=SCALE_WORKER_EXECUTION_ID,
            worker_id=SCALE_WORKER_ID,
            adapter_session_id="r4-scale-worker-item-session",
            status="active",
            last_sequence=2_000,
            last_activity_at=NOW + timedelta(minutes=4),
            input_mode="queued",
            adapter_module_id=None,
            adapter_config_version=None,
            workspace_module_id=None,
            workspace_config_version=None,
            capability_snapshot={"fixture": "scale-worker-item"},
            process_ref="e2e://scale-worker-item-session",
            workspace_ref="e2e://scale-worker-item-workspace",
            version=1,
            created_at=NOW,
            updated_at=NOW + timedelta(minutes=4),
        )
    )
    session.flush()
    session.execute(
        insert(ExecutionEventModel),
        [
            {
                "id": uuid5(NAMESPACE_URL, f"{workspace_id}:scale-trace:{sequence}"),
                "workspace_id": workspace_id,
                "execution_id": SCALE_EXECUTION_ID,
                "session_id": SCALE_SESSION_ID,
                "sequence": sequence,
                "event_type": "tool.call" if sequence % 4 else "assistant.summary",
                "payload": {
                    "tool": "scale-tool" if sequence % 4 else None,
                    "summary": f"Scale trace event {sequence}",
                    "duration_ms": sequence % 97,
                },
                "occurred_at": NOW + timedelta(milliseconds=sequence),
            }
            for sequence in range(1, 50_001)
        ],
    )
    session.execute(
        insert(ExecutionEventModel),
        [
            {
                "id": uuid5(
                    NAMESPACE_URL,
                    f"{workspace_id}:scale-worker-item-activity:{sequence}",
                ),
                "workspace_id": workspace_id,
                "execution_id": SCALE_WORKER_EXECUTION_ID,
                "session_id": SCALE_WORKER_SESSION_ID,
                "sequence": sequence,
                "event_type": "tool.call" if sequence % 4 else "assistant.summary",
                "payload": {
                    "tool": "scale-worker-tool" if sequence % 4 else None,
                    "summary": f"Scale WorkerItem activity {sequence}",
                    "duration_ms": sequence % 89,
                },
                "occurred_at": NOW + timedelta(milliseconds=sequence),
            }
            for sequence in range(1, 2_001)
        ],
    )
    session.add(
        ProjectionChangeModel(
            workspace_id=workspace_id,
            cursor=1,
            schema_version=1,
            projection_type="task_first",
            resource_id=ROOT_TASK_ID,
            change_type="task.plan_snapshot_published",
            resource_version=1,
            occurred_at=NOW,
            correlation_id=GENERATION_ONE_ID,
        )
    )
    session.add(
        ProjectionChangeModel(
            workspace_id=workspace_id,
            cursor=2,
            schema_version=1,
            projection_type="worker_trace",
            resource_id=SCALE_EXECUTION_ID,
            change_type="execution_event.appended",
            resource_version=50_000,
            occurred_at=NOW + timedelta(minutes=5),
            correlation_id=SCALE_SESSION_ID,
        )
    )
    session.add(
        ProjectionChangeModel(
            workspace_id=workspace_id,
            cursor=3,
            schema_version=1,
            projection_type="worker_item_workspace",
            resource_id=SCALE_WORKER_ITEM_ID,
            change_type="worker_item.created",
            resource_version=1,
            occurred_at=NOW,
            correlation_id=SCALE_WORKER_ITEM_ID,
        )
    )
    session.add(
        ProjectionChangeModel(
            workspace_id=workspace_id,
            cursor=4,
            schema_version=1,
            projection_type="worker_item_activity",
            resource_id=SCALE_WORKER_ITEM_ID,
            change_type="worker_item.activity_appended",
            resource_version=2_000,
            occurred_at=NOW + timedelta(minutes=4),
            correlation_id=SCALE_WORKER_SESSION_ID,
        )
    )
    session.add(
        WorkspaceProjectionCursorModel(workspace_id=workspace_id, last_cursor=4)
    )


def _worker_item_model(
    item_id: UUID,
    workspace_id: UUID,
    root_task_id: UUID,
    stable_key: str,
    title: str,
    responsibility: str,
    *,
    status: str,
) -> WorkerItemModel:
    return WorkerItemModel(
        id=item_id,
        workspace_id=workspace_id,
        root_task_id=root_task_id,
        stable_key=stable_key,
        title=title,
        responsibility=responsibility,
        status=status,
        context_version=1,
        acknowledged_context_version=1,
        derived_from_worker_item_ids=[],
        version=1,
        created_at=NOW,
        updated_at=NOW,
    )


def _worker_item_placements(
    workspace_id: UUID,
    *,
    root_task_id: UUID,
    snapshot_id: UUID,
    generation: int,
    placements: tuple[tuple[UUID, str, str, UUID | None, str, tuple[str, ...]], ...],
) -> list[WorkerItemPlacementModel]:
    return [
        WorkerItemPlacementModel(
            plan_snapshot_id=snapshot_id,
            plan_generation=generation,
            workspace_id=workspace_id,
            root_task_id=root_task_id,
            worker_item_id=worker_item_id,
            worker_item_key=worker_item_key,
            subtask_key=subtask_key,
            subtask_task_id=subtask_task_id,
            role=role,
            capability_requirements=list(capability_requirements),
            worker_policy_ref="default",
            configuration_ref=None,
            created_at=NOW,
        )
        for (
            worker_item_id,
            worker_item_key,
            subtask_key,
            subtask_task_id,
            role,
            capability_requirements,
        ) in placements
    ]


def _snapshot(
    workspace_id: UUID,
    *,
    generation: int,
    snapshot_id: UUID,
    parent: UUID | None,
    include_docs: bool,
) -> TaskPlanSnapshot:
    del include_docs
    subtasks = (
        SubtaskPlanItem(
            key="callback-idempotency",
            task_id=BACKEND_TASK_ID,
            kind=WorkItemKind.CHILD,
            title="回调幂等",
            goal_summary="实现回调幂等写入和重试策略。",
            acceptance_refs=("acceptance:callback-idempotency",),
            milestone_key="m1",
            expected_outputs=("output:callback-idempotency",),
            source_refs=("source:callback-idempotency",),
        ),
        SubtaskPlanItem(
            key="callback-test",
            task_id=FRONTEND_TASK_ID,
            kind=WorkItemKind.CHILD,
            title="回调测试",
            goal_summary="验证重复通知、并发和失败重试。",
            acceptance_refs=("acceptance:callback-test",),
            milestone_key="m1",
            expected_outputs=("output:callback-test",),
            source_refs=("source:callback-test",),
        ),
        SubtaskPlanItem(
            key="integration",
            task_id=INTEGRATION_TASK_ID,
            kind=WorkItemKind.INTEGRATION,
            title="集成验收",
            goal_summary="汇总回调实现和测试证据。",
            acceptance_refs=("acceptance:integration",),
            milestone_key="m2",
            expected_outputs=("output:integration",),
            source_refs=("source:integration",),
        ),
    )
    worker_item_plans = (
        WorkerItemPlanItem(
            key="callback-implementer",
            worker_item_id=IMPLEMENTER_WORKER_ITEM_ID,
            subtask_key="callback-idempotency",
            title="实现者",
            role="implementer",
            responsibility="实现回调幂等写入和重试策略。",
            capability_requirements=("python",),
            worker_policy_ref="default",
            configuration_ref="worker-config:implementer",
            concurrency_limit=1,
            source_refs=("source:callback-implementer",),
        ),
        WorkerItemPlanItem(
            key="callback-tester",
            worker_item_id=TESTER_WORKER_ITEM_ID,
            subtask_key="callback-idempotency",
            title="测试者",
            role="tester",
            responsibility="验证重复通知、并发和失败重试。",
            capability_requirements=("python",),
            worker_policy_ref="default",
            configuration_ref="worker-config:tester",
            concurrency_limit=1,
            source_refs=("source:callback-tester",),
        ),
        WorkerItemPlanItem(
            key="callback-reviewer",
            worker_item_id=REVIEWER_WORKER_ITEM_ID,
            subtask_key="callback-idempotency",
            title="审查者",
            role="reviewer",
            responsibility=(
                "审查实现、测试证据和集成验收。"
                if generation == 2
                else "审查实现和测试证据是否满足验收。"
            ),
            capability_requirements=("python",),
            worker_policy_ref="default",
            configuration_ref="worker-config:reviewer",
            concurrency_limit=1,
            source_refs=("source:callback-reviewer",),
        ),
        WorkerItemPlanItem(
            key="migrated-worker",
            worker_item_id=MIGRATED_WORKER_ITEM_ID,
            subtask_key="callback-test" if generation == 2 else "callback-idempotency",
            title="跨代复用者",
            role="tester",
            responsibility="验证跨计划代的 placement 历史复用。",
            capability_requirements=("python",),
            worker_policy_ref="default",
            configuration_ref="worker-config:migrated",
            concurrency_limit=1,
            source_refs=("source:migrated-worker",),
        ),
    )
    return TaskPlanSnapshot.create(
        snapshot_id=snapshot_id,
        schema_version=2,
        workspace_id=workspace_id,
        root_task_id=ROOT_TASK_ID,
        generation=generation,
        parent_snapshot_id=parent,
        source_type="e2e",
        source_ref=f"e2e:g{generation}",
        title="支付重构",
        goal_summary="观察并干预支付回调重构的并行 Worker 工作。",
        acceptance_refs=("graph-visible", "trace-visible", "worker-item-visible"),
        subtasks=subtasks,
        worker_item_plans=worker_item_plans,
        subtask_milestones=(
            SubtaskMilestone(
                key="m1",
                title="并行实现",
                subtask_keys=("callback-idempotency", "callback-test"),
                acceptance_refs=(),
                order_hint=1,
            ),
            SubtaskMilestone(
                key="m2",
                title="集成验收",
                subtask_keys=("integration",),
                acceptance_refs=(),
                order_hint=2,
            ),
        ),
        subtask_dependencies=(
            SubtaskDependency(
                upstream_subtask_key="callback-idempotency",
                downstream_subtask_key="integration",
                kind=DependencyKind.HARD,
                required_evidence_refs=(),
                enforcement=DependencyEnforcement.INFORMATIONAL,
            ),
            SubtaskDependency(
                upstream_subtask_key="callback-test",
                downstream_subtask_key="integration",
                kind=DependencyKind.HARD,
                required_evidence_refs=(),
                enforcement=DependencyEnforcement.INFORMATIONAL,
            ),
        ),
        worker_item_dependencies=(
            WorkerItemDependency(
                upstream_worker_item_key="callback-implementer",
                downstream_worker_item_key="callback-tester",
                kind="dependency",
                required_artifact_refs=(),
            ),
            WorkerItemDependency(
                upstream_worker_item_key="callback-tester",
                downstream_worker_item_key="callback-reviewer",
                kind="review",
                required_artifact_refs=(),
            ),
        ),
        published_by="local:e2e",
        published_at=NOW + timedelta(minutes=generation),
    )


def _secondary_snapshot(
    workspace_id: UUID,
    *,
    generation: int,
    snapshot_id: UUID,
    parent: UUID | None,
) -> TaskPlanSnapshot:
    return TaskPlanSnapshot.create(
        snapshot_id=snapshot_id,
        schema_version=2,
        workspace_id=workspace_id,
        root_task_id=SECOND_ROOT_TASK_ID,
        generation=generation,
        parent_snapshot_id=parent,
        source_type="e2e",
        source_ref=f"e2e:secondary:g{generation}",
        title="发布监控升级",
        goal_summary="验证多个 Root Task 标签之间的切换与历史 WorkerItem。",
        acceptance_refs=("secondary-task-visible",),
        subtasks=(
            SubtaskPlanItem(
                key="release-check",
                task_id=SECOND_SUBTASK_TASK_ID,
                kind=WorkItemKind.CHILD,
                title="发布检查",
                goal_summary="保留历史 WorkerItem 的发布检查。",
                acceptance_refs=("acceptance:release-check",),
                milestone_key="m1",
                expected_outputs=("output:release-check",),
                source_refs=("source:release-check",),
            ),
        ),
        worker_item_plans=(
            WorkerItemPlanItem(
                key="legacy-release-check",
                worker_item_id=LEGACY_WORKER_ITEM_ID,
                subtask_key="release-check",
                title="历史发布检查",
                role="reviewer",
                responsibility="只读保留的旧 WorkerItem 上下文。",
                capability_requirements=("typescript",),
                worker_policy_ref="default",
                configuration_ref="worker-config:legacy",
                concurrency_limit=1,
                source_refs=("source:legacy-release-check",),
            ),
        ),
        subtask_milestones=(
            SubtaskMilestone(
                key="m1",
                title="发布检查",
                subtask_keys=("release-check",),
                acceptance_refs=(),
                order_hint=1,
            ),
        ),
        subtask_dependencies=(),
        worker_item_dependencies=(),
        published_by="local:e2e",
        published_at=NOW + timedelta(minutes=generation),
    )


def _scale_subtask(workspace_id: UUID, index: int) -> SubtaskPlanItem:
    return SubtaskPlanItem(
        key=f"work-{index:03d}",
        task_id=uuid5(NAMESPACE_URL, f"{workspace_id}:scale-task:{index}"),
        kind=WorkItemKind.CHILD,
        title=f"Subtask {index:03d}",
        goal_summary="Scale fixture subtask.",
        acceptance_refs=("scale:graph",),
        milestone_key=f"m{(index - 1) // 50 + 1}",
        expected_outputs=(f"output:{index:03d}",),
        source_refs=(f"source:{index:03d}",),
    )


def _scale_worker_item_id(workspace_id: UUID, index: int) -> UUID:
    if index == 1:
        return SCALE_WORKER_ITEM_ID
    return uuid5(NAMESPACE_URL, f"{workspace_id}:scale-worker-item:{index}")


def _scale_worker_item_plan(workspace_id: UUID, index: int) -> WorkerItemPlanItem:
    return WorkerItemPlanItem(
        key=f"scale-worker-{index:02d}",
        worker_item_id=_scale_worker_item_id(workspace_id, index),
        subtask_key="work-001",
        title=f"Scale WorkerItem {index:02d}",
        role="scale",
        responsibility="Scale fixture WorkerItem activity and graph coverage.",
        capability_requirements=("python",),
        worker_policy_ref="default",
        configuration_ref="worker-config:scale",
        concurrency_limit=1,
        source_refs=(f"source:scale-worker-{index:02d}",),
    )


def _plan_model(snapshot: TaskPlanSnapshot) -> PlanSnapshotModel:
    return PlanSnapshotModel(
        id=snapshot.id,
        schema_version=snapshot.schema_version,
        workspace_id=snapshot.workspace_id,
        root_task_id=snapshot.root_task_id,
        generation=snapshot.generation,
        parent_snapshot_id=snapshot.parent_snapshot_id,
        source_type=snapshot.source_type,
        source_ref=snapshot.source_ref,
        title=snapshot.title,
        goal_summary=snapshot.goal_summary,
        payload=snapshot.to_payload(),
        published_by=snapshot.published_by,
        published_at=snapshot.published_at,
    )


def _capabilities() -> dict[str, str]:
    return {
        "input": "queued",
        "idempotency": "operation-id",
        "resume": "none",
        "stop": "exact-execution",
        "sandbox": "filesystem",
        "stability": "experimental",
    }


if __name__ == "__main__":
    raise SystemExit(main())
