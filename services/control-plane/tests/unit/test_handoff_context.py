from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any, Self
from uuid import UUID, uuid4

import pytest

from sidera_control_plane.application.handoff_context import (
    HandoffContextError,
    HandoffContextService,
)
from sidera_control_plane.domain.context_packages import (
    ContextAssemblyRequest,
    ContextAssemblyStatus,
    ContextBudgetV1,
    ContextConsumerType,
    ContextPackageManifestV1,
    ContextPurpose,
)
from sidera_control_plane.domain.execution import (
    Execution,
    ExecutionSessionStatus,
    ExecutionStatus,
)
from sidera_control_plane.domain.execution_requests import ExecutionRequest
from sidera_control_plane.domain.handoff_evidence import (
    HandoffCheckpointEvidence,
    HandoffInputAction,
    HandoffInputClassification,
    HandoffInputDisposition,
    HandoffSourceFacts,
    HandoffStopEvidence,
)
from sidera_control_plane.domain.handoffs import (
    HandoffPhase,
    HandoffProposal,
    HandoffProposalPayload,
    HandoffSourceChannel,
    HandoffTransferMode,
    HandoffTransition,
)
from sidera_control_plane.domain.work_context import (
    WorkContextRevision,
    WorkContextRevisionReason,
    WorkContextSnapshotV1,
)
from sidera_control_plane.domain.worker import Worker
from sidera_control_plane.domain.worker_item import WorkerItem
from sidera_control_plane.domain.workspace import (
    ExecutionWorkspaceManifest,
    WorkspaceCheckout,
    workspace_manifest_digest,
)

NOW = datetime(2026, 9, 1, 12, 0, tzinfo=UTC)
WORKSPACE_ID = UUID("10000000-0000-0000-0000-000000000001")


class MemoryRepository:
    def __init__(self, values: tuple[Any, ...] = ()) -> None:
        self.values = {_entity_id(value): value for value in values}
        self.added: list[Any] = []
        self.saved: list[tuple[Any, int]] = []

    def get(self, value_id: UUID, workspace_id: UUID, **_kwargs: object) -> Any | None:
        value = self.values.get(value_id)
        if value is None or value.workspace_id != workspace_id:
            return None
        return value

    def add(self, value: Any) -> None:
        value_id = _entity_id(value)
        if value_id in self.values:
            raise AssertionError("duplicate value")
        self.values[value_id] = value
        self.added.append(value)

    def save(self, value: Any, previous_version: int) -> None:
        self.values[_entity_id(value)] = value
        self.saved.append((value, previous_version))

    def get_revision(self, revision_id: UUID, workspace_id: UUID, **_kwargs: object) -> Any | None:
        value = self.values.get(revision_id)
        if value is None or value.workspace_id != workspace_id:
            return None
        return value

    def get_effective_by_execution_lineage(
        self,
        execution_lineage_id: UUID,
        workspace_id: UUID,
        **_kwargs: object,
    ) -> Any | None:
        return next(
            (
                value
                for value in self.values.values()
                if getattr(value, "workspace_id", None) == workspace_id
                and getattr(value, "execution_lineage_id", None)
                == execution_lineage_id
            ),
            None,
        )


class BundleRepository(MemoryRepository):
    def get_by_attempt(
        self,
        transition_id: UUID,
        logical_attempt: int,
        workspace_id: UUID,
        **_kwargs: object,
    ) -> Any | None:
        return next(
            (
                value
                for value in self.values.values()
                if value.workspace_id == workspace_id
                and value.handoff_transition_id == transition_id
                and value.logical_attempt == logical_attempt
            ),
            None,
        )

    def add_append_only(self, value: Any) -> None:
        existing = self.get(value.id, value.workspace_id)
        if existing is not None:
            if existing != value:
                raise AssertionError("bundle is immutable")
            return
        self.add(value)


class AssemblyRepository:
    def __init__(self, values: tuple[ContextAssemblyRequest, ...] = ()) -> None:
        self.values = {value.request_id: value for value in values}

    def add(self, request: ContextAssemblyRequest) -> ContextAssemblyRequest:
        existing = self.get_for_consumer(
            request.purpose,
            request.consumer_id,
            request.workspace_id,
        )
        if existing is not None:
            if (
                existing.request_fingerprint != request.request_fingerprint
                or existing.idempotency_key != request.idempotency_key
            ):
                raise AssertionError("assembly request facts changed")
            return existing
        self.values[request.request_id] = request
        return request

    def get_for_consumer(
        self,
        purpose: ContextPurpose,
        consumer_id: UUID,
        workspace_id: UUID,
        **_kwargs: object,
    ) -> ContextAssemblyRequest | None:
        return next(
            (
                value
                for value in self.values.values()
                if value.purpose is purpose
                and value.consumer_id == consumer_id
                and value.workspace_id == workspace_id
            ),
            None,
        )


class PackageRepository(MemoryRepository):
    def get_for_consumer(
        self,
        purpose: ContextPurpose,
        consumer_id: UUID,
        workspace_id: UUID,
        **kwargs: object,
    ) -> ContextPackageManifestV1 | None:
        return next(
            (
                value
                for value in self.values.values()
                if value.purpose is purpose
                and value.consumer_id == consumer_id
                and value.workspace_id == workspace_id
            ),
            None,
        )


class MemoryUnitOfWork:
    def __init__(self, fixture: SimpleNamespace) -> None:
        self.handoff_transitions = MemoryRepository((fixture.transition,))
        self.handoff_context_bundles = BundleRepository()
        self.execution_requests = MemoryRepository(
            (fixture.source_request, fixture.target_request)
        )
        self.executions = MemoryRepository(
            (fixture.source_execution, fixture.target_execution)
        )
        self.sessions = SimpleNamespace(
            get_any=lambda session_id, workspace_id, **_kwargs: (
                SimpleNamespace(
                    id=session_id,
                    workspace_id=workspace_id,
                    execution_id=fixture.source_execution.id,
                    status=ExecutionSessionStatus.CLOSED,
                )
                if session_id == fixture.source_facts.checkpoint.source_session_id
                else None
            )
        )
        self.workspaces = MemoryRepository((fixture.source_manifest,))
        self.context_packages = PackageRepository((fixture.source_package,))
        self.worker_items = MemoryRepository((fixture.worker_item,))
        self.work_contexts = MemoryRepository((fixture.context_revision,))
        self.context_assemblies = AssemblyRepository()

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *_args: object) -> None:
        return None


def _entity_id(value: Any) -> UUID:
    value_id = getattr(value, "id", None)
    if value_id is None:
        value_id = getattr(value, "package_id", None)
    if value_id is None:
        value_id = getattr(value, "request_id", None)
    if value_id is None:
        value_id = getattr(value, "revision_id", None)
    if not isinstance(value_id, UUID):
        raise TypeError("memory repository entity has no UUID identity")
    return value_id


def _worker(worker_id: UUID | None = None) -> Worker:
    return Worker.register(
        worker_id=worker_id or uuid4(),
        workspace_id=WORKSPACE_ID,
        adapter_type="fake",
        display_name="Target worker",
        capabilities=frozenset({"python"}),
        capacity=1,
        cost_per_hour_microunits=10,
        startup_latency_ms=1,
        now=NOW,
    )


def _manifest(execution: Execution) -> ExecutionWorkspaceManifest:
    manifest = ExecutionWorkspaceManifest.create(
        id=uuid4(),
        workspace_id=WORKSPACE_ID,
        task_id=execution.task_id,
        execution_id=execution.id,
        checkouts=(
            WorkspaceCheckout(
                repository_ref="repo://sidera",
                base_revision="a" * 40,
                branch="sidera/handoff",
                current_head="b" * 40,
            ),
        ),
        location_ref="fake-workspace://handoff/unit",
        retention_seconds=3600,
        fencing_token=7,
        created_at=NOW,
    )
    manifest.mark_preparing()
    manifest.mark_ready()
    manifest.mark_active()
    manifest.freeze(now=NOW)
    return manifest


def _context_revision() -> WorkContextRevision:
    snapshot = WorkContextSnapshotV1.create(
        objective="Continue the handoff task",
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
        revision_id=uuid4(),
        work_context_id=uuid4(),
        workspace_id=WORKSPACE_ID,
        revision_number=2,
        brief_planning_request_id=uuid4(),
        brief_revision=1,
        proposal_revision_id=uuid4(),
        plan_snapshot_id=uuid4(),
        reason=WorkContextRevisionReason.PROPOSAL_CONFIRMED,
        snapshot=snapshot,
        created_by="unit",
        created_at=NOW,
    )


def _fixture() -> SimpleNamespace:
    task_id = uuid4()
    worker_item = WorkerItem.create(
        item_id=uuid4(),
        workspace_id=WORKSPACE_ID,
        root_task_id=task_id,
        title="Handoff item",
        responsibility="Continue the task",
        now=NOW,
    )
    source_worker = _worker()
    target_worker = _worker()
    source_execution = Execution.create(
        execution_id=uuid4(),
        workspace_id=WORKSPACE_ID,
        task_id=task_id,
        worker_item_id=worker_item.id,
        lineage_id=uuid4(),
        attempt=1,
        max_attempts=3,
        purpose="worker-item",
        capability_requirements=frozenset({"python"}),
        pinned_worker_id=source_worker.id,
        not_before=NOW,
        adapter_preferences=("fake",),
    )
    source_execution = replace(
        source_execution,
        status=ExecutionStatus.CANCELED,
        last_fencing_token=7,
        finished_at=NOW,
    )
    source_manifest = _manifest(source_execution)
    source_request = ExecutionRequest.create(
        request_id=uuid4(),
        workspace_id=WORKSPACE_ID,
        planning_request_id=uuid4(),
        proposal_revision_id=uuid4(),
        plan_snapshot_id=uuid4(),
        plan_generation=3,
        root_task_id=task_id,
        subtask_id=task_id,
        worker_item_id=worker_item.id,
        worker_item_key="handoff-item",
        intent_key="handoff-item",
        purpose="worker-item",
        capability_requirements=frozenset({"python"}),
        adapter_preferences=("fake",),
        budget_class="standard",
        not_before=NOW,
        max_attempts=3,
        expected_artifact_refs=(),
        expected_evidence_refs=(),
        expected_outputs=("diff",),
        now=NOW,
    )
    source_request.mark_ready(now=NOW)
    source_request.materialize(
        execution_lineage_id=source_execution.lineage_id,
        first_execution_id=source_execution.id,
        now=NOW,
    )
    context_revision = replace(
        _context_revision(),
        proposal_revision_id=source_request.proposal_revision_id,
        plan_snapshot_id=source_request.plan_snapshot_id,
    )
    source_session_id = uuid4()
    target_execution_id = uuid4()
    source_package_id = uuid4()
    source_package = ContextPackageManifestV1.create(
        package_id=source_package_id,
        workspace_id=WORKSPACE_ID,
        purpose=ContextPurpose.EXECUTION,
        consumer_type=ContextConsumerType.WORKER,
        consumer_id=source_execution.id,
        worker_item_id=worker_item.id,
        work_context_revision_id=context_revision.revision_id,
        context_version=1,
        control_plane_fact_snapshot={"source": "unit"},
        knowledge_refs=(),
        artifact_refs=(),
        evidence_refs=(),
        budget=ContextBudgetV1(
            profile_id=uuid4(),
            profile_version=1,
            max_sections=8,
            max_chars=20_000,
            max_input_tokens=4_000,
        ),
        expires_at=NOW + timedelta(hours=1),
        idempotency_key="source-package",
        created_at=NOW,
    )
    target_runtime = {
        "worker_id": str(target_worker.id),
        "adapter_type": "fake",
        "adapter_config_version": 1,
        "workspace_config_version": 1,
    }
    proposal = HandoffProposal.create(
        id=uuid4(),
        payload=HandoffProposalPayload(
            proposal_schema_version=1,
            workspace_id=WORKSPACE_ID,
            root_task_id=task_id,
            subtask_id=task_id,
            worker_item_id=worker_item.id,
            source_execution_id=source_execution.id,
            source_execution_version=source_execution.version,
            source_session_id=source_session_id,
            source_session_version=1,
            source_session_last_sequence=3,
            source_worker_id=source_worker.id,
            source_worker_version=source_worker.version,
            source_runtime_snapshot={"worker_id": str(source_worker.id)},
            source_workspace_manifest_id=source_manifest.id,
            source_workspace_manifest_version=source_manifest.version,
            source_context_package_id=source_package.package_id,
            source_context_package_hash=source_package.package_hash,
            source_worker_context_version=1,
            observed_plan_snapshot_id=source_request.plan_snapshot_id,
            observed_plan_generation=3,
            observed_plan_head_version=1,
            target_worker_id=target_worker.id,
            target_worker_version=target_worker.version,
            target_runtime_snapshot=target_runtime,
            target_capability_snapshot_hash="b" * 64,
            target_policy_reason={"policy": "test"},
            target_policy_facts={"eligible": True},
            transfer_mode=HandoffTransferMode.REUSE_EXACT,
            handoff_reason="switch target",
            handoff_instruction="continue from checkpoint",
            input_plan={"safe": True},
            impact_summary={"scope": "worker_item"},
        ),
        created_by_type="user",
        created_by_id="unit",
        source_channel=HandoffSourceChannel.WEB,
        created_at=NOW,
        expires_at=NOW + timedelta(hours=1),
    )
    proposal.mark_confirmed(now=NOW)
    transition = HandoffTransition.create(
        id=uuid4(),
        proposal=proposal,
        confirmed_by_type="user",
        confirmed_by_id="unit",
        confirmed_at=NOW,
    )
    transition.start(now=NOW)
    checkpoint = HandoffCheckpointEvidence(
        operation_id=uuid4(),
        transition_id=transition.id,
        source_session_id=source_session_id,
        accepted_sequence=3,
        workspace_digest=workspace_manifest_digest(source_manifest),
        context_package_id=proposal.payload.source_context_package_id,
        context_package_hash=proposal.payload.source_context_package_hash,
        context_version=1,
        progress_summary_ref="progress:source",
        artifact_refs=("artifact:diff",),
        evidence_refs=("evidence:checkpoint",),
        quiescent=True,
        produced_at=NOW,
    )
    transition.record_checkpoint(sequence=3, facts=checkpoint.to_mapping())
    transition.advance(HandoffPhase.STOP_SOURCE, now=NOW)
    stop = HandoffStopEvidence(
        operation_id=uuid4(),
        source_session_id=source_session_id,
        accepted_sequence=3,
        stopped=True,
        produced_at=NOW,
    )
    transition.record_stop(sequence=3, facts=stop.to_mapping())
    transition.advance(HandoffPhase.FREEZE_SOURCE_WORKSPACE, now=NOW)
    transition.record_workspace_freeze(
        operation_id=uuid4(),
        facts={
            "manifest_id": str(source_manifest.id),
            "workspace_digest": workspace_manifest_digest(source_manifest),
            "lifecycle": "frozen",
            "fencing_token": source_manifest.fencing_token,
        },
    )
    transition.advance(HandoffPhase.CREATE_TARGET_EXECUTION, now=NOW)
    transition.target_execution_id = target_execution_id
    transition.advance(HandoffPhase.ASSEMBLE_BUNDLE, now=NOW)
    target_execution = Execution.create(
        execution_id=target_execution_id,
        workspace_id=WORKSPACE_ID,
        task_id=task_id,
        worker_item_id=worker_item.id,
        lineage_id=target_execution_id,
        attempt=1,
        max_attempts=3,
        purpose="handoff",
        capability_requirements=frozenset({"python"}),
        pinned_worker_id=target_worker.id,
        not_before=NOW,
        source_execution_id=source_execution.id,
        source_session_id=source_session_id,
        handoff_transition_id=transition.id,
        adapter_preferences=("fake",),
        initial_fencing_token=source_manifest.fencing_token,
    )
    target_request = source_request.build_handoff_target(
        request_id=uuid4(),
        execution_lineage_id=target_execution_id,
        first_execution_id=target_execution_id,
        source_execution_id=source_execution.id,
        source_session_id=source_session_id,
        handoff_transition_id=transition.id,
        now=NOW,
    )
    delivered_input_id = uuid4()
    transferable_input_id = uuid4()
    excluded_input_id = uuid4()
    classification = HandoffInputClassification(
        source_session_id=source_session_id,
        cutoff_sequence=3,
        dispositions=(
            HandoffInputDisposition(
                source_input_id=delivered_input_id,
                source_status="delivered",
                action=HandoffInputAction.PRESERVE_DELIVERED,
                reason_code="already_accepted",
                accepted_sequence=1,
                operation_id=None,
            ),
            HandoffInputDisposition(
                source_input_id=transferable_input_id,
                source_status="queued",
                action=HandoffInputAction.TRANSFER_QUEUED,
                reason_code="proven_queued",
                accepted_sequence=None,
                operation_id=uuid4(),
            ),
            HandoffInputDisposition(
                source_input_id=excluded_input_id,
                source_status="cancelled",
                action=HandoffInputAction.EXCLUDE,
                reason_code="cancelled",
                accepted_sequence=None,
                operation_id=None,
            ),
        ),
        transferable_input_ids=(transferable_input_id,),
        excluded_input_ids=(excluded_input_id,),
        unknown_input_ids=(),
    )
    fixture = SimpleNamespace(
        proposal=proposal,
        transition=transition,
        source_request=source_request,
        target_request=target_request,
        source_execution=source_execution,
        target_execution=target_execution,
        source_manifest=source_manifest,
        source_package=source_package,
        worker_item=worker_item,
        context_revision=context_revision,
        source_facts=HandoffSourceFacts(
            checkpoint=checkpoint,
            stop=stop,
            source_context_package_ref=(
                f"context-package://{source_package.package_id}/{source_package.package_hash}"
            ),
            source_worker_context_version=1,
            workspace_state={
                "manifest_id": str(source_manifest.id),
                "workspace_digest": workspace_manifest_digest(source_manifest),
                "lifecycle": "frozen",
            },
            artifact_refs=checkpoint.artifact_refs,
            evidence_refs=checkpoint.evidence_refs,
            input_classification=classification,
        ),
    )
    fixture.uow = MemoryUnitOfWork(fixture)
    return fixture


def test_collect_source_facts_requires_exact_stop_and_rejects_unknown_input() -> None:
    fixture = _fixture()
    service = HandoffContextService(lambda: fixture.uow)

    facts = service.collect_source_facts(
        transition=fixture.transition,
        workspace_id=WORKSPACE_ID,
        input_classification=fixture.source_facts.input_classification,
    )

    assert facts.checkpoint.accepted_sequence == 3
    assert facts.stop.accepted_sequence == 3
    assert facts.source_context_package_ref.endswith(fixture.source_package.package_hash)
    assert facts.workspace_state["lifecycle"] == "frozen"

    unknown_input_id = uuid4()
    blocked = replace(
        fixture.source_facts.input_classification,
        dispositions=(
            *fixture.source_facts.input_classification.dispositions,
            HandoffInputDisposition(
                source_input_id=unknown_input_id,
                source_status="unknown",
                action=HandoffInputAction.BLOCK,
                reason_code="not_observed",
                accepted_sequence=None,
                operation_id=None,
            ),
        ),
        unknown_input_ids=(unknown_input_id,),
    )
    with pytest.raises(HandoffContextError, match="unknown"):
        service.collect_source_facts(
            transition=fixture.transition,
            workspace_id=WORKSPACE_ID,
            input_classification=blocked,
        )


def test_create_bundle_and_request_is_idempotent_and_safe() -> None:
    fixture = _fixture()
    service = HandoffContextService(lambda: fixture.uow)

    bundle, request = service.create_bundle_and_request(
        transition=fixture.transition,
        proposal=fixture.proposal,
        target_request=fixture.target_request,
        target_execution=fixture.target_execution,
        source_facts=fixture.source_facts,
        now=NOW,
    )
    repeated_bundle, repeated_request = service.create_bundle_and_request(
        transition=fixture.transition,
        proposal=fixture.proposal,
        target_request=fixture.target_request,
        target_execution=fixture.target_execution,
        source_facts=fixture.source_facts,
        now=NOW,
    )

    assert repeated_bundle == bundle
    assert repeated_request == request
    assert fixture.transition.handoff_context_bundle_id == bundle.id
    assert bundle.target_execution_id == fixture.target_execution.id
    assert bundle.transferable_input_refs
    assert bundle.delivered_input_refs
    assert bundle.excluded_input_refs
    assert bundle.workspace_location_ref_hash
    assert bundle.repository_state
    assert "location_ref" not in bundle.to_mapping()
    assert "trace" not in str(bundle.to_mapping()).lower()
    assert request.consumer_type is ContextConsumerType.WORKER
    assert request.consumer_id == fixture.target_execution.id
    assert request.worker_item_id == fixture.worker_item.id
    assert request.work_context_revision_id == fixture.context_revision.revision_id
    assert request.idempotency_key.startswith("context:handoff:")
    assert request.budget["handoff_context_bundle_id"] == str(bundle.id)
    assert request.budget["handoff_context_bundle_hash"] == bundle.bundle_hash
    assert len(fixture.uow.handoff_context_bundles.added) == 1


def test_wait_until_package_ready_is_bounded_and_persists_exact_reference() -> None:
    fixture = _fixture()
    service = HandoffContextService(lambda: fixture.uow)
    bundle, request = service.create_bundle_and_request(
        transition=fixture.transition,
        proposal=fixture.proposal,
        target_request=fixture.target_request,
        target_execution=fixture.target_execution,
        source_facts=fixture.source_facts,
        now=NOW,
    )

    assert service.wait_until_package_ready(
        transition_id=fixture.transition.id,
        workspace_id=WORKSPACE_ID,
        now=NOW,
    ) is None

    ready_request = replace(request, status=ContextAssemblyStatus.READY)
    fixture.uow.context_assemblies.values[request.request_id] = ready_request
    package = ContextPackageManifestV1.create(
        package_id=uuid4(),
        workspace_id=WORKSPACE_ID,
        purpose=ContextPurpose.EXECUTION,
        consumer_type=ContextConsumerType.WORKER,
        consumer_id=fixture.target_execution.id,
        worker_item_id=fixture.worker_item.id,
        work_context_revision_id=fixture.context_revision.revision_id,
        context_version=fixture.transition.target_context_version,
        control_plane_fact_snapshot={
            "handoff": {
                "bundle_id": str(bundle.id),
                "bundle_hash": bundle.bundle_hash,
                "transition_id": str(fixture.transition.id),
                "logical_attempt": bundle.logical_attempt,
            }
        },
        knowledge_refs=(),
        artifact_refs=(),
        evidence_refs=(),
        budget=ContextBudgetV1(
            profile_id=uuid4(),
            profile_version=1,
            max_sections=8,
            max_chars=20_000,
            max_input_tokens=4_000,
        ),
        expires_at=NOW + timedelta(hours=1),
        idempotency_key=request.idempotency_key,
        created_at=NOW,
    )
    fixture.uow.context_packages.add(package)

    result = service.wait_until_package_ready(
        transition_id=fixture.transition.id,
        workspace_id=WORKSPACE_ID,
        now=NOW,
    )

    assert result == package
    assert fixture.transition.target_context_package_id == package.package_id
    assert fixture.transition.target_context_package_hash == package.package_hash

    fixture.transition.target_context_package_id = None
    fixture.transition.target_context_package_hash = None
    fixture.uow.context_packages.values[package.package_id] = replace(
        package,
        control_plane_fact_snapshot={"handoff": {"bundle_id": str(bundle.id)}},
    )
    with pytest.raises(HandoffContextError) as error:
        service.wait_until_package_ready(
            transition_id=fixture.transition.id,
            workspace_id=WORKSPACE_ID,
            now=NOW,
        )
    assert error.value.code == "handoff_context_package_stale"


def test_wait_until_package_ready_raises_stable_error_for_terminal_assembly() -> None:
    fixture = _fixture()
    service = HandoffContextService(lambda: fixture.uow)
    _bundle, request = service.create_bundle_and_request(
        transition=fixture.transition,
        proposal=fixture.proposal,
        target_request=fixture.target_request,
        target_execution=fixture.target_execution,
        source_facts=fixture.source_facts,
        now=NOW,
    )
    fixture.uow.context_assemblies.values[request.request_id] = replace(
        request,
        status=ContextAssemblyStatus.BLOCKED,
        error_code="context_consumer_stale",
    )

    with pytest.raises(HandoffContextError) as error:
        service.wait_until_package_ready(
            transition_id=fixture.transition.id,
            workspace_id=WORKSPACE_ID,
            now=NOW,
        )
    assert error.value.code == "handoff_context_package_blocked"
