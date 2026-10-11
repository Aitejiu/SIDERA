import json
from contextlib import nullcontext
from copy import deepcopy
from dataclasses import replace
from types import SimpleNamespace
from uuid import uuid4

import pytest
from pydantic import ValidationError
from sidera_agents.errors import AgentError

from sidera_control_plane.api.schemas import (
    CommandRequest,
    NativeExecutionContextAddressPayload,
    NativeHandoffContextAddressPayload,
    SendExecutionInputPayload,
)
from sidera_control_plane.application.agent_sources import (
    TaskContextSourceReader,
    context_source_items,
)
from sidera_control_plane.application.command_service import hash_request
from sidera_control_plane.application.context_materialization import ContextMaterializer
from sidera_control_plane.domain.commands import CommandEnvelope
from sidera_control_plane.domain.context_packages import ContextSelectionReason
from sidera_control_plane.domain.errors import ValidationError as DomainValidationError
from sidera_control_plane.domain.gateway import NativeInputIdentity
from tests.unit.test_agent_sources import validated
from tests.unit.test_context_materialization import (
    CONSUMER_ID,
    FailingCatalog,
    FakeCatalog,
    _body,
    _knowledge_ref,
    _manifest,
    _revision,
)


def input_payload():
    return {
        "context": {
            "worker_item_id": uuid4(),
            "plan_generation": 1,
            "context_version": 1,
            "execution_id": uuid4(),
            "session_id": uuid4(),
            "session_version": 1,
            "worker_id": uuid4(),
            "execution_mode": "native_profile",
            "binding_version": 1,
            "run_id": uuid4(),
        },
        "client_idempotency_key": "original-input",
        "text": "  exact input  ",
        "attachment_refs": (),
    }


def native_handoff_request():
    workspace_id, profile_id = uuid4(), uuid4()
    context = input_payload()["context"]
    return {
        "command_id": uuid4(),
        "workspace_id": workspace_id,
        "command_type": "PrepareProfileHandoff",
        "target_type": "worker_item",
        "target_id": context["worker_item_id"],
        "expected_version": 1,
        "actor": {"type": "user", "id": "local-user"},
        "idempotency_key": "native-prepare",
        "correlation_id": uuid4(),
        "payload": {
            "proposal_schema_version": 2,
            "context": context,
            "target_assignee": {"kind": "profile", "id": str(profile_id)},
            "target_profile_ref": {
                "workspace_id": workspace_id,
                "profile_id": profile_id,
                "revision": 1,
            },
            "handoff_reason": "Explicit reassignment",
            "handoff_instruction": "Continue current work",
            "input_plan": {},
            "proposal_ttl_seconds": 900,
        },
    }


@pytest.mark.parametrize(
    "case",
    [
        "extra",
        "float_schema",
        "bool_schema",
        "partial_context",
        "float_context",
        "target_scope",
        "profile_scope",
        "profile_identity",
        "human_profile",
        "float_version",
    ],
)
def test_api_native_prepare_rejects_invalid_explicit_shape(case):
    request = native_handoff_request()
    assert (
        CommandRequest.model_validate(request).command_type == "PrepareProfileHandoff"
    )
    payload = request["payload"]
    if case == "extra":
        payload["private_trace"] = "forbidden"
    elif case in {"float_schema", "bool_schema"}:
        payload["proposal_schema_version"] = 2.0 if case == "float_schema" else True
    elif case == "partial_context":
        del payload["context"]["run_id"]
    elif case == "float_context":
        payload["context"]["binding_version"] = 1.0
    elif case == "target_scope":
        request["target_id"] = uuid4()
    elif case == "profile_scope":
        payload["target_profile_ref"]["workspace_id"] = uuid4()
    elif case == "profile_identity":
        payload["target_profile_ref"]["profile_id"] = uuid4()
    elif case == "human_profile":
        payload["target_assignee"] = {"kind": "human", "id": "local-user"}
    else:
        request["expected_version"] = 1.0
    with pytest.raises(ValidationError):
        CommandRequest.model_validate(request)


@pytest.mark.parametrize(
    "case", ["extra", "target_scope", "version_scope", "float_version"]
)
def test_api_native_confirm_rejects_invalid_explicit_shape(case):
    request = native_handoff_request()
    proposal_id = uuid4()
    request.update(
        command_type="ConfirmProfileHandoff",
        target_type="handoff_proposal",
        target_id=proposal_id,
    )
    request["payload"] = {
        "proposal_id": proposal_id,
        "expected_proposal_version": 1,
        "expected_proposal_hash": "a" * 64,
        "expected_worker_item_version": 1,
    }
    assert (
        CommandRequest.model_validate(request).command_type == "ConfirmProfileHandoff"
    )
    if case == "extra":
        request["payload"]["private_trace"] = "forbidden"
    elif case == "target_scope":
        request["payload"]["proposal_id"] = uuid4()
    elif case == "version_scope":
        request["payload"]["expected_proposal_version"] = 2
    else:
        request["payload"]["expected_worker_item_version"] = 1.0
    with pytest.raises(ValidationError):
        CommandRequest.model_validate(request)


def test_api_accepts_explicit_native_input_context_without_changing_legacy_shape():
    payload = input_payload()
    parsed = SendExecutionInputPayload.model_validate(payload)
    assert parsed.context.execution_mode == "native_profile"
    assert parsed.context.run_id == payload["context"]["run_id"]
    assert parsed.text == payload["text"]
    legacy = {
        **payload,
        "context": {
            key: value
            for key, value in payload["context"].items()
            if key not in {"execution_mode", "binding_version", "run_id"}
        },
    }
    assert (
        SendExecutionInputPayload.model_validate(legacy).model_dump()["context"]
        == legacy["context"]
    )


def _input_request(payload):
    return {
        "command_id": uuid4(),
        "workspace_id": uuid4(),
        "command_type": "SendExecutionInput",
        "target_type": "execution_session",
        "target_id": payload["context"]["session_id"],
        "expected_version": payload["context"]["session_version"],
        "actor": {"type": "user", "id": "local-user"},
        "idempotency_key": "original-command",
        "correlation_id": uuid4(),
        "payload": payload,
    }


@pytest.mark.parametrize("kind", ["native", "legacy", "native_handoff"])
def test_command_context_proof_omission_preserves_existing_payload_hash(kind):
    payload = input_payload()
    if kind == "legacy":
        for key in ("execution_mode", "binding_version", "run_id"):
            payload["context"].pop(key)
    incoming = (
        native_handoff_request()
        if kind == "native_handoff"
        else _input_request(payload)
    )
    original = deepcopy(incoming)
    old_payload = json.loads(json.dumps(incoming["payload"], default=str))
    parsed = CommandRequest.model_validate(incoming)
    assert parsed.payload == old_payload
    assert "session_last_sequence" not in parsed.payload["context"]
    assert incoming == original
    old_envelope = CommandEnvelope(
        id=parsed.command_id,
        workspace_id=parsed.workspace_id,
        command_type=parsed.command_type,
        target_type=parsed.target_type,
        actor_type=parsed.actor.type,
        actor_id=parsed.actor.id,
        target_id=parsed.target_id,
        expected_version=parsed.expected_version,
        idempotency_key=parsed.idempotency_key,
        correlation_id=parsed.correlation_id,
        payload=old_payload,
    )
    assert hash_request(replace(old_envelope, payload=parsed.payload)) == hash_request(
        old_envelope
    )


@pytest.mark.parametrize("proof", [None, 0, 3])
def test_native_input_schema_preserves_explicit_nullable_proof(proof):
    payload = input_payload()
    payload["context"].update(session_version=5, session_last_sequence=proof)
    parsed = CommandRequest.model_validate(_input_request(payload))
    assert "session_last_sequence" in parsed.payload["context"]
    assert parsed.payload["context"]["session_last_sequence"] == proof
    assert parsed.payload["text"] == "  exact input  "


@pytest.mark.parametrize(
    "context_type",
    [NativeExecutionContextAddressPayload, NativeHandoffContextAddressPayload],
)
def test_native_command_context_proof_schema_is_optional_without_default(context_type):
    schema = context_type.model_json_schema()
    proof_schema = schema["properties"]["session_last_sequence"]
    assert "session_last_sequence" not in schema.get("required", [])
    assert proof_schema["anyOf"] == [
        {"minimum": 0, "type": "integer"},
        {"type": "null"},
    ]
    context = input_payload()["context"]
    context["session_version"] = 5
    assert context_type.model_validate(context).session_last_sequence is None
    for proof in (None, 0, 3):
        assert (
            context_type.model_validate(
                {**context, "session_last_sequence": proof}
            ).session_last_sequence
            == proof
        )
    for proof in (True, -1, 5, 6, 3.0, "3"):
        with pytest.raises(ValidationError):
            context_type.model_validate({**context, "session_last_sequence": proof})
    assert "default" not in proof_schema


@pytest.mark.parametrize("proof", [True, -1, 5, 6, 3.0, "3", {}, []])
def test_native_input_schema_rejects_invalid_observation_proof(proof):
    payload = input_payload()
    payload["context"].update(session_version=5, session_last_sequence=proof)
    with pytest.raises(ValidationError):
        CommandRequest.model_validate(_input_request(payload))


@pytest.mark.parametrize(
    "extra", ["session_last_sequence", "_native", "grant_id", "private_trace"]
)
def test_legacy_input_schema_keeps_forbidding_native_proof_and_private_keys(extra):
    payload = input_payload()
    for key in ("execution_mode", "binding_version", "run_id"):
        payload["context"].pop(key)
    payload["context"][extra] = 0
    with pytest.raises(ValidationError):
        CommandRequest.model_validate(_input_request(payload))


@pytest.mark.parametrize(
    "extra", ["_native", "grant_id", "private_trace", "run_id_extra"]
)
def test_native_input_proof_keeps_forbidding_private_and_extra_keys(extra):
    payload = input_payload()
    payload["context"].update(session_version=5, session_last_sequence=3)
    payload["context"][extra] = "forbidden"
    with pytest.raises(ValidationError):
        CommandRequest.model_validate(_input_request(payload))


@pytest.mark.parametrize("field", ["binding_version", "run_id", "execution_mode"])
def test_api_rejects_partial_native_identity(field):
    payload = input_payload()
    del payload["context"][field]
    with pytest.raises(ValidationError):
        SendExecutionInputPayload.model_validate(payload)


@pytest.fixture
def optional_reader(monkeypatch):
    reference = replace(
        _knowledge_ref(ordinal=0, section_key="selected"),
        selection_reason=ContextSelectionReason.RETRIEVAL,
    )
    revision = _revision()
    facts = validated(
        ContextMaterializer(FakeCatalog((_body(reference, "body-selected"),))),
        _manifest(revision, (reference,)),
        revision,
    )
    items = context_source_items(facts)
    authority = SimpleNamespace(
        execution_id=CONSUMER_ID, sources=tuple(item.source_ref for item in items)
    )
    uow = SimpleNamespace(
        execution_authorities=SimpleNamespace(get=lambda grant_id: authority)
    )
    catalog = FakeCatalog((_body(reference, "body-selected"),))
    reader = TaskContextSourceReader(
        lambda: nullcontext(uow),
        catalog=catalog,
        authorizer=SimpleNamespace(require=lambda *args: None),
    )
    monkeypatch.setattr(reader, "validated_facts", lambda *args: facts)
    return reader, catalog, items[-1].source_ref


def test_selected_optional_reference_is_required_in_same_single_read(optional_reader):
    reader, catalog, selected = optional_reader
    items = reader.context_items(uuid4(), CONSUMER_ID, required_refs=(selected,))
    assert items[-1].required and items[-1].source_ref == selected
    assert items[-1].version == selected.version
    assert catalog.materialize_calls == 1


def test_optional_failure_remains_optional_only_when_unselected(optional_reader):
    reader, _, selected = optional_reader
    failing = FailingCatalog()
    reader._materializer = ContextMaterializer(failing)
    items = reader.context_items(uuid4(), CONSUMER_ID)
    assert not items[-1].required
    assert failing.materialize_calls == 1
    with pytest.raises(AgentError, match="unavailable"):
        reader.context_items(uuid4(), CONSUMER_ID, required_refs=(selected,))
    assert failing.materialize_calls == 2


def test_reserved_input_bytes_share_existing_context_budget(optional_reader):
    reader, _, _ = optional_reader
    reader.context_items(uuid4(), CONSUMER_ID, reserved_bytes=0)
    with pytest.raises(AgentError) as denied:
        reader.context_items(uuid4(), CONSUMER_ID, reserved_bytes=16_000)
    assert denied.value.code == "context_budget_exceeded"


def test_native_result_codec_keeps_terminal_proof_separate_from_pending_identity():
    identity = NativeInputIdentity(*(uuid4() for _ in range(7)), binding_version=2)
    pending = identity.result_ref()
    delivered = identity.result_ref(evidence_sequence=3)
    assert NativeInputIdentity.from_result_ref(pending) == identity
    assert NativeInputIdentity.from_result_ref(delivered) == identity
    assert NativeInputIdentity.delivery_sequence_from_result_ref(pending) is None
    assert NativeInputIdentity.delivery_sequence_from_result_ref(delivered) == 3


@pytest.mark.parametrize("sequence", [0, -1, True, "3"])
def test_native_result_codec_rejects_nonpositive_or_untyped_proof(sequence):
    identity = NativeInputIdentity(*(uuid4() for _ in range(7)), binding_version=2)
    with pytest.raises(DomainValidationError):
        identity.result_ref(evidence_sequence=sequence)
