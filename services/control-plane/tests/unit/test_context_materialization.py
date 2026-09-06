from __future__ import annotations

from collections.abc import Sequence
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from io import BytesIO
from typing import BinaryIO
from uuid import UUID, uuid4

import pytest

from sidera_control_plane.adapters.knowledge_http import KnowledgeAdapterError
from sidera_control_plane.application.context_materialization import (
    ContextMaterializationError,
    ContextMaterializer,
    build_materialization_audit,
)
from sidera_control_plane.application.ports import (
    ExactRevisionManifest,
    ExactSectionBody,
    ExactSectionRef,
    KnowledgeSourceMetadata,
    ResultInputContentPort,
    SearchHit,
)
from sidera_control_plane.domain.context_packages import (
    ContextBudgetV1,
    ContextConsumerType,
    ContextMaterializationMode,
    ContextMaterializationOutcome,
    ContextMaterializationV2,
    ContextPackageKnowledgeRef,
    ContextPackageManifestV1,
    ContextPackageManifestV2,
    ContextPackageStatus,
    ContextPurpose,
    ContextRevocationScope,
    ContextSelectionReason,
    ContextTrustLevel,
)
from sidera_control_plane.domain.work_context import (
    ContextTextItem,
    KnowledgeRevisionRef,
    WorkContextRevision,
    WorkContextRevisionReason,
    WorkContextSnapshotV1,
    WorkReference,
)
from tests.unit.result_input_fixtures import result_input_manifest

NOW = datetime(2026, 8, 26, 12, 0, tzinfo=UTC)
WORKSPACE_ID = UUID("10000000-0000-0000-0000-000000000001")
CONSUMER_ID = UUID("10000000-0000-0000-0000-000000000002")
REVISION_ID = UUID("10000000-0000-0000-0000-000000000003")
SOURCE_ID = UUID("10000000-0000-0000-0000-000000000004")


class FakeCatalog:
    def __init__(self, bodies: tuple[ExactSectionBody, ...] = ()) -> None:
        self.bodies = bodies
        self.materialize_calls = 0

    def search(
        self,
        workspace_id: UUID,
        query: str,
        *,
        tags: Sequence[str] = (),
        source_types: Sequence[str] = (),
        status: str | None = "active",
        limit: int = 20,
        correlation_id: UUID,
        caller_request_id: UUID,
    ) -> tuple[SearchHit, ...]:
        raise NotImplementedError

    def get_revision_manifest(
        self,
        workspace_id: UUID,
        page_id: UUID,
        revision_id: UUID,
        *,
        correlation_id: UUID,
        caller_request_id: UUID,
    ) -> ExactRevisionManifest:
        raise NotImplementedError

    def materialize(
        self,
        workspace_id: UUID,
        sections: Sequence[ExactSectionRef],
        *,
        correlation_id: UUID,
        caller_request_id: UUID,
    ) -> tuple[ExactSectionBody, ...]:
        self.materialize_calls += 1
        return self.bodies


class FailingCatalog(FakeCatalog):
    def materialize(
        self,
        workspace_id: UUID,
        sections: Sequence[ExactSectionRef],
        *,
        correlation_id: UUID,
        caller_request_id: UUID,
    ) -> tuple[ExactSectionBody, ...]:
        self.materialize_calls += 1
        raise KnowledgeAdapterError(
            "temporary_unavailable",
            "temporary",
            temporary=True,
        )


class MemoryResultInputContentPort(ResultInputContentPort):
    def __init__(self, contents: dict[UUID, bytes]) -> None:
        self.contents = contents

    def open_exact(
        self,
        *,
        workspace_id: UUID,
        artifact_id: UUID,
        expected_hash: str,
        expected_size_bytes: int,
    ) -> BinaryIO:
        del workspace_id, expected_hash, expected_size_bytes
        return BytesIO(self.contents[artifact_id])


def _revision(*, knowledge_ref: KnowledgeRevisionRef | None = None) -> WorkContextRevision:
    snapshot = WorkContextSnapshotV1.create(
        objective="Materialize the exact runtime context",
        constraints=(ContextTextItem(key="safety", text="No body in persistence", ordinal=0),),
        acceptance_criteria=(
            ContextTextItem(key="exact", text="Exact hashes are required", ordinal=0),
        ),
        requested_outputs=(
            ContextTextItem(key="payload", text="A typed payload", ordinal=0),
        ),
        clarification_decisions=(),
        accepted_decisions=(),
        unresolved_questions=(),
        knowledge_refs=(knowledge_ref,) if knowledge_ref is not None else (),
        artifact_refs=(
            WorkReference(ref_id=UUID("10000000-0000-0000-0000-000000000005"), ref_type="artifact", ordinal=0),
        ),
        evidence_refs=(
            WorkReference(ref_id=UUID("10000000-0000-0000-0000-000000000006"), ref_type="evidence", ordinal=0),
        ),
        checkpoint_refs=(),
        missing_fact_codes=("missing-owner",),
    )
    return WorkContextRevision.create(
        revision_id=REVISION_ID,
        work_context_id=UUID("10000000-0000-0000-0000-000000000007"),
        workspace_id=WORKSPACE_ID,
        revision_number=3,
        brief_planning_request_id=UUID("10000000-0000-0000-0000-000000000008"),
        brief_revision=1,
        proposal_revision_id=None,
        plan_snapshot_id=None,
        reason=WorkContextRevisionReason.PROPOSAL_CONFIRMED,
        snapshot=snapshot,
        created_by="test",
        created_at=NOW,
    )


def _knowledge_ref(*, ordinal: int, section_key: str, source_ids=(SOURCE_ID,)) -> ContextPackageKnowledgeRef:
    return ContextPackageKnowledgeRef(
        page_id=UUID(f"20000000-0000-0000-0000-00000000000{ordinal + 1}"),
        revision_id=UUID(f"30000000-0000-0000-0000-00000000000{ordinal + 1}"),
        revision_hash=(str(ordinal + 1) * 64)[:64],
        section_key=section_key,
        section_hash=(str(ordinal + 4) * 64)[:64],
        source_ids=source_ids,
        selection_reason=ContextSelectionReason.EXPLICIT,
        trust_level=ContextTrustLevel.WORKSPACE_CONFIRMED,
        ordinal=ordinal,
    )


def _body(reference: ContextPackageKnowledgeRef, text: str = "reference body") -> ExactSectionBody:
    return ExactSectionBody(
        ordinal=reference.ordinal,
        page_id=reference.page_id,
        revision_id=reference.revision_id,
        revision_hash=reference.revision_hash,
        section_key=reference.section_key,
        section_hash=reference.section_hash,
        body_markdown=text,
        sources=(
            KnowledgeSourceMetadata(
                source_id=reference.source_ids[0],
                workspace_id=WORKSPACE_ID,
                source_type="markdown_file",
                label="Test source",
                uri=None,
                workspace_relative_path="docs/context.md",
                git_revision="abc123",
                digest="d" * 64,
                metadata={"owner": "test"},
                availability="available",
                safe_reason_code=None,
                last_checked_at=NOW,
                version=1,
            ),
        ),
        trust=reference.trust_level.value,
        section_byte_count=len(text.encode("utf-8")),
    )


def _manifest(
    revision: WorkContextRevision,
    refs: tuple[ContextPackageKnowledgeRef, ...] = (),
    *,
    created_at: datetime = NOW,
    expires_at: datetime = NOW + timedelta(hours=1),
) -> ContextPackageManifestV1:
    body_bytes = sum(len(f"body-{item.section_key}".encode()) for item in refs)
    body_tokens = sum((len(f"body-{item.section_key}".encode()) + 3) // 4 for item in refs)
    return ContextPackageManifestV1.create(
        package_id=uuid4(),
        workspace_id=WORKSPACE_ID,
        purpose=ContextPurpose.PLANNING_RUN,
        consumer_type=ContextConsumerType.PLANNER,
        consumer_id=CONSUMER_ID,
        worker_item_id=None,
        work_context_revision_id=revision.revision_id,
        context_version=None,
        control_plane_fact_snapshot={"planning_request_id": str(uuid4())},
        knowledge_refs=refs,
        artifact_refs=revision.snapshot.artifact_refs,
        evidence_refs=revision.snapshot.evidence_refs,
        budget=ContextBudgetV1(
            profile_id=uuid4(),
            profile_version=1,
            max_sections=8,
            max_chars=20_000,
            max_input_tokens=4_000,
            actual_sections=len(refs),
            actual_chars=body_bytes,
            actual_input_tokens=body_tokens,
        ),
        expires_at=expires_at,
        idempotency_key=f"context:test:{uuid4()}",
        created_at=created_at,
    )


def _materializer(catalog: FakeCatalog | None = None) -> tuple[ContextMaterializer, FakeCatalog]:
    selected = catalog or FakeCatalog()
    return ContextMaterializer(selected), selected


def _v2_manifest_with_body(
    revision: WorkContextRevision, body: bytes = b"result\n"
) -> tuple[ContextPackageManifestV2, dict[UUID, bytes]]:
    legacy = _manifest(revision)
    result_manifest = result_input_manifest()
    artifact = replace(
        result_manifest.artifacts[0],
        content_hash=sha256(body).hexdigest(),
        size_bytes=len(body),
    )
    result_manifest = replace(result_manifest, artifacts=(artifact,), evidence=())
    manifest = ContextPackageManifestV2(
        package_id=legacy.package_id,
        workspace_id=legacy.workspace_id,
        purpose=legacy.purpose,
        consumer_type=legacy.consumer_type,
        consumer_id=legacy.consumer_id,
        worker_item_id=legacy.worker_item_id,
        work_context_revision_id=legacy.work_context_revision_id,
        context_version=legacy.context_version,
        control_plane_fact_snapshot=legacy.control_plane_fact_snapshot,
        knowledge_refs=legacy.knowledge_refs,
        artifact_refs=legacy.artifact_refs,
        evidence_refs=legacy.evidence_refs,
        budget=legacy.budget,
        expires_at=legacy.expires_at,
        idempotency_key=legacy.idempotency_key,
        status=legacy.status,
        created_at=legacy.created_at,
        result_input_manifest=result_manifest,
    )
    return manifest, {artifact.artifact_id: body}


def _run(materializer: ContextMaterializer, manifest, revision, **kwargs):
    return materializer.materialize(
        manifest,
        revision,
        mode=kwargs.pop("mode", ContextMaterializationMode.CONSUMER_START),
        workspace_id=kwargs.pop("workspace_id", WORKSPACE_ID),
        consumer_id=kwargs.pop("consumer_id", CONSUMER_ID),
        purpose=kwargs.pop("purpose", ContextPurpose.PLANNING_RUN),
        context_version=kwargs.pop("context_version", None),
        expected_package_hash=kwargs.pop("expected_package_hash", manifest.package_hash),
        owner_capability=kwargs.pop("owner_capability", False),
        now=kwargs.pop("now", NOW),
        correlation_id=UUID("40000000-0000-0000-0000-000000000001"),
        caller_request_id=UUID("40000000-0000-0000-0000-000000000002"),
        **kwargs,
    )


def test_no_knowledge_refs_skip_http_and_emit_fixed_typed_sections() -> None:
    revision = _revision()
    manifest = _manifest(revision)
    materializer, catalog = _materializer()

    result = _run(materializer, manifest, revision)

    assert catalog.materialize_calls == 0
    assert list(result.to_mapping()) == [
        "schema_version",
        "metadata",
        "system_constraints",
        "work_context",
        "control_plane_facts",
        "project_knowledge",
        "artifact_and_evidence_refs",
    ]
    assert result.project_knowledge == ()
    assert result.work_context.content_hash == revision.content_hash
    assert [item.category for item in result.artifact_and_evidence_refs] == [
        "artifact",
        "evidence",
    ]
    assert "body_markdown" not in str(result.to_mapping())
    assert result.total_byte_count == len(result.canonical_bytes())


def test_exact_knowledge_is_sorted_by_manifest_ordinal_and_audited_without_body() -> None:
    first = _knowledge_ref(ordinal=1, section_key="first")
    second = _knowledge_ref(ordinal=0, section_key="second")
    revision = _revision(knowledge_ref=KnowledgeRevisionRef(
        page_id=first.page_id,
        revision_id=first.revision_id,
        revision_hash=first.revision_hash,
        section_key=first.section_key,
        section_hash=first.section_hash,
        source_ids=first.source_ids,
        ordinal=first.ordinal,
    ))
    refs = (first, second)
    bodies = (_body(first, "body-first"), _body(second, "body-second"))
    manifest = _manifest(revision, refs)
    materializer, catalog = _materializer(FakeCatalog(bodies))

    result = _run(materializer, manifest, revision)
    audit = build_materialization_audit(
        manifest,
        attempt=1,
        outcome=ContextMaterializationOutcome.SUCCEEDED,
        occurred_at=NOW,
        materialization=result,
    )

    assert catalog.materialize_calls == 1
    assert [item.section_key for item in result.project_knowledge] == ["second", "first"]
    assert result.project_knowledge[0].body_markdown == "body-second"
    assert audit.section_count == 2
    assert audit.byte_count == result.total_byte_count
    assert audit.input_token_count == result.input_token_count
    assert "body-second" not in str(audit)


@pytest.mark.parametrize("mismatch", ["count", "hash", "source", "body_bytes"])
def test_any_knowledge_mismatch_fails_as_corruption_without_partial_result(mismatch: str) -> None:
    reference = _knowledge_ref(ordinal=0, section_key="overview")
    revision = _revision()
    manifest = _manifest(revision, (reference,))
    body = _body(reference, "stable body")
    if mismatch == "count":
        bodies = ()
    elif mismatch == "hash":
        bodies = (replace(body, section_hash="f" * 64),)
    elif mismatch == "source":
        bodies = (replace(body, sources=()),)
    else:
        bodies = (replace(body, section_byte_count=999),)
    materializer, _ = _materializer(FakeCatalog(bodies))

    with pytest.raises(ContextMaterializationError) as exc_info:
        _run(materializer, manifest, revision)

    assert exc_info.value.code == "context_hash_mismatch"
    assert exc_info.value.corrupt is True
    assert not hasattr(exc_info.value, "body_markdown")


def test_expiry_and_revoke_scopes_separate_consumer_start_from_owner_audit() -> None:
    revision = _revision()
    expired = _manifest(
        revision,
        created_at=NOW - timedelta(hours=2),
        expires_at=NOW - timedelta(minutes=1),
    )
    materializer, _ = _materializer()

    with pytest.raises(ContextMaterializationError, match="expired") as expired_error:
        _run(materializer, expired, revision)
    assert expired_error.value.code == "context_package_expired"
    owner_result = _run(
        materializer,
        expired,
        revision,
        mode=ContextMaterializationMode.OWNER_AUDIT,
        owner_capability=True,
    )
    assert owner_result.metadata.package_id == expired.package_id

    revoked = replace(
        expired,
        status=ContextPackageStatus.REVOKED,
        revoked_at=NOW,
        revocation_reason="operator_request",
        revocation_scope=ContextRevocationScope.NEW_STARTS,
    )
    with pytest.raises(ContextMaterializationError) as revoked_error:
        _run(materializer, revoked, revision)
    assert revoked_error.value.code == "context_package_revoked"
    assert _run(
        materializer,
        revoked,
        revision,
        mode=ContextMaterializationMode.OWNER_AUDIT,
        owner_capability=True,
    ).metadata.package_id == revoked.package_id

    all_revoked = replace(revoked, revocation_scope=ContextRevocationScope.ALL_MATERIALIZATION)
    with pytest.raises(ContextMaterializationError) as all_revoked_error:
        _run(
            materializer,
            all_revoked,
            revision,
            mode=ContextMaterializationMode.OWNER_AUDIT,
            owner_capability=True,
        )
    assert all_revoked_error.value.code == "context_package_revoked"


def test_transient_knowledge_failure_is_auditable_and_does_not_mark_corrupt() -> None:
    reference = _knowledge_ref(ordinal=0, section_key="overview")
    revision = _revision()
    manifest = _manifest(revision, (reference,))
    materializer, catalog = _materializer(FailingCatalog())

    with pytest.raises(ContextMaterializationError) as exc_info:
        _run(materializer, manifest, revision)

    error = exc_info.value
    assert error.code == "context_service_unavailable"
    assert error.temporary is True
    assert error.corrupt is False
    audit = build_materialization_audit(
        manifest,
        attempt=2,
        outcome=ContextMaterializationOutcome.FAILED,
        occurred_at=NOW,
        failure_code=error.code,
    )
    assert catalog.materialize_calls == 1
    assert (audit.section_count, audit.byte_count, audit.input_token_count) == (0, 0, 0)
    assert audit.failure_code == "context_service_unavailable"


def test_hard_payload_limit_blocks_before_returning_a_large_body() -> None:
    revision = _revision()
    manifest = _manifest(revision)
    materializer = ContextMaterializer(FakeCatalog(), max_payload_bytes=100)

    with pytest.raises(ContextMaterializationError) as exc_info:
        _run(materializer, manifest, revision)

    assert exc_info.value.code == "context_budget_exceeded"


def test_v2_materialization_enforces_exact_payload_and_pinned_token_limits() -> None:
    revision = _revision()
    manifest, contents = _v2_manifest_with_body(revision)
    port = MemoryResultInputContentPort(contents)
    probe = _run(
        ContextMaterializer(FakeCatalog(), result_input_content=port),
        manifest,
        revision,
    )
    assert isinstance(probe, ContextMaterializationV2)

    exact_payload = _run(
        ContextMaterializer(
            FakeCatalog(),
            max_payload_bytes=probe.total_byte_count,
            result_input_content=port,
        ),
        manifest,
        revision,
    )
    assert exact_payload.total_byte_count == probe.total_byte_count

    with pytest.raises(ContextMaterializationError) as payload_error:
        _run(
            ContextMaterializer(
                FakeCatalog(),
                max_payload_bytes=probe.total_byte_count - 1,
                result_input_content=port,
            ),
            manifest,
            revision,
        )
    assert payload_error.value.code == "context_budget_exceeded"

    pinned_manifest = replace(
        manifest,
        budget=replace(
            manifest.budget,
            max_input_tokens=probe.input_token_count,
        ),
    )
    exact_tokens = _run(
        ContextMaterializer(FakeCatalog(), result_input_content=port),
        pinned_manifest,
        revision,
    )
    assert exact_tokens.input_token_count <= pinned_manifest.budget.max_input_tokens

    too_small_manifest = replace(
        pinned_manifest,
        budget=replace(
            pinned_manifest.budget,
            max_input_tokens=max(exact_tokens.input_token_count - 1, 1),
        ),
    )
    with pytest.raises(ContextMaterializationError) as token_error:
        _run(
            ContextMaterializer(FakeCatalog(), result_input_content=port),
            too_small_manifest,
            revision,
        )
    assert token_error.value.code == "result_input_budget_exceeded"


def test_owner_audit_requires_capability_and_expected_hash() -> None:
    revision = _revision()
    manifest = _manifest(revision)
    materializer, _ = _materializer()

    with pytest.raises(ContextMaterializationError) as permission_error:
        _run(
            materializer,
            manifest,
            revision,
            mode=ContextMaterializationMode.OWNER_AUDIT,
        )
    assert permission_error.value.code == "context_permission_denied"

    with pytest.raises(ContextMaterializationError) as hash_error:
        _run(
            materializer,
            manifest,
            revision,
            expected_package_hash="f" * 64,
            mode=ContextMaterializationMode.OWNER_AUDIT,
            owner_capability=True,
        )
    assert hash_error.value.code == "context_hash_mismatch"
    assert hash_error.value.corrupt is False
