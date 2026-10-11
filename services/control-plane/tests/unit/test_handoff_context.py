from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from types import SimpleNamespace
from typing import Any, Self
from uuid import UUID, uuid4

import pytest

from sidera_control_plane.application.context_requests import (
    build_execution_context_assembly_request,
)
from sidera_control_plane.application.execution_code_inputs import (
    execution_code_input_manifest,
)
from sidera_control_plane.application.handoff_context import (
    HandoffContextError,
    HandoffContextService,
)
from sidera_control_plane.domain.code_results import CodeInputManifestV1
from sidera_control_plane.domain.collaboration_runtime import canonical_json_bytes
from sidera_control_plane.domain.context_packages import (
    ContextAssemblyRequest,
    ContextAssemblyStatus,
    ContextBudgetV1,
    ContextConsumerType,
    ContextPackageManifestV1,
    ContextPackageManifestV2,
    ContextPackageManifestV3,
    ContextPurpose,
    ResultInputManifestV1,
    VerificationManifestV1,
    context_package_reference,
)
from sidera_control_plane.domain.errors import ValidationError
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
            manifest_schema_version=request.manifest_schema_version,
        )
        if existing is None:
            existing = next(
                (
                    value
                    for value in self.values.values()
                    if value.workspace_id == request.workspace_id
                    and value.idempotency_key == request.idempotency_key
                ),
                None,
            )
        if existing is not None:
            if (
                existing.request_fingerprint != request.request_fingerprint
                or existing.idempotency_key != request.idempotency_key
            ):
                raise ValidationError(
                    "context assembly fingerprint or idempotency key conflicts"
                )
            return existing
        self.values[request.request_id] = request
        return request

    def get_for_consumer(
        self,
        purpose: ContextPurpose,
        consumer_id: UUID,
        workspace_id: UUID,
        *,
        manifest_schema_version: int = 1,
        **_kwargs: object,
    ) -> ContextAssemblyRequest | None:
        return next(
            (
                value
                for value in self.values.values()
                if value.purpose is purpose
                and value.consumer_id == consumer_id
                and value.workspace_id == workspace_id
                and value.manifest_schema_version == manifest_schema_version
            ),
            None,
        )


class PackageRepository(MemoryRepository):
    def get_for_consumer(
        self,
        purpose: ContextPurpose,
        consumer_id: UUID,
        workspace_id: UUID,
        *,
        manifest_schema_version: int = 1,
        **kwargs: object,
    ) -> ContextPackageManifestV1 | None:
        return next(
            (
                value
                for value in self.values.values()
                if value.purpose is purpose
                and value.consumer_id == consumer_id
                and value.workspace_id == workspace_id
                and value.manifest_schema_version == manifest_schema_version
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


def _worker(
    worker_id: UUID | None = None, *, workspace_id: UUID = WORKSPACE_ID
) -> Worker:
    return Worker.register(
        worker_id=worker_id or uuid4(),
        workspace_id=workspace_id,
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
        workspace_id=execution.workspace_id,
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


def _fixture(
    *, selected_branch: str | None = None, hard_only: bool = False
) -> SimpleNamespace:
    from .test_context_requests import derived_code_case

    selected = (
        derived_code_case(kind="handoff", branch=selected_branch, hard_only=hard_only)
        if selected_branch is not None
        else None
    )
    code_case = selected[0] if selected is not None else None
    workspace_id = code_case.request.workspace_id if code_case else WORKSPACE_ID
    task_id = code_case.request.subtask_id if code_case else uuid4()
    worker_item = WorkerItem.create(
        item_id=code_case.request.worker_item_id if code_case else uuid4(),
        workspace_id=workspace_id,
        root_task_id=code_case.request.root_task_id if code_case else task_id,
        title="Handoff item",
        responsibility="Continue the task",
        now=NOW,
    )
    source_worker = _worker(workspace_id=workspace_id)
    target_worker = _worker(workspace_id=workspace_id)
    source_execution = Execution.create(
        execution_id=uuid4(),
        workspace_id=workspace_id,
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
    if code_case is not None:
        source_execution = replace(
            code_case.execution, pinned_worker_id=source_worker.id
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
        workspace_id=workspace_id,
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
    if code_case is not None:
        source_request = code_case.request
    context_revision = replace(
        _context_revision(),
        workspace_id=workspace_id,
        proposal_revision_id=source_request.proposal_revision_id,
        plan_snapshot_id=source_request.plan_snapshot_id,
    )
    source_session_id = selected[2].id if selected is not None else uuid4()
    target_execution_id = uuid4()
    source_package_id = uuid4()
    source_package = ContextPackageManifestV1.create(
        package_id=source_package_id,
        workspace_id=workspace_id,
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
            workspace_id=workspace_id,
            root_task_id=worker_item.root_task_id,
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
            observed_plan_generation=source_request.plan_generation,
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
        workspace_id=workspace_id,
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
    if selected is not None:
        fixture.code_case = code_case
        selected_uow = selected[3]
        for name in ("plans", "results", "input_selections", "collaboration_ledger"):
            setattr(fixture.uow, name, getattr(selected_uow, name))
        for value in code_case.source_requests:
            fixture.uow.execution_requests.add(value)
        for value in code_case.source_executions:
            fixture.uow.executions.add(value)
        source_session = replace(selected[2], execution_id=source_execution.id)
        fixture.uow.sessions = SimpleNamespace(
            get_any=lambda session_id, scope_id: (
                source_session
                if (session_id, scope_id) == (source_session.id, workspace_id)
                else None
            )
        )
        fixture.source_assembly = build_execution_context_assembly_request(
            execution=source_execution,
            execution_request=source_request,
            worker_item=worker_item,
            work_context_revision=context_revision,
            ttl_seconds=3600,
            created_at=NOW,
            manifest_schema_version=2,
            input_selection=code_case.selection,
        )
        fixture.uow.context_assemblies.add(fixture.source_assembly)
        _set_source_package(
            fixture, _package_for_assembly(fixture, fixture.source_assembly)
        )
    return fixture


def _package_for_assembly(fixture, assembly, *, bundle=None):
    facts = (
        {"source": "unit"}
        if bundle is None
        else {
            "handoff": {
                "bundle_id": str(bundle.id),
                "bundle_hash": bundle.bundle_hash,
                "transition_id": str(fixture.transition.id),
                "logical_attempt": bundle.logical_attempt,
            }
        }
    )
    arguments = {
        "package_id": uuid4(),
        "workspace_id": assembly.workspace_id,
        "purpose": assembly.purpose,
        "consumer_type": assembly.consumer_type,
        "consumer_id": assembly.consumer_id,
        "worker_item_id": assembly.worker_item_id,
        "work_context_revision_id": assembly.work_context_revision_id,
        "context_version": assembly.context_version,
        "control_plane_fact_snapshot": facts,
        "knowledge_refs": (),
        "artifact_refs": (),
        "evidence_refs": (),
        "budget": fixture.source_package.budget,
        "expires_at": assembly.expires_at,
        "idempotency_key": assembly.idempotency_key,
        "created_at": assembly.created_at,
    }
    if assembly.manifest_schema_version == 1:
        return ContextPackageManifestV1.create(**arguments)
    result_manifest = ResultInputManifestV1.from_selection(fixture.code_case.selection)
    if assembly.manifest_schema_version == 2:
        return ContextPackageManifestV2.create(
            **arguments, result_input_manifest=result_manifest
        )
    return ContextPackageManifestV3.create(
        **arguments,
        result_input_manifest=result_manifest,
        code_input_manifest=assembly.code_input_manifest,
    )


def _set_source_package(fixture, package):
    fixture.uow.context_packages.values.pop(fixture.source_package.package_id)
    fixture.source_package = package
    fixture.uow.context_packages.add(package)
    checkpoint = replace(
        fixture.source_facts.checkpoint,
        context_package_id=package.package_id,
        context_package_hash=package.package_hash,
    )
    fixture.source_facts = replace(
        fixture.source_facts,
        checkpoint=checkpoint,
        source_context_package_ref=context_package_reference(
            package.package_id, package.package_hash
        ),
    )
    fixture.transition.source_checkpoint_facts = checkpoint.to_mapping()
    payload = replace(
        fixture.proposal.payload,
        source_context_package_id=package.package_id,
        source_context_package_hash=package.package_hash,
    )
    fixture.proposal = replace(
        fixture.proposal, payload=payload, proposal_hash=payload.content_hash
    )


def test_selected_code_handoff_creation_uses_fixed_authority_and_exact_v3_key():
    fixture = _fixture(selected_branch="code", hard_only=True)
    service = HandoffContextService(lambda: fixture.uow)
    arguments = {
        "transition": fixture.transition,
        "proposal": fixture.proposal,
        "target_request": fixture.target_request,
        "target_execution": fixture.target_execution,
        "source_facts": fixture.source_facts,
    }
    bundle, assembly = service.create_bundle_and_request(**arguments, now=NOW)
    replay_bundle, replay = service.create_bundle_and_request(
        **arguments, now=NOW + timedelta(seconds=1)
    )
    assert replay_bundle == bundle
    assert replay.request_fingerprint == assembly.request_fingerprint
    assert replay.idempotency_key == assembly.idempotency_key
    assert assembly.manifest_schema_version == 3
    assert assembly.code_input_manifest is not None
    manifest = assembly.code_input_manifest
    assert assembly.idempotency_key == (
        f"context:handoff:{fixture.transition.id}:"
        f"attempt:{fixture.transition.logical_attempt}:"
        f"execution:{fixture.target_execution.id}:v3:input:{manifest.input_hash}"
    )
    assert manifest.request_id == fixture.source_request.id
    assert manifest.execution_id == fixture.target_execution.id
    assert manifest.selection_id == fixture.code_case.selection.id
    assert manifest.selection_hash == fixture.code_case.selection.selection_hash
    assert fixture.target_request.input_selection_id is None
    assert bundle.logical_attempt == fixture.transition.logical_attempt
    assert assembly.created_at == replay.created_at == bundle.created_at == NOW
    assert assembly.budget["handoff_context_bundle_hash"] == bundle.bundle_hash


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
    assert request.idempotency_key == (
        f"context:handoff:{fixture.transition.id}:"
        f"attempt:{fixture.transition.logical_attempt}:"
        f"execution:{fixture.target_execution.id}:v1"
    )
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


def _use_source_v3(fixture):
    manifest = execution_code_input_manifest(
        fixture.uow,
        fixture.source_request,
        fixture.source_execution,
        fixture.code_case.selection,
    )
    assert manifest is not None
    assembly = build_execution_context_assembly_request(
        execution=fixture.source_execution,
        execution_request=fixture.source_request,
        worker_item=fixture.worker_item,
        work_context_revision=fixture.context_revision,
        ttl_seconds=3600,
        created_at=NOW,
        request_id=fixture.source_assembly.request_id,
        manifest_schema_version=3,
        input_selection=fixture.code_case.selection,
        code_input_manifest=manifest,
    )
    fixture.source_assembly = assembly
    fixture.uow.context_assemblies.values[assembly.request_id] = assembly
    _set_source_package(fixture, _package_for_assembly(fixture, assembly))


def _create_handoff(fixture):
    return HandoffContextService(lambda: fixture.uow).create_bundle_and_request(
        transition=fixture.transition,
        proposal=fixture.proposal,
        target_request=fixture.target_request,
        target_execution=fixture.target_execution,
        source_facts=fixture.source_facts,
        now=NOW,
    )


def _ready_handoff(*, branch="code", hard_only=False, source_v3=False):
    fixture = _fixture(selected_branch=branch, hard_only=hard_only)
    if source_v3:
        _use_source_v3(fixture)
    bundle, assembly = _create_handoff(fixture)
    assembly = replace(assembly, status=ContextAssemblyStatus.READY)
    fixture.uow.context_assemblies.values[assembly.request_id] = assembly
    package = _package_for_assembly(fixture, assembly, bundle=bundle)
    fixture.uow.context_packages.add(package)
    return fixture, assembly, package


def _wait_for_target(fixture):
    return HandoffContextService(lambda: fixture.uow).wait_until_package_ready(
        transition_id=fixture.transition.id,
        workspace_id=fixture.transition.workspace_id,
        now=NOW,
    )


def _changed_code_manifest(manifest, **changes):
    document = manifest.to_mapping()
    document.pop("input_hash")
    document.update(
        {
            key: str(value) if isinstance(value, UUID) else value
            for key, value in changes.items()
        }
    )
    return CodeInputManifestV1.from_mapping(
        document | {"input_hash": sha256(canonical_json_bytes(document)).hexdigest()}
    )


@pytest.mark.parametrize("hard_only", (False, True))
@pytest.mark.parametrize("source_v3", (False, True))
def test_handoff_reads_original_source_and_target_v3_code_contract(
    hard_only, source_v3
):
    fixture, assembly, package = _ready_handoff(
        hard_only=hard_only, source_v3=source_v3
    )
    if source_v3:
        assert isinstance(fixture.source_package, ContextPackageManifestV3)
        assert (
            fixture.source_package.code_input_manifest.execution_id
            == fixture.source_execution.id
        )
        assert (
            fixture.source_package.code_input_manifest.request_id
            == fixture.source_request.id
        )
    else:
        assert isinstance(fixture.source_package, ContextPackageManifestV2)
    assert assembly.code_input_manifest.execution_id == fixture.target_execution.id
    assert assembly.code_input_manifest.request_id == fixture.source_request.id
    assert _wait_for_target(fixture) == package
    assert _wait_for_target(fixture) == package
    assert fixture.transition.target_context_package_hash == package.package_hash
    assert fixture.transition.target_context_package_id == package.package_id


@pytest.mark.parametrize("branch", ("text", "legacy_diff"))
def test_selected_handoff_retains_exact_v2_key_and_source_target_packages(branch):
    fixture, assembly, package = _ready_handoff(branch=branch)
    assert assembly.manifest_schema_version == package.manifest_schema_version == 2
    assert assembly.idempotency_key == (
        f"context:handoff:{fixture.transition.id}:"
        f"attempt:{fixture.transition.logical_attempt}:"
        f"execution:{fixture.target_execution.id}:"
        f"v2:selection:{fixture.code_case.selection.selection_hash}"
    )
    assert _wait_for_target(fixture) == package
    assert fixture.source_package.manifest_schema_version == 2
    _, replay = _create_handoff(fixture)
    assert replay.request_fingerprint == assembly.request_fingerprint
    assert replay.idempotency_key == assembly.idempotency_key


@pytest.mark.parametrize("target_schema", (2, 3))
def test_handoff_target_rejects_opposite_package_schema(target_schema):
    fixture, assembly, package = _ready_handoff(
        branch="text" if target_schema == 2 else "code"
    )
    if target_schema == 3:
        replacement = ContextPackageManifestV2.create(
            **{
                key: getattr(package, key)
                for key in (
                    "package_id",
                    "workspace_id",
                    "purpose",
                    "consumer_type",
                    "consumer_id",
                    "worker_item_id",
                    "work_context_revision_id",
                    "context_version",
                    "control_plane_fact_snapshot",
                    "knowledge_refs",
                    "artifact_refs",
                    "evidence_refs",
                    "budget",
                    "expires_at",
                    "idempotency_key",
                    "created_at",
                    "result_input_manifest",
                )
            }
        )
    else:
        manifest = execution_code_input_manifest(
            fixture.uow,
            fixture.source_request,
            fixture.target_execution,
            fixture.code_case.selection,
        )
        assert manifest is None
        code_fixture = _fixture(selected_branch="code")
        manifest = execution_code_input_manifest(
            code_fixture.uow,
            code_fixture.source_request,
            code_fixture.target_execution,
            code_fixture.code_case.selection,
        )
        replacement = ContextPackageManifestV3.create(
            **{
                key: getattr(package, key)
                for key in (
                    "package_id",
                    "workspace_id",
                    "purpose",
                    "consumer_type",
                    "consumer_id",
                    "worker_item_id",
                    "work_context_revision_id",
                    "context_version",
                    "control_plane_fact_snapshot",
                    "knowledge_refs",
                    "artifact_refs",
                    "evidence_refs",
                    "budget",
                    "expires_at",
                    "idempotency_key",
                    "created_at",
                )
            },
            code_input_manifest=manifest,
        )
    fixture.uow.context_packages.values[package.package_id] = replacement
    with pytest.raises(HandoffContextError) as error:
        _wait_for_target(fixture)
    assert error.value.code == "handoff_context_package_missing"
    assert assembly.manifest_schema_version == target_schema
    assert fixture.transition.target_context_package_id is None


@pytest.mark.parametrize("side", ("source", "target"))
@pytest.mark.parametrize("matching_assembly", (False, True))
@pytest.mark.parametrize(
    "fact",
    (
        "workspace_id",
        "root_task_id",
        "request_id",
        "execution_id",
        "input_hash",
        "selection",
        "selection_hash",
    ),
)
def test_handoff_code_reader_rejects_changed_exact_manifest_identity(
    side, matching_assembly, fact
):
    fixture = _fixture(selected_branch="code", hard_only=True)
    _use_source_v3(fixture)
    if side == "source":
        package = fixture.source_package
        assembly = fixture.source_assembly
    else:
        fixture, assembly, package = _ready_handoff(hard_only=True)
    manifest = package.code_input_manifest
    if fact == "input_hash":
        changed = _changed_code_manifest(manifest, base_commit="e" * 40)
    elif fact == "selection":
        changed = _changed_code_manifest(manifest, selection_id=uuid4())
    elif fact == "selection_hash":
        changed = _changed_code_manifest(manifest, selection_hash="f" * 64)
    else:
        changed = _changed_code_manifest(manifest, **{fact: uuid4()})
    result_manifest = package.result_input_manifest
    if fact in {"selection", "selection_hash"}:
        result_manifest = replace(
            result_manifest,
            input_selection_id=changed.selection_id,
            input_selection_hash=changed.selection_hash,
        )
    replacement = replace(
        package, code_input_manifest=changed, result_input_manifest=result_manifest
    )
    if matching_assembly:
        fixture.uow.context_assemblies.values[assembly.request_id] = replace(
            assembly,
            code_input_manifest=changed,
            input_selection_id=changed.selection_id,
            input_selection_hash=changed.selection_hash,
            budget=dict(assembly.budget)
            | {
                "code_input_hash": changed.input_hash,
                "execution_request_id": str(changed.request_id),
                "execution_id": str(changed.execution_id),
            },
        )
    if side == "source":
        _set_source_package(fixture, replacement)
        operation = lambda: _create_handoff(fixture)
        expected_code = "handoff_context_source_input_selection_stale"
    else:
        fixture.uow.context_packages.values[package.package_id] = replacement
        operation = lambda: _wait_for_target(fixture)
        expected_code = "handoff_context_input_selection_stale"
    with pytest.raises(HandoffContextError) as error:
        operation()
    assert error.value.code == expected_code
    assert fixture.transition.target_context_package_id is None


@pytest.mark.parametrize("side", ("source", "target"))
@pytest.mark.parametrize("code_value", (None, "untyped"))
def test_handoff_code_reader_rejects_missing_or_untyped_persisted_manifest(
    side, code_value
):
    fixture = _fixture(selected_branch="code")
    _use_source_v3(fixture)
    if side == "source":
        package = fixture.source_package
    else:
        fixture, _assembly, package = _ready_handoff()
    # Simulate a malformed repository row that bypassed typed deserialization.
    object.__setattr__(package, "code_input_manifest", code_value)
    if side == "source":
        operation = lambda: _create_handoff(fixture)
        expected_code = "handoff_context_source_input_selection_stale"
    else:
        operation = lambda: _wait_for_target(fixture)
        expected_code = "handoff_context_input_selection_stale"
    with pytest.raises(HandoffContextError) as error:
        operation()
    assert error.value.code == expected_code
    assert fixture.transition.target_context_package_id is None


@pytest.mark.parametrize("fact", ("bundle_hash", "logical_attempt"))
def test_handoff_v3_target_keeps_bundle_and_attempt_guards(fact):
    fixture, _assembly, package = _ready_handoff()
    facts = dict(package.control_plane_fact_snapshot["handoff"])
    facts[fact] = "f" * 64 if fact == "bundle_hash" else 99
    fixture.uow.context_packages.values[package.package_id] = replace(
        package, control_plane_fact_snapshot={"handoff": facts}
    )
    with pytest.raises(HandoffContextError) as error:
        _wait_for_target(fixture)
    assert error.value.code == "handoff_context_package_stale"
    assert fixture.transition.target_context_package_id is None


def test_handoff_writer_translates_bounded_missing_code_validation():
    fixture = _fixture(selected_branch="code")
    fixture.uow.results.retained_code_results = lambda _workspace_id: ()
    with pytest.raises(HandoffContextError) as error:
        _create_handoff(fixture)
    assert error.value.code == "handoff_context_input_selection_invalid"
    assert isinstance(error.value.__cause__, ValidationError)
    assert len(fixture.uow.context_assemblies.values) == 1


@pytest.mark.parametrize("side", ("source", "target"))
def test_handoff_v3_reader_reconciles_retained_snapshot_identity(side):
    fixture = _fixture(selected_branch="code")
    _use_source_v3(fixture)
    if side == "target":
        fixture, _assembly, _package = _ready_handoff()
    fixture.uow.results.get_record = lambda *_args: None
    with pytest.raises(HandoffContextError) as error:
        _create_handoff(fixture) if side == "source" else _wait_for_target(fixture)
    assert error.value.code == (
        "handoff_context_source_input_selection_invalid"
        if side == "source"
        else "handoff_context_input_selection_invalid"
    )
    assert fixture.transition.target_context_package_id is None


def test_handoff_v3_target_rejects_changed_source_session_authority():
    fixture, _assembly, _package = _ready_handoff()
    fixture.uow.executions.values[fixture.target_execution.id] = replace(
        fixture.target_execution, source_session_id=uuid4()
    )
    with pytest.raises(HandoffContextError) as error:
        _wait_for_target(fixture)
    assert error.value.code == "handoff_context_input_selection_invalid"
    assert fixture.transition.target_context_package_id is None


@pytest.mark.parametrize("side", ("source", "target"))
def test_handoff_v3_reader_translates_bounded_repository_validation(side):
    fixture = _fixture(selected_branch="code")
    _use_source_v3(fixture)
    if side == "target":
        fixture, _assembly, _package = _ready_handoff()

    def invalid_record(*_args):
        raise ValidationError("retained code snapshot is malformed")

    fixture.uow.results.get_record = invalid_record
    with pytest.raises(HandoffContextError) as error:
        _create_handoff(fixture) if side == "source" else _wait_for_target(fixture)
    assert error.value.code == (
        "handoff_context_source_input_selection_invalid"
        if side == "source"
        else "handoff_context_input_selection_invalid"
    )
    assert isinstance(error.value.__cause__, ValidationError)
    assert fixture.transition.target_context_package_id is None


@pytest.mark.parametrize("side", ("source", "target"))
def test_handoff_v3_reader_rejects_code_verification_mix(side):
    from .result_integration_fixtures import verification_manifest_mapping

    fixture = _fixture(selected_branch="code")
    _use_source_v3(fixture)
    if side == "source":
        package = fixture.source_package
    else:
        fixture, _assembly, package = _ready_handoff()
    object.__setattr__(
        package,
        "verification_manifest",
        VerificationManifestV1.from_mapping(verification_manifest_mapping()),
    )
    with pytest.raises(HandoffContextError) as error:
        _create_handoff(fixture) if side == "source" else _wait_for_target(fixture)
    assert error.value.code == (
        "handoff_context_source_input_selection_stale"
        if side == "source"
        else "handoff_context_input_selection_stale"
    )
    assert fixture.transition.target_context_package_id is None


@pytest.mark.parametrize("source_schema", (2, 3))
def test_handoff_source_rejects_opposite_persisted_assembly_schema(source_schema):
    fixture = _fixture(selected_branch="code")
    v2_assembly = fixture.source_assembly
    v2_package = fixture.source_package
    _use_source_v3(fixture)
    if source_schema == 2:
        _set_source_package(fixture, v2_package)
    else:
        fixture.uow.context_assemblies.values[v2_assembly.request_id] = v2_assembly
    with pytest.raises(HandoffContextError) as error:
        _create_handoff(fixture)
    assert error.value.code == "handoff_context_source_input_selection_stale"
    assert len(fixture.uow.context_assemblies.values) == 1


def test_handoff_replay_does_not_accept_changed_input_under_old_request_key():
    from .test_execution_code_inputs import changed_snapshot

    fixture = _fixture(selected_branch="code", hard_only=True)
    _bundle, original = _create_handoff(fixture)
    changed_case = replace(
        fixture.code_case,
        snapshots=(
            changed_snapshot(fixture.code_case.snapshots[0], result_commit="e" * 40),
        ),
    )
    fixture.uow.results = changed_case.uow().results
    with pytest.raises(ValidationError, match="fingerprint.*idempotency"):
        _create_handoff(fixture)
    assert fixture.uow.context_assemblies.values[original.request_id] == original
    assert fixture.transition.target_context_package_id is None


def test_assembly_repository_retains_v2_and_v3_for_the_same_consumer():
    fixture = _fixture(selected_branch="code")
    v2 = fixture.source_assembly
    _use_source_v3(fixture)
    v3 = replace(fixture.source_assembly, request_id=uuid4())
    repository = AssemblyRepository()
    assert repository.add(v2) == v2
    assert repository.add(v3) == v3
    assert len(repository.values) == 2
    for version, expected in ((2, v2), (3, v3)):
        assert (
            repository.get_for_consumer(
                ContextPurpose.EXECUTION,
                v2.consumer_id,
                v2.workspace_id,
                manifest_schema_version=version,
            )
            == expected
        )
        assert repository.add(expected) == expected
    packages = PackageRepository(
        (_package_for_assembly(fixture, v2), _package_for_assembly(fixture, v3))
    )
    for version in (2, 3):
        assert (
            packages.get_for_consumer(
                ContextPurpose.EXECUTION,
                v2.consumer_id,
                v2.workspace_id,
                manifest_schema_version=version,
            ).manifest_schema_version
            == version
        )


@pytest.mark.parametrize("same_workspace", (False, True))
def test_assembly_repository_scopes_idempotency_collisions_to_workspace(same_workspace):
    fixture = _fixture()
    _, original = _create_handoff(fixture)
    candidate = replace(
        original,
        request_id=uuid4(),
        consumer_id=uuid4(),
        workspace_id=original.workspace_id if same_workspace else uuid4(),
        request_fingerprint="f" * 64,
    )
    repository = AssemblyRepository((original,))
    if same_workspace:
        with pytest.raises(ValidationError, match="fingerprint.*idempotency"):
            repository.add(candidate)
        assert len(repository.values) == 1
    else:
        assert repository.add(candidate) == candidate
        assert len(repository.values) == 2


def test_handoff_v3_source_requires_its_original_selection_authority():
    from sidera_control_plane.domain.collaboration import CollaborationGateMode

    fixture = _fixture(selected_branch="code")
    _use_source_v3(fixture)
    fixture.uow.execution_requests.values[fixture.source_request.id] = replace(
        fixture.source_request,
        input_selection_id=None,
        input_selection_hash=None,
        collaboration_gate_mode=CollaborationGateMode.UNMANAGED,
    )
    with pytest.raises(HandoffContextError) as error:
        _create_handoff(fixture)
    assert error.value.code == "handoff_context_source_input_selection_stale"
    assert fixture.transition.target_context_package_id is None
