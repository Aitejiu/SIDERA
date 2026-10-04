import importlib
import importlib.util
from contextlib import contextmanager, nullcontext
from dataclasses import replace
from datetime import UTC, datetime
from types import SimpleNamespace
from uuid import NAMESPACE_URL, UUID, uuid4, uuid5

import pytest
from sidera_agents.contracts import ProfileRef, RunEvent, StartLookup
from sidera_agents.errors import AgentError


def native_adapter():
    name = "sidera_control_plane.application.agent_worker_adapter"
    assert importlib.util.find_spec(name) is not None, (
        "N12 generic native adapter is missing"
    )
    return importlib.import_module(name)


@pytest.fixture
def native_start_case():
    from sidera_agents.contracts import (
        EventPage,
        ExecutionWorkspace,
        RunStart,
        StartReceipt,
    )

    from sidera_control_plane.domain.agent_bindings import AgentExecutionLink

    now = datetime.now(UTC)
    workspace_id, execution_id, grant_id, run_id = (uuid4() for _ in range(4))
    profile = ProfileRef(workspace_id=workspace_id, profile_id=uuid4(), revision=1)
    request = RunStart(
        operation_id=uuid4(),
        profile_ref=profile,
        grant_id=grant_id,
        policy="bounded",
        instructions=(),
        text="explicit test work",
        context=(),
        tools=(),
        workspace=ExecutionWorkspace(
            directory="/test-owned", writable=True, artifact_directory=None
        ),
        required_capabilities=(),
    )
    state = SimpleNamespace(
        link=AgentExecutionLink(
            workspace_id,
            execution_id,
            request.operation_id,
            profile,
            None,
            0,
            "pending",
            grant_id,
            1,
        ),
        changes=[],
        starts=[],
        queries=[],
        permit=[],
        denied=None,
        start_error=False,
        persistence_error=False,
    )
    authority = SimpleNamespace(
        execution_id=execution_id, profile_ref=profile, binding_version=1
    )

    def begin_start(link, *, request_sha256, now):
        state.changes.append("starting")
        state.link = replace(
            link, status="starting", request_sha256=request_sha256, start_receipt=None
        )
        return state.link

    def record_start(link, receipt, *, now):
        if state.persistence_error:
            raise RuntimeError("controlled receipt persistence failure")
        state.changes.append(receipt.status.value)
        state.link = replace(
            link,
            start_receipt=receipt,
            run_id=receipt.run_id,
            status="running" if receipt.status.value == "accepted" else "unknown",
        )
        return state.link

    repository = SimpleNamespace(
        get_link=lambda *args: state.link,
        get=lambda *args, **kwargs: authority,
        begin_start=begin_start,
        record_start=record_start,
    )
    uow = SimpleNamespace(
        execution_authorities=repository,
        executions=SimpleNamespace(
            get=lambda *args: SimpleNamespace(
                purpose="code",
                worker_item_id=None,
                workspace_id=workspace_id,
                task_id=uuid4(),
            )
        ),
        agent_bindings=SimpleNamespace(
            get=lambda *args: SimpleNamespace(profile_ref=profile, version=1)
        ),
    )
    receipt = StartReceipt(
        operation_id=request.operation_id,
        status="accepted",
        run_id=run_id,
        reason_code=None,
    )

    class Execution:
        def start(self, supplied):
            assert state.permit == ["enter"]
            assert supplied == request and state.link.status == "starting"
            state.starts.append(supplied)
            if state.start_error:
                raise TimeoutError("controlled lost native ACK")
            return receipt

        def get_start(self, workspace, operation_id, *, grant_id):
            state.queries.append((workspace, operation_id, grant_id))
            return StartLookup(found=True, receipt=receipt)

        def list_events(self, workspace, supplied_run, after_sequence, *, grant_id):
            assert (workspace, supplied_run, after_sequence, grant_id) == (
                workspace_id,
                run_id,
                0,
                request.grant_id,
            )
            return EventPage(
                events=(
                    RunEvent(
                        run_id=run_id,
                        sequence=1,
                        kind="run.started",
                        occurred_at=now,
                        operation_id=request.operation_id,
                        response_id=None,
                        data={},
                    ),
                ),
                next_sequence=1,
                has_more=False,
            )

    @contextmanager
    def permit():
        state.permit.append("enter")
        if state.denied is not None:
            raise AgentError(state.denied, "New native admission denied.")
        try:
            yield
        finally:
            state.permit.append("exit")

    def adapter():
        return native_adapter().AgentWorkerAdapter(
            lambda: nullcontext(uow),
            execution=Execution(),
            input_builder=SimpleNamespace(build=lambda *args: request),
            authorizer=SimpleNamespace(require=lambda *args: None),
            workspace_id=workspace_id,
            execution_id=execution_id,
            clock=lambda: now,
            owner_check=lambda: True,
            start_admission=permit,
        )

    def busy():
        state.link = replace(
            state.link,
            status="waiting_capacity",
            request_sha256=native_adapter().request_fingerprint(request),
            start_receipt=StartReceipt(
                operation_id=request.operation_id,
                status="busy",
                run_id=None,
                reason_code=None,
            ),
        )

    return SimpleNamespace(
        state=state,
        request=request,
        now=now,
        uow=uow,
        adapter=adapter,
        busy=busy,
        permit=permit,
        spec=SimpleNamespace(workspace_id=workspace_id, execution_id=execution_id),
    )


@pytest.mark.parametrize("busy", (False, True))
@pytest.mark.parametrize("code", ("permission_denied", "control_plane_closed"))
def test_worker_start_admission_denies_before_starting_without_uncertainty(
    native_start_case, busy, code
):
    s = native_start_case
    if busy:
        s.busy()
    original = s.state.link
    s.state.denied = code
    with pytest.raises(AgentError) as denied:
        s.adapter().start_session(s.spec, None, operation_id=s.request.operation_id)
    assert denied.value.code == code
    assert s.state.link == original and not s.state.changes and not s.state.starts
    assert not s.state.queries and s.state.permit == ["enter"]


@pytest.mark.parametrize("failure", (None, "lost_ack", "persistence"))
def test_worker_start_admission_tracks_native_call_and_exits_after_ack_failure(
    native_start_case, failure
):
    s = native_start_case
    s.state.start_error = failure == "lost_ack"
    s.state.persistence_error = failure == "persistence"
    if failure is None:
        result = s.adapter().start_session(
            s.spec, None, operation_id=s.request.operation_id
        )
        assert result.adapter_session_id == "agent-run:" + str(s.state.link.run_id)
    else:
        with pytest.raises(native_adapter().NativeStartDeferred):
            s.adapter().start_session(s.spec, None, operation_id=s.request.operation_id)
    assert len(s.state.starts) == 1 and not s.state.queries
    assert s.state.permit == ["enter", "exit"]
    assert (
        s.state.link.status
        == {None: "running", "lost_ack": "unknown", "persistence": "starting"}[failure]
    )


def test_worker_original_unknown_query_never_enters_new_start_admission(
    native_start_case,
):
    s = native_start_case
    s.state.link = replace(s.state.link, status="unknown")
    s.state.denied = "control_plane_closed"
    result = s.adapter().start_session(
        s.spec, None, operation_id=s.request.operation_id
    )
    assert result.adapter_session_id == "agent-run:" + str(s.state.link.run_id)
    assert not s.state.permit and not s.state.starts
    assert s.state.queries == [
        (s.request.profile_ref.workspace_id, s.request.operation_id, s.request.grant_id)
    ]


def test_worker_busy_retry_keeps_original_intention_and_enters_start_admission(
    native_start_case,
):
    s = native_start_case
    s.busy()
    original = s.state.link
    result = s.adapter().start_session(
        s.spec, None, operation_id=s.request.operation_id
    )
    assert result.adapter_session_id == "agent-run:" + str(s.state.link.run_id)
    assert s.state.starts == [s.request] and not s.state.queries
    assert s.state.permit == ["enter", "exit"]
    assert s.state.link.start_operation_id == original.start_operation_id
    assert s.state.link.authority_id == original.authority_id
    assert s.state.link.request_sha256 == original.request_sha256


def test_response_completion_does_not_become_execution_completion():
    from datetime import UTC, datetime

    adapter = native_adapter()
    event = RunEvent(
        run_id=uuid4(),
        sequence=7,
        kind="response.completed",
        occurred_at=datetime.now(UTC),
        operation_id=uuid4(),
        response_id=uuid4(),
        data={"outcome": "succeeded"},
    )
    projected = adapter.project_run_event(event)
    assert projected.sequence == 7
    assert projected.event_type == "native.response.completed"
    assert projected.payload["run_id"] == str(event.run_id)
    assert projected.payload["response_id"] == str(event.response_id)


def test_native_preparation_has_a_distinct_typed_receipt():
    adapter = native_adapter()
    receipt = getattr(adapter, "NativeContextPreparationReceipt", None)
    assert receipt is not None, "Native source preparation is not legacy assembly"
    assert receipt.model_config["frozen"] is True
    assert "final_prompt" not in receipt.model_fields
    assert "materialized_at" not in receipt.model_fields


@pytest.mark.parametrize("state", ("running", "closing", "unknown"))
def test_unconfirmed_close_never_projects_a_terminal_session(state):
    from datetime import UTC, datetime

    event = RunEvent(
        run_id=uuid4(),
        sequence=2,
        kind="run.closed",
        occurred_at=datetime.now(UTC),
        operation_id=None,
        response_id=None,
        data={},
    )
    assert (
        native_adapter().project_run_event(event, run_state=state).event_type
        == "native.run.closed"
    )


@pytest.mark.parametrize(
    ("kind", "status", "expected"),
    (
        ("run.closed", "completed", "session.completed"),
        ("run.closed", "stopped", "session.closed"),
        ("run.failed", "failed", "session.failed"),
    ),
)
def test_immutable_terminal_fact_wins_over_earlier_run_snapshot(kind, status, expected):
    from datetime import UTC, datetime

    event = RunEvent(
        run_id=uuid4(),
        sequence=2,
        kind=kind,
        occurred_at=datetime.now(UTC),
        operation_id=None,
        response_id=None,
        data={"status": status},
    )
    first = native_adapter().project_run_event(event, run_state="running")
    replay = native_adapter().project_run_event(event, run_state=status)
    assert first.event_type == expected
    assert first == replay
    assert first.raw_ref.startswith("native-event-sha256:")


def test_native_event_identity_detects_content_hidden_by_redaction():
    from datetime import UTC, datetime

    from sidera_control_plane.application.event_ingestor import _redact_payload

    event = RunEvent(
        run_id=uuid4(),
        sequence=2,
        kind="message",
        occurred_at=datetime.now(UTC),
        operation_id=None,
        response_id=None,
        data={"token": "first-private-value", "text": "progress"},
    )
    changed = event.model_copy(
        update={"data": {**event.data, "token": "changed-private-value"}}
    )
    first = native_adapter().project_run_event(event)
    second = native_adapter().project_run_event(changed)
    assert _redact_payload(first.payload) == _redact_payload(second.payload)
    assert first.raw_ref != second.raw_ref
    assert "private-value" not in first.raw_ref


@pytest.mark.parametrize("status", (None, "running", "closing", "unknown", "invalid"))
def test_snapshot_cannot_change_identity_of_incomplete_closed_event(status):
    from datetime import UTC, datetime

    event = RunEvent(
        run_id=uuid4(),
        sequence=2,
        kind="run.closed",
        occurred_at=datetime.now(UTC),
        operation_id=None,
        response_id=None,
        data={} if status is None else {"status": status},
    )
    before = native_adapter().project_run_event(event, run_state="running")
    after = native_adapter().project_run_event(event, run_state="completed")
    assert before == after
    assert after.event_type == "native.run.closed"


@pytest.mark.parametrize("status", ([], {}, True, 1, 1.5))
def test_malformed_terminal_status_is_stable_unconfirmed_observation(status):
    from datetime import UTC, datetime

    event = RunEvent(
        run_id=uuid4(),
        sequence=2,
        kind="run.closed",
        occurred_at=datetime.now(UTC),
        operation_id=None,
        response_id=None,
        data={"status": status},
    )
    first = native_adapter().project_run_event(event, run_state="running")
    replay = native_adapter().project_run_event(event, run_state="completed")
    assert first.event_type == "native.run.closed"
    assert first == replay
    assert first.raw_ref.startswith("native-event-sha256:")


@pytest.mark.parametrize(
    "read", ("start_operation", "start_workspace", "run_id", "run_workspace")
)
def test_workspace_coarse_public_reads_are_pinned_before_forwarding(read):
    adapter_module = native_adapter()
    from sidera_control_plane.domain.agent_bindings import AgentExecutionLink

    workspace_id, execution_id, run_id, grant_id = uuid4(), uuid4(), uuid4(), uuid4()
    profile = ProfileRef(workspace_id=workspace_id, profile_id=uuid4(), revision=1)
    link = AgentExecutionLink(
        workspace_id, execution_id, uuid4(), profile, run_id, 1, "running", grant_id, 1
    )
    calls = []
    authority = SimpleNamespace(
        execution_id=execution_id, profile_ref=profile, binding_version=1
    )
    uow = SimpleNamespace(
        execution_authorities=SimpleNamespace(
            get_link=lambda *args: link, get=lambda *args, **kwargs: authority
        )
    )

    class PublicExecution:
        def get_start(self, *args, **kwargs):
            calls.append((args, kwargs))
            return StartLookup(found=False, receipt=None)

        def get_run(self, *args, **kwargs):
            pytest.fail("unrelated workspace-coarse Run read was forwarded")

    adapter = adapter_module.AgentWorkerAdapter(
        lambda: nullcontext(uow),
        execution=PublicExecution(),
        input_builder=None,
        authorizer=SimpleNamespace(require=lambda *args: None),
        workspace_id=workspace_id,
        execution_id=execution_id,
    )
    with pytest.raises(AgentError, match="Original native"):
        if read.startswith("start"):
            adapter.get_start(
                uuid4() if read == "start_workspace" else workspace_id,
                uuid4() if read == "start_operation" else link.start_operation_id,
            )
        else:
            adapter.get_run(
                uuid4() if read == "run_workspace" else workspace_id,
                uuid4() if read == "run_id" else run_id,
            )
    assert calls == []
    adapter.get_start(workspace_id, link.start_operation_id)
    assert calls == [((workspace_id, link.start_operation_id), {"grant_id": grant_id})]


def test_native_authority_pins_only_declared_references_and_mcp_tools():
    from sidera_agents.contracts import (
        CapabilityResourceDraft,
        CredentialRef,
        ResourceRef,
        ResourceRevision,
        SecretBinding,
        ToolBinding,
    )

    from tests.unit.test_native_profile_scheduler import native_runtime_fixture

    _, _, _, services = native_runtime_fixture()
    profile = next(iter(services.catalog.profiles.values()))
    workspace = profile.ref.workspace_id
    credentials = tuple(
        CredentialRef(workspace_id=workspace, credential_id=uuid4()) for _ in range(2)
    )
    refs = tuple(
        ResourceRef(workspace_id=workspace, resource_id=uuid4(), revision=1)
        for _ in range(2)
    )
    profile = profile.model_copy(
        update={
            "draft": profile.draft.model_copy(
                update={
                    "resource_refs": refs,
                    "secret_refs": (
                        SecretBinding(
                            credential_ref=credentials[0], destination="provider"
                        ),
                    ),
                }
            )
        }
    )
    services.catalog.profiles[profile.ref] = profile
    for ref, kind in zip(refs, ("skill", "mcp"), strict=True):
        services.catalog.resources[ref] = ResourceRevision(
            ref=ref,
            draft=CapabilityResourceDraft(
                name=kind,
                kind=kind,
                settings={"fixture_private_payload": "not copied"},
                secret_refs=(
                    SecretBinding(credential_ref=credentials[1], destination="tool"),
                )
                if kind == "mcp"
                else (),
            ),
            content_hash="fixture",
            created_at=profile.created_at,
        )
    helper = getattr(native_adapter(), "native_authority_pins", None)
    assert callable(helper), (
        "N12 authority must honor exact confirmed Profile references"
    )
    pins = helper(services, profile.ref)
    assert pins.credentials == credentials
    assert pins.resources == refs
    assert pins.tools == (ToolBinding(resource_ref=refs[1]),)
    assert pins.actions == ("secret.read", "resource.read", "tool.execute")
    assert "fixture_private_payload" not in repr(pins)
    services.catalog.enabled = False
    with pytest.raises(AgentError):
        helper(services, profile.ref)


def test_deterministic_verification_does_not_inherit_a_model_profile():
    def model_binding(*args):
        pytest.fail(
            "deterministic verification must not select its WorkerItem's model Profile"
        )

    uow = SimpleNamespace(agent_bindings=SimpleNamespace(get=model_binding))
    execution = SimpleNamespace(
        purpose="verification",
        worker_item_id=uuid4(),
        task_id=uuid4(),
        workspace_id=uuid4(),
    )
    assert native_adapter().native_binding(uow, execution) is None


@pytest.fixture
def preparation_evidence():
    from sidera_control_plane.application.command_service import hash_request
    from sidera_control_plane.domain.agent_bindings import AgentExecutionLink
    from sidera_control_plane.domain.commands import CommandEnvelope

    workspace_id = UUID("00000000-0000-0000-0000-000000000001")
    execution_id = UUID("00000000-0000-0000-0000-000000000002")
    profile = ProfileRef(workspace_id=workspace_id, profile_id=uuid4(), revision=1)
    package_id, authority_id, lease_id = uuid4(), uuid4(), uuid4()
    command_id = uuid5(
        NAMESPACE_URL,
        f"sidera.native.prepare-context:{execution_id}:2:{package_id}:{'a' * 64}:{'b' * 64}",
    )
    receipt = native_adapter().NativeContextPreparationReceipt(
        command_id=command_id,
        workspace_id=workspace_id,
        execution_id=execution_id,
        binding_version=2,
        authority_id=authority_id,
        lease_id=lease_id,
        fencing_token=3,
        package_id=package_id,
        package_hash="a" * 64,
        revision_id=uuid4(),
        revision_hash="b" * 64,
        context_version=1,
        sources=(),
        validated_bytes=0,
        validated_sections=0,
        workspace_manifest_id=uuid4(),
        workspace_manifest_digest="c" * 64,
        workspace_location_hash="d" * 64,
        physical_input=None,
        capture_operation_id=UUID("ea8c88f9-a018-5b5e-83ca-6d6f71676304"),
    )
    mapping = receipt.model_dump(mode="json")
    command = CommandEnvelope(
        command_id,
        workspace_id,
        "PrepareNativeContextSources",
        "execution",
        "system",
        "gateway",
        execution_id,
        {"preparation": mapping},
        None,
        f"native-context:{command_id}",
        command_id,
    )
    stored = SimpleNamespace(
        id=command_id,
        workspace_id=workspace_id,
        status="applied",
        request_hash=hash_request(command),
        result={"data": {"preparation": mapping}},
    )
    authority = SimpleNamespace(
        id=authority_id,
        workspace_id=workspace_id,
        execution_id=execution_id,
        binding_version=2,
        profile_ref=profile,
        lease_id=lease_id,
        fencing_token=3,
        revoked=True,
    )
    link = AgentExecutionLink(
        workspace_id,
        execution_id,
        uuid4(),
        profile,
        None,
        0,
        "pending",
        authority_id,
        2,
        preparation=mapping,
    )
    uow = SimpleNamespace(
        execution_authorities=SimpleNamespace(
            get_link=lambda *args: link, get=lambda *args, **kwargs: authority
        ),
        commands=SimpleNamespace(find_by_id=lambda *args: stored),
    )
    return SimpleNamespace(
        link=link, authority=authority, stored=stored, uow=uow, receipt=receipt
    )


def test_canonical_capture_reads_genuine_revoked_preparation_audit(
    preparation_evidence,
):
    from sidera_control_plane.application import code_result_capture

    s = preparation_evidence
    selector = getattr(code_result_capture, "capture_operation_for_execution", None)
    assert callable(selector), (
        "Native result consumers must use the canonical preparation pin"
    )
    assert selector(
        s.uow, workspace_id=s.link.workspace_id, execution_id=s.link.execution_id
    ) == UUID("ea8c88f9-a018-5b5e-83ca-6d6f71676304")
    assert native_adapter().read_native_preparation_receipt(s.uow, s.link) == s.receipt


@pytest.mark.parametrize(
    "case",
    (
        "missing_preparation",
        "malformed_preparation",
        "missing_command",
        "missing_authority",
        "request_hash",
        "result_data",
        "command_workspace",
        "command_id",
        "receipt_workspace",
        "receipt_execution",
        "receipt_binding",
        "receipt_authority",
        "receipt_lease",
        "receipt_fence",
        "receipt_capture",
        "receipt_command",
        "authority_workspace",
        "authority_execution",
        "authority_binding",
        "authority_profile",
    ),
)
def test_canonical_capture_rejects_bad_native_pin_without_legacy_fallback(
    preparation_evidence, case
):
    from dataclasses import replace

    from sidera_control_plane.application import code_result_capture
    from sidera_control_plane.domain.result_operations import ResultError

    s = preparation_evidence
    if case == "missing_preparation":
        s.link = replace(s.link, preparation=None)
    elif case == "malformed_preparation":
        s.link = replace(s.link, preparation={"internal_context": "must not leak"})
    elif case == "missing_command":
        s.uow.commands.find_by_id = lambda *args: None
    elif case == "missing_authority":
        s.uow.execution_authorities.get = lambda *args, **kwargs: None
    elif case == "request_hash":
        s.stored.request_hash = "0" * 64
    elif case == "result_data":
        s.stored.result = {"data": None}
    elif case == "command_workspace":
        s.stored.workspace_id = uuid4()
    elif case == "command_id":
        s.stored.id = uuid4()
    elif case.startswith("receipt_"):
        field = {
            "workspace": "workspace_id",
            "execution": "execution_id",
            "binding": "binding_version",
            "authority": "authority_id",
            "lease": "lease_id",
            "fence": "fencing_token",
            "capture": "capture_operation_id",
            "command": "command_id",
        }[case.removeprefix("receipt_")]
        mapping = dict(s.link.preparation)
        mapping[field] = (
            99 if field in {"binding_version", "fencing_token"} else str(uuid4())
        )
        s.link = replace(s.link, preparation=mapping)
    else:
        field = {
            "workspace": "workspace_id",
            "execution": "execution_id",
            "binding": "binding_version",
            "profile": "profile_ref",
        }[case.removeprefix("authority_")]
        setattr(s.authority, field, 99 if field == "binding_version" else uuid4())
    s.uow.execution_authorities.get_link = lambda *args: s.link
    selector = getattr(code_result_capture, "capture_operation_for_execution", None)
    assert callable(selector), (
        "Native result consumers must use the canonical preparation pin"
    )
    with pytest.raises(ResultError) as denied:
        selector(
            s.uow, workspace_id=s.link.workspace_id, execution_id=s.link.execution_id
        )
    assert denied.value.code in {"context_package_missing", "context_hash_mismatch"}
    assert denied.value.details == {}
    assert "internal_context" not in str(denied.value)


def test_no_native_link_preserves_exact_legacy_capture_identity():
    from sidera_control_plane.application import code_result_capture

    uow = SimpleNamespace(
        execution_authorities=SimpleNamespace(get_link=lambda *args: None)
    )
    selector = getattr(code_result_capture, "capture_operation_for_execution", None)
    assert callable(selector), (
        "Result consumers need one native/legacy capture selector"
    )
    assert selector(
        uow,
        workspace_id=uuid4(),
        execution_id=UUID("00000000-0000-0000-0000-000000000002"),
    ) == UUID("cb1f011e-3d66-5181-b775-78e85b2c6234")


@pytest.fixture
def ordinary_stop():
    from datetime import UTC, datetime

    from sidera_agents.contracts import (
        EffectiveConfig,
        RunSnapshot,
        RuntimeRef,
        StopReceipt,
    )

    from sidera_control_plane.domain.agent_bindings import AgentExecutionLink
    from sidera_control_plane.domain.execution import ExecutionSession
    from sidera_control_plane.domain.gateway import (
        AdapterSessionHandle,
        GatewayOperation,
        GatewayOperationType,
        gateway_operation_id,
    )
    from sidera_control_plane.domain.modules import ExecutionBinding

    module = native_adapter()
    now = datetime(2026, 10, 4, tzinfo=UTC)
    workspace_id, execution_id, run_id = uuid4(), uuid4(), uuid4()
    grant, lease_id, worker_id, adapter_id = uuid4(), uuid4(), uuid4(), uuid4()
    profile = ProfileRef(workspace_id=workspace_id, profile_id=uuid4(), revision=1)
    link = AgentExecutionLink(
        workspace_id, execution_id, uuid4(), profile, run_id, 1, "running", grant, 2
    )
    session = ExecutionSession.start(
        session_id=uuid5(NAMESPACE_URL, f"sidera.native.session:{run_id}"),
        workspace_id=workspace_id,
        execution_id=execution_id,
        worker_id=worker_id,
        adapter_session_id=f"agent-run:{run_id}",
        now=now,
        adapter_module_id=adapter_id,
        adapter_config_version=3,
    )
    binding = ExecutionBinding(
        uuid4(),
        workspace_id,
        execution_id,
        worker_id,
        adapter_id,
        uuid4(),
        3,
        1,
        module.native_capabilities(),
        lease_id,
        7,
        1,
    )
    operation_id = gateway_operation_id(execution_id, GatewayOperationType.STOP_SESSION)
    operation = GatewayOperation.create(
        id=uuid4(),
        operation_id=operation_id,
        workspace_id=workspace_id,
        task_id=uuid4(),
        execution_id=execution_id,
        session_id=session.id,
        operation_type=GatewayOperationType.STOP_SESSION,
        module_instance_id=adapter_id,
        config_version=3,
        lease_id=lease_id,
        fencing_token=7,
        now=now,
    )
    operation.start(now=now)
    authority = SimpleNamespace(
        id=grant,
        workspace_id=workspace_id,
        execution_id=execution_id,
        profile_ref=profile,
        binding_version=2,
        lease_id=lease_id,
        fencing_token=7,
        revoked=True,
    )
    receipt = StopReceipt(
        operation_id=operation_id,
        run_id=run_id,
        status="stopped",
        reason_code=None,
        evidence_sequence=2,
    )
    snapshot = RunSnapshot(
        run_id=run_id,
        workspace_id=workspace_id,
        profile_ref=profile,
        policy="bounded",
        state="stopped",
        input_open=False,
        active_response_id=None,
        last_event_sequence=2,
        capabilities={},
        reason_code=None,
        effective_config=EffectiveConfig(
            profile_ref=profile,
            config_hash="fixture",
            runtime_ref=RuntimeRef(workspace_id=workspace_id, runtime_id=uuid4()),
            actual_runtime_version="1",
            selected_model=None,
            observed_model=None,
            inherited_fields=(),
            applied_fields=(),
            resource_refs=(),
            context_receipts=(),
        ),
    )
    state = SimpleNamespace(
        link=link,
        authority=authority,
        session=session,
        binding=binding,
        operation=operation,
        receipt=receipt,
        snapshot=snapshot,
        calls=[],
        owner=True,
        query_error=None,
    )
    uow = SimpleNamespace(
        execution_authorities=SimpleNamespace(
            get_link=lambda *args: state.link,
            get=lambda *args, **kwargs: state.authority,
        ),
        sessions=SimpleNamespace(get_any=lambda *args, **kwargs: state.session),
        bindings=SimpleNamespace(
            get_for_execution=lambda *args, **kwargs: state.binding
        ),
        gateway_operations=SimpleNamespace(
            get_by_operation_id=lambda operation_id, workspace_id, *, for_update=False: (
                state.operation
            )
        ),
    )

    class PublicExecution:
        def stop(self, request):
            state.calls.append(("stop", request))
            return state.receipt

        def get_stop(self, workspace, run, operation, *, grant_id):
            state.calls.append(("get_stop", workspace, run, operation, grant_id))
            if state.query_error is not None:
                raise state.query_error
            return state.receipt

        def get_run(self, workspace, run, *, grant_id):
            state.calls.append(("get_run", workspace, run, grant_id))
            return state.snapshot

    state.adapter = module.AgentWorkerAdapter(
        lambda: nullcontext(uow),
        execution=PublicExecution(),
        input_builder=None,
        authorizer=SimpleNamespace(
            require=lambda *args: state.calls.append(("require", *args))
        ),
        workspace_id=workspace_id,
        execution_id=execution_id,
        owner_check=lambda: state.owner,
    )
    state.handle = AdapterSessionHandle(
        session.id,
        session.adapter_session_id,
        "agent-run-reference",
        module.native_capabilities(),
        1,
        now,
    )
    return state


def test_ordinary_native_stop_queries_only_the_saved_original(ordinary_stop):
    s = ordinary_stop
    result = s.adapter.get_stop(s.handle, operation_id=s.operation.operation_id)
    assert result.outcome.value == "delivered" and result.accepted_sequence == 2
    assert s.calls == [
        ("require", s.link.authority_id, "run.read", s.link.workspace_id),
        (
            "get_stop",
            s.link.workspace_id,
            s.link.run_id,
            s.operation.operation_id,
            s.link.authority_id,
        ),
        ("require", s.link.authority_id, "run.read", s.link.workspace_id),
        ("get_run", s.link.workspace_id, s.link.run_id, s.link.authority_id),
    ]


@pytest.mark.parametrize("kind", ("stop", "query"))
@pytest.mark.parametrize(
    "bad",
    (
        "running",
        "input_open",
        "active_response",
        "old_watermark",
        "wrong_run",
        "wrong_profile",
        "wrong_workspace",
        "receipt_identity",
        "malformed_receipt",
    ),
)
def test_native_stop_requires_exact_positive_closed_proof(ordinary_stop, kind, bad):
    from sidera_agents.contracts import RunSnapshot

    s = ordinary_stop
    changes = {
        "running": {"state": "running"},
        "input_open": {"input_open": True},
        "active_response": {"active_response_id": uuid4()},
        "old_watermark": {"last_event_sequence": 1},
        "wrong_run": {"run_id": uuid4()},
        "wrong_profile": {
            "profile_ref": s.link.profile_ref.model_copy(update={"revision": 2})
        },
        "wrong_workspace": {"workspace_id": uuid4()},
    }
    if bad == "receipt_identity":
        s.receipt = s.receipt.model_copy(update={"operation_id": uuid4()})
    elif bad == "malformed_receipt":
        s.receipt = {"status": "stopped", "private": "credential"}
    else:
        s.snapshot = RunSnapshot.model_validate(
            {**s.snapshot.model_dump(), **changes[bad]}
        )
    method = s.adapter.stop_session if kind == "stop" else s.adapter.get_stop
    result = method(s.handle, operation_id=s.operation.operation_id)
    assert result.outcome.value == "unknown" and result.accepted_sequence is None
    assert "credential" not in repr(result)


@pytest.mark.parametrize("status", ("accepted", "unknown", "failed"))
def test_native_stop_query_does_not_promote_pending_or_failed_receipts(
    ordinary_stop, status
):
    from sidera_agents.contracts import StopReceipt

    s = ordinary_stop
    s.receipt = StopReceipt.model_validate(
        {**s.receipt.model_dump(), "status": status, "evidence_sequence": None}
    )
    result = s.adapter.get_stop(s.handle, operation_id=s.operation.operation_id)
    assert result.outcome.value == ("failed" if status == "failed" else "unknown")
    assert result.accepted_sequence is None
    assert not any(call[0] in {"stop", "get_run"} for call in s.calls)


@pytest.mark.parametrize("bad", ("missing", "timeout", "read_failure"))
def test_native_stop_query_failures_are_safe_unconfirmed_reads(ordinary_stop, bad):
    s = ordinary_stop
    s.query_error = {
        "missing": AgentError("not_found", "PRIVATE credential"),
        "timeout": TimeoutError("PRIVATE credential"),
        "read_failure": RuntimeError("PRIVATE credential"),
    }[bad]
    result = s.adapter.get_stop(s.handle, operation_id=s.operation.operation_id)
    assert result.outcome.value == "unknown" and result.accepted_sequence is None
    assert "PRIVATE" not in repr(result)


@pytest.mark.parametrize(
    "field",
    (
        "operation_id",
        "workspace_id",
        "execution_id",
        "session_id",
        "lease_id",
        "fencing_token",
        "module_instance_id",
        "config_version",
        "operation_type",
    ),
)
def test_native_stop_query_rejects_changed_saved_operation_before_public_call(
    ordinary_stop, field
):
    from sidera_control_plane.domain.gateway import GatewayOperationType

    s = ordinary_stop
    setattr(
        s.operation,
        field,
        {
            "fencing_token": 8,
            "config_version": 4,
            "operation_type": GatewayOperationType.START_SESSION,
        }.get(field, uuid4()),
    )
    with pytest.raises(AgentError) as denied:
        s.adapter.get_stop(s.handle, operation_id=s.operation.operation_id)
    assert denied.value.code == "permission_denied" and not s.calls


def test_native_stop_query_owner_loss_after_lookup_cannot_confirm(ordinary_stop):
    s = ordinary_stop
    original = s.adapter._execution.get_stop

    def lose_owner(*args, **kwargs):
        receipt = original(*args, **kwargs)
        s.owner = False
        return receipt

    s.adapter._execution.get_stop = lose_owner
    with pytest.raises(AgentError) as denied:
        s.adapter.get_stop(s.handle, operation_id=s.operation.operation_id)
    assert denied.value.code == "gateway_owner_lost"
    assert not any(call[0] == "get_run" for call in s.calls)
