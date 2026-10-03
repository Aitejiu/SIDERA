import json
from uuid import uuid4

import pytest
from pydantic import SecretStr

from sidera_agents.contracts import (
    CredentialRef,
    NativeOutcome,
    SecretBinding,
    ToolBinding,
    ToolConnection,
)

SECRETS = (
    "N08_RESOLVED_HEADER_SECRET_SENTINEL",
    "N08_RESOLVED_TOOL_ENV_SECRET_SENTINEL",
)


@pytest.fixture
def secret_observation_scenario(monkeypatch, observation_feed):
    def exercise(lab, *, policy="bounded", observed_model=None, usage=None):
        feed = observation_feed(lab)
        connection_id = uuid4()

        class Connections:
            def resolve(self, grant_id, requested_id):
                assert grant_id == lab.grant_id and requested_id == connection_id
                return ToolConnection(
                    name="fixture-tool",
                    transport="http",
                    endpoint_or_argv="https://fixture.invalid/mcp",
                    headers={"X-Fixture": SecretStr(SECRETS[0])},
                    environment={"FIXTURE_TOKEN": SecretStr(SECRETS[1])},
                )

        lab.execution._tool_connections = Connections()
        request = lab.request(
            policy=policy, tools=(ToolBinding(connection_id=connection_id),)
        )
        start = lab.execution.start(request)
        original_prompt = lab.native.prompt

        def prompt(handle, assembled, response_id):
            feed.emit(
                handle.run_id, cursor=1, text="ordinary text " + SECRETS[0] + " end"
            )
            feed.emit(
                handle.run_id,
                cursor=2,
                kind="tool.updated",
                data={
                    "tool_name": "search",
                    "text": "tool result " + SECRETS[1],
                    "update_type": "tool_call_update",
                    "status": "completed",
                    "headers": {"X-Fixture": SECRETS[0]},
                    "environment": {"TOKEN": SECRETS[1]},
                    "auth": {"cookie": "FAKE_COOKIE_MUST_NOT_PERSIST"},
                    "grant_id": str(lab.grant_id),
                    "unknown": {"private": "DROP_UNKNOWN_STRUCTURE"},
                },
            )
            feed.emit(
                handle.run_id,
                cursor=3,
                kind="stderr",
                data={
                    "text": "raw diagnostic " + SECRETS[0] + "\n" + "\u4e2d" * 12000,
                    "reason_code": SECRETS[1],
                },
            )
            outcome = original_prompt(handle, assembled, response_id)
            return NativeOutcome.model_validate(
                outcome.model_copy(
                    update={
                        "observed_model": observed_model,
                        "usage": usage,
                    }
                ).model_dump(mode="python")
            )

        monkeypatch.setattr(lab.native, "prompt", prompt)
        lab.pump()
        assert lab.service.observations is not None, "Observation service is not wired."
        page = lab.service.observations.read_trace(
            lab.workspace_id, start.run_id, 0, grant_id=lab.grant_id
        )
        return start, page

    return exercise


def test_resolved_secrets_are_redacted_before_store_and_public_trace(
    admission_lab, secret_observation_scenario
):
    lab = admission_lab
    start, page = secret_observation_scenario(
        lab,
        policy="conversation",
        observed_model="provider/" + SECRETS[0],
        usage={
            "input_tokens": 12,
            "authorization": SECRETS[0],
            "nested": {"token": SECRETS[1]},
        },
    )
    public = page.model_dump_json()
    stored = json.dumps(
        [
            event.model_dump(mode="json")
            for event in lab.store.get_lifecycle_events(lab.workspace_id, start.run_id)
        ]
    )
    for value in (
        *SECRETS,
        "FAKE_COOKIE_MUST_NOT_PERSIST",
        "DROP_UNKNOWN_STRUCTURE",
        str(lab.grant_id),
    ):
        assert value not in public and value not in stored
    message = next(e for e in page.events if e.kind == "message")
    assert message.data["text"] == "ordinary text [REDACTED] end"
    tool = next(e for e in page.events if e.kind == "tool")
    assert tool.data["tool_name"] == "search"
    assert tool.data["text"] == "tool result [REDACTED]"
    assert tool.data["reason_code"] == "unsafe_fields_omitted"
    assert tool.operation_id is not None and tool.response_id is not None
    snapshot = lab.execution.get_run(
        lab.workspace_id, start.run_id, grant_id=lab.grant_id
    )
    assert snapshot.effective_config.observed_model is None
    assert snapshot.effective_config.selected_model is None
    assert "ordinary text" not in snapshot.model_dump_json()
    usage = next(e for e in page.events if e.kind == "usage")
    assert usage.data["usage"] == {"input_tokens": 12}
    assert usage.data["model_reason_code"] == "observed_model_omitted"


def test_stderr_is_redacted_bounded_and_explicitly_truncated(
    admission_lab, secret_observation_scenario
):
    _, page = secret_observation_scenario(admission_lab)
    diagnostic = next(e for e in page.events if e.kind == "diagnostic")
    assert diagnostic.data["text"].startswith("raw diagnostic [REDACTED]\n")
    assert len(diagnostic.data["text"].encode("utf-8")) <= 4096
    assert "\u4e2d" in diagnostic.data["text"]
    assert diagnostic.data["reason_code"] == "text_truncated"
    assert diagnostic.data["truncated"] is True


@pytest.mark.parametrize(
    "usage", (None, {}, {"tokens": "untrusted"}, {"input_tokens": True, "cost": -1})
)
def test_unavailable_usage_stays_null(
    admission_lab, secret_observation_scenario, usage
):
    _, page = secret_observation_scenario(admission_lab, usage=usage)
    record = next(e for e in page.events if e.kind == "usage")
    assert record.data["usage"] is None
    assert record.data["reason_code"] == "usage_unavailable"


def test_observed_model_is_distinct_and_safe(
    admission_lab, secret_observation_scenario
):
    start, page = secret_observation_scenario(
        admission_lab, observed_model="fixture/observed-model"
    )
    snapshot = admission_lab.execution.get_run(
        admission_lab.workspace_id, start.run_id, grant_id=admission_lab.grant_id
    )
    assert snapshot.effective_config.selected_model is None
    assert snapshot.effective_config.observed_model == "fixture/observed-model"
    assert (
        next(e for e in page.events if e.kind == "usage").data["observed_model"]
        == "fixture/observed-model"
    )


@pytest.mark.parametrize(
    "kind,data",
    (
        ("auth", {"text": "FAKE_UNTRUSTED_AUTH_TEXT"}),
        ("message", {"text": {"credential": "FAKE_STRUCTURED_MESSAGE"}}),
        ("message", {"text": "Cookie: fixture-unresolved-cookie"}),
    ),
)
def test_unsafe_native_content_is_omitted_with_safe_reason(
    admission_lab, observation_feed, kind, data
):
    lab = admission_lab
    feed = observation_feed(lab)
    start = lab.execution.start(lab.request())
    feed.emit(start.run_id, cursor=1, kind=kind, data=data)
    lab.pump()
    assert lab.service.observations is not None, "Observation service is not wired."
    page = lab.service.observations.read_trace(
        lab.workspace_id, start.run_id, 0, grant_id=lab.grant_id
    )
    assert all(
        value not in page.model_dump_json()
        for value in (
            "FAKE_UNTRUSTED_AUTH_TEXT",
            "FAKE_STRUCTURED_MESSAGE",
            "fixture-unresolved-cookie",
        )
    )
    omissions = [
        e
        for e in page.events
        if e.data.get("reason_code")
        in {"native_content_omitted", "unsafe_text_omitted"}
    ]
    assert len(omissions) == 1


def test_untrusted_native_identity_and_nested_labels_cannot_break_ingestion(
    admission_lab, observation_feed
):
    lab = admission_lab
    feed = observation_feed(lab)
    start = lab.execution.start(lab.request())
    feed.emit(
        start.run_id,
        cursor=1,
        native_id="\ud800",
        data={
            "text": "safe text survives malformed metadata",
            "update_type": {"private": "FAKE_METADATA_SECRET"},
            "status": ["FAKE_METADATA_SECRET"],
        },
    )
    lab.pump()
    page = lab.service.observations.read_trace(
        lab.workspace_id, start.run_id, 0, grant_id=lab.grant_id
    )
    assert [e.data["text"] for e in page.events if e.kind == "message"] == [
        "safe text survives malformed metadata"
    ]
    assert "FAKE_METADATA_SECRET" not in page.model_dump_json()


def test_resolved_profile_environment_is_redacted_and_cleared_with_native_context(
    admission_lab, observation_feed, monkeypatch
):
    lab = admission_lab
    secret = "N08_RESOLVED_PROFILE_SECRET_SENTINEL"
    binding = SecretBinding(
        credential_ref=CredentialRef(
            workspace_id=lab.workspace_id, credential_id=uuid4()
        ),
        destination="PROFILE_TOKEN",
    )
    profile = lab.catalog.revise_profile(
        lab.profile.ref,
        lab.profile.draft.model_copy(update={"secret_refs": (binding,)}),
        grant_id=lab.grant_id,
    )

    class Secrets:
        def read(self, grant_id, requested):
            assert grant_id == lab.grant_id and requested == binding
            return SecretStr(secret)

    lab.execution._secrets = Secrets()
    feed = observation_feed(lab)
    start = lab.execution.start(lab.request(profile_ref=profile.ref))
    original_prompt = lab.native.prompt
    ingestors = []

    def prompt(handle, assembled, response_id):
        ingestors.append(lab.execution._observations[handle.run_id])
        feed.emit(handle.run_id, cursor=1, text="profile result " + secret)
        return original_prompt(handle, assembled, response_id)

    monkeypatch.setattr(lab.native, "prompt", prompt)
    lab.pump()
    trace = lab.service.observations.read_trace(
        lab.workspace_id, start.run_id, 0, grant_id=lab.grant_id
    )
    assert (
        next(e for e in trace.events if e.kind == "message").data["text"]
        == "profile result [REDACTED]"
    )
    assert secret not in repr(
        lab.store.get_lifecycle_events(lab.workspace_id, start.run_id)
    )
    assert secret not in trace.model_dump_json()
    assert (
        secret
        not in lab.execution.get_run(
            lab.workspace_id, start.run_id, grant_id=lab.grant_id
        ).model_dump_json()
    )
    assert ingestors[0].closed and ingestors[0]._redactor._secrets == ()
    assert ingestors[0]._grant_id is None and ingestors[0]._workspace is None
