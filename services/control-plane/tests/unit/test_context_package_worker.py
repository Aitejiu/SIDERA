from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Self
from uuid import UUID, uuid4

from sidera_control_plane.adapters.knowledge_http import KnowledgeAdapterError
from sidera_control_plane.application.ports import (
    ExactRevisionManifest,
    ExactSectionBody,
    KnowledgeCatalogPort,
    SearchHit,
)
from sidera_control_plane.context_package_worker import ContextPackageWorker
from sidera_control_plane.domain.context_packages import (
    ContextAssemblyRequest,
    ContextAssemblyStatus,
    ContextConsumerType,
    ContextPurpose,
)
from sidera_control_plane.domain.work_context import (
    WorkContextRevision,
    WorkContextRevisionReason,
    WorkContextSnapshotV1,
)

NOW = datetime(2026, 8, 26, 12, 0, tzinfo=UTC)
WORKSPACE_ID = UUID("10000000-0000-0000-0000-000000000001")
RUN_ID = UUID("20000000-0000-0000-0000-000000000002")
REQUEST_ID = UUID("30000000-0000-0000-0000-000000000003")
CONTEXT_REVISION_ID = UUID("40000000-0000-0000-0000-000000000004")
PROFILE_ID = UUID("50000000-0000-0000-0000-000000000005")


def _work_context_revision() -> WorkContextRevision:
    snapshot = WorkContextSnapshotV1.create(
        objective="Assemble worker context",
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
    return WorkContextRevision.create(
        revision_id=CONTEXT_REVISION_ID,
        work_context_id=uuid4(),
        workspace_id=WORKSPACE_ID,
        revision_number=1,
        brief_planning_request_id=uuid4(),
        brief_revision=1,
        proposal_revision_id=None,
        plan_snapshot_id=None,
        reason=WorkContextRevisionReason.REQUEST_CREATED,
        snapshot=snapshot,
        created_by="test",
        created_at=NOW,
    )


def _assembly_request(*, query: str | None = "context") -> ContextAssemblyRequest:
    return ContextAssemblyRequest.create(
        request_id=REQUEST_ID,
        workspace_id=WORKSPACE_ID,
        purpose=ContextPurpose.PLANNING_RUN,
        consumer_type=ContextConsumerType.PLANNER,
        consumer_id=RUN_ID,
        worker_item_id=None,
        work_context_revision_id=CONTEXT_REVISION_ID,
        context_version=None,
        knowledge_search_scope={"query": query, "status": "active", "limit": 10},
        knowledge_filter_refs=(),
        fact_scope_refs=("planning_request",),
        budget={
            "profile_id": str(PROFILE_ID),
            "profile_version": 1,
            "max_sections": 16,
            "max_chars": 65_536,
            "max_input_tokens": 16_384,
        },
        expires_at=NOW + timedelta(minutes=30),
        request_fingerprint="a" * 64,
        idempotency_key="context:planning-run:test:v1",
        created_at=NOW,
    )


class FakeAssemblyRepository:
    def __init__(self, request: ContextAssemblyRequest) -> None:
        self.request = request
        self.package = None
        self.retries: list[tuple[int, datetime, str]] = []
        self.blocked: list[str] = []

    def claim_next(self, *, workspace_id, owner, now, lease_ttl, limit):
        del workspace_id, owner, lease_ttl, limit
        request = self.request
        if request.status not in {
            ContextAssemblyStatus.QUEUED,
            ContextAssemblyStatus.RETRY_WAIT,
        }:
            return ()
        attempt = request.attempt + int(request.status is ContextAssemblyStatus.RETRY_WAIT)
        self.request = replace(
            request,
            status=ContextAssemblyStatus.ASSEMBLING,
            lease_owner="test-worker",
            lease_expires_at=now + timedelta(seconds=60),
            attempt=attempt,
            version=request.version + 1,
            updated_at=now,
        )
        return (self.request,)

    def complete_with_package(self, request, manifest, **kwargs):
        assert kwargs["expected_version"] == request.version
        self.package = manifest
        self.request = replace(
            request,
            status=ContextAssemblyStatus.READY,
            lease_owner=None,
            lease_expires_at=None,
            version=request.version + 1,
        )
        return manifest

    def save_retry(self, request, *, next_attempt_at, error_code, **kwargs):
        assert kwargs["expected_version"] == request.version
        self.retries.append((request.attempt, next_attempt_at, error_code))
        self.request = replace(
            request,
            status=ContextAssemblyStatus.RETRY_WAIT,
            lease_owner=None,
            lease_expires_at=None,
            next_attempt_at=next_attempt_at,
            error_code=error_code,
            version=request.version + 1,
        )
        return self.request

    def save_blocked(self, request, *, error_code, **kwargs):
        assert kwargs["expected_version"] == request.version
        self.blocked.append(error_code)
        self.request = replace(
            request,
            status=ContextAssemblyStatus.BLOCKED,
            lease_owner=None,
            lease_expires_at=None,
            error_code=error_code,
            version=request.version + 1,
        )
        return self.request


class FakeCatalog(KnowledgeCatalogPort):
    def __init__(self, state: FakeState) -> None:
        self.state = state
        self.search_calls = 0

    def search(
        self,
        workspace_id: UUID,
        query: str,
        *,
        tags=(),
        source_types=(),
        status="active",
        limit=20,
        correlation_id: UUID,
        caller_request_id: UUID,
    ) -> tuple[SearchHit, ...]:
        del workspace_id, query, tags, source_types, status, limit
        del correlation_id, caller_request_id
        self.search_calls += 1
        assert self.state.active_uows == 0
        if self.state.catalog_error is not None:
            raise self.state.catalog_error
        return ()

    def get_revision_manifest(self, *args, **kwargs) -> ExactRevisionManifest:
        raise AssertionError("test request has no exact Knowledge refs")

    def materialize(self, *args, **kwargs) -> tuple[ExactSectionBody, ...]:
        raise AssertionError("worker test does not materialize Knowledge bodies")


class FakePlanningRun:
    def __init__(self) -> None:
        self.id = RUN_ID
        self.planning_request_id = uuid4()
        self.status = SimpleNamespace(value="queued")
        self.attempt = 1
        self.brief_revision = 1
        self.operation_id = uuid4()
        self.version = 1
        self.error_code: str | None = None
        self.profile_snapshot = SimpleNamespace(
            profile_id=PROFILE_ID,
            profile_key="test",
            profile_version=1,
            planner_module_instance_id=uuid4(),
            planner_module_config_version=1,
            planner_interface_version="sidera.planner.v1",
            prompt_policy_version="prompt.v1",
            proposal_schema_version=2,
        )


class FakePlanningRequest:
    def __init__(self, run: FakePlanningRun) -> None:
        self.id = run.planning_request_id
        self.status = SimpleNamespace(value="submitted")
        self.version = 1
        self.current_brief_revision = 1
        self.briefs = (
            SimpleNamespace(
                planning_request_id=self.id,
                revision=1,
                objective="Assemble worker context",
                constraints=(),
                acceptance_criteria=(),
                context_refs=(),
                requested_outputs=(),
                user_feedback=None,
            ),
        )
        self.run_item = run

    def run(self, run_id: UUID) -> FakePlanningRun:
        assert run_id == self.run_item.id
        return self.run_item

    def block_queued_run(self, *, run_id, error_code, error_message, now) -> None:
        del run_id, error_message, now
        self.run_item.status = SimpleNamespace(value="blocked")
        self.run_item.error_code = error_code
        self.run_item.version += 1
        self.status = SimpleNamespace(value="failed")
        self.version += 1


class FakeState:
    def __init__(self, request: ContextAssemblyRequest) -> None:
        self.active_uows = 0
        self.catalog_error: Exception | None = None
        self.assembly = FakeAssemblyRepository(request)
        self.revision = _work_context_revision()
        self.run = FakePlanningRun()
        self.planning_request = FakePlanningRequest(self.run)
        self.saved_run = None
        self.saved_request = None
        self.events = []
        self.commands = []


class FakeUnitOfWork:
    def __init__(self, state: FakeState) -> None:
        self._state = state
        self.context_assemblies = state.assembly
        self.work_contexts = SimpleNamespace(
            get_revision=lambda *_args, **_kwargs: state.revision
        )
        self.planning_runs = SimpleNamespace(
            get=lambda *_args, **_kwargs: state.run,
            save=lambda run, previous_version: state.__setattr__("saved_run", (run, previous_version)),
        )
        self.planning_requests = SimpleNamespace(
            get=lambda *_args, **_kwargs: state.planning_request,
            save=lambda request, previous_version: state.__setattr__(
                "saved_request", (request, previous_version)
            ),
        )
        self.commands = SimpleNamespace(record_applied=self._record_command)
        self.events = SimpleNamespace(append_with_outbox=state.events.append)

    def _record_command(self, envelope, request_hash, result) -> None:
        self._state.commands.append((envelope, request_hash, result))

    def __enter__(self) -> Self:
        self._state.active_uows += 1
        return self

    def __exit__(self, exc_type, exc_value, traceback) -> None:
        del exc_type, exc_value, traceback
        self._state.active_uows -= 1


def _worker(state: FakeState, catalog: FakeCatalog, *, max_attempts: int = 5) -> ContextPackageWorker:
    return ContextPackageWorker(
        uow_factory=lambda: FakeUnitOfWork(state),
        catalog=catalog,
        owner_id="test-worker",
        lease_seconds=60,
        max_attempts=max_attempts,
        clock=lambda: NOW,
    )


def test_worker_keeps_catalog_call_outside_uow_and_commits_ready_package() -> None:
    state = FakeState(_assembly_request())
    catalog = FakeCatalog(state)

    result = _worker(state, catalog).run_once(limit=1)

    assert result.claimed == 1
    assert result.ready == 1
    assert result.retried == 0
    assert result.blocked == 0
    assert result.failed == 0
    assert catalog.search_calls == 1
    assert state.assembly.package is not None
    assert state.assembly.request.status is ContextAssemblyStatus.READY
    assert state.active_uows == 0


def test_worker_builds_local_package_without_retrieval_scope() -> None:
    state = FakeState(_assembly_request(query=None))
    catalog = FakeCatalog(state)

    result = _worker(state, catalog).run_once(limit=1)

    assert result.ready == 1
    assert result.failed == 0
    assert catalog.search_calls == 0


def test_worker_retries_temporary_failure_then_blocks_at_attempt_limit() -> None:
    state = FakeState(_assembly_request())
    state.catalog_error = KnowledgeAdapterError(
        "context_service_unavailable",
        "Knowledge service is unavailable",
        temporary=True,
    )
    catalog = FakeCatalog(state)
    worker = _worker(state, catalog, max_attempts=2)

    first = worker.run_once(limit=1)
    second = worker.run_once(limit=1)

    assert first == type(first)(1, 0, 1, 0, 0)
    assert second == type(second)(1, 0, 0, 1, 0)
    assert state.assembly.retries[0][0] == 1
    assert state.assembly.retries[0][1] == NOW + timedelta(seconds=5)
    assert state.assembly.blocked == ["context_service_unavailable"]
    assert state.assembly.request.status is ContextAssemblyStatus.BLOCKED
    assert state.run.status.value == "blocked"
    assert state.planning_request.status.value == "failed"


def test_worker_does_not_block_terminal_planning_run_after_stale_claim() -> None:
    state = FakeState(_assembly_request())
    state.run.status = SimpleNamespace(value="succeeded")
    state.catalog_error = KnowledgeAdapterError(
        "permission_denied",
        "Knowledge service permission was denied",
        temporary=False,
    )
    catalog = FakeCatalog(state)

    result = _worker(state, catalog).run_once(limit=1)

    assert result.claimed == 1
    assert result.blocked == 1
    assert result.failed == 0
    assert state.run.status.value == "succeeded"
    assert state.saved_run is None
    assert state.planning_request.status.value == "submitted"
