from __future__ import annotations

import hashlib
import json
import os
import subprocess
from collections.abc import Sequence
from datetime import UTC, datetime
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select

if os.environ.get("SIDERA_INTERVIEW_DEMO_TEST_RUN") != "1":
    raise RuntimeError(
        "test_interview_intervention.py must run through the isolated demo test runner"
    )

from sidera_control_plane.activation_worker import build_activation_worker
from sidera_control_plane.adapters.git_workspace import opaque_repository_ref
from sidera_control_plane.api.app import create_app
from sidera_control_plane.application.context_materialization import ContextMaterializer
from sidera_control_plane.application.input_dispatcher import InputDispatcher
from sidera_control_plane.application.ports import (
    ExactRevisionManifest,
    ExactSectionBody,
    ExactSectionRef,
    KnowledgeCatalogPort,
    SearchHit,
)
from sidera_control_plane.collaboration_worker import build_collaboration_worker
from sidera_control_plane.config import Settings
from sidera_control_plane.context_package_worker import (
    ContextPackageWorker,
    build_context_package_worker,
)
from sidera_control_plane.demo.bootstrap import bootstrap_demo
from sidera_control_plane.demo.state import DemoStateStore
from sidera_control_plane.demo.test_runner import validate_interview_test_context
from sidera_control_plane.domain.inputs import input_operation_id
from sidera_control_plane.gateway_worker import build_gateway
from sidera_control_plane.persistence.artifact_content_store import (
    LocalArtifactContentStore,
)
from sidera_control_plane.persistence.database import Database
from sidera_control_plane.persistence.models import (
    ArtifactModel,
    CollaborationRunModel,
    ContextAssemblyRequestModel,
    ExecutionInputModel,
    ExecutionModel,
    ExecutionRequestModel,
    ExecutionSessionModel,
    RootTaskPlanHeadModel,
    WorkerItemPlacementModel,
)
from sidera_control_plane.persistence.unit_of_work import SqlAlchemyUnitOfWork
from sidera_control_plane.planning_worker import build_planning_worker
from sidera_control_plane.scheduler_worker import build_scheduler

pytestmark = pytest.mark.integration


class EmptyKnowledgeCatalog(KnowledgeCatalogPort):
    def search(
        self,
        workspace_id: UUID,
        query: str,
        *,
        tags: Sequence[str] = (),
        source_types: Sequence[str] = (),
        status: str | None = "active",
        limit: int = 20,
        correlation_id: UUID,
        caller_request_id: UUID,
    ) -> tuple[SearchHit, ...]:
        del workspace_id, query, tags, source_types, status, limit
        del correlation_id, caller_request_id
        return ()

    def get_revision_manifest(
        self,
        workspace_id: UUID,
        page_id: UUID,
        revision_id: UUID,
        *,
        correlation_id: UUID,
        caller_request_id: UUID,
    ) -> ExactRevisionManifest:
        del workspace_id, page_id, revision_id, correlation_id, caller_request_id
        raise AssertionError("interview demo integration has no Knowledge refs")

    def materialize(
        self,
        workspace_id: UUID,
        sections: Sequence[ExactSectionRef],
        *,
        correlation_id: UUID,
        caller_request_id: UUID,
    ) -> tuple[ExactSectionBody, ...]:
        del workspace_id, sections, correlation_id, caller_request_id
        raise AssertionError("interview demo integration has no Knowledge refs")


def test_send_execution_input_uses_fresh_target_context_and_persists_result(
    database: Database,
    database_url: str,
    clean_database: None,
) -> None:
    manifest = validate_interview_test_context(os.environ)
    bootstrap_demo(manifest, database=database, check_only=False)
    settings = Settings(
        database_url=database_url,
        local_actor_id="interview-task9-test",
        collaboration_workspace_id=manifest.workspace_id,
        interview_demo_manifest=manifest.run_root / "manifest.json",
        collaboration_governance_enabled=True,
        result_inputs_enabled=True,
        result_integration_enabled=True,
        context_package_cutover_at=datetime(2026, 8, 26, 16, 0, tzinfo=UTC),
        workspace_root=manifest.run_root / "workspaces",
        artifact_root=manifest.run_root / "artifacts",
        planning_temp_root=manifest.run_root / "planning",
        knowledge_candidate_spool_dir=manifest.run_root / "candidate-spool",
        result_repository_ref=opaque_repository_ref(manifest.repository),
        result_repository_path=manifest.repository,
        result_base_commit=manifest.base_commit,
    )
    settings.workspace_root.mkdir(parents=True, exist_ok=True)
    settings.artifact_root.mkdir(parents=True, exist_ok=True)
    settings.planning_temp_root.mkdir(parents=True, exist_ok=True)
    settings.knowledge_candidate_spool_dir.mkdir(parents=True, exist_ok=True)

    context_runtime = build_context_package_worker(settings)
    planning_runtime = build_planning_worker(settings)
    empty_catalog = EmptyKnowledgeCatalog()
    context_runtime.worker = ContextPackageWorker(
        uow_factory=lambda: SqlAlchemyUnitOfWork(
            context_runtime.database,
            projection_change_retention_seconds=settings.projection_change_retention_seconds,
        ),
        catalog=empty_catalog,
        owner_id=f"context-worker:{settings.local_actor_id}",
        lease_seconds=settings.context_assembly_lease_seconds,
        max_attempts=settings.context_assembly_max_attempts,
    )
    planning_runtime.worker._runner._context_materializer = ContextMaterializer(
        empty_catalog
    )
    collaboration_runtime = build_collaboration_worker(settings)
    activation_runtime = build_activation_worker(settings)
    scheduler_runtime = build_scheduler(settings)
    gateway_runtime = build_gateway(settings)
    try:
        with TestClient(create_app(settings)) as client:
            submitted = client.post(
                "/api/v1/commands",
                json=_command(
                    manifest.workspace_id,
                    "SubmitPlanningRequest",
                    "workspace",
                    {
                        "profile_id": str(manifest.planning_profile_id),
                        "profile_version": manifest.planning_profile_version,
                        "objective": "为 CSV 导入增加字段校验、报告和集成验证",
                        "constraints": ["手机号策略由 Worker 在执行时向用户确认"],
                        "acceptance_criteria": ["样例 CSV 有可验证的校验结果"],
                        "context_refs": [],
                        "requested_outputs": ["proposal"],
                        "source_type": "integration",
                        "source_ref": "test:interview-worker-intervention",
                    },
                    idempotency_key="task9-submit-planning-request",
                ),
            )
            assert submitted.status_code == 201, submitted.text
            request_id = UUID(submitted.json()["data"]["planning_request_id"])

            planning_context = context_runtime.worker.run_once(10)
            if planning_context.ready < 1:
                with database.session() as session:
                    failed_context = session.scalar(
                        select(ContextAssemblyRequestModel).where(
                            ContextAssemblyRequestModel.workspace_id
                            == manifest.workspace_id,
                            ContextAssemblyRequestModel.purpose == "planning_run",
                        )
                    )
                    context_diagnostic = (
                        None
                        if failed_context is None
                        else {
                            "status": failed_context.status,
                            "error_code": failed_context.error_code,
                            "attempt": failed_context.attempt,
                        }
                    )
                pytest.fail(
                    f"planning context did not become ready: "
                    f"{planning_context}; {context_diagnostic}"
                )
            planning_result = planning_runtime.worker.run_once(10)
            assert planning_result.succeeded == 1, planning_result

            request_detail = client.get(
                f"/api/v1/workspaces/{manifest.workspace_id}/planning-requests/{request_id}"
            )
            assert request_detail.status_code == 200, request_detail.text
            proposal_response = client.get(
                f"/api/v1/workspaces/{manifest.workspace_id}/planning-requests/"
                f"{request_id}/proposals/1"
            )
            assert proposal_response.status_code == 200, proposal_response.text
            proposal = proposal_response.json()
            confirmed = client.post(
                "/api/v1/commands",
                json=_command(
                    manifest.workspace_id,
                    "ConfirmPlanningProposal",
                    "planning_request",
                    {
                        "proposal_revision": 1,
                        "proposal_content_hash": proposal["content_hash"],
                        "profile_id": str(manifest.planning_profile_id),
                        "profile_version": manifest.planning_profile_version,
                    },
                    idempotency_key="task9-confirm-planning-proposal",
                    target_id=request_id,
                    expected_version=request_detail.json()["version"],
                ),
            )
            assert confirmed.status_code == 201, confirmed.text
            root_task_id = UUID(confirmed.json()["data"]["root_task_id"])

            collaboration = collaboration_runtime.worker.run_once(20)
            activation = activation_runtime.worker.run_once(20)
            if activation.materialized < 1:
                with database.session() as session:
                    execution_requests = session.scalars(
                        select(ExecutionRequestModel).where(
                            ExecutionRequestModel.workspace_id == manifest.workspace_id,
                            ExecutionRequestModel.root_task_id == root_task_id,
                        )
                    ).all()
                    collaboration_runs = session.scalars(
                        select(CollaborationRunModel).where(
                            CollaborationRunModel.workspace_id == manifest.workspace_id,
                            CollaborationRunModel.root_task_id == root_task_id,
                        )
                    ).all()
                    gate_diagnostics = [
                        {
                            "status": item.status,
                            "gate_reason": item.gate_reason,
                            "gate_details": item.gate_details,
                            "collaboration_gate_mode": item.collaboration_gate_mode,
                        }
                        for item in execution_requests
                    ]
                    run_diagnostics = [
                        {
                            "status": item.status,
                            "blocked_reason": item.blocked_reason,
                            "runtime_interface_version": item.runtime_interface_version,
                            "runtime_capability_snapshot": item.runtime_capability_snapshot_json,
                        }
                        for item in collaboration_runs
                    ]
                pytest.fail(
                    f"activation did not materialize an execution: {activation}; "
                    f"collaboration={collaboration}; gates={gate_diagnostics}; "
                    f"runs={run_diagnostics}"
                )
            execution_contexts = context_runtime.worker.run_once(20)
            assert execution_contexts.ready >= 1, execution_contexts
            scheduling = scheduler_runtime.scheduler.run_once(20)
            assert scheduling.assigned >= 1, scheduling

            demo_state = DemoStateStore(
                manifest.run_root / "state", manifest.run_id
            )
            demo_state.set_gate("parallel", True)
            demo_state.set_gate("before-artifact", False)
            claimed, processed, failed = gateway_runtime.gateway.run_once(20)
            assert claimed >= 1
            assert failed == 0
            assert processed >= 1

            with database.session() as session:
                placement = session.scalar(
                    select(WorkerItemPlacementModel).where(
                        WorkerItemPlacementModel.workspace_id == manifest.workspace_id,
                        WorkerItemPlacementModel.root_task_id == root_task_id,
                        WorkerItemPlacementModel.worker_item_key == "validate",
                    )
                )
                assert placement is not None
                execution = session.scalar(
                    select(ExecutionModel).where(
                        ExecutionModel.workspace_id == manifest.workspace_id,
                        ExecutionModel.worker_item_id == placement.worker_item_id,
                    )
                )
                assert execution is not None and execution.active_lease_id is not None
                execution_id = execution.id
                lease_id = execution.active_lease_id
                fencing_token = execution.last_fencing_token
                active_session = session.scalar(
                    select(ExecutionSessionModel).where(
                        ExecutionSessionModel.workspace_id == manifest.workspace_id,
                        ExecutionSessionModel.execution_id == execution_id,
                        ExecutionSessionModel.status == "active",
                    )
                )
                assert active_session is not None
                session_id = active_session.id
                assert active_session.worker_id == manifest.worker_ids["validation"]
                before_generation = session.scalar(
                    select(RootTaskPlanHeadModel.active_generation).where(
                        RootTaskPlanHeadModel.workspace_id == manifest.workspace_id,
                        RootTaskPlanHeadModel.root_task_id == root_task_id,
                    )
                )
                assert before_generation is not None
                sibling_inputs_before = session.scalar(
                    select(func.count()).select_from(ExecutionInputModel).where(
                        ExecutionInputModel.workspace_id == manifest.workspace_id,
                        ExecutionInputModel.session_id != session_id,
                    )
                )
                target_artifacts_before = session.scalar(
                    select(func.count()).select_from(ArtifactModel).where(
                        ArtifactModel.workspace_id == manifest.workspace_id,
                        ArtifactModel.execution_id == execution_id,
                    )
                )
                assert sibling_inputs_before == 0
                assert target_artifacts_before == 0

            workspace_response = client.get(
                f"/api/v1/workspaces/{manifest.workspace_id}/worker-items/"
                f"{placement.worker_item_id}/workspace"
            )
            assert workspace_response.status_code == 200, workspace_response.text
            context = workspace_response.json()["intervention_context"]
            assert isinstance(context, dict)
            expected_context_fields = {
                "worker_item_id",
                "plan_generation",
                "context_version",
                "execution_id",
                "session_id",
                "session_version",
                "worker_id",
            }
            assert set(context) == expected_context_fields
            assert context == {
                "worker_item_id": str(placement.worker_item_id),
                "plan_generation": placement.plan_generation,
                "context_version": context["context_version"],
                "execution_id": str(execution_id),
                "session_id": str(session_id),
                "session_version": active_session.version,
                "worker_id": str(manifest.worker_ids["validation"]),
            }

            stale_context = dict(context)
            stale_context["plan_generation"] += 1
            stale = client.post(
                "/api/v1/commands",
                json=_send_input_command(
                    manifest.workspace_id,
                    session_id,
                    stale_context,
                    "task9-stale-context",
                ),
            )
            assert stale.status_code == 409, stale.text
            assert stale.json()["error"]["code"] == "execution_context_changed"
            with database.session() as session:
                assert (
                    session.scalar(
                        select(func.count()).select_from(ExecutionInputModel).where(
                            ExecutionInputModel.workspace_id == manifest.workspace_id
                        )
                    )
                    == 0
                )

            accepted = client.post(
                "/api/v1/commands",
                json=_send_input_command(
                    manifest.workspace_id,
                    session_id,
                    context,
                    "task9-valid-context",
                ),
            )
            assert accepted.status_code in {200, 201}, accepted.text

            dispatcher = InputDispatcher(
                lambda: SqlAlchemyUnitOfWork(gateway_runtime.database),
                gateway_runtime.worker_gateway,
            )
            delivery = dispatcher.run_once(10)
            if delivery.delivered != 1:
                with database.session() as session:
                    input_diagnostics = session.scalars(
                        select(ExecutionInputModel).where(
                            ExecutionInputModel.workspace_id == manifest.workspace_id,
                            ExecutionInputModel.session_id == session_id,
                        )
                    ).all()
                    input_states = [
                        {"status": item.status, "failure_code": item.failure_code}
                        for item in input_diagnostics
                    ]
                pytest.fail(
                    f"intervention dispatch did not succeed: {delivery}; "
                    f"inputs={input_states}"
                )
            gateway_runtime.worker_gateway.sync_session(
                workspace_id=manifest.workspace_id,
                execution_id=execution_id,
                session_id=session_id,
                lease_id=lease_id,
                fencing_token=fencing_token,
            )
            with database.session() as session:
                assert (
                    session.scalar(
                        select(func.count()).select_from(ArtifactModel).where(
                            ArtifactModel.workspace_id == manifest.workspace_id,
                            ArtifactModel.execution_id == execution_id,
                        )
                    )
                    == 0
                )

            demo_state.set_gate("before-artifact", True)
            gateway_runtime.worker_gateway.sync_session(
                workspace_id=manifest.workspace_id,
                execution_id=execution_id,
                session_id=session_id,
                lease_id=lease_id,
                fencing_token=fencing_token,
            )

            with database.session() as session:
                target_artifacts = session.scalars(
                    select(ArtifactModel).where(
                        ArtifactModel.workspace_id == manifest.workspace_id,
                        ArtifactModel.execution_id == execution_id,
                        ArtifactModel.artifact_type == "report",
                    )
                ).all()
                target_artifact = next(
                    (
                        artifact
                        for artifact in target_artifacts
                        if artifact.provenance.get("simulation") is True
                    ),
                    None,
                )
                assert target_artifact is not None
                input_item = session.scalar(
                    select(ExecutionInputModel).where(
                        ExecutionInputModel.workspace_id == manifest.workspace_id,
                        ExecutionInputModel.session_id == session_id,
                    )
                )
                assert input_item is not None and input_item.status == "delivered"
                assert input_item.text == "手机号允许为空，有值时才校验格式"
                assert (
                    session.scalar(
                        select(func.count()).select_from(ExecutionInputModel).where(
                            ExecutionInputModel.workspace_id == manifest.workspace_id,
                            ExecutionInputModel.session_id != session_id,
                        )
                    )
                    == sibling_inputs_before
                )
                after_generation = session.scalar(
                    select(RootTaskPlanHeadModel.active_generation).where(
                        RootTaskPlanHeadModel.workspace_id == manifest.workspace_id,
                        RootTaskPlanHeadModel.root_task_id == root_task_id,
                    )
                )
                assert after_generation == before_generation
                stored_artifact = {
                    "artifact_id": target_artifact.id,
                    "storage_ref": target_artifact.storage_ref,
                    "content_hash": target_artifact.content_hash,
                    "provenance": target_artifact.provenance,
                }

            content = LocalArtifactContentStore(settings.artifact_root).read(
                str(stored_artifact["storage_ref"])
            )
            assert hashlib.sha256(content).hexdigest() == stored_artifact["content_hash"]
            result = json.loads(content)
            bob = next(row for row in result["rows"] if row["row"]["name"] == "Bob")
            assert result["phone_policy"] == "optional"
            assert bob["valid"] is True
            assert stored_artifact["provenance"]["simulation"] is True
            assert stored_artifact["provenance"]["input_operation_id"] == str(
                input_operation_id(input_item.id)
            )

            with database.session() as session:
                report_placement = session.scalar(
                    select(WorkerItemPlacementModel).where(
                        WorkerItemPlacementModel.workspace_id == manifest.workspace_id,
                        WorkerItemPlacementModel.root_task_id == root_task_id,
                        WorkerItemPlacementModel.worker_item_key == "report",
                    )
                )
                assert report_placement is not None
                report_execution = session.scalar(
                    select(ExecutionModel).where(
                        ExecutionModel.workspace_id == manifest.workspace_id,
                        ExecutionModel.worker_item_id == report_placement.worker_item_id,
                    )
                )
                assert report_execution is not None
                report_session = session.scalar(
                    select(ExecutionSessionModel).where(
                        ExecutionSessionModel.workspace_id == manifest.workspace_id,
                        ExecutionSessionModel.execution_id == report_execution.id,
                        ExecutionSessionModel.status == "active",
                    )
                )
                assert report_session is not None
                report_execution_id = report_execution.id
                report_session_id = report_session.id
                report_lease_id = report_execution.active_lease_id
                report_fencing_token = report_execution.last_fencing_token
                report_worker_id = report_session.worker_id
                assert report_lease_id is not None

            gateway_runtime.worker_gateway.sync_session(
                workspace_id=manifest.workspace_id,
                execution_id=report_execution_id,
                session_id=report_session_id,
                lease_id=report_lease_id,
                fencing_token=report_fencing_token,
            )
            with database.session() as session:
                report_artifacts = session.scalars(
                    select(ArtifactModel).where(
                        ArtifactModel.workspace_id == manifest.workspace_id,
                        ArtifactModel.execution_id == report_execution_id,
                        ArtifactModel.artifact_type == "report",
                    )
                ).all()
                report_artifact = next(
                    (
                        artifact
                        for artifact in report_artifacts
                        if artifact.provenance.get("simulation") is True
                    ),
                    None,
                )
                assert report_artifact is not None
                assert report_artifact.provenance.get("acceptance_criterion_refs") == [
                    "acceptance:report"
                ], dict(report_artifact.provenance)
                report_artifact_ref = report_artifact.storage_ref
                report_artifact_id = report_artifact.id
                report_artifact_hash = report_artifact.content_hash

            report_content = LocalArtifactContentStore(settings.artifact_root).read(
                report_artifact_ref
            )
            assert hashlib.sha256(report_content).hexdigest() == report_artifact_hash
            report_document = json.loads(report_content)
            assert report_document["report"]["accepted"] == 2

            _report_execution_succeeded(
                client,
                database,
                manifest.workspace_id,
                execution_id,
                manifest.worker_ids["validation"],
                idempotency_key="task9-validation-succeeded",
            )
            _report_execution_succeeded(
                client,
                database,
                manifest.workspace_id,
                report_execution_id,
                report_worker_id,
                idempotency_key="task9-report-succeeded",
            )

            collaboration_after_outputs = collaboration_runtime.worker.run_once(20)
            activation_after_outputs = activation_runtime.worker.run_once(20)
            assert activation_after_outputs.materialized >= 1, (
                activation_after_outputs,
                collaboration_after_outputs,
            )
            execution_contexts = context_runtime.worker.run_once(20)
            assert execution_contexts.ready >= 1, execution_contexts
            scheduling_after_outputs = scheduler_runtime.scheduler.run_once(20)
            assert scheduling_after_outputs.assigned >= 1, scheduling_after_outputs
            claimed, processed, failed = gateway_runtime.gateway.run_once(20)
            assert failed == 0
            assert claimed >= 1 and processed >= 1

            with database.session() as session:
                review_placement = session.scalar(
                    select(WorkerItemPlacementModel).where(
                        WorkerItemPlacementModel.workspace_id == manifest.workspace_id,
                        WorkerItemPlacementModel.root_task_id == root_task_id,
                        WorkerItemPlacementModel.worker_item_key == "review-report",
                    )
                )
                assert review_placement is not None
                review_execution = session.scalar(
                    select(ExecutionModel).where(
                        ExecutionModel.workspace_id == manifest.workspace_id,
                        ExecutionModel.worker_item_id == review_placement.worker_item_id,
                    )
                )
                assert review_execution is not None
                review_execution_id = review_execution.id
                review_worker_id = review_execution.assigned_worker_id
                assert review_worker_id is not None
                review_artifacts = session.scalars(
                    select(ArtifactModel).where(
                        ArtifactModel.workspace_id == manifest.workspace_id,
                        ArtifactModel.execution_id == review_execution.id,
                        ArtifactModel.artifact_type == "report",
                    )
                ).all()
                review_artifact = next(
                    (
                        artifact
                        for artifact in review_artifacts
                        if artifact.provenance.get("simulation") is True
                    ),
                    None,
                )
                assert review_artifact is not None
                assert str(report_artifact_id) in review_artifact.provenance[
                    "source_artifact_ids"
                ]
                review_storage_ref = review_artifact.storage_ref
                review_content_hash = review_artifact.content_hash

            review_content = LocalArtifactContentStore(settings.artifact_root).read(
                review_storage_ref
            )
            assert hashlib.sha256(review_content).hexdigest() == review_content_hash
            review_text = review_content.decode("utf-8")
            assert f"Source artifact: `{report_artifact_id}`" in review_text
            assert "Accepted rows: 2" in review_text

            _report_execution_succeeded(
                client,
                database,
                manifest.workspace_id,
                review_execution_id,
                review_worker_id,
                idempotency_key="task9-review-succeeded",
            )
            collaboration_after_review = collaboration_runtime.worker.run_once(20)
            activation_after_review = activation_runtime.worker.run_once(20)
            if activation_after_review.materialized < 1:
                with database.session() as session:
                    request_states = session.scalars(
                        select(ExecutionRequestModel).where(
                            ExecutionRequestModel.workspace_id == manifest.workspace_id,
                            ExecutionRequestModel.root_task_id == root_task_id,
                        )
                    ).all()
                    collaboration_states = session.scalars(
                        select(CollaborationRunModel).where(
                            CollaborationRunModel.workspace_id == manifest.workspace_id,
                            CollaborationRunModel.root_task_id == root_task_id,
                        )
                    ).all()
                    request_diagnostics = [
                        {
                            "worker_item_key": item.worker_item_key,
                            "status": item.status,
                            "gate_reason": item.gate_reason,
                            "gate_details": item.gate_details,
                        }
                        for item in request_states
                    ]
                    run_diagnostics = [
                        {"status": item.status, "blocked_reason": item.blocked_reason}
                        for item in collaboration_states
                    ]
                pytest.fail(
                    "integration did not become activatable after review succeeded: "
                    f"activation={activation_after_review}; "
                    f"collaboration={collaboration_after_review}; "
                    f"requests={request_diagnostics}; collaboration_runs={run_diagnostics}"
                )
            execution_contexts = context_runtime.worker.run_once(20)
            assert execution_contexts.ready >= 1, execution_contexts
            scheduling_after_review = scheduler_runtime.scheduler.run_once(20)
            assert scheduling_after_review.assigned >= 1, scheduling_after_review
            claimed, processed, failed = gateway_runtime.gateway.run_once(20)
            assert failed == 0
            assert claimed >= 1 and processed >= 1

            with database.session() as session:
                integration_placement = session.scalar(
                    select(WorkerItemPlacementModel).where(
                        WorkerItemPlacementModel.workspace_id == manifest.workspace_id,
                        WorkerItemPlacementModel.root_task_id == root_task_id,
                        WorkerItemPlacementModel.worker_item_key == "integrate",
                    )
                )
                assert integration_placement is not None
                integration_execution = session.scalar(
                    select(ExecutionModel).where(
                        ExecutionModel.workspace_id == manifest.workspace_id,
                        ExecutionModel.worker_item_id == integration_placement.worker_item_id,
                    )
                )
                if integration_execution is None:
                    pytest.fail(
                        "integration execution not materialized after review success: "
                        f"collaboration={collaboration_after_review}; "
                        f"activation={activation_after_review}; "
                        f"scheduling={scheduling_after_review}"
                    )
                diff_artifacts = session.scalars(
                    select(ArtifactModel).where(
                        ArtifactModel.workspace_id == manifest.workspace_id,
                        ArtifactModel.execution_id == integration_execution.id,
                        ArtifactModel.artifact_type == "diff",
                    )
                ).all()
                diff_artifact = next(iter(diff_artifacts), None)
                assert diff_artifact is not None
                diff_storage_ref = diff_artifact.storage_ref
                diff_content_hash = diff_artifact.content_hash

            diff_content = LocalArtifactContentStore(settings.artifact_root).read(
                diff_storage_ref
            )
            assert hashlib.sha256(diff_content).hexdigest() == diff_content_hash
            applied_check = subprocess.run(
                ["git", "apply", "--check"],
                cwd=manifest.repository,
                input=diff_content,
                capture_output=True,
                check=False,
            )
            assert applied_check.returncode == 0, applied_check.stderr.decode(
                "utf-8", errors="replace"
            )
    finally:
        gateway_runtime.catalog.close()
        gateway_runtime.database.dispose()
        scheduler_runtime.database.dispose()
        activation_runtime.database.dispose()
        collaboration_runtime.database.dispose()
        planning_runtime.catalog.close()
        planning_runtime.database.dispose()
        context_runtime.catalog.close()
        context_runtime.database.dispose()


def _command(
    workspace_id: UUID,
    command_type: str,
    target_type: str,
    payload: dict[str, object],
    *,
    idempotency_key: str,
    target_id: UUID | None = None,
    expected_version: int | None = None,
) -> dict[str, object]:
    return {
        "command_id": str(uuid4()),
        "workspace_id": str(workspace_id),
        "command_type": command_type,
        "target_type": target_type,
        "actor": {"type": "local", "id": "interview-task9-test"},
        "target_id": str(target_id) if target_id else None,
        "expected_version": expected_version,
        "idempotency_key": idempotency_key,
        "correlation_id": str(uuid4()),
        "payload": payload,
    }


def _send_input_command(
    workspace_id: UUID,
    session_id: UUID,
    context: dict[str, object],
    client_key: str,
) -> dict[str, object]:
    return _command(
        workspace_id,
        "SendExecutionInput",
        "execution_session",
        {
            "context": context,
            "client_idempotency_key": client_key,
            "text": "手机号允许为空，有值时才校验格式",
            "attachment_refs": [],
        },
        idempotency_key=f"task9-send-input:{client_key}",
        target_id=session_id,
        expected_version=int(context["session_version"]),
    )


def _report_execution_succeeded(
    client: TestClient,
    database: Database,
    workspace_id: UUID,
    execution_id: UUID,
    worker_id: UUID,
    *,
    idempotency_key: str,
) -> None:
    with database.session() as session:
        execution = session.scalar(
            select(ExecutionModel).where(
                ExecutionModel.workspace_id == workspace_id,
                ExecutionModel.id == execution_id,
            )
        )
        assert execution is not None
        assert execution.assigned_worker_id == worker_id
        if execution.status == "succeeded":
            return
        assert execution.active_lease_id is not None
        command = {
            "command_id": str(uuid4()),
            "workspace_id": str(workspace_id),
            "command_type": "ReportExecutionSucceeded",
            "target_type": "execution",
            "actor": {"type": "worker", "id": str(worker_id)},
            "target_id": str(execution_id),
            "expected_version": execution.version,
            "idempotency_key": idempotency_key,
            "correlation_id": str(uuid4()),
            "payload": {
                "lease_id": str(execution.active_lease_id),
                "worker_id": str(worker_id),
                "fencing_token": execution.last_fencing_token,
            },
        }

    response = client.post("/api/v1/commands", json=command)
    assert response.status_code in {200, 201}, response.text
