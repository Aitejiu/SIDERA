from collections.abc import Sequence
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from uuid import UUID

import pytest

from sidera_control_plane.application.context_assembly import (
    ContextAssembler,
    ContextAssemblyError,
)
from sidera_control_plane.application.ports import (
    ExactRevisionManifest,
    ExactRevisionSection,
    KnowledgeCatalogPort,
    SearchHit,
)
from sidera_control_plane.domain.context_packages import (
    ContextAssemblyRequest,
    ContextConsumerType,
    ContextPackageStatus,
    ContextPurpose,
    ContextSelectionReason,
    ContextTrustLevel,
)
from sidera_control_plane.domain.work_context import (
    KnowledgeRevisionRef,
    WorkContextRevision,
    WorkContextRevisionReason,
    WorkContextSnapshotV1,
)

NOW = datetime(2026, 8, 26, 12, 0, tzinfo=UTC)
WORKSPACE_ID = UUID("10000000-0000-0000-0000-000000000001")
WORK_CONTEXT_ID = UUID("20000000-0000-0000-0000-000000000002")
PLANNING_REQUEST_ID = UUID("30000000-0000-0000-0000-000000000003")
CONSUMER_ID = UUID("40000000-0000-0000-0000-000000000004")
PACKAGE_ID = UUID("50000000-0000-0000-0000-000000000005")
PAGE_EXPLICIT = UUID("60000000-0000-0000-0000-000000000006")
PAGE_WORK = UUID("70000000-0000-0000-0000-000000000007")
PAGE_RETRIEVED = UUID("80000000-0000-0000-0000-000000000008")
REV_EXPLICIT = UUID("90000000-0000-0000-0000-000000000009")
REV_WORK = UUID("a0000000-0000-0000-0000-00000000000a")
REV_RETRIEVED = UUID("b0000000-0000-0000-0000-00000000000b")
SOURCE_ID = UUID("c0000000-0000-0000-0000-00000000000c")
CORRELATION_ID = UUID("d0000000-0000-0000-0000-00000000000d")
CALLER_REQUEST_ID = UUID("e0000000-0000-0000-0000-00000000000e")


def _request(
    *,
    budget: dict[str, object] | None = None,
    expires_at: datetime = NOW + timedelta(hours=1),
    consumer_id: UUID = CONSUMER_ID,
    knowledge_filter_refs: Sequence[object] = (),
) -> ContextAssemblyRequest:
    return ContextAssemblyRequest.create(
        request_id=UUID("f0000000-0000-0000-0000-00000000000f"),
        workspace_id=WORKSPACE_ID,
        purpose=ContextPurpose.PLANNING_RUN,
        consumer_type=ContextConsumerType.PLANNER,
        consumer_id=consumer_id,
        worker_item_id=None,
        work_context_revision_id=UUID("11000000-0000-0000-0000-000000000011"),
        context_version=None,
        knowledge_search_scope={
            "query": "retrieved",
            "tags": ["context"],
            "source_types": ["markdown_file"],
            "status": "active",
            "limit": 10,
        },
        knowledge_filter_refs=knowledge_filter_refs,
        fact_scope_refs=("planning_request", "brief"),
        budget=budget
        or {
            "profile_id": str(UUID("12000000-0000-0000-0000-000000000012")),
            "profile_version": 3,
            "max_sections": 16,
            "max_chars": 65_536,
            "max_input_tokens": 16_384,
        },
        expires_at=expires_at,
        request_fingerprint="a" * 64,
        idempotency_key="context:planning-run:test:v1",
        created_at=NOW,
    )


def _revision(work_ref: KnowledgeRevisionRef | None = None) -> WorkContextRevision:
    snapshot = WorkContextSnapshotV1.create(
        objective="Assemble a deterministic context package",
        constraints=(),
        acceptance_criteria=(),
        requested_outputs=(),
        clarification_decisions=(),
        accepted_decisions=(),
        unresolved_questions=(),
        knowledge_refs=(work_ref,) if work_ref is not None else (),
        artifact_refs=(),
        evidence_refs=(),
        checkpoint_refs=(),
    )
    return WorkContextRevision.create(
        revision_id=UUID("11000000-0000-0000-0000-000000000011"),
        work_context_id=WORK_CONTEXT_ID,
        workspace_id=WORKSPACE_ID,
        revision_number=2,
        brief_planning_request_id=PLANNING_REQUEST_ID,
        brief_revision=4,
        proposal_revision_id=None,
        plan_snapshot_id=None,
        reason=WorkContextRevisionReason.PROPOSAL_CONFIRMED,
        snapshot=snapshot,
        created_by="test",
        created_at=NOW,
    )


def _work_ref() -> KnowledgeRevisionRef:
    return KnowledgeRevisionRef(
        page_id=PAGE_WORK,
        revision_id=REV_WORK,
        revision_hash="2" * 64,
        section_key="work",
        section_hash="3" * 64,
        source_ids=(SOURCE_ID,),
        ordinal=2,
    )


def _explicit_ref() -> dict[str, object]:
    return {
        "page_id": str(PAGE_EXPLICIT),
        "revision_id": str(REV_EXPLICIT),
        "revision_hash": "4" * 64,
        "section_key": "explicit",
        "section_hash": "5" * 64,
        "source_ids": [str(SOURCE_ID)],
        "ordinal": 7,
    }


def _hit(
    *,
    page_id: UUID,
    revision_id: UUID,
    section_key: str,
    revision_hash: str,
    section_hash: str,
    rank: float,
    section_byte_count: int = 100,
) -> SearchHit:
    return SearchHit(
        page_id=page_id,
        revision_id=revision_id,
        section_key=section_key,
        revision_number=1,
        revision_hash=revision_hash,
        section_hash=section_hash,
        title="Retrieved",
        summary="Retrieved summary",
        tags=("context",),
        source_ids=(SOURCE_ID,),
        source_types=("markdown_file",),
        source_availability=("available",),
        trust="workspace_confirmed",
        section_byte_count=section_byte_count,
        rank=rank,
        rank_explanation="test",
    )


def _manifest(
    *,
    page_id: UUID,
    revision_id: UUID,
    revision_hash: str,
    section_key: str,
    section_hash: str,
    workspace_id: UUID = WORKSPACE_ID,
    source_ids: tuple[UUID, ...] = (SOURCE_ID,),
    section_byte_count: int = 100,
) -> ExactRevisionManifest:
    return ExactRevisionManifest(
        workspace_id=workspace_id,
        page_id=page_id,
        revision_id=revision_id,
        revision_number=1,
        revision_hash=revision_hash,
        title="Test revision",
        summary="Test summary",
        tags=("context",),
        source_ids=source_ids,
        sections=(
            ExactRevisionSection(
                section_key=section_key,
                heading=section_key.title(),
                summary="Section summary",
                ordinal=0,
                section_hash=section_hash,
                content_byte_count=section_byte_count,
            ),
        ),
    )


class FakeCatalog(KnowledgeCatalogPort):
    def __init__(
        self,
        hits: Sequence[SearchHit] = (),
        manifests: Sequence[ExactRevisionManifest] = (),
    ) -> None:
        self.hits = tuple(hits)
        self.manifests: dict[tuple[UUID, UUID], ExactRevisionManifest] = {}
        for item in manifests:
            key = (item.page_id, item.revision_id)
            previous = self.manifests.get(key)
            self.manifests[key] = (
                replace(item, sections=previous.sections + item.sections)
                if previous is not None
                else item
            )
        self.search_calls: list[dict[str, object]] = []
        self.manifest_calls: list[tuple[UUID, UUID, UUID]] = []

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
        self.search_calls.append(
            {
                "workspace_id": workspace_id,
                "query": query,
                "tags": tuple(tags),
                "source_types": tuple(source_types),
                "status": status,
                "limit": limit,
                "correlation_id": correlation_id,
                "caller_request_id": caller_request_id,
            }
        )
        return self.hits

    def get_revision_manifest(
        self,
        workspace_id: UUID,
        page_id: UUID,
        revision_id: UUID,
        *,
        correlation_id: UUID,
        caller_request_id: UUID,
    ) -> ExactRevisionManifest:
        del correlation_id, caller_request_id
        self.manifest_calls.append((workspace_id, page_id, revision_id))
        return self.manifests[(page_id, revision_id)]

    def materialize(self, *args, **kwargs):
        raise AssertionError("assembler must not materialize Knowledge bodies")


def _assembler(catalog: FakeCatalog) -> ContextAssembler:
    return ContextAssembler(catalog)


def _assemble(
    assembler: ContextAssembler,
    request: ContextAssemblyRequest,
    revision: WorkContextRevision,
    *,
    facts: dict[str, object] | None = None,
):
    return assembler.assemble(
        request,
        work_context_revision=revision,
        control_plane_fact_snapshot=facts or {"planning_request_id": str(PLANNING_REQUEST_ID)},
        package_id=PACKAGE_ID,
        created_at=NOW,
        correlation_id=CORRELATION_ID,
        caller_request_id=CALLER_REQUEST_ID,
    )


def test_assembler_orders_explicit_work_context_and_retrieval_deterministically() -> None:
    work = _work_ref()
    high = _hit(
        page_id=PAGE_RETRIEVED,
        revision_id=REV_RETRIEVED,
        section_key="high",
        revision_hash="6" * 64,
        section_hash="7" * 64,
        rank=0.9,
    )
    low = _hit(
        page_id=PAGE_RETRIEVED,
        revision_id=REV_RETRIEVED,
        section_key="low",
        revision_hash="6" * 64,
        section_hash="8" * 64,
        rank=0.4,
    )
    catalog = FakeCatalog(
        hits=(low, high),
        manifests=(
            _manifest(
                page_id=PAGE_EXPLICIT,
                revision_id=REV_EXPLICIT,
                revision_hash="4" * 64,
                section_key="explicit",
                section_hash="5" * 64,
            ),
            _manifest(
                page_id=PAGE_WORK,
                revision_id=REV_WORK,
                revision_hash="2" * 64,
                section_key="work",
                section_hash="3" * 64,
            ),
            _manifest(
                page_id=PAGE_RETRIEVED,
                revision_id=REV_RETRIEVED,
                revision_hash="6" * 64,
                section_key="high",
                section_hash="7" * 64,
            ),
            _manifest(
                page_id=PAGE_RETRIEVED,
                revision_id=REV_RETRIEVED,
                revision_hash="6" * 64,
                section_key="low",
                section_hash="8" * 64,
            ),
        ),
    )
    manifest = _assemble(_assembler(catalog), _request(knowledge_filter_refs=(_explicit_ref(),)), _revision(work))

    assert [item.section_key for item in manifest.knowledge_refs] == [
        "explicit",
        "work",
        "high",
        "low",
    ]
    assert manifest.knowledge_refs[0].ordinal == 7
    assert [item.selection_reason for item in manifest.knowledge_refs] == [
        ContextSelectionReason.EXPLICIT,
        ContextSelectionReason.WORK_CONTEXT,
        ContextSelectionReason.RETRIEVAL,
        ContextSelectionReason.RETRIEVAL,
    ]
    assert all(
        item.trust_level is ContextTrustLevel.WORKSPACE_CONFIRMED
        for item in manifest.knowledge_refs[:2]
    )
    assert manifest.status is ContextPackageStatus.ACTIVE
    assert catalog.search_calls[0]["status"] == "active"


def test_assembler_deduplicates_explicit_work_and_retrieval_refs() -> None:
    work = _work_ref()
    duplicate_hit = _hit(
        page_id=PAGE_WORK,
        revision_id=REV_WORK,
        section_key="work",
        revision_hash="2" * 64,
        section_hash="3" * 64,
        rank=1.0,
    )
    catalog = FakeCatalog(
        hits=(duplicate_hit,),
        manifests=(
            _manifest(
                page_id=PAGE_WORK,
                revision_id=REV_WORK,
                revision_hash="2" * 64,
                section_key="work",
                section_hash="3" * 64,
            ),
        ),
    )

    manifest = _assemble(_assembler(catalog), _request(), _revision(work))

    assert len(manifest.knowledge_refs) == 1
    assert manifest.knowledge_refs[0].selection_reason is ContextSelectionReason.WORK_CONTEXT


def test_assembler_fails_explicit_refs_that_exceed_budget_but_truncates_retrieval() -> None:
    second_explicit = {
        **_explicit_ref(),
        "section_key": "other",
        "section_hash": "9" * 64,
    }
    explicit_catalog = FakeCatalog(
        manifests=(
            _manifest(
                page_id=PAGE_EXPLICIT,
                revision_id=REV_EXPLICIT,
                revision_hash="4" * 64,
                section_key="explicit",
                section_hash="5" * 64,
            ),
            _manifest(
                page_id=PAGE_EXPLICIT,
                revision_id=REV_EXPLICIT,
                revision_hash="4" * 64,
                section_key="other",
                section_hash="9" * 64,
            ),
        )
    )
    with pytest.raises(ContextAssemblyError) as explicit_error:
        _assemble(
            _assembler(explicit_catalog),
            _request(
                budget={
                    "profile_id": str(UUID("12000000-0000-0000-0000-000000000012")),
                    "profile_version": 3,
                    "max_sections": 1,
                    "max_chars": 65_536,
                    "max_input_tokens": 16_384,
                },
                knowledge_filter_refs=(_explicit_ref(), second_explicit),
            ),
            _revision(),
        )
    assert explicit_error.value.code == "context_budget_exceeded"

    hits = (
        _hit(
            page_id=PAGE_RETRIEVED,
            revision_id=REV_RETRIEVED,
            section_key="first",
            revision_hash="6" * 64,
            section_hash="7" * 64,
            rank=0.9,
        ),
        _hit(
            page_id=PAGE_RETRIEVED,
            revision_id=REV_RETRIEVED,
            section_key="second",
            revision_hash="6" * 64,
            section_hash="8" * 64,
            rank=0.8,
        ),
    )
    retrieval_catalog = FakeCatalog(
        hits=hits,
        manifests=tuple(
            _manifest(
                page_id=PAGE_RETRIEVED,
                revision_id=REV_RETRIEVED,
                revision_hash="6" * 64,
                section_key=item.section_key,
                section_hash=item.section_hash,
            )
            for item in hits
        ),
    )
    retrieval_manifest = _assemble(
        _assembler(retrieval_catalog),
        _request(
            budget={
                "profile_id": str(UUID("12000000-0000-0000-0000-000000000012")),
                "profile_version": 3,
                "max_sections": 1,
                "max_chars": 65_536,
                "max_input_tokens": 16_384,
            }
        ),
        _revision(),
    )
    assert [item.section_key for item in retrieval_manifest.knowledge_refs] == ["first"]


@pytest.mark.parametrize(
    "budget",
    [
        {
            "profile_id": "not-a-uuid",
            "profile_version": 3,
            "max_sections": 16,
            "max_chars": 65_536,
            "max_input_tokens": 16_384,
        },
        {
            "profile_id": str(UUID("12000000-0000-0000-0000-000000000012")),
            "profile_version": 0,
            "max_sections": 16,
            "max_chars": 65_536,
            "max_input_tokens": 16_384,
        },
    ],
)
def test_assembler_classifies_invalid_budget_profile(budget: dict[str, object]) -> None:
    with pytest.raises(ContextAssemblyError) as error:
        _assemble(_assembler(FakeCatalog()), _request(budget=budget), _revision())
    assert error.value.code == "context_input_invalid"


@pytest.mark.parametrize("field", ["workspace", "revision", "section", "source"])
def test_assembler_rejects_exact_metadata_mismatch(field: str) -> None:
    manifest_kwargs: dict[str, object] = {
        "page_id": PAGE_EXPLICIT,
        "revision_id": REV_EXPLICIT,
        "revision_hash": "4" * 64,
        "section_key": "explicit",
        "section_hash": "5" * 64,
    }
    if field == "workspace":
        manifest_kwargs["workspace_id"] = UUID("ffffffff-ffff-ffff-ffff-ffffffffffff")
    elif field == "revision":
        manifest_kwargs["revision_hash"] = "e" * 64
    elif field == "section":
        manifest_kwargs["section_hash"] = "e" * 64
    elif field == "source":
        manifest_kwargs["source_ids"] = (UUID("ffffffff-ffff-ffff-ffff-ffffffffffff"),)
    catalog = FakeCatalog(
        manifests=(
            _manifest(**manifest_kwargs),  # pyright: ignore[reportArgumentType]
        )
    )

    with pytest.raises(ContextAssemblyError) as error:
        _assemble(
            _assembler(catalog),
            _request(knowledge_filter_refs=(_explicit_ref(),)),
            _revision(),
        )
    assert error.value.code in {"context_workspace_mismatch", "context_hash_mismatch"}


def test_assembler_hash_is_stable_for_hit_order_and_changes_for_context_inputs() -> None:
    hits = (
        _hit(
            page_id=PAGE_RETRIEVED,
            revision_id=REV_RETRIEVED,
            section_key="a",
            revision_hash="6" * 64,
            section_hash="7" * 64,
            rank=0.5,
        ),
        _hit(
            page_id=PAGE_RETRIEVED,
            revision_id=REV_RETRIEVED,
            section_key="b",
            revision_hash="6" * 64,
            section_hash="8" * 64,
            rank=0.5,
        ),
    )
    manifests = tuple(
        _manifest(
            page_id=PAGE_RETRIEVED,
            revision_id=REV_RETRIEVED,
            revision_hash="6" * 64,
            section_key=item.section_key,
            section_hash=item.section_hash,
        )
        for item in hits
    )
    first = _assemble(
        _assembler(FakeCatalog(hits=hits, manifests=manifests)),
        _request(),
        _revision(),
    )
    second = _assemble(
        _assembler(FakeCatalog(hits=tuple(reversed(hits)), manifests=manifests)),
        _request(),
        _revision(),
    )
    changed_facts = _assemble(
        _assembler(FakeCatalog(hits=hits, manifests=manifests)),
        _request(),
        _revision(),
        facts={"planning_request_id": str(PLANNING_REQUEST_ID), "mode": "changed"},
    )
    changed_expiry = _assemble(
        _assembler(FakeCatalog(hits=hits, manifests=manifests)),
        _request(expires_at=NOW + timedelta(hours=2)),
        _revision(),
    )

    assert first.package_hash == second.package_hash
    assert first.knowledge_refs == second.knowledge_refs
    assert first.package_hash != changed_facts.package_hash
    assert first.package_hash != changed_expiry.package_hash
    assert first.expires_at == NOW + timedelta(hours=1)
