from dataclasses import replace
from uuid import uuid4

import pytest

from sidera_control_plane.application.context_materialization import (
    ContextMaterializationError,
    ContextMaterializer,
)
from sidera_control_plane.domain.context_packages import ContextMaterializationMode
from tests.unit.test_context_materialization import (
    CONSUMER_ID,
    NOW,
    WORKSPACE_ID,
    FakeCatalog,
    MemoryResultInputContentPort,
    _body,
    _knowledge_ref,
    _manifest,
    _revision,
    _v2_manifest_with_body,
)


def validated(materializer, manifest, revision, **changes):
    assert hasattr(materializer, "validate_facts"), (
        "N11 needs shared validated facts before legacy final assembly"
    )
    arguments = {
        "mode": ContextMaterializationMode.CONSUMER_START,
        "workspace_id": WORKSPACE_ID,
        "consumer_id": CONSUMER_ID,
        "purpose": manifest.purpose,
        "context_version": manifest.context_version,
        "expected_package_hash": manifest.package_hash,
        "now": NOW,
        "correlation_id": uuid4(),
        "caller_request_id": uuid4(),
    }
    arguments.update(changes)
    return materializer.validate_facts(manifest, revision, **arguments)


def test_shared_validation_returns_facts_without_legacy_assembly(monkeypatch):
    revision = _revision()
    manifest = _manifest(revision)
    materializer = ContextMaterializer(FakeCatalog())

    def forbidden(*args, **kwargs):
        pytest.fail("native validated facts must not call legacy _assemble")

    monkeypatch.setattr(materializer, "_assemble", forbidden)
    facts = validated(materializer, manifest, revision)
    assert facts.manifest == manifest
    assert facts.revision == revision
    assert facts.project_knowledge == facts.result_inputs == ()


@pytest.mark.parametrize("case", ["expired", "consumer", "package_hash", "revision"])
def test_shared_validation_retains_manifest_and_revision_gates(case):
    revision = _revision()
    manifest = _manifest(revision)
    changes = {}
    if case == "expired":
        changes["now"] = manifest.expires_at
    elif case == "consumer":
        changes["consumer_id"] = uuid4()
    elif case == "package_hash":
        changes["expected_package_hash"] = "f" * 64
    else:
        revision = replace(revision, revision_id=uuid4())
    with pytest.raises(ContextMaterializationError):
        validated(ContextMaterializer(FakeCatalog()), manifest, revision, **changes)


def test_shared_validation_preserves_exact_knowledge_and_result_reads():
    revision = _revision()
    reference = _knowledge_ref(ordinal=0, section_key="overview")
    body = _body(reference, "body-overview")
    knowledge_manifest = _manifest(revision, (reference,))
    facts = validated(
        ContextMaterializer(FakeCatalog((body,))), knowledge_manifest, revision
    )
    assert facts.project_knowledge[0].body_markdown == "body-overview"
    assert facts.project_knowledge[0].section_hash == reference.section_hash
    manifest, contents = _v2_manifest_with_body(revision)
    facts = validated(
        ContextMaterializer(
            FakeCatalog(), result_input_content=MemoryResultInputContentPort(contents)
        ),
        manifest,
        revision,
    )
    assert facts.result_inputs[0].body_text == "result\n"
    assert (
        facts.result_inputs[0].artifact_id
        == manifest.result_input_manifest.artifacts[0].artifact_id
    )


def test_metadata_validation_and_single_exact_read_do_not_fetch_other_bodies():
    references = (
        _knowledge_ref(ordinal=0, section_key="first"),
        _knowledge_ref(ordinal=1, section_key="second"),
    )
    revision = _revision()
    manifest = _manifest(revision, references)

    class ExactIO(FakeCatalog):
        def materialize(self, workspace_id, refs, **kwargs):
            assert tuple(ref.ordinal for ref in refs) == (0,)
            self.materialize_calls += 1
            return (_body(references[0], "body-first"),)

    catalog = ExactIO()
    materializer = ContextMaterializer(catalog)
    facts = validated(materializer, manifest, revision, include_bodies=False)
    assert catalog.materialize_calls == 0
    section = materializer.read_knowledge_section(
        facts, 0, correlation_id=uuid4(), caller_request_id=uuid4()
    )
    assert section.body_markdown == "body-first"
    assert catalog.materialize_calls == 1


def test_cp_source_items_seal_exact_bytes_without_assembly():
    import json
    from hashlib import sha256

    from sidera_control_plane.application.agent_sources import context_source_items

    revision = _revision()
    reference = _knowledge_ref(ordinal=0, section_key="overview")
    manifest = _manifest(revision, (reference,))
    facts = validated(
        ContextMaterializer(FakeCatalog((_body(reference, "body-overview"),))),
        manifest,
        revision,
    )
    items = context_source_items(facts)
    assert [item.ordinal for item in items] == [0, 1, 2]
    assert [item.item_id for item in items] == ["task", "facts", "knowledge/0"]
    assert all(item.version == item.source_ref.version for item in items)
    assert all(item.text is None for item in items)
    task_text = json.dumps(
        {
            "revision_id": str(revision.revision_id),
            "revision_number": revision.revision_number,
            "content_hash": revision.content_hash,
            "snapshot": revision.snapshot.to_mapping(),
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    assert (
        items[0].content_hash
        == "sha256:" + sha256(task_text.encode("utf-8")).hexdigest()
    )


@pytest.mark.parametrize("body", [b"wrong-size", b"broken\n"])
def test_validated_result_stage_retains_size_and_hash_checks(body):
    revision = _revision()
    manifest, contents = _v2_manifest_with_body(revision)
    contents[manifest.result_input_manifest.artifacts[0].artifact_id] = body
    with pytest.raises(ContextMaterializationError):
        validated(
            ContextMaterializer(
                FakeCatalog(),
                result_input_content=MemoryResultInputContentPort(contents),
            ),
            manifest,
            revision,
        )
