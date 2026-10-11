from uuid import uuid4

import pytest

from sidera_agents.contracts import CredentialRef, SecretBinding
from sidera_agents.errors import AgentError


def _resource(catalog_lab, kind, settings):
    return catalog_lab.skill_draft.model_copy(
        update={"kind": kind, "settings": settings}
    )


def _assert_invalid_resource(catalog_lab, kind, settings):
    catalog = catalog_lab.catalog
    before = catalog.list_resources(
        catalog_lab.workspace_id, grant_id=catalog_lab.grant_id
    )
    with pytest.raises(AgentError) as caught:
        catalog.create_resource(
            catalog_lab.workspace_id,
            _resource(catalog_lab, kind, settings),
            grant_id=catalog_lab.grant_id,
        )
    assert caught.value.code == "configuration_invalid"
    assert caught.value.field == "settings"
    assert (
        catalog.list_resources(catalog_lab.workspace_id, grant_id=catalog_lab.grant_id)
        == before
    )


@pytest.mark.parametrize(
    "settings",
    [
        {"body": 7},
        {"body": " "},
        {"path": "../outside/SKILL.md"},
        {"body": "ok", "download_url": "https://example.invalid/x"},
        {},
        {
            "body": "ok",
            "source_ref": {
                "provider": "local-skills",
                "key": "review",
                "version": "v1",
            },
            "content_hash": "sha256:" + "a" * 64,
        },
        {
            "source_ref": {"provider": "local-skills", "key": "review"},
            "content_hash": "sha256:" + "a" * 64,
        },
        {
            "source_ref": {
                "provider": "Local Skills",
                "key": "review",
                "version": "v1",
            },
            "content_hash": "sha256:" + "a" * 64,
        },
        {
            "source_ref": {"provider": "local-skills", "key": "review", "version": " "},
            "content_hash": "sha256:" + "a" * 64,
        },
        {
            "source_ref": {
                "provider": "local-skills",
                "key": "review",
                "version": "v1",
            },
            "content_hash": "sha256:" + "A" * 64,
        },
        {"body": "ok", "unknown": True},
    ],
)
def test_invalid_skill_is_not_saved(catalog_lab, settings):
    _assert_invalid_resource(catalog_lab, "skill", settings)


@pytest.mark.parametrize(
    "settings",
    [
        {},
        {"connection_id": "not-a-uuid"},
        {"connection_id": "20000000-0000-0000-0000-000000000001", "transport": "stdio"},
        {"connection_id": "20000000-0000-0000-0000-000000000001", "environment": {}},
        {"transport": "stdio", "command": "tool"},
        {"transport": "stdio", "command": []},
        {"transport": "stdio", "command": [1]},
        {"transport": "stdio", "command": ["relative/tool"]},
        {"transport": "stdio", "command": ["/opt/tools/../bin/tool"]},
        {"transport": "stdio", "command": ["/opt/tools/\u0000tool"]},
        {
            "transport": "stdio",
            "command": ["/opt/tools/tool"],
            "environment": {"PATH": "/tmp"},
        },
        {
            "transport": "stdio",
            "command": ["/opt/tools/tool"],
            "environment": {"API_KEY": "synthetic"},
        },
        {
            "transport": "stdio",
            "command": ["/opt/tools/tool"],
            "environment": {"LANG": 1},
        },
        {
            "transport": "stdio",
            "command": ["/opt/tools/tool"],
            "environment": {"BAD-NAME": "x"},
        },
        {
            "transport": "stdio",
            "command": ["/opt/tools/tool"],
            "environment": {"LANG": {"nested": "x"}},
        },
        {
            "transport": "stdio",
            "command": ["/opt/tools/tool"],
            "environment": {"LANG": "C"},
            "download_url": "https://example.invalid/tool",
        },
        {"transport": "unknown", "command": ["/opt/tools/tool"]},
        {"transport": "http", "url": "http:///mcp"},
        {"transport": "http", "url": "https://example.invalid:99999/mcp"},
        {"transport": "http", "url": "https://example.invalid:/mcp"},
        {"transport": "http", "url": "https://user@example.invalid/mcp"},
        {"transport": "http", "url": "https://example.invalid/mcp?key=value"},
        {"transport": "http", "url": "https://example.invalid/mcp#fragment"},
        {"transport": "http", "url": "http://example.invalid/mcp"},
        {"transport": "http", "url": "https://exa mple.invalid/mcp"},
        {
            "transport": "http",
            "url": "https://example.invalid/mcp",
            "headers": {"Authorization": "Bearer synthetic-header-value"},
        },
        {"transport": "http", "url": "https://example.invalid/mcp", "extra": True},
    ],
)
def test_invalid_mcp_settings_are_not_saved(catalog_lab, settings):
    _assert_invalid_resource(catalog_lab, "mcp", settings)


@pytest.mark.parametrize(
    "settings",
    [
        {},
        {"connection_id": "not-a-uuid"},
        {"class": "package.Module", "import": "package"},
        {"download_url": "https://example.invalid/plugin"},
    ],
)
def test_invalid_integration_settings_are_not_saved(catalog_lab, settings):
    _assert_invalid_resource(catalog_lab, "integration", settings)


def test_resource_settings_accept_supported_shapes(catalog_lab):
    accepted = [
        ("skill", {"body": "  Review carefully.  "}),
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
        ),
        ("mcp", {"connection_id": "20000000-0000-0000-0000-000000000001"}),
        (
            "mcp",
            {
                "transport": "stdio",
                "command": ["/opt/local/bin/review-mcp", "--read-only"],
                "environment": {"LANG": "C.UTF-8"},
            },
        ),
        (
            "mcp",
            {
                "transport": "http",
                "url": "https://mcp.example.invalid/v1",
                "headers": {"X-Client": "sidera"},
            },
        ),
        (
            "mcp",
            {"transport": "http", "url": "http://[::1]:9001/mcp"},
        ),
        ("integration", {"connection_id": "20000000-0000-0000-0000-000000000002"}),
    ]
    for kind, settings in accepted:
        saved = catalog_lab.catalog.create_resource(
            catalog_lab.workspace_id,
            _resource(catalog_lab, kind, settings),
            grant_id=catalog_lab.grant_id,
        )
        assert saved.draft.settings == settings
        assert (
            catalog_lab.catalog.get_resource(saved.ref, grant_id=catalog_lab.grant_id)
            == saved
        )


@pytest.mark.parametrize(
    "field",
    ["name", "description", "instructions", "model_id", "thinking"],
)
def test_profile_text_cannot_persist_recognizable_secret(catalog_lab, field):
    value = "Bearer synthetic-secret-not-a-real-key"
    draft = catalog_lab.draft.model_copy(update={field: value})
    with pytest.raises(AgentError) as caught:
        catalog_lab.catalog.create_profile(
            catalog_lab.workspace_id, draft, grant_id=catalog_lab.grant_id
        )
    assert caught.value.code == "configuration_invalid"
    assert value not in str(caught.value)
    assert (
        catalog_lab.catalog.list_profiles(
            catalog_lab.workspace_id, grant_id=catalog_lab.grant_id
        )
        == ()
    )


def test_ordinary_secret_related_words_and_secret_references_are_accepted(catalog_lab):
    binding = SecretBinding(
        credential_ref=CredentialRef(
            workspace_id=catalog_lab.workspace_id, credential_id=uuid4()
        ),
        destination="SERVICE_API_KEY",
    )
    draft = catalog_lab.draft.model_copy(
        update={
            "description": "Discuss credential rotation and token lifetimes.",
            "instructions": "Explain password policy without including a password.",
            "environment": {"APP_MODE": "review"},
            "native_settings": {"outer": [{"environment": {"LANG": "C"}}]},
            "secret_refs": (binding,),
        }
    )
    saved = catalog_lab.catalog.create_profile(
        catalog_lab.workspace_id, draft, grant_id=catalog_lab.grant_id
    )
    assert (
        catalog_lab.catalog.get_profile(saved.ref, grant_id=catalog_lab.grant_id)
        == saved
    )


def test_secret_binding_destination_cannot_contain_secret_values(catalog_lab):
    binding = SecretBinding(
        credential_ref=CredentialRef(
            workspace_id=catalog_lab.workspace_id, credential_id=uuid4()
        ),
        destination="API_KEY Bearer synthetic-destination-value",
    )
    draft = catalog_lab.draft.model_copy(update={"secret_refs": (binding,)})

    with pytest.raises(AgentError) as caught:
        catalog_lab.catalog.create_profile(
            catalog_lab.workspace_id, draft, grant_id=catalog_lab.grant_id
        )

    assert caught.value.code == "configuration_invalid"
    assert "synthetic-destination-value" not in str(caught.value)
    assert (
        catalog_lab.catalog.list_profiles(
            catalog_lab.workspace_id, grant_id=catalog_lab.grant_id
        )
        == ()
    )


@pytest.mark.parametrize(
    "field,value",
    [
        ("name", "Bearer synthetic-name-value"),
        ("settings", {"body": "Bearer synthetic-body-value"}),
        ("settings", {"description": "Bearer synthetic-json-value"}),
        ("settings", {"outer": [{"environment": {"PATH": "/tmp"}}]}),
        ("settings", {"outer": [{"environment": [{"LANG": "C"}]}]}),
        ("settings", {"environment": {"LANG": {"nested": "C"}}}),
        ("settings", {"bad\u0000key": "value"}),
        ("settings", {"body": "contains\u0000nul"}),
    ],
)
def test_resource_name_body_json_and_nested_environment_are_validated(
    catalog_lab, field, value
):
    draft = catalog_lab.skill_draft.model_copy(
        update={field: value} if field == "name" else {"settings": value}
    )
    before = catalog_lab.catalog.list_resources(
        catalog_lab.workspace_id, grant_id=catalog_lab.grant_id
    )
    with pytest.raises(AgentError) as caught:
        catalog_lab.catalog.create_resource(
            catalog_lab.workspace_id, draft, grant_id=catalog_lab.grant_id
        )
    assert caught.value.code == "configuration_invalid"
    assert (
        catalog_lab.catalog.list_resources(
            catalog_lab.workspace_id, grant_id=catalog_lab.grant_id
        )
        == before
    )


@pytest.mark.parametrize(
    "environment",
    [
        {"PATH": "/tmp"},
        {"OPENCODE_CONFIG_CONTENT": "{}"},
        {"MODE": {"nested": "x"}},
        {"MODE": 1},
        {"BAD-NAME": "x"},
        {"TOKEN": "ordinary"},
    ],
)
def test_nested_native_environment_rejects_reserved_or_non_string_maps(
    catalog_lab, environment
):
    draft = catalog_lab.draft.model_copy(
        update={"native_settings": {"outer": [{"env": environment}]}}
    )
    with pytest.raises(AgentError) as caught:
        catalog_lab.catalog.create_profile(
            catalog_lab.workspace_id, draft, grant_id=catalog_lab.grant_id
        )
    assert caught.value.code == "configuration_invalid"
    assert caught.value.field == "native_settings"


def test_failed_revisions_leave_resource_and_profile_heads_unchanged(catalog_lab):
    catalog = catalog_lab.catalog
    resource = catalog.create_resource(
        catalog_lab.workspace_id,
        catalog_lab.skill_draft,
        grant_id=catalog_lab.grant_id,
    )
    profile = catalog.create_profile(
        catalog_lab.workspace_id,
        catalog_lab.draft,
        grant_id=catalog_lab.grant_id,
    )

    with pytest.raises(AgentError) as resource_error:
        catalog.revise_resource(
            resource.ref,
            catalog_lab.skill_draft.model_copy(update={"settings": {"body": " "}}),
            grant_id=catalog_lab.grant_id,
        )
    with pytest.raises(AgentError) as profile_error:
        catalog.revise_profile(
            profile.ref,
            catalog_lab.draft.model_copy(
                update={"instructions": "Bearer synthetic-revision-value"}
            ),
            grant_id=catalog_lab.grant_id,
        )

    assert resource_error.value.code == "configuration_invalid"
    assert profile_error.value.code == "configuration_invalid"
    assert catalog.list_resources(
        catalog_lab.workspace_id, grant_id=catalog_lab.grant_id
    ) == (resource,)
    assert catalog.get_profile(profile.ref, grant_id=catalog_lab.grant_id) == profile
    assert catalog.list_profiles(
        catalog_lab.workspace_id, grant_id=catalog_lab.grant_id
    ) == (profile,)


def test_invalid_catalog_input_has_no_network_process_or_file_side_effects(
    catalog_lab, monkeypatch
):
    import socket
    import subprocess
    import urllib.request
    from pathlib import Path

    def fail(*args, **kwargs):
        raise AssertionError("Catalog validation must not perform external I/O.")

    monkeypatch.setattr(socket, "create_connection", fail)
    monkeypatch.setattr(subprocess, "Popen", fail)
    monkeypatch.setattr(urllib.request, "urlopen", fail)
    monkeypatch.setattr(Path, "open", fail)

    with pytest.raises(AgentError):
        catalog_lab.catalog.create_resource(
            catalog_lab.workspace_id,
            _resource(
                catalog_lab,
                "mcp",
                {"transport": "stdio", "command": ["local/bin/tool"]},
            ),
            grant_id=catalog_lab.grant_id,
        )


def test_authorization_precedes_malformed_configuration(catalog_lab):
    with pytest.raises(AgentError) as caught:
        catalog_lab.catalog.create_resource(
            catalog_lab.workspace_id,
            _resource(catalog_lab, "mcp", {"transport": "stdio", "command": []}),
            grant_id=uuid4(),
        )
    assert caught.value.code == "permission_denied"
