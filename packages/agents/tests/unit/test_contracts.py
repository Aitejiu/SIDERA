from datetime import UTC, datetime, timedelta, timezone
from inspect import Parameter, signature
from typing import get_type_hints
from uuid import uuid4

import pytest
from pydantic import SecretStr, ValidationError

import sidera_agents
from sidera_agents.api import (
    AgentCatalog,
    AgentExecution,
    AgentObservations,
    AgentRuntimes,
)
from sidera_agents.contracts import (
    CapabilityResourceDraft,
    ContextItem,
    ContractModel,
    CredentialRef,
    ExecutionWorkspace,
    NativeCancelOutcome,
    NativeCloseOutcome,
    NativeEvent,
    NativeHandle,
    NativeInspection,
    NativeOpenRequest,
    NativeOutcome,
    ProcessIdentity,
    ProfileDraft,
    ProfileRef,
    ProfileRevision,
    RunStart,
    RuntimeProbe,
    RuntimeRef,
    RuntimeSnapshot,
    SecretBinding,
    SourceRef,
)
from sidera_agents.errors import AgentError
from sidera_agents.ports import (
    DenyContextSourceReader,
    DenyExecutionAuthorizer,
    DenySecretReader,
    DenyToolConnectionReader,
    ToolConnection,
)


def _profile_draft(**overrides: object) -> ProfileDraft:
    fields: dict[str, object] = {
        "name": "writer",
        "description": "Writing assistant",
        "runtime_ref": RuntimeRef(workspace_id=uuid4(), runtime_id=uuid4()),
        "instructions": "Write clearly.",
        "resource_refs": (),
        "model_id": None,
        "thinking": None,
        "max_active": 1,
        "environment": {},
        "secret_refs": (),
        "native_settings": {},
    }
    fields.update(overrides)
    return ProfileDraft(**fields)


def _run_start(**overrides: object) -> RunStart:
    fields: dict[str, object] = {
        "contract_version": 1,
        "operation_id": uuid4(),
        "profile_ref": ProfileRef(workspace_id=uuid4(), profile_id=uuid4(), revision=1),
        "grant_id": uuid4(),
        "policy": "bounded",
        "instructions": ("Complete the requested work.",),
        "text": "Start.",
        "context": (),
        "tools": (),
        "workspace": ExecutionWorkspace(
            directory="/workspace", writable=True, artifact_directory=None
        ),
        "required_capabilities": (),
    }
    fields.update(overrides)
    return RunStart(**fields)


def _runtime_snapshot() -> RuntimeSnapshot:
    return RuntimeSnapshot(
        ref=RuntimeRef(workspace_id=uuid4(), runtime_id=uuid4()),
        harness_id="opencode",
        executable="opencode",
        actual_version="1.0.0",
        installed=True,
        enabled=True,
        version=1,
        protocol_state="ready",
        auth_state="unconfirmed",
        capabilities={},
        checked_at=datetime(2026, 10, 1, tzinfo=UTC),
        limits={},
    )


def test_profile_ref_rejects_role_and_invalid_revision() -> None:
    base = {"workspace_id": uuid4(), "profile_id": uuid4()}

    with pytest.raises(ValidationError):
        ProfileRef(**base, revision=0)
    with pytest.raises(ValidationError):
        ProfileRef(**base, revision=True)
    with pytest.raises(ValidationError):
        ProfileRef(**base, revision=1, role="worker")


def test_profile_ref_requires_valid_uuids() -> None:
    with pytest.raises(ValidationError):
        ProfileRef(workspace_id="not-a-uuid", profile_id=uuid4(), revision=1)


def test_run_start_rejects_unknown_and_boolean_contract_versions() -> None:
    fields = _run_start().model_dump()

    with pytest.raises(ValidationError):
        RunStart(**{**fields, "contract_version": 2})
    with pytest.raises(ValidationError):
        RunStart(**{**fields, "contract_version": True})


def test_context_item_requires_exactly_one_text_or_source_reference() -> None:
    fields = {
        "item_id": "task-goal",
        "origin": "task",
        "required": True,
        "ordinal": 0,
        "version": "v1",
        "content_hash": "sha256:abc",
    }

    with pytest.raises(ValidationError):
        ContextItem(**fields, text=None, source_ref=None)
    with pytest.raises(ValidationError):
        ContextItem(
            **fields,
            text="inline text",
            source_ref=SourceRef(provider="knowledge", key="goal", version="v1"),
        )
    with pytest.raises(ValidationError):
        ContextItem(
            **{**fields, "version": "v2"},
            text=None,
            source_ref=SourceRef(provider="knowledge", key="goal", version="v1"),
        )


def test_profile_revision_requires_utc_aware_timestamp() -> None:
    with pytest.raises(ValidationError):
        ProfileRevision(
            ref=ProfileRef(workspace_id=uuid4(), profile_id=uuid4(), revision=1),
            draft=_profile_draft(),
            config_hash="sha256:abc",
            created_at=datetime.fromisoformat("2026-10-01T00:00:00"),
        )

    with pytest.raises(ValidationError):
        ProfileRevision(
            ref=ProfileRef(workspace_id=uuid4(), profile_id=uuid4(), revision=1),
            draft=_profile_draft(),
            config_hash="sha256:abc",
            created_at=datetime(2026, 10, 1, 10, tzinfo=timezone(timedelta(hours=2))),
        )


def test_resource_draft_rejects_unsupported_kind() -> None:
    with pytest.raises(ValidationError):
        CapabilityResourceDraft(
            name="Unknown",
            kind="agent",
            settings={},
            secret_refs=(),
        )


def test_nested_json_inputs_are_copied_and_canonicalized() -> None:
    nested = {"zeta": [1, {"omega": True}], "alpha": {"beta": "value"}}
    draft = CapabilityResourceDraft(
        name="Knowledge",
        kind="skill",
        settings=nested,
        secret_refs=(),
    )

    nested["zeta"][1]["omega"] = False
    nested["alpha"]["beta"] = "changed"

    assert list(draft.settings) == ["alpha", "zeta"]
    assert draft.settings["zeta"][1]["omega"] is True
    assert draft.settings["alpha"]["beta"] == "value"


def test_tool_connection_redacts_secret_values_from_repr_and_serialization() -> None:
    marker = "do-not-print-this-token"
    connection = ToolConnection(
        name="task-tools",
        transport="streamable_http",
        endpoint_or_argv="http://127.0.0.1:9000/mcp",
        headers={"Authorization": SecretStr(marker)},
        environment={"TASK_TOKEN": SecretStr(marker)},
    )

    assert marker not in repr(connection)
    assert marker not in str(connection.model_dump())
    assert marker not in connection.model_dump_json()


def test_public_contract_exports_are_frozen_and_forbid_extra_fields() -> None:
    models = [
        value
        for name in sidera_agents.__all__
        if isinstance((value := getattr(sidera_agents, name)), type)
        and issubclass(value, ContractModel)
    ]
    assert models

    for model in models:
        assert model.model_config["frozen"] is True
        assert model.model_config["extra"] == "forbid"
        with pytest.raises(ValidationError) as error:
            model.model_validate({"unexpected_contract_field": "value"})
        assert any(
            item["loc"] == ("unexpected_contract_field",)
            and item["type"] == "extra_forbidden"
            for item in error.value.errors()
        )


def test_public_dtos_do_not_accept_secret_values() -> None:
    binding = SecretBinding(
        credential_ref=CredentialRef(workspace_id=uuid4(), credential_id=uuid4()),
        destination="Authorization",
    )
    draft = _profile_draft(secret_refs=(binding,))

    assert "credential_value" not in draft.model_dump()
    assert "credential_value" not in repr(draft)
    with pytest.raises(ValidationError):
        SecretBinding(
            credential_ref=binding.credential_ref,
            destination="Authorization",
            credential_value="sensitive",
        )


def test_public_api_protocol_method_names_and_parameter_modes() -> None:
    expected = {
        AgentCatalog: {
            "create_profile": ("self", "workspace_id", "draft", "grant_id"),
            "revise_profile": ("self", "ref", "draft", "grant_id"),
            "get_profile": ("self", "ref", "grant_id"),
            "list_profiles": ("self", "workspace_id", "grant_id"),
            "create_resource": ("self", "workspace_id", "draft", "grant_id"),
            "revise_resource": ("self", "ref", "draft", "grant_id"),
            "get_resource": ("self", "ref", "grant_id"),
            "list_resources": ("self", "workspace_id", "grant_id"),
            "set_profile_enabled": (
                "self",
                "workspace_id",
                "profile_id",
                "enabled",
                "expected_version",
                "grant_id",
            ),
            "set_resource_enabled": (
                "self",
                "workspace_id",
                "resource_id",
                "enabled",
                "expected_version",
                "grant_id",
            ),
        },
        AgentRuntimes: {
            "discover": ("self", "workspace_id", "grant_id"),
            "register": (
                "self",
                "workspace_id",
                "harness_id",
                "executable",
                "max_active",
                "max_sessions",
                "grant_id",
            ),
            "refresh": ("self", "ref", "grant_id"),
            "get_runtime": ("self", "ref", "grant_id"),
            "list_runtimes": ("self", "workspace_id", "grant_id"),
            "describe": ("self", "harness_id", "grant_id"),
            "validate_profile": ("self", "ref", "grant_id"),
            "set_runtime_limits": (
                "self",
                "ref",
                "max_active",
                "max_sessions",
                "expected_version",
                "grant_id",
            ),
            "set_runtime_enabled": (
                "self",
                "ref",
                "enabled",
                "expected_version",
                "grant_id",
            ),
        },
        AgentExecution: {
            "start": ("self", "request"),
            "get_start": ("self", "workspace_id", "operation_id", "grant_id"),
            "get_run": ("self", "workspace_id", "run_id", "grant_id"),
            "list_events": (
                "self",
                "workspace_id",
                "run_id",
                "after_sequence",
                "limit",
                "grant_id",
            ),
            "send_input": ("self", "request"),
            "get_input": ("self", "workspace_id", "run_id", "operation_id", "grant_id"),
            "interrupt": ("self", "request"),
            "get_interrupt": (
                "self",
                "workspace_id",
                "run_id",
                "operation_id",
                "grant_id",
            ),
            "stop": ("self", "request"),
            "get_stop": ("self", "workspace_id", "run_id", "operation_id", "grant_id"),
            "recover": ("self", "request"),
        },
        AgentObservations: {
            "list_outputs": ("self", "workspace_id", "run_id", "grant_id"),
            "read_trace": (
                "self",
                "workspace_id",
                "run_id",
                "after_sequence",
                "limit",
                "grant_id",
            ),
        },
    }

    for protocol, methods in expected.items():
        assert {
            name
            for name, value in protocol.__dict__.items()
            if callable(value) and not name.startswith("_")
        } == set(methods)
        for method_name, parameters in methods.items():
            method_signature = signature(getattr(protocol, method_name))
            assert tuple(method_signature.parameters) == parameters
            if "grant_id" in parameters:
                assert (
                    method_signature.parameters["grant_id"].kind
                    is Parameter.KEYWORD_ONLY
                )
            if "limit" in parameters:
                assert method_signature.parameters["limit"].default == 100


def test_agent_error_preserves_public_error_fields() -> None:
    error = AgentError("permission_denied", "Access denied.", field="grant_id")

    assert str(error) == "Access denied."
    assert (error.code, error.message, error.field) == (
        "permission_denied",
        "Access denied.",
        "grant_id",
    )


def test_default_ports_deny_access() -> None:
    grant_id = uuid4()
    source_ref = SourceRef(provider="knowledge", key="item", version="v1")
    binding = SecretBinding(
        credential_ref=CredentialRef(workspace_id=uuid4(), credential_id=uuid4()),
        destination="Authorization",
    )
    connection_id = uuid4()

    calls = (
        lambda: DenyExecutionAuthorizer().require(grant_id, "read", source_ref),
        lambda: DenyContextSourceReader().read(grant_id, source_ref),
        lambda: DenySecretReader().read(grant_id, binding),
        lambda: DenyToolConnectionReader().resolve(grant_id, connection_id),
    )
    for call in calls:
        with pytest.raises(AgentError) as error:
            call()
        assert error.value.code == "permission_denied"


def test_native_adapter_contract_is_internal_and_has_fixed_methods() -> None:
    from sidera_agents.contracts import HarnessAdapter

    expected = {
        "describe": ("self",),
        "probe": ("self", "candidate"),
        "validate": ("self", "profile", "runtime"),
        "open": ("self", "request"),
        "prompt": ("self", "handle", "input", "response_id"),
        "read_events": ("self", "handle", "after_cursor", "limit"),
        "cancel": ("self", "handle", "response_id"),
        "inspect": ("self", "handle"),
        "close": ("self", "handle"),
    }
    assert {
        name
        for name, value in HarnessAdapter.__dict__.items()
        if callable(value) and not name.startswith("_")
    } == set(expected)
    for method_name, parameters in expected.items():
        method_signature = signature(getattr(HarnessAdapter, method_name))
        assert tuple(method_signature.parameters) == parameters
    assert signature(HarnessAdapter.read_events).parameters["limit"].default == 100

    internal_names = (
        "AssembledInput",
        "HarnessAdapter",
        "NativeCancelOutcome",
        "NativeCloseOutcome",
        "NativeEvent",
        "NativeHandle",
        "NativeInspection",
        "NativeOpenRequest",
        "NativeOutcome",
        "ProcessIdentity",
        "RuntimeProbe",
    )
    assert not set(internal_names).intersection(sidera_agents.__all__)
    assert all(not hasattr(sidera_agents, name) for name in internal_names)
    assert get_type_hints(HarnessAdapter.read_events)["limit"] == int


def test_native_dtos_keep_unknown_states_and_separate_cursors() -> None:
    response_id = uuid4()
    event = NativeEvent(
        cursor=7,
        native_id="event-7",
        kind="response.completed",
        response_id=response_id,
        operation_id=None,
        occurred_at=datetime(2026, 10, 1, tzinfo=UTC),
        data={},
    )
    outcome = NativeOutcome(
        response_id=response_id,
        status="unknown",
        reason_code=None,
        observed_model=None,
        usage=None,
    )
    cancel = NativeCancelOutcome(
        response_id=response_id, status="unknown", reason_code=None
    )
    inspection = NativeInspection(
        process_state="unknown", response_state="unknown", reason_code=None
    )
    close = NativeCloseOutcome(status="unknown", reason_code=None)

    assert event.cursor == 7
    assert "sequence" not in NativeEvent.model_fields
    assert "auth_state" not in RuntimeProbe.model_fields
    assert outcome.status.value == "unknown"
    assert cancel.status.value == "unknown"
    assert inspection.process_state.value == "unknown"
    assert inspection.response_state.value == "unknown"
    assert close.status.value == "unknown"


def test_native_process_identity_is_bound_to_pid_start_and_owned_nonce() -> None:
    identity = ProcessIdentity(
        pid=412,
        start_identity="started-at-2026-10-01T00:00:00Z",
        owned_nonce=uuid4(),
    )
    handle = NativeHandle(
        run_id=uuid4(),
        session_id="session-1",
        process_identity=identity,
        capabilities=(),
    )

    assert handle.process_identity == identity
    with pytest.raises(ValidationError):
        ProcessIdentity(pid=0, start_identity="start", owned_nonce=uuid4())


def test_native_open_request_hides_secret_projection_from_repr_and_dump() -> None:
    marker = "native-secret-marker"
    connection = ToolConnection(
        name="authorized-tools",
        transport="streamable_http",
        endpoint_or_argv="http://127.0.0.1:9000/mcp",
        headers={"Authorization": SecretStr(marker)},
        environment={"TOOL_TOKEN": SecretStr(marker)},
    )
    request = NativeOpenRequest(
        run_id=uuid4(),
        runtime=_runtime_snapshot(),
        projected_config={"credential": SecretStr(marker)},
        workspace=ExecutionWorkspace(
            directory="/workspace", writable=True, artifact_directory=None
        ),
        tools=(connection,),
        environment={"API_TOKEN": SecretStr(marker)},
    )

    assert marker not in repr(request)
    assert marker not in repr(request.model_dump())
    assert marker not in request.model_dump_json()


def test_run_start_rejects_unexpected_role_field() -> None:
    with pytest.raises(ValidationError):
        RunStart(**_run_start().model_dump(), role="worker")
