"""Lock-order and source-fence audit for R6.3.4 result aggregation.

The control plane uses one global acquisition order whenever a path can touch
result-source identity or a release decision:

====================  ================================================
Call site             Conflict fence
====================  ================================================
decision apply        PlanHead version/hash -> Run resource_version ->
                      Evaluation lease/operation -> target request versions
approve/resume        PlanHead version/hash -> Run resource_version -> sorted
                      WorkerItems/Requests -> Hold/Proposal -> Application
governance recovery   PlanHead -> Run -> sorted WorkerItems/Requests -> sorted
                      Holds/Proposals -> Applications
Revision              PlanHead expected version -> sorted WorkerItems; moving
                      generation makes old Run/Selection stale
handoff                PlanHead/generation -> WorkerItem -> source request and
                      execution versions -> transition operation CAS
new attempt/result    ExecutionRequest/Execution version CAS; append-only
                      Artifact/Evidence rows remain bound to that execution
continuation          source Execution version plus target request/transition
                      identity; inherited lineage is explicit, never inferred
====================  ================================================

Selection rows are introduced in Task 13. They are acquired after Proposal and
before Release/Application while reusing the same parent fences above.
"""

import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from uuid import UUID, uuid4

import pytest
from sqlalchemy import event, select, text
from sqlalchemy.exc import OperationalError
from sqlalchemy.sql import Select

from sidera_control_plane.adapters.reference_collaboration_runtime import (
    ReferenceCollaborationRuntime,
)
from sidera_control_plane.application.collaboration_runtime_invoker import (
    RuntimeInvocationResult,
)
from sidera_control_plane.application.event_ingestor import ArtifactRegistry
from sidera_control_plane.domain.artifacts import ArtifactCandidate, ArtifactType
from sidera_control_plane.persistence.models import ArtifactModel
from sidera_control_plane.persistence.unit_of_work import SqlAlchemyUnitOfWork

from .test_collaboration_decision_application import _service
from .test_collaboration_evaluation_recovery import _invoker, _prepare
from .test_r5_1_execution_request_schema import (
    request_row,
    seed_execution_request_facts,
)

pytestmark = [
    pytest.mark.integration,
    pytest.mark.usefixtures("clean_database"),
]

NOW = datetime(2026, 9, 3, 12, 0, tzinfo=UTC)


def test_apply_locks_plan_before_run_and_evaluation(
    database,
    workspace_id: UUID,
    uow_factory,
) -> None:
    _ids, _snapshot, _reconciler, prepared = _prepare(
        database, workspace_id, uow_factory
    )
    invocation = _invoker(ReferenceCollaborationRuntime(clock=lambda: NOW)).invoke(
        prepared, now=NOW
    )
    assert isinstance(invocation, RuntimeInvocationResult)
    acquired: list[str] = []

    def observe(_conn, clause, _multiparams, _params, _options) -> None:
        if isinstance(clause, Select) and clause._for_update_arg is not None:
            for table in clause.get_final_froms():
                name = getattr(table, "name", None)
                if name in {
                    "root_task_plan_heads",
                    "collaboration_runs",
                    "collaboration_evaluations",
                    "worker_items",
                    "execution_requests",
                }:
                    acquired.append(name)

    event.listen(database.engine, "before_execute", observe)
    try:
        _service(uow_factory).apply(prepared, invocation, now=NOW)
    finally:
        event.remove(database.engine, "before_execute", observe)

    assert list(dict.fromkeys(acquired)) == [
        "root_task_plan_heads",
        "collaboration_runs",
        "collaboration_evaluations",
        "worker_items",
        "execution_requests",
    ]


def test_artifact_registration_locks_worker_request_then_execution(
    database,
    workspace_id: UUID,
) -> None:
    ids, request_id = _seed_materialized_source(database, workspace_id)
    acquired: list[str] = []

    def observe(_conn, clause, _multiparams, _params, _options) -> None:
        if isinstance(clause, Select) and clause._for_update_arg is not None:
            for table in clause.get_final_froms():
                name = getattr(table, "name", None)
                if name in {"worker_items", "execution_requests", "executions"}:
                    acquired.append(name)

    event.listen(database.engine, "before_execute", observe)
    try:
        report = _registry(database).register(**_registration_kwargs(workspace_id, ids))
    finally:
        event.remove(database.engine, "before_execute", observe)

    assert len(report.artifacts) == 1
    assert request_id is not None
    assert list(dict.fromkeys(acquired)) == [
        "worker_items",
        "execution_requests",
        "executions",
    ]


def test_source_registration_has_a_bounded_shared_fence(
    database,
    workspace_id: UUID,
) -> None:
    ids, request_id = _seed_materialized_source(database, workspace_id)
    attempted = threading.Event()
    writer_thread_id: list[int] = []

    class BoundedUnitOfWork(SqlAlchemyUnitOfWork):
        def __enter__(self):
            entered = super().__enter__()
            self.session.execute(text("SET LOCAL lock_timeout = '250ms'"))
            self.session.execute(text("SET LOCAL statement_timeout = '2s'"))
            return entered

    def observe(_conn, clause, _multiparams, _params, _options) -> None:
        if (
            writer_thread_id
            and threading.get_ident() == writer_thread_id[0]
            and isinstance(clause, Select)
            and clause._for_update_arg is not None
            and any(
                getattr(table, "name", None) == "worker_items"
                for table in clause.get_final_froms()
            )
        ):
            attempted.set()

    def register() -> object:
        writer_thread_id.append(threading.get_ident())
        registry = ArtifactRegistry(
            lambda: BoundedUnitOfWork(database),
            clock=lambda: NOW,
        )
        return registry.register(**_registration_kwargs(workspace_id, ids))

    with SqlAlchemyUnitOfWork(database) as locking_uow:
        worker = locking_uow.worker_items.get(
            ids["worker_item"], workspace_id, for_update=True
        )
        request = locking_uow.execution_requests.get(
            request_id, workspace_id, for_update=True
        )
        assert worker is not None and request is not None
        event.listen(database.engine, "before_execute", observe)
        try:
            with ThreadPoolExecutor(max_workers=1) as executor:
                future = executor.submit(register)
                assert attempted.wait(timeout=5)
                with pytest.raises(OperationalError):
                    future.result(timeout=5)
        finally:
            event.remove(database.engine, "before_execute", observe)

    with database.session() as session:
        assert session.scalars(select(ArtifactModel)).all() == []
    assert (
        len(
            _registry(database)
            .register(**_registration_kwargs(workspace_id, ids))
            .artifacts
        )
        == 1
    )


def _seed_materialized_source(database, workspace_id: UUID):
    ids = seed_execution_request_facts(database, workspace_id)
    source_request = request_row(ids, request_id=uuid4(), worker_key="backend-source")
    source_request.status = "materialized"
    source_request.gate_reason = "ready"
    source_request.gate_details = {}
    source_request.execution_lineage_id = ids["execution"]
    source_request.first_execution_id = ids["execution"]
    with database.session() as session:
        session.add(source_request)
        session.commit()
    return ids, source_request.id


def _registry(database) -> ArtifactRegistry:
    return ArtifactRegistry(
        lambda: SqlAlchemyUnitOfWork(database),
        clock=lambda: NOW,
    )


def _registration_kwargs(workspace_id: UUID, ids: dict[str, UUID]):
    return {
        "workspace_id": workspace_id,
        "task_id": ids["subtask"],
        "execution_id": ids["execution"],
        "session_id": None,
        "candidates": (
            ArtifactCandidate(
                artifact_type=ArtifactType.REPORT,
                storage_ref="artifact://result-input-locking/report",
                content_hash="b" * 64,
                size_bytes=12,
                media_type="text/plain",
                provenance={"acceptance_criterion_ref": "acceptance:test"},
            ),
        ),
        "acceptance_criteria": ("acceptance:test",),
        "producer": "locking-test",
        "environment_ref": "env://locking-test",
    }
