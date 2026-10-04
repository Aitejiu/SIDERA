import importlib
import importlib.util
from contextlib import nullcontext
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
