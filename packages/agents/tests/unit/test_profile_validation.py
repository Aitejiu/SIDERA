from dataclasses import replace
from uuid import uuid4

import pytest
from pydantic import SecretStr

from sidera_agents.contracts import (
    AuthenticationState,
    CapabilityResourceDraft,
    CapabilitySupport,
    ConfigValidation,
    CredentialRef,
    ProfileDraft,
    ProfileRef,
    RuntimeRef,
    SecretBinding,
    SupportStatus,
    ToolBinding,
    ToolConnection,
)
from sidera_agents.errors import AgentError
from sidera_agents.harnesses import opencode_descriptor
from sidera_agents.ports import RuntimeObservation


def _register_runtime(runtime_lab, monkeypatch):
    monkeypatch.setattr(opencode_descriptor, "read_version", lambda _: "1.4.2")
    return runtime_lab.runtimes.register(
        runtime_lab.workspace_id,
        "opencode",
        str(runtime_lab.executable),
        max_active=2,
        max_sessions=4,
        grant_id=runtime_lab.grant_id,
    )


def _profile_draft(runtime_ref, **changes):
    values = {
        "name": "reviewer",
        "description": "Reviews changes",
        "runtime_ref": runtime_ref,
        "instructions": "Review the change carefully.",
        "resource_refs": (),
        "model_id": None,
        "thinking": None,
        "max_active": 1,
        "environment": {},
        "secret_refs": (),
        "native_settings": {},
    }
    values.update(changes)
    return ProfileDraft(**values)


def _create_profile(runtime_lab, runtime_ref, **changes):
    return runtime_lab.catalog.create_profile(
        runtime_lab.workspace_id,
        _profile_draft(runtime_ref, **changes),
        grant_id=runtime_lab.grant_id,
    )


def test_model_evidence_is_empty_and_selected_model_never_becomes_observed(
    runtime_lab, monkeypatch
):
    runtime = _register_runtime(runtime_lab, monkeypatch)
    descriptor = runtime_lab.runtimes.describe(
        "opencode", grant_id=runtime_lab.grant_id
    )
    profile = _create_profile(
        runtime_lab, runtime.ref, model_id="unverified/provider-model"
    )
    calls = []
    monkeypatch.setattr(
        opencode_descriptor,
        "read_version",
        lambda executable: calls.append(executable) or "unexpected",
    )

    validation = runtime_lab.runtimes.validate_profile(
        profile.ref, grant_id=runtime_lab.grant_id
    )

    assert descriptor.model_options == ()
    assert descriptor.thinking_options == ()
    assert validation.valid is True
    assert not any(issue.field == "model_id" for issue in validation.issues)
    assert (
        next(
            field for field in descriptor.fields if field.key == "model_id"
        ).support.status
        is SupportStatus.SUPPORTED
    )
    assert validation.effective_preview.selected_model == "unverified/provider-model"
    assert validation.effective_preview.observed_model is None
    assert validation.effective_preview.applied_fields == ()
    assert runtime.auth_state is AuthenticationState.UNCONFIRMED
    assert calls == []


def test_inherited_model_and_thinking_remain_none_and_auth_is_unconfirmed(
    runtime_lab, monkeypatch
):
    runtime = _register_runtime(runtime_lab, monkeypatch)
    profile = _create_profile(runtime_lab, runtime.ref)

    validation = runtime_lab.runtimes.validate_profile(
        profile.ref, grant_id=runtime_lab.grant_id
    )

    assert validation.effective_preview.selected_model is None
    assert validation.effective_preview.observed_model is None
    assert validation.effective_preview.inherited_fields == ("model_id", "thinking")
    assert validation.valid is True
    auth_issue = next(
        issue
        for issue in validation.issues
        if issue.code == "authentication_unconfirmed"
    )
    assert auth_issue.support.status == "unconfirmed"
    assert not any(
        issue.code.startswith("runtime_protocol_") for issue in validation.issues
    )


def test_unsupported_native_options_remain_saveable_but_not_executable(
    runtime_lab, monkeypatch
):
    runtime = _register_runtime(runtime_lab, monkeypatch)
    profile = _create_profile(
        runtime_lab,
        runtime.ref,
        native_settings={"temperature": 0.2},
        thinking="extended",
    )

    validation = runtime_lab.runtimes.validate_profile(
        profile.ref, grant_id=runtime_lab.grant_id
    )

    assert profile.draft.native_settings == {"temperature": 0.2}
    assert validation.valid is False
    assert {issue.field for issue in validation.issues} >= {
        "native_settings.temperature",
        "thinking",
    }


@pytest.mark.parametrize("mode", ["none", "read", "write"])
def test_descriptor_accepts_declared_native_tool_mode(runtime_lab, monkeypatch, mode):
    runtime = _register_runtime(runtime_lab, monkeypatch)
    profile = _create_profile(
        runtime_lab, runtime.ref, native_settings={"native_tools": mode}
    )
    descriptor = runtime_lab.runtimes.describe(
        "opencode", grant_id=runtime_lab.grant_id
    )
    field = next(
        field
        for field in descriptor.fields
        if field.key == "native_settings.native_tools"
    )
    assert field.kind.value == "select"
    assert field.options == ("none", "read", "write")
    assert field.support.status is SupportStatus.SUPPORTED
    assert runtime_lab.runtimes.validate_profile(
        profile.ref, grant_id=runtime_lab.grant_id
    ).valid


def test_descriptor_rejects_unknown_native_tool_mode(runtime_lab, monkeypatch):
    runtime = _register_runtime(runtime_lab, monkeypatch)
    profile = _create_profile(
        runtime_lab, runtime.ref, native_settings={"native_tools": "all"}
    )
    validation = runtime_lab.runtimes.validate_profile(
        profile.ref, grant_id=runtime_lab.grant_id
    )
    assert not validation.valid
    assert any(
        issue.field == "native_settings.native_tools"
        and issue.code == "configuration_invalid"
        for issue in validation.issues
    )


def test_harness_validation_returns_config_validation_contract(
    runtime_lab, monkeypatch
):
    runtime = _register_runtime(runtime_lab, monkeypatch)
    profile = _create_profile(
        runtime_lab, runtime.ref, model_id="unverified/provider-model"
    )
    harness = runtime_lab.registry.get("opencode")
    runtime_snapshot = runtime_lab.runtimes.get_runtime(
        runtime.ref, grant_id=runtime_lab.grant_id
    )

    descriptor_validation = opencode_descriptor.OpenCodeDescriptor().validate(
        profile, runtime_snapshot
    )
    local = harness.validate(profile, runtime_snapshot)
    aggregate = runtime_lab.runtimes.validate_profile(
        profile.ref, grant_id=runtime_lab.grant_id
    )

    assert isinstance(descriptor_validation, ConfigValidation)
    assert descriptor_validation.effective_preview is None
    assert isinstance(local, ConfigValidation)
    assert local.effective_preview is None
    assert local.valid
    assert not any(issue.field == "model_id" for issue in local.issues)
    assert {issue.field for issue in aggregate.issues} >= {
        issue.field for issue in local.issues
    }
    assert aggregate.effective_preview is not None
    assert aggregate.effective_preview.selected_model == "unverified/provider-model"


@pytest.mark.parametrize("destination", ["OPENCODE_API_KEY", "CUSTOM_PROVIDER_API_KEY"])
def test_provider_env_accepts_only_declared_profile_secret_binding(
    runtime_lab, monkeypatch, destination
):
    runtime = _register_runtime(runtime_lab, monkeypatch)
    binding = SecretBinding(
        credential_ref=CredentialRef(
            workspace_id=runtime_lab.workspace_id, credential_id=uuid4()
        ),
        destination=destination,
    )
    profile = _create_profile(
        runtime_lab,
        runtime.ref,
        model_id="opencode-go/deepseek-v4.1-flash",
        secret_refs=(binding,),
        native_settings={"provider_env": destination},
    )
    validation = runtime_lab.runtimes.validate_profile(
        profile.ref, grant_id=runtime_lab.grant_id
    )
    assert validation.valid
    assert validation.effective_preview.observed_model is None
    assert runtime.auth_state is AuthenticationState.UNCONFIRMED
    assert any(
        issue.code == "authentication_unconfirmed" for issue in validation.issues
    )


@pytest.mark.parametrize(
    "model,destination,selected",
    [
        (None, "OPENCODE_API_KEY", "OPENCODE_API_KEY"),
        ("opencode-go/model", "BOUND_API_KEY", "OTHER_API_KEY"),
        (
            "opencode-go/model",
            "OPENCODE_EXPERIMENTAL_API_KEY",
            "OPENCODE_EXPERIMENTAL_API_KEY",
        ),
    ],
)
def test_provider_env_binding_mismatch_is_safe_and_blocks_execution(
    runtime_lab, monkeypatch, model, destination, selected
):
    runtime = _register_runtime(runtime_lab, monkeypatch)
    binding = SecretBinding(
        credential_ref=CredentialRef(
            workspace_id=runtime_lab.workspace_id, credential_id=uuid4()
        ),
        destination=destination,
    )
    profile = _create_profile(
        runtime_lab,
        runtime.ref,
        model_id=model,
        secret_refs=(binding,),
        native_settings={"provider_env": selected},
    )
    validation = runtime_lab.runtimes.validate_profile(
        profile.ref, grant_id=runtime_lab.grant_id
    )
    assert not validation.valid
    assert any(
        issue.field == "native_settings.provider_env"
        and issue.code == "configuration_invalid"
        for issue in validation.issues
    )


@pytest.mark.parametrize("failure", ["revoked", "failed", "plain"])
def test_secret_reader_failure_prevents_native_open(admission_lab, failure):
    lab = admission_lab
    binding = SecretBinding(
        credential_ref=CredentialRef(
            workspace_id=lab.workspace_id, credential_id=uuid4()
        ),
        destination="OPENCODE_API_KEY",
    )
    profile = _create_profile(
        lab, lab.runtime.ref, model_id="opencode-go/model", secret_refs=(binding,)
    )

    class Secrets:
        def read(self, grant_id, selected):
            assert grant_id == lab.grant_id and selected == binding
            if failure == "revoked":
                raise AgentError("permission_denied", "N09_FAKE_PRIVATE_READER_REASON")
            if failure == "failed":
                raise RuntimeError("N09_FAKE_PRIVATE_READER_REASON")
            return "N09_FAKE_PRIVATE_READER_REASON"

    lab.execution._secrets = Secrets()
    start = lab.execution.start(lab.request(profile_ref=profile.ref))
    lab.pump()
    assert lab.native.open_calls == []
    snapshot = lab.execution.get_run(
        lab.workspace_id, start.run_id, grant_id=lab.grant_id
    )
    assert snapshot.state.value == "failed"
    assert snapshot.reason_code == (
        "permission_denied" if failure == "revoked" else "secret_unavailable"
    )
    assert "N09_FAKE_PRIVATE_READER_REASON" not in snapshot.model_dump_json()


def test_reserved_environment_fields_are_reported_for_saved_legacy_profile(
    runtime_lab, monkeypatch
):
    runtime = _register_runtime(runtime_lab, monkeypatch)
    draft = _profile_draft(runtime.ref, environment={"PATH": "/tmp/tools"})
    saved = runtime_lab.store.create_profile(
        runtime_lab.workspace_id,
        uuid4(),
        draft,
        "sha256:" + "a" * 64,
        runtime_lab.clock(),
    )

    validation = runtime_lab.runtimes.validate_profile(
        saved.ref, grant_id=runtime_lab.grant_id
    )

    assert any(
        issue.field == "environment.PATH" and issue.code == "reserved_environment_field"
        for issue in validation.issues
    )


@pytest.mark.parametrize(
    ("kind", "settings", "expected_status"),
    [
        ("skill", {"body": "Review the code."}, "unconfirmed"),
        (
            "skill",
            {
                "source_ref": {
                    "provider": "local-skills",
                    "key": "review-v1",
                    "version": "v1",
                },
                "content_hash": "sha256:" + "a" * 64,
            },
            "unconfirmed",
        ),
        (
            "mcp",
            {"connection_id": "20000000-0000-0000-0000-000000000001"},
            "supported",
        ),
        (
            "mcp",
            {"transport": "stdio", "command": ["/opt/tools/review-mcp"]},
            "supported",
        ),
        (
            "mcp",
            {"transport": "http", "url": "https://mcp.example.invalid/v1"},
            "unsupported",
        ),
        (
            "integration",
            {"connection_id": "20000000-0000-0000-0000-000000000002"},
            "unsupported",
        ),
    ],
)
def test_pinned_resource_revisions_have_explicit_runtime_support(
    runtime_lab, monkeypatch, kind, settings, expected_status
):
    runtime = _register_runtime(runtime_lab, monkeypatch)
    resource = runtime_lab.catalog.create_resource(
        runtime_lab.workspace_id,
        CapabilityResourceDraft(
            name=f"{kind}-resource",
            kind=kind,
            settings=settings,
            secret_refs=(),
        ),
        grant_id=runtime_lab.grant_id,
    )
    profile = _create_profile(runtime_lab, runtime.ref, resource_refs=(resource.ref,))

    validation = runtime_lab.runtimes.validate_profile(
        profile.ref, grant_id=runtime_lab.grant_id
    )

    issue_suffix = (
        ".mcp_http"
        if kind == "mcp" and settings.get("transport") == "http"
        else f".{kind}"
    )
    resource_issues = tuple(
        issue for issue in validation.issues if issue.field.endswith(issue_suffix)
    )
    if expected_status == "supported":
        assert resource_issues == ()
    else:
        assert len(resource_issues) == 1
        assert resource_issues[0].support.status == expected_status
    if expected_status == "unsupported":
        assert validation.valid is False
    assert resource.ref in validation.effective_preview.resource_refs
    assert not any(
        action == "connection.resolve" for _, action, _ in runtime_lab.authorizer.calls
    )


def test_disabled_profile_resource_and_runtime_are_each_reported(
    runtime_lab, monkeypatch
):
    runtime = _register_runtime(runtime_lab, monkeypatch)
    resource = runtime_lab.catalog.create_resource(
        runtime_lab.workspace_id,
        runtime_lab.skill_draft,
        grant_id=runtime_lab.grant_id,
    )
    profile = _create_profile(runtime_lab, runtime.ref, resource_refs=(resource.ref,))
    runtime_lab.catalog.set_profile_enabled(
        runtime_lab.workspace_id,
        profile.ref.profile_id,
        False,
        expected_version=1,
        grant_id=runtime_lab.grant_id,
    )
    runtime_lab.catalog.set_resource_enabled(
        runtime_lab.workspace_id,
        resource.ref.resource_id,
        False,
        expected_version=1,
        grant_id=runtime_lab.grant_id,
    )
    runtime_lab.runtimes.set_runtime_enabled(
        runtime.ref,
        False,
        expected_version=1,
        grant_id=runtime_lab.admin_grant_id,
    )

    validation = runtime_lab.runtimes.validate_profile(
        profile.ref, grant_id=runtime_lab.grant_id
    )

    assert {issue.code for issue in validation.issues} >= {
        "profile_disabled",
        "resource_disabled",
        "runtime_disabled",
    }


def test_http_mcp_requires_its_own_negotiated_capability(runtime_lab, monkeypatch):
    runtime = _register_runtime(runtime_lab, monkeypatch)
    entry = runtime_lab.store.get_runtime_entry(runtime.ref)
    probe = entry.record.observation.probe.model_copy(
        update={
            "protocol_state": "ready",
            "capabilities": {
                "acp": CapabilitySupport(
                    status=SupportStatus.SUPPORTED,
                    reason_code="acp_v1_initialized",
                    constraints={},
                ),
                "mcp": CapabilitySupport(
                    status=SupportStatus.SUPPORTED,
                    reason_code="acp_v1_stdio",
                    constraints={},
                ),
                "mcp_http": CapabilitySupport(
                    status=SupportStatus.UNSUPPORTED,
                    reason_code="http_not_advertised",
                    constraints={},
                ),
            },
        }
    )
    runtime_lab.store.update_runtime_observation(
        runtime.ref,
        RuntimeObservation(
            probe=probe,
            installed=entry.record.observation.installed,
            file_identity=entry.record.observation.file_identity,
        ),
    )
    resource = runtime_lab.catalog.create_resource(
        runtime_lab.workspace_id,
        CapabilityResourceDraft(
            name="remote-tools",
            kind="mcp",
            settings={"transport": "http", "url": "https://mcp.example.invalid/v1"},
            secret_refs=(),
        ),
        grant_id=runtime_lab.grant_id,
    )
    profile = _create_profile(runtime_lab, runtime.ref, resource_refs=(resource.ref,))

    validation = runtime_lab.runtimes.validate_profile(
        profile.ref, grant_id=runtime_lab.grant_id
    )

    issue = next(
        issue for issue in validation.issues if issue.field.endswith(".mcp_http")
    )
    assert issue.support.status is SupportStatus.UNSUPPORTED
    assert issue.support.reason_code == "http_not_advertised"
    assert validation.valid is False


def _tool_projection_profile(lab):
    binding = SecretBinding(
        credential_ref=CredentialRef(
            workspace_id=lab.workspace_id, credential_id=uuid4()
        ),
        destination="PROFILE_TOKEN",
    )
    return _create_profile(lab, lab.runtime.ref, secret_refs=(binding,)), binding


def _instrument_tool_projection(lab, *, deny_tool_execute):
    class SecretReader:
        def __init__(self):
            self.calls = []

        def read(self, grant_id, binding):
            self.calls.append((grant_id, binding))
            return SecretStr("authorized-profile-secret")

    class ConnectionReader:
        def __init__(self):
            self.calls = []
            self.connection = ToolConnection(
                name="task-bridge",
                transport="stdio",
                endpoint_or_argv=("/synthetic/tool",),
                headers={},
                environment={"TOOL_TOKEN": SecretStr("scoped-tool-secret")},
            )

        def resolve(self, grant_id, connection_id):
            self.calls.append((grant_id, connection_id))
            return self.connection

    class Authorizer:
        def __init__(self, delegate):
            self.delegate = delegate
            self.tool_targets = []

        def require(self, grant_id, action, target):
            if action == "tool.execute":
                self.tool_targets.append(target)
                if deny_tool_execute:
                    raise AgentError("permission_denied", "Access denied.")
            self.delegate.require(grant_id, action, target)

    secrets = SecretReader()
    connections = ConnectionReader()
    authorizer = Authorizer(lab.execution._authorizer)
    resource_reads = []
    get_resource = lab.store.get_resource

    def read_resource(ref):
        resource_reads.append(ref)
        return get_resource(ref)

    lab.execution._secrets = secrets
    lab.execution._tool_connections = connections
    lab.execution._authorizer = authorizer
    lab.store.get_resource = read_resource
    return secrets, connections, authorizer, resource_reads


def test_tool_execute_denial_precedes_connection_and_secret_resolution(admission_lab):
    lab = admission_lab
    profile, _ = _tool_projection_profile(lab)
    secrets, connections, authorizer, resource_reads = _instrument_tool_projection(
        lab, deny_tool_execute=True
    )
    binding = ToolBinding(connection_id=uuid4())
    receipt = lab.execution.start(
        lab.request(profile_ref=profile.ref, tools=(binding,))
    )

    assert receipt.status.value == "accepted"
    lab.pump()

    assert authorizer.tool_targets == [binding]
    assert connections.calls == []
    assert resource_reads == []
    assert secrets.calls == []
    assert lab.native.open_calls == []


def test_tool_execute_grant_authorizes_exact_binding_before_projection(admission_lab):
    lab = admission_lab
    profile, secret_binding = _tool_projection_profile(lab)
    secrets, connections, authorizer, resource_reads = _instrument_tool_projection(
        lab, deny_tool_execute=False
    )
    connection_id = uuid4()
    binding = ToolBinding(connection_id=connection_id)
    receipt = lab.execution.start(
        lab.request(profile_ref=profile.ref, tools=(binding,))
    )

    assert receipt.status.value == "accepted"
    lab.pump()

    assert authorizer.tool_targets == [binding]
    assert connections.calls == [(lab.grant_id, connection_id)]
    assert resource_reads == []
    assert secrets.calls == [(lab.grant_id, secret_binding)]
    assert len(lab.native.open_calls) == 1
    open_request = lab.native.open_calls[0]
    assert open_request.tools == (connections.connection,)
    assert (
        open_request.environment["PROFILE_TOKEN"].get_secret_value()
        == "authorized-profile-secret"
    )


def test_missing_runtime_and_cross_workspace_profile_reads_are_safe(
    runtime_lab, monkeypatch
):
    missing_ref = RuntimeRef(workspace_id=runtime_lab.workspace_id, runtime_id=uuid4())
    profile = _create_profile(runtime_lab, missing_ref)

    validation = runtime_lab.runtimes.validate_profile(
        profile.ref, grant_id=runtime_lab.grant_id
    )

    assert any(issue.code == "runtime_unavailable" for issue in validation.issues)
    other_workspace = uuid4()
    other_profile = ProfileRef(
        workspace_id=other_workspace, profile_id=profile.ref.profile_id, revision=1
    )
    with pytest.raises(AgentError) as error:
        runtime_lab.runtimes.validate_profile(
            other_profile, grant_id=runtime_lab.grant_id
        )
    assert error.value.code == "permission_denied"


def test_unavailable_runtime_blocks_profile_readiness(runtime_lab, monkeypatch):
    runtime = _register_runtime(runtime_lab, monkeypatch)
    profile = _create_profile(runtime_lab, runtime.ref)
    moved = runtime_lab.executable.with_name("moved-opencode")
    runtime_lab.executable.rename(moved)
    runtime_lab.runtimes.refresh(runtime.ref, grant_id=runtime_lab.grant_id)

    validation = runtime_lab.runtimes.validate_profile(
        profile.ref, grant_id=runtime_lab.grant_id
    )

    assert validation.valid is False
    assert any(issue.code == "runtime_unavailable" for issue in validation.issues)
    assert any(
        issue.code == "runtime_protocol_unavailable" for issue in validation.issues
    )


def test_failed_authentication_blocks_profile_readiness(runtime_lab, monkeypatch):
    runtime = _register_runtime(runtime_lab, monkeypatch)
    profile = _create_profile(runtime_lab, runtime.ref)
    entry = runtime_lab.store.get_runtime_entry(runtime.ref)
    failed_observation = replace(
        entry.record.observation,
        auth_state=AuthenticationState.FAILED,
    )
    runtime_lab.store.update_runtime_observation(runtime.ref, failed_observation)

    validation = runtime_lab.runtimes.validate_profile(
        profile.ref, grant_id=runtime_lab.grant_id
    )

    assert validation.valid is False
    assert any(issue.code == "authentication_failed" for issue in validation.issues)
