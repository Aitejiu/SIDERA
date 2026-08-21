"""Seed a deterministic, isolated R4 workspace for browser tests."""

from __future__ import annotations

import argparse
import json
import os
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from uuid import NAMESPACE_URL, UUID, uuid5

from sqlalchemy import delete, insert, select, update

from sidera_control_plane.domain.plans import (
    Dependency,
    DependencyEnforcement,
    DependencyKind,
    Milestone,
    TaskPlanSnapshot,
    WorkerSlot,
    WorkItem,
    WorkItemKind,
)
from sidera_control_plane.persistence.database import Database
from sidera_control_plane.persistence.models import (
    ArtifactModel,
    AssignmentLeaseModel,
    AttentionAcknowledgementModel,
    CommandModel,
    EventModel,
    EvidenceClaimModel,
    ExecutionBindingModel,
    ExecutionEventModel,
    ExecutionInputModel,
    ExecutionModel,
    ExecutionSessionModel,
    ExecutionWorkspaceModel,
    GatewayOperationModel,
    ModuleDefinitionModel,
    ModuleInstanceModel,
    OutboxMessageModel,
    PlanSnapshotModel,
    ProcessDescriptorModel,
    ProjectionChangeModel,
    TaskModel,
    WorkerModel,
    WorkspaceModel,
    WorkspaceProjectionCursorModel,
)

NOW = datetime(2026, 8, 21, 12, 0, tzinfo=UTC)
DEFAULT_WORKSPACE_ID = UUID("20000000-0000-0000-0000-000000000001")
ROOT_TASK_ID = UUID("30000000-0000-0000-0000-000000000001")
BACKEND_TASK_ID = UUID("30000000-0000-0000-0000-000000000002")
FRONTEND_TASK_ID = UUID("30000000-0000-0000-0000-000000000003")
BACKEND_EXECUTION_ID = UUID("60000000-0000-0000-0000-000000000001")
SCALE_EXECUTION_ID = UUID("60000000-0000-0000-0000-000000000002")
SESSION_ID = UUID("70000000-0000-0000-0000-000000000001")
SCALE_SESSION_ID = UUID("70000000-0000-0000-0000-000000000002")
WORKER_ID = UUID("80000000-0000-0000-0000-000000000001")
SCALE_WORKER_ID = UUID("80000000-0000-0000-0000-000000000002")
LEASE_ID = UUID("90000000-0000-0000-0000-000000000001")
ARTIFACT_ID = UUID("a0000000-0000-0000-0000-000000000001")
GENERATION_ONE_ID = UUID("50000000-0000-0000-0000-000000000001")
GENERATION_TWO_ID = UUID("50000000-0000-0000-0000-000000000002")
WORKSPACE_NAME = "SIDERA R4 E2E Workspace"


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--scenario",
        choices=("task-first", "scale"),
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
            else:
                _seed_task_first(session, workspace_id)
            session.commit()
    finally:
        database.dispose()

    execution_id = SCALE_EXECUTION_ID if args.scenario == "scale" else BACKEND_EXECUTION_ID
    session_id = SCALE_SESSION_ID if args.scenario == "scale" else SESSION_ID
    print(
        json.dumps(
            {
                "workspace_id": str(workspace_id),
                "root_task_id": str(ROOT_TASK_ID),
                "execution_id": str(execution_id),
                "session_id": str(session_id),
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
        update(ExecutionModel)
        .where(ExecutionModel.workspace_id == workspace_id)
        .values(active_lease_id=None, source_execution_id=None, source_session_id=None)
    )
    models = (
        AttentionAcknowledgementModel,
        ProjectionChangeModel,
        WorkspaceProjectionCursorModel,
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
        ModuleInstanceModel,
        ModuleDefinitionModel,
        WorkerModel,
        OutboxMessageModel,
        EventModel,
        CommandModel,
        TaskModel,
    )
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
            session.execute(delete(model).where(model.workspace_id == workspace_id))
    if existing is not None:
        session.execute(
            delete(WorkspaceModel).where(WorkspaceModel.id == workspace_id)
        )
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
    adapter_definition_id = uuid5(NAMESPACE_URL, f"{workspace_id}:adapter-definition")
    workspace_definition_id = uuid5(NAMESPACE_URL, f"{workspace_id}:workspace-definition")
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
                title="R4 control room delivery",
                description="Validate the complete Task First control room.",
                acceptance_criteria=["graph is visible", "trace is inspectable"],
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
                title="Backend execution",
                description="Run the backend worker path.",
                acceptance_criteria=["backend trace exists"],
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
                title="Frontend review",
                description="Wait for an operator decision.",
                acceptance_criteria=["operator confirms direction"],
                status="blocked",
                priority=50,
                requested_by="e2e",
                version=1,
                created_at=NOW,
                updated_at=NOW + timedelta(seconds=2),
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
    session.add_all([_plan_model(first), _plan_model(second)])
    session.add(
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
        )
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
                    "title": "Backend checkpoint",
                    "summary": "Repository baseline captured",
                    "artifact_ids": [str(ARTIFACT_ID)],
                },
                occurred_at=NOW + timedelta(seconds=1),
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
        ]
    )
    session.add(WorkspaceProjectionCursorModel(workspace_id=workspace_id, last_cursor=3))
    session.flush()
    session.execute(
        update(ExecutionModel)
        .where(
            ExecutionModel.workspace_id == workspace_id,
            ExecutionModel.id == BACKEND_EXECUTION_ID,
        )
        .values(active_lease_id=LEASE_ID)
    )


def _seed_scale(session, workspace_id: UUID) -> None:
    work_items = tuple(
        _scale_work_item(index)
        for index in range(1, 201)
    )
    task_ids = tuple(item.task_id for item in work_items if item.task_id is not None)
    dependencies = tuple(
        Dependency(
            upstream_work_item_key=f"work-{index:03d}",
            downstream_work_item_key=f"work-{index + offset:03d}",
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
    snapshot = TaskPlanSnapshot.create(
        snapshot_id=GENERATION_ONE_ID,
        schema_version=1,
        workspace_id=workspace_id,
        root_task_id=ROOT_TASK_ID,
        generation=1,
        parent_snapshot_id=None,
        source_type="e2e",
        source_ref="e2e:scale",
        title="Scale graph",
        goal_summary="Render a 200 WorkItem graph with 400 informational edges.",
        acceptance_refs=("scale:graph",),
        work_items=work_items,
        worker_slots=(
            WorkerSlot(
                key="scale-worker",
                role="scale",
                capability_requirements=("python",),
                worker_policy_ref=None,
                configuration_ref=None,
                concurrency_limit=10,
            ),
        ),
        milestones=tuple(
            Milestone(
                key=f"m{index}",
                title=f"Scale phase {index}",
                work_item_keys=tuple(
                    f"work-{item:03d}"
                    for item in range((index - 1) * 50 + 1, index * 50 + 1)
                ),
                acceptance_refs=(),
                order_hint=index,
            )
            for index in range(1, 5)
        ),
        dependencies=dependencies,
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
    session.add(_plan_model(snapshot))
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
    session.add(WorkspaceProjectionCursorModel(workspace_id=workspace_id, last_cursor=2))


def _snapshot(
    workspace_id: UUID,
    *,
    generation: int,
    snapshot_id: UUID,
    parent: UUID | None,
    include_docs: bool,
) -> TaskPlanSnapshot:
    work_items = (
        _work_item(
            "backend",
            BACKEND_TASK_ID,
            "Backend execution",
            slot="opencode-backend",
            kind=WorkItemKind.CHILD,
        ),
        _work_item(
            "frontend",
            FRONTEND_TASK_ID,
            "Frontend review" if generation == 1 else "Frontend review patch",
            slot="opencode-frontend",
            kind=WorkItemKind.CHILD,
        ),
        _work_item(
            "integration",
            None,
            "Integration checkpoint",
            slot="opencode-backend",
            kind=WorkItemKind.INTEGRATION,
        ),
    )
    if include_docs:
        work_items += (
            _work_item(
                "docs",
                None,
                "Documentation handoff",
                slot="opencode-backend",
                kind=WorkItemKind.CHILD,
            ),
        )
    return TaskPlanSnapshot.create(
        snapshot_id=snapshot_id,
        schema_version=1,
        workspace_id=workspace_id,
        root_task_id=ROOT_TASK_ID,
        generation=generation,
        parent_snapshot_id=parent,
        source_type="e2e",
        source_ref=f"e2e:g{generation}",
        title="R4 control room delivery",
        goal_summary="Observe parallel workers, fan-in integration and exact intervention.",
        acceptance_refs=("graph-visible", "trace-visible"),
        work_items=work_items,
        worker_slots=(
            WorkerSlot(
                key="opencode-backend",
                role="backend",
                capability_requirements=("python",),
                worker_policy_ref="default",
                configuration_ref=None,
                concurrency_limit=1,
            ),
            WorkerSlot(
                key="opencode-frontend",
                role="frontend",
                capability_requirements=("typescript",),
                worker_policy_ref="default",
                configuration_ref=None,
                concurrency_limit=1,
            ),
        ),
        milestones=(
            Milestone(
                key="m1",
                title="Parallel implementation",
                work_item_keys=("backend", "frontend"),
                acceptance_refs=(),
                order_hint=1,
            ),
            Milestone(
                key="m2",
                title="Fan-in integration",
                work_item_keys=("integration", "docs") if include_docs else ("integration",),
                acceptance_refs=(),
                order_hint=2,
            ),
        ),
        dependencies=(
            Dependency(
                upstream_work_item_key="backend",
                downstream_work_item_key="integration",
                kind=DependencyKind.HARD,
                required_evidence_refs=(),
                enforcement=DependencyEnforcement.INFORMATIONAL,
            ),
            Dependency(
                upstream_work_item_key="frontend",
                downstream_work_item_key="integration",
                kind=DependencyKind.HARD,
                required_evidence_refs=(),
                enforcement=DependencyEnforcement.INFORMATIONAL,
            ),
        ),
        published_by="local:e2e",
        published_at=NOW + timedelta(minutes=generation),
    )


def _work_item(
    key: str,
    task_id: UUID | None,
    title: str,
    *,
    slot: str,
    kind: WorkItemKind,
) -> WorkItem:
    return WorkItem(
        key=key,
        task_id=task_id,
        kind=kind,
        title=title,
        goal_summary=f"Deliver {key} for the R4 browser journey.",
        acceptance_refs=(f"acceptance:{key}",),
        milestone_key="m1" if key in {"backend", "frontend"} else "m2",
        worker_slot_key=slot,
        expected_outputs=(f"output:{key}",),
        source_refs=(f"source:{key}",),
    )


def _scale_work_item(index: int) -> WorkItem:
    return WorkItem(
        key=f"work-{index:03d}",
        task_id=uuid5(NAMESPACE_URL, f"{DEFAULT_WORKSPACE_ID}:scale-task:{index}"),
        kind=WorkItemKind.CHILD,
        title=f"Work Item {index:03d}",
        goal_summary="Scale fixture work item.",
        acceptance_refs=("scale:graph",),
        milestone_key=f"m{(index - 1) // 50 + 1}",
        worker_slot_key="scale-worker",
        expected_outputs=(f"output:{index:03d}",),
        source_refs=(f"source:{index:03d}",),
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
