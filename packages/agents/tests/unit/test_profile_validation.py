from dataclasses import replace
from uuid import uuid4

import pytest

from sidera_agents.contracts import (
    AuthenticationState,
    CapabilityResourceDraft,
    ProfileDraft,
    ProfileRef,
    RuntimeRef,
)
from sidera_agents.errors import AgentError
from sidera_agents.harnesses import opencode_descriptor


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

    model_issue = next(
        issue for issue in validation.issues if issue.field == "model_id"
    )
    assert descriptor.model_options == ()
    assert descriptor.thinking_options == ()
    assert validation.valid is False
    assert model_issue.code == "model_evidence_unavailable"
    assert model_issue.support.status == "unconfirmed"
    assert validation.effective_preview.selected_model == "unverified/provider-model"
    assert validation.effective_preview.observed_model is None
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
    protocol_issue = next(
        issue
        for issue in validation.issues
        if issue.code == "runtime_protocol_unconfirmed"
    )
    assert auth_issue.support.status == "unconfirmed"
    assert protocol_issue.support.status == "unconfirmed"


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
            "unconfirmed",
        ),
        (
            "mcp",
            {"transport": "stdio", "command": ["/opt/tools/review-mcp"]},
            "unconfirmed",
        ),
        (
            "mcp",
            {"transport": "http", "url": "https://mcp.example.invalid/v1"},
            "unconfirmed",
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

    resource_issue = next(
        issue for issue in validation.issues if issue.field.endswith(f".{kind}")
    )
    assert resource_issue.support.status == expected_status
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
