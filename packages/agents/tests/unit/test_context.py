import traceback
from datetime import UTC, datetime
from hashlib import sha256
from uuid import uuid4

import pytest

from sidera_agents.context import ContextAssembler
from sidera_agents.contracts import (
    AssembledInput,
    ContextItem,
    ExecutionWorkspace,
    ProfileDraft,
    ProfileRef,
    ProfileRevision,
    RunInput,
    RunPolicy,
    RunStart,
    RuntimeRef,
    SourceContent,
    SourceRef,
)
from sidera_agents.errors import AgentError


class FixedSourceReader:
    def __init__(self, content: SourceContent) -> None:
        self.content = content
        self.calls: list[tuple[object, SourceRef]] = []

    def read(self, grant_id, ref: SourceRef) -> SourceContent:
        self.calls.append((grant_id, ref))
        return self.content


class RaisingSourceReader:
    def __init__(self, message: str) -> None:
        self.message = message
        self.calls: list[tuple[object, SourceRef]] = []

    def read(self, grant_id, ref: SourceRef) -> SourceContent:
        self.calls.append((grant_id, ref))
        raise RuntimeError(self.message)


def _profile(workspace_id) -> ProfileRevision:
    return ProfileRevision(
        ref=ProfileRef(
            workspace_id=workspace_id,
            profile_id=uuid4(),
            revision=1,
        ),
        draft=ProfileDraft(
            name="reviewer",
            description="Reviews changes",
            runtime_ref=RuntimeRef(workspace_id=workspace_id, runtime_id=uuid4()),
            instructions="Profile instructions",
            resource_refs=(),
            model_id=None,
            thinking=None,
            max_active=1,
            environment={},
            secret_refs=(),
            native_settings={},
        ),
        config_hash="sha256:" + "a" * 64,
        created_at=datetime(2026, 10, 1, tzinfo=UTC),
    )


def _start(profile: ProfileRevision, policy: RunPolicy, grant_id) -> RunStart:
    material = "authorized project note"
    source_ref = SourceRef(provider="fixture", key="notes/one", version="v3")
    item = ContextItem(
        item_id="note-1",
        origin="project",
        required=True,
        ordinal=2,
        version="v3",
        content_hash="sha256:" + sha256(material.encode("utf-8")).hexdigest(),
        source_ref=source_ref,
    )
    return RunStart(
        operation_id=uuid4(),
        profile_ref=profile.ref,
        grant_id=grant_id,
        policy=policy,
        instructions=("Task instructions",),
        text="User request",
        context=(item,),
        tools=(),
        workspace=ExecutionWorkspace(
            directory="/workspace",
            writable=False,
            artifact_directory=None,
        ),
        required_capabilities=(),
    )


def test_one_assembler_preserves_materials_across_run_policies() -> None:
    workspace_id = uuid4()
    grant_id = uuid4()
    profile = _profile(workspace_id)
    material = "authorized project note"
    source_ref = SourceRef(provider="fixture", key="notes/one", version="v3")
    reader = FixedSourceReader(
        SourceContent(
            text=material,
            actual_version="v3",
            content_hash="sha256:" + sha256(material.encode("utf-8")).hexdigest(),
        )
    )
    assembler = ContextAssembler()

    conversation = assembler.assemble(
        profile,
        _start(profile, RunPolicy.CONVERSATION, grant_id),
        reader,
    )
    bounded = assembler.assemble(
        profile,
        _start(profile, RunPolicy.BOUNDED, grant_id),
        reader,
    )

    assert conversation.input.instructions == (
        "Profile instructions",
        "Task instructions",
    )
    assert conversation.input.text == "User request"
    assert conversation.input.context == bounded.input.context
    assert conversation.input.instructions == bounded.input.instructions
    assert conversation.context_receipts == (
        {
            "item_id": "note-1",
            "origin": "project",
            "requested_version": "v3",
            "actual_version": "v3",
            "content_hash": "sha256:" + sha256(material.encode("utf-8")).hexdigest(),
            "status": "resolved",
            "reason_code": None,
        },
    )
    assert material not in repr(conversation.context_receipts)
    assert tuple(AssembledInput.model_fields) == ("instructions", "text", "context")
    assert reader.calls == [(grant_id, source_ref), (grant_id, source_ref)]


def test_run_input_uses_the_same_assembler_and_authorized_source_path() -> None:
    workspace_id = uuid4()
    grant_id = uuid4()
    profile = _profile(workspace_id)
    start = _start(profile, RunPolicy.CONVERSATION, grant_id)
    run_input = RunInput(
        workspace_id=workspace_id,
        run_id=uuid4(),
        operation_id=uuid4(),
        grant_id=grant_id,
        text="Follow-up request",
        context=start.context,
    )
    material = "authorized project note"
    source_ref = start.context[0].source_ref
    reader = FixedSourceReader(
        SourceContent(
            text=material,
            actual_version="v3",
            content_hash="sha256:" + sha256(material.encode("utf-8")).hexdigest(),
        )
    )
    assembler = ContextAssembler()

    initial = assembler.assemble(profile, start, reader)
    follow_up = assembler.assemble(profile, run_input, reader)

    assert follow_up.input.instructions == ("Profile instructions",)
    assert follow_up.input.text == "Follow-up request"
    assert follow_up.input.context == initial.input.context
    assert reader.calls == [(grant_id, source_ref), (grant_id, source_ref)]


def test_inline_context_order_is_stable_by_ordinal_then_item_id() -> None:
    workspace_id = uuid4()
    grant_id = uuid4()
    profile = _profile(workspace_id)
    request = _start(profile, RunPolicy.BOUNDED, grant_id)
    materials = (
        ("zeta", 1, "first zeta"),
        ("middle", 2, "second"),
        ("alpha", 1, "first alpha"),
    )
    items = tuple(
        ContextItem(
            item_id=item_id,
            origin="fixture",
            required=True,
            ordinal=ordinal,
            version="inline-v1",
            content_hash="sha256:" + sha256(text.encode("utf-8")).hexdigest(),
            text=text,
        )
        for item_id, ordinal, text in materials
    )
    request = request.model_copy(update={"context": items})
    reader = FixedSourceReader(
        SourceContent(text="unused", actual_version="v1", content_hash="unused")
    )

    result = ContextAssembler().assemble(profile, request, reader)

    assert [item.item_id for item in result.input.context] == [
        "alpha",
        "zeta",
        "middle",
    ]
    assert [item.text for item in result.input.context] == [
        "first alpha",
        "first zeta",
        "second",
    ]
    assert reader.calls == []


def test_inline_unicode_context_uses_utf8_sha256() -> None:
    workspace_id = uuid4()
    grant_id = uuid4()
    profile = _profile(workspace_id)
    request = _start(profile, RunPolicy.BOUNDED, grant_id)
    text = "发布说明：通过验证 🛰️"
    item = ContextItem(
        item_id="unicode-note",
        origin="inline",
        required=True,
        ordinal=0,
        version="inline-v1",
        content_hash="sha256:" + sha256(text.encode("utf-8")).hexdigest(),
        text=text,
    )
    request = request.model_copy(update={"context": (item,)})
    reader = FixedSourceReader(
        SourceContent(text="unused", actual_version="v1", content_hash="unused")
    )

    result = ContextAssembler().assemble(profile, request, reader)

    assert result.input.context[0].text == text
    assert result.input.context[0].content_hash == (
        "sha256:" + sha256("发布说明：通过验证 🛰️".encode()).hexdigest()
    )


def test_empty_user_input_and_context_are_preserved() -> None:
    workspace_id = uuid4()
    grant_id = uuid4()
    profile = _profile(workspace_id)
    request = _start(profile, RunPolicy.BOUNDED, grant_id).model_copy(
        update={"instructions": (), "text": "", "context": ()}
    )
    reader = FixedSourceReader(
        SourceContent(text="unused", actual_version="v1", content_hash="unused")
    )

    result = ContextAssembler().assemble(profile, request, reader)

    assert result.input.instructions == ("Profile instructions",)
    assert result.input.text == ""
    assert result.input.context == ()
    assert result.context_receipts == ()


def test_required_context_rejects_a_different_source_version() -> None:
    workspace_id = uuid4()
    grant_id = uuid4()
    profile = _profile(workspace_id)
    request = _start(profile, RunPolicy.BOUNDED, grant_id)
    material = "authorized project note"
    reader = FixedSourceReader(
        SourceContent(
            text=material,
            actual_version="v4",
            content_hash="sha256:" + sha256(material.encode("utf-8")).hexdigest(),
        )
    )

    with pytest.raises(AgentError) as rejected:
        ContextAssembler().assemble(profile, request, reader)

    assert rejected.value.code == "context_version_mismatch"


def test_run_start_must_match_the_supplied_profile_revision_before_reading() -> None:
    workspace_id = uuid4()
    grant_id = uuid4()
    profile = _profile(workspace_id)
    request = _start(profile, RunPolicy.BOUNDED, grant_id).model_copy(
        update={"profile_ref": profile.ref.model_copy(update={"revision": 2})}
    )
    material = "authorized project note"
    reader = FixedSourceReader(
        SourceContent(
            text=material,
            actual_version="v3",
            content_hash="sha256:" + sha256(material.encode("utf-8")).hexdigest(),
        )
    )

    with pytest.raises(AgentError) as rejected:
        ContextAssembler().assemble(profile, request, reader)

    assert rejected.value.code == "profile_reference_mismatch"
    assert reader.calls == []


def test_run_input_must_match_profile_workspace_before_reading() -> None:
    workspace_id = uuid4()
    grant_id = uuid4()
    profile = _profile(workspace_id)
    start = _start(profile, RunPolicy.BOUNDED, grant_id)
    request = RunInput(
        workspace_id=uuid4(),
        run_id=uuid4(),
        operation_id=uuid4(),
        grant_id=grant_id,
        text="Follow-up request",
        context=start.context,
    )
    material = "authorized project note"
    reader = FixedSourceReader(
        SourceContent(
            text=material,
            actual_version="v3",
            content_hash="sha256:" + sha256(material.encode("utf-8")).hexdigest(),
        )
    )

    with pytest.raises(AgentError) as rejected:
        ContextAssembler().assemble(profile, request, reader)

    assert rejected.value.code == "permission_denied"
    assert reader.calls == []


def test_required_context_rejects_a_false_source_hash() -> None:
    workspace_id = uuid4()
    grant_id = uuid4()
    profile = _profile(workspace_id)
    request = _start(profile, RunPolicy.BOUNDED, grant_id)
    reader = FixedSourceReader(
        SourceContent(
            text="authorized project note",
            actual_version="v3",
            content_hash="sha256:" + "0" * 64,
        )
    )

    with pytest.raises(AgentError) as rejected:
        ContextAssembler().assemble(profile, request, reader)

    assert rejected.value.code == "context_hash_mismatch"


def test_optional_version_mismatch_has_an_omission_receipt() -> None:
    workspace_id = uuid4()
    grant_id = uuid4()
    profile = _profile(workspace_id)
    required_request = _start(profile, RunPolicy.BOUNDED, grant_id)
    item = required_request.context[0].model_copy(update={"required": False})
    request = required_request.model_copy(update={"context": (item,)})
    material = "authorized project note"
    reader = FixedSourceReader(
        SourceContent(
            text=material,
            actual_version="v4",
            content_hash="sha256:" + sha256(material.encode("utf-8")).hexdigest(),
        )
    )

    result = ContextAssembler().assemble(profile, request, reader)

    assert result.input.context == ()
    assert result.context_receipts == (
        {
            "item_id": "note-1",
            "origin": "project",
            "requested_version": "v3",
            "actual_version": None,
            "content_hash": None,
            "status": "omitted",
            "reason_code": "context_version_mismatch",
        },
    )


def test_optional_hash_mismatch_is_omitted_without_claiming_hash() -> None:
    workspace_id = uuid4()
    grant_id = uuid4()
    profile = _profile(workspace_id)
    required_request = _start(profile, RunPolicy.BOUNDED, grant_id)
    item = required_request.context[0].model_copy(update={"required": False})
    request = required_request.model_copy(update={"context": (item,)})
    material = "authorized project note"
    reader = FixedSourceReader(
        SourceContent(
            text=material,
            actual_version="v3",
            content_hash="sha256:" + "0" * 64,
        )
    )

    result = ContextAssembler().assemble(profile, request, reader)

    assert result.input.context == ()
    assert result.context_receipts[0] == {
        "item_id": "note-1",
        "origin": "project",
        "requested_version": "v3",
        "actual_version": None,
        "content_hash": None,
        "status": "omitted",
        "reason_code": "context_hash_mismatch",
    }


def test_required_source_failure_is_sanitized() -> None:
    workspace_id = uuid4()
    grant_id = uuid4()
    profile = _profile(workspace_id)
    request = _start(profile, RunPolicy.BOUNDED, grant_id)
    marker = "fixture-secret-from-source-error"
    reader = RaisingSourceReader(f"untrusted storage detail: {marker}")

    with pytest.raises(AgentError) as rejected:
        ContextAssembler().assemble(profile, request, reader)

    error = rejected.value
    rendered_traceback = "".join(
        traceback.format_exception(type(error), error, error.__traceback__)
    )
    assert error.code == "context_required_missing"
    assert marker not in "\n".join((str(error), repr(error), rendered_traceback))
    assert error.__cause__ is None
    assert error.__suppress_context__ is True


def test_optional_source_failure_is_a_safe_omission() -> None:
    workspace_id = uuid4()
    grant_id = uuid4()
    profile = _profile(workspace_id)
    required_request = _start(profile, RunPolicy.BOUNDED, grant_id)
    item = required_request.context[0].model_copy(update={"required": False})
    request = required_request.model_copy(update={"context": (item,)})
    marker = "fixture-secret-from-optional-source-error"
    reader = RaisingSourceReader(f"untrusted storage detail: {marker}")

    result = ContextAssembler().assemble(profile, request, reader)

    assert result.input.context == ()
    assert result.context_receipts == (
        {
            "item_id": "note-1",
            "origin": "project",
            "requested_version": "v3",
            "actual_version": None,
            "content_hash": None,
            "status": "omitted",
            "reason_code": "source_unavailable",
        },
    )
    assert marker not in repr(result.context_receipts)
