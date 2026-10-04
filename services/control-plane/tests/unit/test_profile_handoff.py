from dataclasses import fields, replace
from datetime import UTC, datetime
from types import SimpleNamespace
from uuid import uuid4

import pytest
from sidera_agents.contracts import ProfileRef

from sidera_control_plane.application.workspace_handoff import (
    WorkspaceHandoffError,
    WorkspaceHandoffService,
)
from sidera_control_plane.domain.errors import ValidationError
from sidera_control_plane.domain.handoff_evidence import (
    HandoffCheckpointEvidence,
    HandoffSourceFacts,
    HandoffStopEvidence,
    HandoffWorkspaceTargetSpec,
)
from tests.unit.test_handoff_evidence import bundle, checkpoint

NOW = datetime(2026, 10, 4, tzinfo=UTC)


def native_checkpoint():
    workspace_id = uuid4()
    return HandoffCheckpointEvidence(
        operation_id=uuid4(),
        transition_id=uuid4(),
        source_session_id=uuid4(),
        accepted_sequence=2,
        workspace_digest="a" * 64,
        context_package_id=uuid4(),
        context_package_hash="b" * 64,
        context_version=1,
        progress_summary_ref="execution:original",
        artifact_refs=(),
        evidence_refs=(),
        quiescent=False,
        produced_at=NOW,
        schema_version=2,
        workspace_id=workspace_id,
        source_execution_id=uuid4(),
        source_run_id=uuid4(),
        source_authority_id=uuid4(),
        source_binding_version=1,
        source_profile_ref=ProfileRef(
            workspace_id=workspace_id, profile_id=uuid4(), revision=1
        ),
        observation_kind="cp_run_snapshot",
        expected_stop_operation_id=uuid4(),
    )


def native_stop(snapshot, **changes):
    values = {
        "operation_id": snapshot.expected_stop_operation_id,
        "source_session_id": snapshot.source_session_id,
        "accepted_sequence": 3,
        "stopped": True,
        "produced_at": NOW,
        "schema_version": 2,
        "transition_id": snapshot.transition_id,
        "workspace_id": snapshot.workspace_id,
        "source_execution_id": snapshot.source_execution_id,
        "source_run_id": snapshot.source_run_id,
        "source_authority_id": snapshot.source_authority_id,
        "source_binding_version": snapshot.source_binding_version,
        "source_profile_ref": snapshot.source_profile_ref,
        "run_state": "stopped",
        "input_open": False,
        "active_response_id": None,
        "observed_sequence": 3,
    }
    values.update(changes)
    return HandoffStopEvidence(**values)


@pytest.mark.parametrize("pending", ["capacity", "admission"])
def test_native_coordinator_keeps_target_start_pending_without_phase_advance(pending):
    from sidera_control_plane.application.agent_worker_adapter import (
        NativeDeferredResult,
    )
    from sidera_control_plane.application.handoff_coordinator import HandoffCoordinator
    from sidera_control_plane.domain.handoffs import HandoffPhase
    from tests.unit.test_handoff_target import MemoryRepository

    transition = SimpleNamespace(
        id=uuid4(),
        workspace_id=uuid4(),
        target_execution_id=uuid4(),
        handoff_schema_version=2,
        version=8,
        target_worker_id=uuid4(),
        phase=HandoffPhase.START_TARGET_SESSION,
        lease_owner="coordinator",
    )
    uow = SimpleNamespace(handoff_transitions=MemoryRepository((transition,)))

    class Uow:
        def __enter__(self):
            return uow

        def __exit__(self, *args):
            return None

    class Scheduler:
        def assign_handoff_target(self, **kwargs):
            return None if pending == "capacity" else object()

    class Gateway:
        def start_handoff_target(self, **kwargs):
            assert pending == "admission"
            return NativeDeferredResult(
                transition.workspace_id,
                transition.target_execution_id,
                "starting",
                uuid4(),
            )

    coordinator = HandoffCoordinator(
        uow_factory=Uow, scheduler=Scheduler(), gateway=Gateway(), clock=lambda: NOW
    )
    result = coordinator._start_target_session(transition)
    assert result.kind.value == "wait" and result.target_phase is None
    assert result.pending_reason == (
        "target_waiting_capacity"
        if pending == "capacity"
        else "target_start_unconfirmed"
    )
    assert (
        transition.phase is HandoffPhase.START_TARGET_SESSION
        and transition.version == 8
    )


@pytest.mark.parametrize(
    "bad", [None, "unfinished", "legacy", "target", "session", "request", "profile"]
)
def test_native_target_creation_requires_exact_previous_converged_lineage(bad):
    from sidera_control_plane.application.handoff_target import (
        HandoffTargetError,
        HandoffTargetService,
    )
    from sidera_control_plane.domain.agent_bindings import AssigneeRef
    from sidera_control_plane.domain.handoffs import (
        HandoffProposal,
        HandoffTransitionStatus,
        NativeHandoffProposalPayload,
    )
    from tests.unit.test_handoff_target import MemoryRepository, _fixture

    uow, old_proposal, old_transition, request, execution, manifest, _ = _fixture()
    previous_id, ancestor_id, ancestor_session = uuid4(), uuid4(), uuid4()
    execution = replace(
        execution,
        lineage_id=execution.id,
        purpose="handoff",
        handoff_transition_id=previous_id,
        source_execution_id=ancestor_id,
        source_session_id=ancestor_session,
    )
    request = replace(
        request,
        purpose="handoff",
        execution_lineage_id=execution.id,
        handoff_transition_id=previous_id,
        source_execution_id=ancestor_id,
        source_session_id=ancestor_session,
    )
    old = old_proposal.payload
    profile = ProfileRef(workspace_id=old.workspace_id, profile_id=uuid4(), revision=1)
    target_profile = ProfileRef(
        workspace_id=old.workspace_id, profile_id=uuid4(), revision=2
    )
    values = {
        field.name: getattr(old, field.name)
        for field in fields(NativeHandoffProposalPayload)
        if hasattr(old, field.name)
    }
    values.update(
        proposal_schema_version=2,
        source_worker_item_version=1,
        source_run_id=uuid4(),
        source_authority_id=uuid4(),
        source_binding_version=1,
        source_profile_ref=profile,
        source_binding_target_type="worker_item",
        source_binding_target_id=old.worker_item_id,
        work_context_id=uuid4(),
        work_context_revision_id=uuid4(),
        work_context_hash="e" * 64,
        work_context_version=1,
        target_assignee=AssigneeRef("profile", target_profile.profile_id),
        target_profile_ref=target_profile,
        impact_summary={"worker_item_context_version": 1},
        observed_plan_snapshot_id=request.plan_snapshot_id,
    )
    payload = NativeHandoffProposalPayload(**values)
    proposal = HandoffProposal.create(
        id=old_proposal.id,
        payload=payload,
        created_by_type="user",
        created_by_id="unit",
        source_channel="web",
        created_at=old_proposal.created_at,
        expires_at=old_proposal.expires_at,
    )
    proposal.mark_confirmed(now=old_transition.confirmed_at)
    current = replace(
        old_transition,
        handoff_schema_version=2,
        native_payload=payload,
        target_worker_id=None,
        target_runtime_snapshot=payload.target_runtime_snapshot,
        target_binding_version=2,
    )
    previous = SimpleNamespace(
        id=previous_id,
        workspace_id=current.workspace_id,
        status=HandoffTransitionStatus.CONVERGED,
        handoff_schema_version=2,
        worker_item_id=current.worker_item_id,
        target_execution_id=execution.id,
        target_session_id=current.source_session_id,
        target_worker_id=payload.source_worker_id,
        target_binding_version=1,
        source_execution_id=ancestor_id,
        source_session_id=ancestor_session,
        native_payload=SimpleNamespace(
            target_profile_ref=profile,
            observed_plan_snapshot_id=request.plan_snapshot_id,
            observed_plan_generation=request.plan_generation,
        ),
    )
    session = SimpleNamespace(
        id=current.source_session_id,
        workspace_id=current.workspace_id,
        execution_id=execution.id,
        adapter_session_id=f"agent-run:{payload.source_run_id}",
        worker_id=payload.source_worker_id,
    )
    link = SimpleNamespace(
        run_id=payload.source_run_id,
        authority_id=payload.source_authority_id,
        binding_version=1,
        profile_ref=profile,
    )
    if bad == "unfinished":
        previous.status = HandoffTransitionStatus.RUNNING
    elif bad == "legacy":
        previous.handoff_schema_version = 1
    elif bad == "target":
        previous.target_execution_id = uuid4()
    elif bad == "session":
        previous.target_session_id = uuid4()
    elif bad == "request":
        request = replace(request, handoff_transition_id=uuid4())
    elif bad == "profile":
        previous.native_payload.target_profile_ref = target_profile
    uow.executions = MemoryRepository((execution,))
    uow.execution_requests = MemoryRepository((request,))
    uow.handoff_transitions.values = {current.id: current, previous.id: previous}
    uow.agent_bindings = SimpleNamespace(
        get=lambda *a, **k: SimpleNamespace(
            version=2, profile_ref=target_profile, runnable=True
        )
    )
    uow.sessions = SimpleNamespace(get_any=lambda *a, **k: session)
    uow.execution_authorities = SimpleNamespace(get_link=lambda *a, **k: link)
    service = HandoffTargetService(lambda: uow)
    call = lambda: service.create_target(
        transition=current,
        proposal=proposal,
        source_request=request,
        source_execution=execution,
        source_manifest=manifest,
        now=old_transition.confirmed_at,
    )
    if bad is not None:
        with pytest.raises(HandoffTargetError):
            call()
    else:
        target = call()
        assert (
            uow.executions.values[target.execution_id].source_execution_id
            == execution.id
        )
        assert (
            uow.execution_requests.values[target.execution_request_id].source_session_id
            == current.source_session_id
        )


def test_native_source_facts_require_closed_stop_after_nonquiescent_snapshot():
    snapshot = native_checkpoint()
    stop = native_stop(snapshot)
    facts = HandoffSourceFacts(
        checkpoint=snapshot,
        stop=stop,
        source_context_package_ref="context-package:original",
        source_worker_context_version=1,
        workspace_state={"manifest_id": str(uuid4())},
        artifact_refs=(),
        evidence_refs=(),
        input_classification=None,
    )
    assert facts.checkpoint.quiescent is False
    assert facts.stop.accepted_sequence == 3
    assert facts.checkpoint.accepted_sequence == 2
    assert snapshot.to_mapping()["observation_kind"] == "cp_run_snapshot"


@pytest.mark.parametrize("bad", [None, "missing", "run", "active", "sequence"])
def test_native_freeze_accepts_only_original_closed_proof(bad):
    snapshot = native_checkpoint()
    stop = native_stop(snapshot)
    source = SimpleNamespace(fencing_token=1)
    payload = SimpleNamespace(
        source_run_id=snapshot.source_run_id,
        source_authority_id=snapshot.source_authority_id,
        source_binding_version=snapshot.source_binding_version,
        source_profile_ref=snapshot.source_profile_ref,
        source_context_package_id=snapshot.context_package_id,
        source_context_package_hash=snapshot.context_package_hash,
    )
    transition = SimpleNamespace(
        id=snapshot.transition_id,
        workspace_id=snapshot.workspace_id,
        source_execution_id=snapshot.source_execution_id,
        source_session_id=snapshot.source_session_id,
        source_checkpoint_sequence=2,
        source_stop_sequence=3,
        handoff_schema_version=2,
        native_payload=payload,
        source_checkpoint_facts=snapshot.to_mapping(),
        source_stop_facts=stop.to_mapping(),
    )
    if bad == "missing":
        transition.source_stop_facts = None
    elif bad is not None:
        transition.source_stop_facts = {
            **stop.to_mapping(),
            {
                "run": "source_run_id",
                "active": "active_response_id",
                "sequence": "accepted_sequence",
            }[bad]: 1 if bad == "sequence" else str(uuid4()),
        }
    service = WorkspaceHandoffService(None)
    if bad is None:
        service._validate_exact_stop(transition, source)
    else:
        with pytest.raises((WorkspaceHandoffError, ValidationError)):
            service._validate_exact_stop(transition, source)


@pytest.mark.parametrize(
    "changes",
    [
        {"source_run_id": uuid4()},
        {"operation_id": uuid4()},
        {"source_session_id": uuid4()},
        {"source_authority_id": uuid4()},
        {"accepted_sequence": 1},
        {"accepted_sequence": 0},
        {"run_state": "closing"},
        {"input_open": True},
        {"active_response_id": uuid4()},
        {"observed_sequence": 2},
    ],
)
def test_native_stop_denies_unclosed_unobserved_or_wrong_original_pin(changes):
    snapshot = native_checkpoint()
    with pytest.raises(ValidationError):
        native_stop(snapshot, **changes).assert_matches(snapshot)


def test_native_and_legacy_checkpoint_stop_variants_cannot_mix():
    snapshot = native_checkpoint()
    with pytest.raises(ValidationError):
        native_stop(snapshot).assert_matches(checkpoint())
    with pytest.raises(ValidationError):
        HandoffStopEvidence(
            operation_id=uuid4(),
            source_session_id=snapshot.source_session_id,
            accepted_sequence=2,
            stopped=True,
            produced_at=NOW,
        ).assert_matches(snapshot)
    with pytest.raises(ValidationError):
        replace(snapshot, schema_version=1, quiescent=True)


def native_workspace_target():
    workspace_id = uuid4()
    return HandoffWorkspaceTargetSpec(
        workspace_id=workspace_id,
        task_id=uuid4(),
        target_execution_id=uuid4(),
        target_worker_id=None,
        adapter_module_ref=None,
        adapter_config_version=None,
        workspace_module_ref=uuid4(),
        workspace_config_version=1,
        repositories=({"repository_ref": "repo:owned", "revision": "a" * 40},),
        anticipated_fencing_token=2,
        schema_version=2,
        target_profile_ref=ProfileRef(
            workspace_id=workspace_id, profile_id=uuid4(), revision=1
        ),
        target_binding_version=2,
    )


def test_native_workspace_target_does_not_require_unreserved_worker_or_adapter():
    target = native_workspace_target()
    assert target.target_worker_id is target.adapter_module_ref is None
    with pytest.raises(ValidationError):
        replace(target, schema_version=1)
    with pytest.raises(ValidationError):
        replace(target, target_profile_ref=None)


def test_native_bundle_is_strict_immutable_and_truthful_before_reservation():
    legacy = bundle()
    profile = ProfileRef(
        workspace_id=legacy.workspace_id, profile_id=uuid4(), revision=1
    )
    native = bundle(
        schema_version=2,
        target_worker_id=None,
        target_runtime_snapshot_hash=None,
        target_profile_ref=profile,
        target_binding_version=2,
        target_native_metadata={"execution_mode": "native_profile"},
    )
    assert native.canonical_payload()["target_worker_id"] is None
    assert native.canonical_payload()["target_profile_ref"] == profile.model_dump(
        mode="json"
    )
    assert native.bundle_hash == native.content_hash
    assert "target_profile_ref" not in legacy.canonical_payload()
    with pytest.raises(ValidationError):
        bundle(schema_version=2, target_profile_ref=profile, target_binding_version=2)
    with pytest.raises(ValidationError, match="forbidden"):
        bundle(
            schema_version=2,
            target_worker_id=None,
            target_runtime_snapshot_hash=None,
            target_profile_ref=profile,
            target_binding_version=2,
            target_native_metadata={
                "execution_mode": "native_profile",
                "api_key": "must-never-be-copied",
            },
        )


def test_native_package_handoff_facts_project_profile_without_fake_worker():
    from sidera_control_plane.context_package_worker import _safe_handoff_facts

    legacy = bundle()
    profile = ProfileRef(
        workspace_id=legacy.workspace_id, profile_id=uuid4(), revision=1
    )
    native = bundle(
        schema_version=2,
        target_worker_id=None,
        target_runtime_snapshot_hash=None,
        target_profile_ref=profile,
        target_binding_version=2,
        target_native_metadata={"execution_mode": "native_profile"},
    )
    target = _safe_handoff_facts(native)["target"]
    assert (
        target["profile_ref"] == profile.model_dump(mode="json")
        and target["binding_version"] == 2
    )
    assert "worker_id" not in target and "runtime_hash" not in target


@pytest.mark.parametrize("changed", ["profile", "binding"])
def test_native_git_transfer_replay_requires_exact_appointment(tmp_path, changed):
    from sidera_control_plane.adapters.git_workspace import (
        GitWorktreeWorkspaceManager,
        WorkspaceProviderError,
    )
    from tests.unit.test_git_workspace import _repository, _spec

    repository, revision = _repository(tmp_path)
    root = tmp_path / "workspaces"
    root.mkdir()
    manager = GitWorktreeWorkspaceManager(root)
    spec = _spec(repository, revision)
    manifest = manager.prepare(spec, operation_id=uuid4())
    manifest.mark_preparing()
    manifest.mark_ready()
    manifest.mark_active()
    frozen = manager.freeze(manifest, operation_id=uuid4())
    target = replace(
        native_workspace_target(),
        workspace_id=manifest.workspace_id,
        task_id=manifest.task_id,
        workspace_module_ref=spec.workspace_module_ref,
        workspace_config_version=spec.workspace_config_version,
        target_profile_ref=ProfileRef(
            workspace_id=manifest.workspace_id, profile_id=uuid4(), revision=1
        ),
        repositories=tuple(
            {"repository_ref": item.repository_ref, "revision": item.base_revision}
            for item in frozen.checkouts
        ),
    )
    transition_id, operation_id = uuid4(), uuid4()
    receipt = manager.transfer_handoff(
        frozen,
        target,
        transition_id=transition_id,
        logical_attempt=1,
        operation_id=operation_id,
    )
    assert (
        manager.transfer_handoff(
            frozen,
            target,
            transition_id=transition_id,
            logical_attempt=1,
            operation_id=operation_id,
        )
        == receipt
    )
    mismatch = replace(
        target,
        **(
            {"target_binding_version": 3}
            if changed == "binding"
            else {
                "target_profile_ref": target.target_profile_ref.model_copy(
                    update={"profile_id": uuid4()}
                )
            }
        ),
    )
    with pytest.raises(WorkspaceProviderError, match="identity"):
        manager.transfer_handoff(
            frozen,
            mismatch,
            transition_id=transition_id,
            logical_attempt=1,
            operation_id=operation_id,
        )


@pytest.mark.parametrize("schema", [2.0, "2", True])
@pytest.mark.parametrize("kind", ["checkpoint", "stop", "workspace", "bundle"])
def test_new_native_discriminator_requires_exact_integer(kind, schema):
    snapshot = native_checkpoint()
    profile = ProfileRef(
        workspace_id=bundle().workspace_id, profile_id=uuid4(), revision=1
    )
    if kind == "bundle":
        baseline = bundle(
            schema_version=2,
            target_worker_id=None,
            target_runtime_snapshot_hash=None,
            target_profile_ref=profile,
            target_binding_version=2,
            target_native_metadata={"execution_mode": "native_profile"},
        )
        assert baseline.schema_version == 2
    with pytest.raises(ValidationError):
        if kind == "checkpoint":
            replace(snapshot, schema_version=schema)
        elif kind == "stop":
            native_stop(snapshot, schema_version=schema)
        elif kind == "workspace":
            replace(native_workspace_target(), schema_version=schema)
        else:
            bundle(
                schema_version=schema,
                target_worker_id=None,
                target_runtime_snapshot_hash=None,
                target_profile_ref=profile,
                target_binding_version=2,
                target_native_metadata={"execution_mode": "native_profile"},
            )


@pytest.mark.parametrize("kind", ["checkpoint", "stop"])
@pytest.mark.parametrize("mutation", ["extra", "missing", "float", "string", "bool"])
def test_native_evidence_decoder_rejects_single_bad_field(kind, mutation):
    snapshot = native_checkpoint()
    value = snapshot if kind == "checkpoint" else native_stop(snapshot)
    facts = value.to_mapping()
    assert type(value).from_native_mapping(facts) == value
    if mutation == "extra":
        facts["private_trace"] = "must-not-be-silently-dropped"
    elif mutation == "missing":
        del facts["source_authority_id"]
    else:
        facts["schema_version"] = {"float": 2.0, "string": "2", "bool": True}[mutation]
    with pytest.raises(ValidationError):
        type(value).from_native_mapping(facts)
