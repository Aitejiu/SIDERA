from copy import deepcopy
from dataclasses import fields, replace
from datetime import UTC, datetime, timedelta, timezone
from types import SimpleNamespace
from uuid import uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sidera_agents.contracts import ProfileRef, RunSnapshot

from sidera_control_plane.api.schemas import (
    CommandResponse,
    NativeHandoffProposalResponse,
    NativeHandoffTransitionResponse,
)
from sidera_control_plane.application.assignments import TrustedAssignmentIdentity
from sidera_control_plane.application.profile_handoff import (
    ProfileHandoffValidator,
    native_proposal_projection,
    native_transition_projection,
)
from sidera_control_plane.application.workspace_handoff import (
    WorkspaceHandoffError,
    WorkspaceHandoffService,
)
from sidera_control_plane.domain.agent_bindings import AssignmentDenied
from sidera_control_plane.domain.commands import CommandEnvelope
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


@pytest.fixture
def native_handoff_validator_case(monkeypatch):
    from tests.unit.test_native_handoff_drain import NOW as DRAIN_NOW
    from tests.unit.test_native_handoff_drain import make_drain_case

    case = make_drain_case()
    stored_payload = case.uow.handoff_transitions.item.native_payload
    stored_bytes = stored_payload.canonical_bytes()
    case.payload = replace(
        stored_payload,
        input_plan={"pending_inputs": ["input:original"]},
        impact_summary={
            "worker_item_context_version": 1,
            "capability_requirements": ["code"],
        },
    )
    payload = case.payload
    original_bytes, original_hash = payload.canonical_bytes(), payload.content_hash
    case.snapshots = [payload, payload]
    case.collect_calls, case.read_calls = [], []
    case.target_calls, case.authorize_calls, case.collected = [], [], []
    case.deny_read_at = None
    case.address = {
        "worker_item_id": payload.worker_item_id,
        "execution_id": payload.source_execution_id,
        "session_id": payload.source_session_id,
        "worker_id": payload.source_worker_id,
        "run_id": payload.source_run_id,
        "binding_version": payload.source_binding_version,
        "session_version": payload.source_session_version,
        "context_version": 1,
        "plan_generation": payload.observed_plan_generation,
    }
    run = case.snapshot.model_dump()
    run.update(state="running", input_open=True)
    case.snapshot = RunSnapshot.model_validate(run)

    def authorize_target(workspace_id, target_type, target_id):
        assert (workspace_id, target_type, target_id) == (
            payload.workspace_id,
            "worker_item",
            payload.worker_item_id,
        )
        case.authorize_calls.append((workspace_id, target_type, target_id))

    def resolve_profile(workspace_id, profile_id, revision, requirements):
        assert (workspace_id, profile_id, revision, requirements) == (
            payload.workspace_id,
            payload.target_profile_ref.profile_id,
            payload.target_profile_ref.revision,
            frozenset({"code"}),
        )
        case.target_calls.append((workspace_id, profile_id, revision, requirements))
        return payload.target_profile_ref, ()

    def require(authority_id, operation, workspace_id):
        assert (authority_id, operation, workspace_id) == (
            payload.source_authority_id,
            "run.read",
            payload.workspace_id,
        )
        case.read_calls.append((authority_id, operation, workspace_id))
        if len(case.read_calls) == case.deny_read_at:
            raise AssignmentDenied("controlled source read denied")
        return case.authority

    def collect(
        uow,
        workspace_id,
        address,
        assignee,
        profile,
        *,
        reason,
        instruction,
        input_plan,
        expected_version,
        lock=False,
    ):
        assert uow is case.uow and workspace_id == payload.workspace_id
        assert address in (
            case.address,
            {**case.address, "execution_mode": "native_profile"},
        )
        assert (assignee, profile, reason, instruction, expected_version) == (
            payload.target_assignee,
            payload.target_profile_ref,
            payload.handoff_reason,
            payload.handoff_instruction,
            payload.source_worker_item_version,
        )
        assert input_plan in (
            payload.input_plan,
            payload.canonical_payload()["input_plan"],
        )
        snapshot = case.snapshots[len(case.collect_calls)]
        case.collect_calls.append(lock)
        case.collected.append(
            (snapshot, snapshot.canonical_bytes(), snapshot.content_hash)
        )
        return snapshot

    def forbidden_command(*args, **kwargs):
        pytest.fail("Native handoff validation attempted a command mutation")

    case.validator = ProfileHandoffValidator(
        assignments=SimpleNamespace(
            authorize_target=authorize_target, resolve_profile=resolve_profile
        ),
        identity=TrustedAssignmentIdentity(
            case.authority.principal_id, case.services.catalog_grant_id
        ),
        native_services=case.services,
        clock=lambda: DRAIN_NOW,
    )
    monkeypatch.setattr(case.validator, "collect", collect)
    monkeypatch.setattr(
        case.validator, "reader", lambda uow: SimpleNamespace(require=require)
    )
    monkeypatch.setattr(case.uow.commands, "record_applied", forbidden_command)
    monkeypatch.setattr(case.uow.commands, "record_rejected", forbidden_command)
    yield case
    assert payload.canonical_bytes() == original_bytes
    assert payload.content_hash == original_hash
    assert stored_payload.canonical_bytes() == stored_bytes
    for snapshot, canonical_bytes, content_hash in case.collected:
        assert snapshot.canonical_bytes() == canonical_bytes
        assert snapshot.content_hash == content_hash
    assert case.uow.commands.applied == case.uow.commands.rejected == []
    assert case.events == []
    for name in (
        "executions",
        "sessions",
        "leases",
        "workers",
        "bindings",
        "workspaces",
        "gateway_operations",
    ):
        repository = getattr(case.uow, name)
        assert repository.saved == repository.added == []


def native_heartbeat_prepare_envelope(case):
    payload = case.payload
    return CommandEnvelope(
        id=uuid4(),
        workspace_id=payload.workspace_id,
        command_type="PrepareProfileHandoff",
        target_type="worker_item",
        actor_type="user",
        actor_id=case.authority.principal_id,
        target_id=payload.worker_item_id,
        payload={
            "proposal_schema_version": 2,
            "context": {
                "execution_mode": "native_profile",
                **{
                    key: str(value) if key.endswith("_id") else value
                    for key, value in case.address.items()
                },
            },
            "target_assignee": {
                "kind": payload.target_assignee.kind,
                "id": str(payload.target_assignee.id),
            },
            "target_profile_ref": payload.target_profile_ref.model_dump(mode="json"),
            "handoff_reason": payload.handoff_reason,
            "handoff_instruction": payload.handoff_instruction,
            "input_plan": payload.canonical_payload()["input_plan"],
            "proposal_ttl_seconds": 300,
        },
        expected_version=payload.source_worker_item_version,
        idempotency_key="native-heartbeat-prepare",
        correlation_id=uuid4(),
    )


def assert_native_heartbeat_inspection(case, *, authorizations):
    payload = case.payload
    assert case.collect_calls == [False, True]
    assert (
        case.read_calls
        == [(payload.source_authority_id, "run.read", payload.workspace_id)] * 2
    )
    assert case.public.calls == [("get_run", payload.source_run_id)]
    assert len(case.target_calls) == 2
    assert len(case.authorize_calls) == authorizations


@pytest.mark.parametrize(
    ("first_delta", "locked_delta"),
    [(1, 2), (0, 1)],
    ids=["before-and-locked", "locked-only"],
)
def test_native_handoff_validator_tolerates_worker_heartbeats(
    native_handoff_validator_case, first_delta, locked_delta
):
    case = native_handoff_validator_case
    case.snapshots = [
        replace(
            case.payload,
            source_worker_version=case.payload.source_worker_version + delta,
        )
        for delta in (first_delta, locked_delta)
    ]
    case.validator.validate_payload(case.uow, case.payload)
    assert_native_heartbeat_inspection(case, authorizations=2)


def test_native_handoff_prepare_records_latest_worker_heartbeat(
    native_handoff_validator_case,
):
    case = native_handoff_validator_case
    latest = replace(
        case.payload, source_worker_version=case.payload.source_worker_version + 1
    )
    case.snapshots = [case.payload, latest]
    envelope = native_heartbeat_prepare_envelope(case)
    original_command = deepcopy(envelope)
    result = case.validator.prepare_payload(
        case.uow, envelope, max_proposal_ttl_seconds=300
    )
    assert result is latest
    assert result.source_worker_version == case.payload.source_worker_version + 1
    for field in fields(case.payload):
        if field.name != "source_worker_version":
            assert getattr(result, field.name) == getattr(case.payload, field.name)
    assert envelope == original_command
    assert_native_heartbeat_inspection(case, authorizations=1)


@pytest.mark.parametrize("boundary", ["before", "locked"])
@pytest.mark.parametrize(
    "field",
    [
        "source_execution_version",
        "source_session_version",
        "source_session_last_sequence",
        "source_worker_id",
        "source_workspace_manifest_version",
        "source_context_package_hash",
        "observed_plan_generation",
        "observed_plan_head_version",
        "source_run_id",
        "source_authority_id",
        "source_binding_version",
        "source_profile_ref",
        "work_context_version",
        "work_context_hash",
        "target_profile_ref",
    ],
)
def test_native_handoff_validator_rejects_changed_pin_despite_heartbeat(
    native_handoff_validator_case, field, boundary
):
    case = native_handoff_validator_case
    original = getattr(case.payload, field)
    if field.endswith("_profile_ref"):
        changed = ProfileRef(
            workspace_id=original.workspace_id,
            profile_id=original.profile_id,
            revision=original.revision + 1,
        )
    elif field.endswith("_id"):
        changed = uuid4()
    elif field.endswith("_hash"):
        changed = "f" * 64 if original != "f" * 64 else "e" * 64
    else:
        changed = original + 1
    mismatch = replace(
        case.payload,
        source_worker_version=case.payload.source_worker_version + 1,
        **{field: changed},
    )
    case.snapshots[0 if boundary == "before" else 1] = mismatch
    message = (
        "native handoff proposal source is stale"
        if boundary == "before"
        else "native handoff proposal changed during inspection"
    )
    with pytest.raises(ValidationError, match=message):
        case.validator.validate_payload(case.uow, case.payload)
    if boundary == "locked":
        assert_native_heartbeat_inspection(case, authorizations=2)
    else:
        assert case.collect_calls == [False]
        assert case.read_calls == case.public.calls == case.target_calls == []
        assert len(case.authorize_calls) == 1


@pytest.mark.parametrize("method", ["validate", "prepare"])
@pytest.mark.parametrize(
    "mismatch", ["completed", "stopped", "failed", "run", "profile"]
)
def test_native_handoff_validator_denies_inactive_or_wrong_source_run(
    native_handoff_validator_case, method, mismatch
):
    case = native_handoff_validator_case
    run = case.snapshot.model_dump()
    if mismatch == "run":
        run["run_id"] = uuid4()
    elif mismatch == "profile":
        profile = ProfileRef(
            workspace_id=case.payload.workspace_id,
            profile_id=case.payload.source_profile_ref.profile_id,
            revision=case.payload.source_profile_ref.revision + 1,
        )
        run["profile_ref"] = profile.model_dump()
        run["effective_config"]["profile_ref"] = profile.model_dump()
    else:
        run.update(state=mismatch, input_open=False)
    case.snapshot = RunSnapshot.model_validate(run)
    envelope = native_heartbeat_prepare_envelope(case)
    original_command = deepcopy(envelope)
    with pytest.raises(AssignmentDenied, match="(?:no longer active|not active)"):
        if method == "validate":
            case.validator.validate_payload(case.uow, case.payload)
        else:
            case.validator.prepare_payload(
                case.uow, envelope, max_proposal_ttl_seconds=300
            )
    assert case.collect_calls == [False]
    assert len(case.read_calls) == len(case.target_calls) == 1
    assert case.public.calls == [("get_run", case.payload.source_run_id)]
    assert len(case.authorize_calls) == (1 if method == "validate" else 0)
    assert envelope == original_command


@pytest.mark.parametrize("method", ["validate", "prepare"])
@pytest.mark.parametrize("deny_read_at", [1, 2])
def test_native_handoff_validator_keeps_both_authority_read_guards(
    native_handoff_validator_case, method, deny_read_at
):
    case = native_handoff_validator_case
    case.deny_read_at = deny_read_at
    envelope = native_heartbeat_prepare_envelope(case)
    original_command = deepcopy(envelope)
    with pytest.raises(AssignmentDenied, match="controlled source read denied"):
        if method == "validate":
            case.validator.validate_payload(case.uow, case.payload)
        else:
            case.validator.prepare_payload(
                case.uow, envelope, max_proposal_ttl_seconds=300
            )
    assert case.collect_calls == [False]
    assert len(case.read_calls) == deny_read_at
    assert len(case.target_calls) == 1
    assert case.public.calls == (
        [] if deny_read_at == 1 else [("get_run", case.payload.source_run_id)]
    )
    assert len(case.authorize_calls) == (1 if method == "validate" else 0)
    assert envelope == original_command


PROJECTION_TIMEZONES = [
    pytest.param(timezone(timedelta(hours=8)), id="+08:00"),
    pytest.param(timezone(timedelta(hours=-5)), id="-05:00"),
    pytest.param(timezone(timedelta(hours=5, minutes=30)), id="+05:30"),
]


@pytest.fixture
def native_projection_case():
    from sidera_control_plane.domain.handoffs import HandoffProposal
    from tests.unit.test_native_handoff_drain import make_drain_case

    transition = make_drain_case().uow.handoff_transitions.item
    created_at = datetime(2026, 10, 8, 8, 10, 39, 427454, tzinfo=UTC)
    proposal = HandoffProposal.create(
        id=transition.proposal_id,
        payload=transition.native_payload,
        created_by_type="user",
        created_by_id="unit",
        source_channel="web",
        created_at=created_at,
        expires_at=created_at + timedelta(minutes=15),
    )
    return proposal, replace(transition)


@pytest.mark.parametrize("offset", PROJECTION_TIMEZONES)
@pytest.mark.parametrize("status", ["prepared", "confirmed", "cancelled"])
def test_native_proposal_projection_uses_same_utc_timestamps(
    native_projection_case, offset, status
):
    proposal, _ = native_projection_case
    changed_at = proposal.created_at + timedelta(minutes=1)
    if status == "confirmed":
        proposal.mark_confirmed(now=changed_at)
    elif status == "cancelled":
        proposal.mark_cancelled(now=changed_at)
    timestamp_fields = ("created_at", "expires_at", "confirmed_at", "cancelled_at")
    offset_proposal = replace(
        proposal,
        **{
            name: value.astimezone(offset)
            for name in timestamp_fields
            if (value := getattr(proposal, name)) is not None
        },
    )
    proposals = (proposal, offset_proposal)
    original_payloads = [value.payload.canonical_bytes() for value in proposals]
    original_hashes = [value.proposal_hash for value in proposals]
    original_dates = [
        {name: getattr(value, name) for name in timestamp_fields} for value in proposals
    ]
    original_timezones = [
        {name: date.tzinfo for name, date in dates.items() if date is not None}
        for dates in original_dates
    ]

    utc_projection = native_proposal_projection(proposal)
    offset_projection = native_proposal_projection(offset_proposal)

    assert offset_projection == utc_projection
    assert utc_projection["id"] == str(proposal.id)
    assert utc_projection["status"] == status
    assert utc_projection["version"] == (1 if status == "prepared" else 2)
    assert utc_projection["proposal_hash"] == original_hashes[0]
    assert utc_projection["created_at"] == "2026-10-08T08:10:39.427454Z"
    assert utc_projection["expires_at"] == "2026-10-08T08:25:39.427454Z"
    assert utc_projection["confirmed_at"] == (
        "2026-10-08T08:11:39.427454Z" if status == "confirmed" else None
    )
    assert utc_projection["cancelled_at"] == (
        "2026-10-08T08:11:39.427454Z" if status == "cancelled" else None
    )
    for value, payload, proposal_hash, dates, timezones in zip(
        proposals,
        original_payloads,
        original_hashes,
        original_dates,
        original_timezones,
    ):
        assert value.payload.canonical_bytes() == payload
        assert value.proposal_hash == proposal_hash
        for name, date in dates.items():
            assert getattr(value, name) is date
            if date is not None:
                assert date.tzinfo is timezones[name]


@pytest.mark.parametrize("offset", PROJECTION_TIMEZONES)
@pytest.mark.parametrize("optional_dates", [False, True], ids=["absent", "populated"])
def test_native_transition_projection_uses_same_utc_timestamps(
    native_projection_case, offset, optional_dates
):
    proposal, transition = native_projection_case
    confirmed_at = proposal.created_at + timedelta(minutes=1)
    transition = replace(
        transition,
        confirmed_at=confirmed_at,
        next_attempt_at=confirmed_at + timedelta(seconds=2) if optional_dates else None,
        started_at=confirmed_at + timedelta(seconds=1) if optional_dates else None,
        completed_at=confirmed_at + timedelta(seconds=3) if optional_dates else None,
    )
    timestamp_fields = ("confirmed_at", "next_attempt_at", "started_at", "completed_at")
    offset_transition = replace(
        transition,
        **{
            name: value.astimezone(offset)
            for name in timestamp_fields
            if (value := getattr(transition, name)) is not None
        },
    )
    transitions = (transition, offset_transition)
    original_payloads = [
        value.native_payload.canonical_bytes() for value in transitions
    ]
    original_hash = proposal.proposal_hash
    original_dates = [
        {name: getattr(value, name) for name in timestamp_fields}
        for value in transitions
    ]
    original_timezones = [
        {name: date.tzinfo for name, date in dates.items() if date is not None}
        for dates in original_dates
    ]
    target_run_id = uuid4() if optional_dates else None

    utc_projection = native_transition_projection(
        transition, target_run_id=target_run_id
    )
    offset_projection = native_transition_projection(
        offset_transition, target_run_id=target_run_id
    )

    assert offset_projection == utc_projection
    assert utc_projection["id"] == str(transition.id)
    assert utc_projection["proposal_id"] == str(proposal.id)
    assert utc_projection["version"] == transition.version
    assert utc_projection["status"] == transition.status.value
    assert utc_projection["confirmed_at"] == "2026-10-08T08:11:39.427454Z"
    for name, expected in (
        ("next_attempt_at", "2026-10-08T08:11:41.427454Z"),
        ("started_at", "2026-10-08T08:11:40.427454Z"),
        ("completed_at", "2026-10-08T08:11:42.427454Z"),
    ):
        assert utc_projection[name] == (expected if optional_dates else None)
    assert proposal.proposal_hash == original_hash
    for value, payload, dates, timezones in zip(
        transitions, original_payloads, original_dates, original_timezones
    ):
        assert value.native_payload.canonical_bytes() == payload
        for name, date in dates.items():
            assert getattr(value, name) is date
            if date is not None:
                assert date.tzinfo is timezones[name]


@pytest.mark.parametrize(
    ("kind", "timestamp"),
    [
        ("proposal", "created_at"),
        ("proposal", "expires_at"),
        ("proposal", "confirmed_at"),
        ("proposal", "cancelled_at"),
        ("transition", "confirmed_at"),
        ("transition", "next_attempt_at"),
        ("transition", "started_at"),
        ("transition", "completed_at"),
    ],
)
def test_native_handoff_projection_keeps_distinct_microsecond_instants(
    native_projection_case, kind, timestamp
):
    proposal, transition = native_projection_case
    if kind == "proposal":
        if timestamp == "confirmed_at":
            proposal.mark_confirmed(now=proposal.created_at)
        elif timestamp == "cancelled_at":
            proposal.mark_cancelled(now=proposal.created_at)
        value = proposal
        project = native_proposal_projection
    else:
        value = replace(
            transition,
            confirmed_at=proposal.created_at,
            next_attempt_at=proposal.created_at,
            started_at=proposal.created_at,
            completed_at=proposal.created_at,
        )
        project = native_transition_projection
    later = replace(
        value,
        **{timestamp: getattr(value, timestamp) + timedelta(microseconds=1)},
    )

    projection = project(value)
    later_projection = project(later)

    assert projection[timestamp].endswith(".427454Z")
    assert later_projection[timestamp].endswith(".427455Z")
    assert projection != later_projection
    assert later_projection == projection | {timestamp: later_projection[timestamp]}


@pytest.mark.parametrize("offset", PROJECTION_TIMEZONES)
@pytest.mark.parametrize("status", ["prepared", "confirmed", "cancelled"])
@pytest.mark.parametrize("microsecond", [0, 1, 100000, 427454, 999999])
def test_native_proposal_http_serialization_preserves_projection(
    native_projection_case, offset, status, microsecond
):
    proposal, _ = native_projection_case
    proposal = replace(
        proposal,
        created_at=proposal.created_at.replace(microsecond=microsecond).astimezone(
            offset
        ),
        expires_at=proposal.expires_at.replace(microsecond=microsecond).astimezone(
            offset
        ),
    )
    changed_at = proposal.created_at + timedelta(minutes=1)
    if status == "confirmed":
        proposal.mark_confirmed(now=changed_at)
    elif status == "cancelled":
        proposal.mark_cancelled(now=changed_at)
    timestamp_fields = ("created_at", "expires_at", "confirmed_at", "cancelled_at")
    original_payload = proposal.payload.canonical_bytes()
    original_hash = proposal.proposal_hash
    original_proposal = replace(proposal)
    original_dates = {name: getattr(proposal, name) for name in timestamp_fields}
    original_timezones = {
        name: date.tzinfo for name, date in original_dates.items() if date is not None
    }
    projection = native_proposal_projection(proposal)
    original_projection = deepcopy(projection)
    document_kind = "proposal"
    document_id = proposal.id
    document_version = proposal.version
    command_id = uuid4()
    app = FastAPI()

    @app.get("/command", response_model=CommandResponse)
    def get_command():
        return {
            "command_id": command_id,
            "aggregate_id": document_id,
            "aggregate_version": document_version,
            "event_ids": (),
            "data": {document_kind: projection},
        }

    @app.get("/document", response_model=NativeHandoffProposalResponse)
    def get_document():
        return projection

    with TestClient(app) as client:
        command_response = client.get("/command")
        document_response = client.get("/document")
        assert command_response.status_code == document_response.status_code == 200
        command_json = command_response.json()
        get_json = document_response.json()

    assert command_json["data"][document_kind] == get_json == projection
    assert projection == original_projection
    assert projection["id"] == str(document_id)
    assert projection["status"] == status
    assert projection["version"] == (1 if status == "prepared" else 2)
    assert projection["proposal_hash"] == original_hash
    assert proposal == original_proposal
    assert proposal.payload.canonical_bytes() == original_payload
    assert proposal.proposal_hash == original_hash
    for name, date in original_dates.items():
        assert getattr(proposal, name) is date
        if date is None:
            assert projection[name] is None
        else:
            assert date.tzinfo is original_timezones[name]
            assert projection[name] == date.astimezone(UTC).isoformat().replace(
                "+00:00", "Z"
            )
            if microsecond == 427454:
                assert projection[name].endswith(".427454Z")


@pytest.mark.parametrize("offset", PROJECTION_TIMEZONES)
@pytest.mark.parametrize("optional_dates", [False, True], ids=["absent", "populated"])
@pytest.mark.parametrize("microsecond", [0, 1, 100000, 427454, 999999])
def test_native_transition_http_serialization_preserves_projection(
    native_projection_case, offset, optional_dates, microsecond
):
    proposal, transition = native_projection_case
    confirmed_at = (
        proposal.created_at.replace(microsecond=microsecond) + timedelta(minutes=1)
    ).astimezone(offset)
    transition = replace(
        transition,
        confirmed_at=confirmed_at,
        next_attempt_at=confirmed_at + timedelta(seconds=2) if optional_dates else None,
        started_at=confirmed_at + timedelta(seconds=1) if optional_dates else None,
        completed_at=confirmed_at + timedelta(seconds=3) if optional_dates else None,
    )
    timestamp_fields = ("confirmed_at", "next_attempt_at", "started_at", "completed_at")
    original_payload = transition.native_payload.canonical_bytes()
    original_hash = proposal.proposal_hash
    original_transition = replace(transition)
    original_dates = {name: getattr(transition, name) for name in timestamp_fields}
    original_timezones = {
        name: date.tzinfo for name, date in original_dates.items() if date is not None
    }
    target_run_id = uuid4() if optional_dates else None
    projection = native_transition_projection(transition, target_run_id=target_run_id)
    original_projection = deepcopy(projection)
    document_kind = "transition"
    document_id = transition.id
    document_version = transition.version
    command_id = uuid4()
    app = FastAPI()

    @app.get("/command", response_model=CommandResponse)
    def get_command():
        return {
            "command_id": command_id,
            "aggregate_id": document_id,
            "aggregate_version": document_version,
            "event_ids": (),
            "data": {document_kind: projection},
        }

    @app.get("/document", response_model=NativeHandoffTransitionResponse)
    def get_document():
        return projection

    with TestClient(app) as client:
        command_response = client.get("/command")
        document_response = client.get("/document")
        assert command_response.status_code == document_response.status_code == 200
        command_json = command_response.json()
        get_json = document_response.json()

    assert command_json["data"][document_kind] == get_json == projection
    assert projection == original_projection
    assert projection["id"] == str(document_id)
    assert projection["proposal_id"] == str(proposal.id)
    assert projection["version"] == document_version
    assert projection["status"] == transition.status.value
    assert projection["target_run_id"] == (
        None if target_run_id is None else str(target_run_id)
    )
    assert transition == original_transition
    assert transition.native_payload.canonical_bytes() == original_payload
    assert proposal.proposal_hash == original_hash
    for name, date in original_dates.items():
        assert getattr(transition, name) is date
        if date is None:
            assert projection[name] is None
        else:
            assert date.tzinfo is original_timezones[name]
            assert projection[name] == date.astimezone(UTC).isoformat().replace(
                "+00:00", "Z"
            )
            if microsecond == 427454:
                assert projection[name].endswith(".427454Z")
