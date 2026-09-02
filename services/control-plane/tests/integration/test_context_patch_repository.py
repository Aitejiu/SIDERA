from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest
from sqlalchemy import select
from tests.integration.test_context_package_repository import _seed_context
from tests.integration.test_plan_transition_repository import (
    _seed_records,
    _transition,
    _worker_transition,
)

from sidera_control_plane.domain.context_patches import (
    ContextPatchAcknowledgement,
    ContextPatchV1,
)
from sidera_control_plane.domain.execution import Execution, ExecutionSession
from sidera_control_plane.domain.worker import Worker
from sidera_control_plane.persistence.context_patch_repository import (
    ContextPatchRepository,
)
from sidera_control_plane.persistence.execution_repository import ExecutionRepository
from sidera_control_plane.persistence.models import (
    ContextAssemblyRequestModel,
    ContextPackageModel,
    WorkContextRevisionModel,
)
from sidera_control_plane.persistence.plan_transition_repository import (
    PlanTransitionRepository,
    WorkerTransitionRepository,
)
from sidera_control_plane.persistence.session_repository import SessionRepository
from sidera_control_plane.persistence.worker_repository import WorkerRepository

pytestmark = pytest.mark.integration

NOW = datetime(2026, 8, 28, 12, 0, tzinfo=UTC)


def test_context_patch_round_trips_and_is_workspace_scoped(
    database, workspace_id: UUID
) -> None:
    request_id, proposal_id, base_snapshot_id, target_snapshot_id, _ = _seed_records(
        database, workspace_id
    )
    transition = _transition(
        workspace_id, request_id, proposal_id, base_snapshot_id, target_snapshot_id
    )
    worker_transition = _worker_transition(workspace_id)
    context_request_id, context_revision_id, _, _ = _seed_context(
        database, workspace_id
    )
    with database.session() as session:
        session.add(
            ContextAssemblyRequestModel(
                id=context_request_id,
                workspace_id=workspace_id,
                purpose="planning_run",
                consumer_type="planner",
                consumer_id=uuid4(),
                worker_item_id=None,
                work_context_revision_id=context_revision_id,
                context_version=None,
                knowledge_search_scope={},
                knowledge_filter_refs=[],
                fact_scope_refs=[],
                budget={},
                expires_at=NOW + timedelta(hours=1),
                status="ready",
                lease_owner=None,
                lease_expires_at=None,
                attempt=1,
                next_attempt_at=None,
                error_code=None,
                request_fingerprint="d" * 64,
                idempotency_key=f"patch-context-{context_request_id}",
                version=1,
            )
        )
        session.flush()
        session.add(
            ContextPackageModel(
                id=uuid4(),
                workspace_id=workspace_id,
                assembly_request_id=context_request_id,
                purpose="planning_run",
                consumer_type="planner",
                consumer_id=uuid4(),
                worker_item_id=None,
                work_context_revision_id=context_revision_id,
                context_version=None,
                control_plane_fact_snapshot={"test": True},
                artifact_refs=[],
                evidence_refs=[],
                budget={},
                expires_at=NOW + timedelta(hours=1),
                package_hash="b" * 64,
                status="active",
                revoked_at=None,
                revocation_reason=None,
                revocation_scope=None,
                idempotency_key=f"patch-package-{context_request_id}",
                materialized_at=None,
                created_at=NOW,
            )
        )
        session.flush()
        package_id = session.scalar(
            select(ContextPackageModel.id).where(
                ContextPackageModel.assembly_request_id == context_request_id
            )
        )
        context_hash = session.scalar(
            select(WorkContextRevisionModel.content_hash).where(
                WorkContextRevisionModel.id == context_revision_id,
                WorkContextRevisionModel.workspace_id == workspace_id,
            )
        )
        assert package_id is not None
        assert context_hash is not None
        PlanTransitionRepository(session).add(transition)
        session.flush()
        WorkerTransitionRepository(session).add(worker_transition)
        session.flush()
        patch = ContextPatchV1.create(
            id=uuid4(),
            workspace_id=workspace_id,
            root_task_id=transition.root_task_id,
            worker_item_id=worker_transition.worker_item_id,
            plan_transition_id=transition.id,
            worker_transition_id=worker_transition.id,
            base_plan_snapshot_id=base_snapshot_id,
            base_generation=1,
            target_plan_snapshot_id=target_snapshot_id,
            target_generation=2,
            base_context_package_id=package_id,
            base_package_hash="b" * 64,
            base_work_context_revision_id=context_revision_id,
            base_context_hash=context_hash,
            target_context_version=2,
            changed_requirements=("new requirement",),
            changed_acceptance_refs=(),
            changed_dependency_refs=(),
            decision_refs=(),
            artifact_refs=(),
            evidence_refs=(),
            intervention_refs=("user:1",),
            unfinished_work="continue the implementation",
            checkpoint_ref="checkpoint:1",
            proposer="integration",
            expires_at=NOW + timedelta(minutes=30),
            created_at=NOW,
        )
        repository = ContextPatchRepository(session)
        repository.add(patch)

        worker = Worker.register(
            worker_id=uuid4(),
            workspace_id=workspace_id,
            adapter_type="fake",
            display_name="Patch acknowledgement worker",
            capabilities=frozenset({"code"}),
            capacity=1,
            cost_per_hour_microunits=1,
            startup_latency_ms=1,
            now=NOW,
        )
        WorkerRepository(session).add(worker)
        session.flush()
        execution_id = uuid4()
        execution = Execution.create(
            execution_id=execution_id,
            workspace_id=workspace_id,
            task_id=transition.root_task_id,
            worker_item_id=worker_transition.worker_item_id,
            lineage_id=execution_id,
            attempt=1,
            max_attempts=1,
            purpose="acknowledge context patch",
            capability_requirements=frozenset({"code"}),
            pinned_worker_id=worker.id,
            not_before=NOW,
        )
        ExecutionRepository(session).add(execution)
        session.flush()
        execution_session = ExecutionSession.start(
            session_id=uuid4(),
            workspace_id=workspace_id,
            execution_id=execution.id,
            worker_id=worker.id,
            adapter_session_id="patch-ack-session",
            now=NOW,
        )
        SessionRepository(session).add(execution_session)
        session.flush()
        acknowledgement = ContextPatchAcknowledgement.create(
            patch=patch,
            execution_id=execution.id,
            session_id=execution_session.id,
            operation_id=uuid4(),
            accepted_sequence=1,
            adapter_ref="fake://patch-adapter",
            acknowledged_at=NOW,
        )
        repository.add_acknowledgement(acknowledgement, workspace_id)
        session.commit()

    with database.session() as session:
        repository = ContextPatchRepository(session)
        loaded = repository.get(patch.id, workspace_id)
        assert loaded == patch
        assert repository.get_by_hash(patch.patch_hash, workspace_id) == patch
        assert repository.list_for_worker_item(
            patch.worker_item_id, workspace_id
        ) == (patch,)
        assert repository.get_acknowledgement(
            patch.id, workspace_id
        ) == acknowledgement
        assert repository.get_acknowledgement(
            patch.id, UUID("00000000-0000-0000-0000-000000000099")
        ) is None
        assert repository.get(patch.id, UUID("00000000-0000-0000-0000-000000000099")) is None
