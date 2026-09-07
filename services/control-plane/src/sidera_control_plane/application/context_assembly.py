from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from math import ceil
from typing import Any, cast
from uuid import UUID, uuid4

from sidera_control_plane.application.ports import (
    ExactRevisionManifest,
    KnowledgeCatalogPort,
)
from sidera_control_plane.domain.context_packages import (
    ContextAssemblyRequest,
    ContextBudgetV1,
    ContextConsumerType,
    ContextPackageKnowledgeRef,
    ContextPackageManifest,
    ContextPackageManifestV1,
    ContextPackageManifestV2,
    ContextPackageManifestV3,
    ContextPurpose,
    ContextSelectionReason,
    ContextTrustLevel,
    ResultInputManifestV1,
)
from sidera_control_plane.domain.input_selections import CollaborationInputSelectionV1
from sidera_control_plane.domain.work_context import (
    KnowledgeRevisionRef,
    WorkContextRevision,
    WorkReference,
)

DEFAULT_MAX_SECTIONS = 16
DEFAULT_MAX_CHARS = 64 * 1024
DEFAULT_MAX_INPUT_TOKENS = 16 * 1024
HARD_MAX_SECTIONS = 32
HARD_MAX_CHARS = 128 * 1024
HARD_MAX_INPUT_TOKENS = 64 * 1024
MAX_PACKAGE_PAYLOAD_BYTES = 256 * 1024


class ContextAssemblyError(RuntimeError):
    """A deterministic, classified failure while assembling a package."""

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(message)


@dataclass(frozen=True, slots=True)
class _ValidatedRef:
    reference: ContextPackageKnowledgeRef
    estimated_chars: int
    estimated_input_tokens: int


class ContextAssembler:
    """Build an immutable ContextPackage manifest without opening a DB transaction."""

    def __init__(
        self,
        catalog: KnowledgeCatalogPort,
        *,
        adapter_max_sections: int = HARD_MAX_SECTIONS,
        adapter_max_chars: int = HARD_MAX_CHARS,
        adapter_max_input_tokens: int = HARD_MAX_INPUT_TOKENS,
    ) -> None:
        if min(
            adapter_max_sections,
            adapter_max_chars,
            adapter_max_input_tokens,
        ) < 1:
            raise ValueError("Context adapter limits must be positive")
        self._catalog = catalog
        self._adapter_max_sections = adapter_max_sections
        self._adapter_max_chars = adapter_max_chars
        self._adapter_max_input_tokens = adapter_max_input_tokens

    def assemble(
        self,
        request: ContextAssemblyRequest,
        *,
        work_context_revision: WorkContextRevision,
        control_plane_fact_snapshot: Mapping[str, object],
        artifact_refs: Sequence[WorkReference] | None = None,
        evidence_refs: Sequence[WorkReference] | None = None,
        package_id: UUID | None = None,
        created_at: datetime | None = None,
        correlation_id: UUID,
        caller_request_id: UUID,
        input_selection: CollaborationInputSelectionV1 | None = None,
    ) -> ContextPackageManifest:
        self._validate_input_scope(request, work_context_revision)
        self._validate_result_input_scope(request, input_selection)
        explicit_refs = tuple(
            _explicit_reference(item) for item in request.knowledge_filter_refs
        )
        work_refs = tuple(
            _work_context_reference(item)
            for item in work_context_revision.snapshot.knowledge_refs
        )
        fixed_refs = _merge_references(explicit_refs, work_refs)
        retrieval_refs = self._retrieval_references(
            request,
            correlation_id=correlation_id,
            caller_request_id=caller_request_id,
        )
        retrieval_refs = _merge_references((), retrieval_refs)
        all_refs = _merge_references(fixed_refs, retrieval_refs)
        validated = self._validate_exact_references(
            request,
            all_refs,
            correlation_id=correlation_id,
            caller_request_id=caller_request_id,
        )
        fixed_keys = {
            _reference_key(item) for item in fixed_refs
        }
        fixed_validated = tuple(
            item for item in validated if _reference_key(item.reference) in fixed_keys
        )
        retrieval_validated = tuple(
            item for item in validated if _reference_key(item.reference) not in fixed_keys
        )
        budget = self._budget(request)
        selected = self._select_with_budget(
            fixed_validated,
            retrieval_validated,
            budget=budget,
        )
        actual_chars = sum(item.estimated_chars for item in selected)
        actual_input_tokens = sum(item.estimated_input_tokens for item in selected)
        frozen_budget = ContextBudgetV1(
            profile_id=budget[0],
            profile_version=budget[1],
            max_sections=budget[2],
            max_chars=budget[3],
            max_input_tokens=budget[4],
            actual_sections=len(selected),
            actual_chars=actual_chars,
            actual_input_tokens=actual_input_tokens,
        )
        common = {
            "package_id": package_id or uuid4(),
            "workspace_id": request.workspace_id,
            "purpose": request.purpose,
            "consumer_type": request.consumer_type,
            "consumer_id": request.consumer_id,
            "worker_item_id": request.worker_item_id,
            "work_context_revision_id": request.work_context_revision_id,
            "context_version": request.context_version,
            "control_plane_fact_snapshot": control_plane_fact_snapshot,
            "knowledge_refs": tuple(item.reference for item in selected),
            "artifact_refs": (
                tuple(artifact_refs)
                if artifact_refs is not None
                else work_context_revision.snapshot.artifact_refs
            ),
            "evidence_refs": (
                tuple(evidence_refs)
                if evidence_refs is not None
                else work_context_revision.snapshot.evidence_refs
            ),
            "budget": frozen_budget,
            "expires_at": request.expires_at,
            "idempotency_key": request.idempotency_key,
            "created_at": created_at or request.created_at,
        }
        if request.manifest_schema_version == 1:
            manifest: ContextPackageManifest = ContextPackageManifestV1.create(
                **cast(Any, common)
            )
        elif request.manifest_schema_version == 2:
            assert input_selection is not None
            manifest = ContextPackageManifestV2.create(
                **cast(Any, common),
                result_input_manifest=ResultInputManifestV1.from_selection(
                    input_selection,
                    budget=request.result_input_budget,
                ),
            )
        else:
            result_input_manifest = (
                ResultInputManifestV1.from_selection(
                    input_selection,
                    budget=request.result_input_budget,
                )
                if input_selection is not None
                else None
            )
            manifest = ContextPackageManifestV3.create(
                **cast(Any, common),
                result_input_manifest=result_input_manifest,
                code_input_manifest=request.code_input_manifest,
                verification_manifest=request.verification_manifest,
            )
        if _json_size(manifest.hash_document()) > MAX_PACKAGE_PAYLOAD_BYTES:
            raise ContextAssemblyError(
                "context_budget_exceeded",
                "context package payload exceeds the hard limit",
            )
        return manifest

    def _retrieval_references(
        self,
        request: ContextAssemblyRequest,
        *,
        correlation_id: UUID,
        caller_request_id: UUID,
    ) -> tuple[ContextPackageKnowledgeRef, ...]:
        scope = _mapping(request.knowledge_search_scope, "knowledge_search_scope")
        query = _optional_text(scope.get("query"), "knowledge query")
        tags = _text_sequence(scope.get("tags", ()), "knowledge tags")
        source_types = _text_sequence(
            scope.get("source_types", ()), "knowledge source_types"
        )
        status = scope.get("status", "active")
        if status not in {None, "active"}:
            raise ContextAssemblyError(
                "context_search_scope_invalid",
                "context retrieval only supports active Knowledge hits",
            )
        if not query and not tags and not source_types:
            return ()
        limit = _integer(scope.get("limit", 20), "knowledge limit")
        if not 1 <= limit <= 50:
            raise ContextAssemblyError(
                "context_search_scope_invalid",
                "knowledge search limit must be between 1 and 50",
            )
        hits = self._catalog.search(
            request.workspace_id,
            query,
            tags=tags,
            source_types=source_types,
            status="active",
            limit=limit,
            correlation_id=correlation_id,
            caller_request_id=caller_request_id,
        )
        ordered_hits = sorted(
            hits,
            key=lambda item: (
                -item.rank,
                str(item.page_id),
                str(item.revision_id),
                item.section_key,
                item.section_hash,
                tuple(sorted(str(source_id) for source_id in item.source_ids)),
            ),
        )
        return tuple(
            ContextPackageKnowledgeRef(
                page_id=item.page_id,
                revision_id=item.revision_id,
                revision_hash=item.revision_hash,
                section_key=item.section_key,
                section_hash=item.section_hash,
                source_ids=item.source_ids,
                selection_reason=ContextSelectionReason.RETRIEVAL,
                trust_level=ContextTrustLevel.EXTERNAL_REFERENCE,
                ordinal=index,
            )
            for index, item in enumerate(ordered_hits)
        )

    def _validate_exact_references(
        self,
        request: ContextAssemblyRequest,
        references: Sequence[ContextPackageKnowledgeRef],
        *,
        correlation_id: UUID,
        caller_request_id: UUID,
    ) -> tuple[_ValidatedRef, ...]:
        manifests: dict[tuple[UUID, UUID], ExactRevisionManifest] = {}
        validated: list[_ValidatedRef] = []
        for reference in references:
            revision_key = (reference.page_id, reference.revision_id)
            manifest = manifests.get(revision_key)
            if manifest is None:
                manifest = self._catalog.get_revision_manifest(
                    request.workspace_id,
                    reference.page_id,
                    reference.revision_id,
                    correlation_id=correlation_id,
                    caller_request_id=caller_request_id,
                )
                manifests[revision_key] = manifest
            if manifest.workspace_id != request.workspace_id:
                raise ContextAssemblyError(
                    "context_workspace_mismatch",
                    "Knowledge revision belongs to another workspace",
                )
            if manifest.page_id != reference.page_id or manifest.revision_id != reference.revision_id:
                raise ContextAssemblyError(
                    "context_reference_not_found",
                    "Knowledge revision identity does not match the request",
                )
            if manifest.revision_hash != reference.revision_hash:
                raise ContextAssemblyError(
                    "context_hash_mismatch",
                    "Knowledge revision hash does not match the request",
                )
            if set(manifest.source_ids) != set(reference.source_ids):
                raise ContextAssemblyError(
                    "context_hash_mismatch",
                    "Knowledge source IDs do not match the request",
                )
            section = next(
                (
                    item
                    for item in manifest.sections
                    if item.section_key == reference.section_key
                ),
                None,
            )
            if section is None:
                raise ContextAssemblyError(
                    "context_reference_not_found",
                    "Knowledge section was not found in the requested revision",
                )
            if section.section_hash != reference.section_hash:
                raise ContextAssemblyError(
                    "context_hash_mismatch",
                    "Knowledge section hash does not match the request",
                )
            chars = section.content_byte_count
            validated.append(
                _ValidatedRef(
                    reference=reference,
                    estimated_chars=chars,
                    estimated_input_tokens=ceil(chars / 4),
                )
            )
        return tuple(validated)

    def _budget(self, request: ContextAssemblyRequest) -> tuple[UUID, int, int, int, int]:
        values = _mapping(request.budget, "budget")
        profile_id = _uuid(values.get("profile_id"), "profile_id")
        profile_version = _positive_required(values.get("profile_version"), "profile_version")
        profile_max_sections = _positive_or_default(
            values.get("max_sections"), DEFAULT_MAX_SECTIONS, "max_sections"
        )
        profile_max_chars = _positive_or_default(
            values.get("max_chars"), DEFAULT_MAX_CHARS, "max_chars"
        )
        profile_max_input_tokens = _positive_or_default(
            values.get("max_input_tokens"),
            DEFAULT_MAX_INPUT_TOKENS,
            "max_input_tokens",
        )
        purpose_max_sections = DEFAULT_MAX_SECTIONS
        purpose_max_chars = DEFAULT_MAX_CHARS
        purpose_max_input_tokens = DEFAULT_MAX_INPUT_TOKENS
        return (
            profile_id,
            profile_version,
            min(
                profile_max_sections,
                purpose_max_sections,
                self._adapter_max_sections,
                HARD_MAX_SECTIONS,
            ),
            min(
                profile_max_chars,
                purpose_max_chars,
                self._adapter_max_chars,
                HARD_MAX_CHARS,
            ),
            min(
                profile_max_input_tokens,
                purpose_max_input_tokens,
                self._adapter_max_input_tokens,
                HARD_MAX_INPUT_TOKENS,
            ),
        )

    @staticmethod
    def _select_with_budget(
        fixed: Sequence[_ValidatedRef],
        retrieval: Sequence[_ValidatedRef],
        *,
        budget: tuple[UUID, int, int, int, int],
    ) -> tuple[_ValidatedRef, ...]:
        max_sections, max_chars, max_input_tokens = budget[2:]
        selected: list[_ValidatedRef] = []
        used_chars = 0
        used_tokens = 0
        for item in fixed:
            if not _fits(
                item,
                section_count=len(selected),
                used_chars=used_chars,
                used_tokens=used_tokens,
                max_sections=max_sections,
                max_chars=max_chars,
                max_input_tokens=max_input_tokens,
            ):
                raise ContextAssemblyError(
                    "context_budget_exceeded",
                    "explicit or WorkContext context exceeds the package budget",
                )
            selected.append(item)
            used_chars += item.estimated_chars
            used_tokens += item.estimated_input_tokens
        for item in retrieval:
            if not _fits(
                item,
                section_count=len(selected),
                used_chars=used_chars,
                used_tokens=used_tokens,
                max_sections=max_sections,
                max_chars=max_chars,
                max_input_tokens=max_input_tokens,
            ):
                continue
            selected.append(item)
            used_chars += item.estimated_chars
            used_tokens += item.estimated_input_tokens
        return tuple(selected)

    @staticmethod
    def _validate_input_scope(
        request: ContextAssemblyRequest, revision: WorkContextRevision
    ) -> None:
        if request.workspace_id != revision.workspace_id:
            raise ContextAssemblyError(
                "context_workspace_mismatch",
                "WorkContext revision belongs to another workspace",
            )
        if request.work_context_revision_id != revision.revision_id:
            raise ContextAssemblyError(
                "context_consumer_stale",
                "WorkContext revision is stale for this assembly request",
            )

    @staticmethod
    def _validate_result_input_scope(
        request: ContextAssemblyRequest,
        input_selection: CollaborationInputSelectionV1 | None,
    ) -> None:
        if request.manifest_schema_version == 1:
            if input_selection is not None:
                raise ContextAssemblyError(
                    "context_input_selection_unexpected",
                    "v1 context assembly cannot receive an input Selection",
                )
            return
        if request.manifest_schema_version not in {2, 3}:
            raise ContextAssemblyError(
                "context_manifest_schema_unsupported",
                "context assembly manifest schema is unsupported",
            )
        if input_selection is None:
            if request.manifest_schema_version == 3 and request.input_selection_id is None:
                return
            raise ContextAssemblyError(
                "context_input_selection_missing",
                f"v{request.manifest_schema_version} context assembly requires the committed input Selection",
            )
        if (
            request.purpose is not ContextPurpose.EXECUTION
            or request.consumer_type is not ContextConsumerType.WORKER
        ):
            raise ContextAssemblyError(
                "context_input_selection_scope_invalid",
                "result input context requires an execution Worker consumer",
            )
        if (
            input_selection.workspace_id != request.workspace_id
            or input_selection.target_worker_item_id != request.worker_item_id
            or input_selection.id != request.input_selection_id
            or input_selection.selection_hash != request.input_selection_hash
        ):
            raise ContextAssemblyError(
                "context_input_selection_scope_invalid",
                "input Selection does not match the assembly target",
            )
        execution_request_id = request.budget.get("execution_request_id")
        if str(input_selection.target_execution_request_id) != str(execution_request_id):
            raise ContextAssemblyError(
                "context_input_selection_scope_invalid",
                "input Selection does not match the execution request",
            )
        if (
            request.manifest_schema_version == 3
            and request.code_input_manifest is not None
            and request.code_input_manifest.request_id
            != input_selection.target_execution_request_id
        ):
            raise ContextAssemblyError(
                "context_input_manifest_scope_invalid",
                "code input manifest does not match the execution request",
            )


def _explicit_reference(value: object) -> ContextPackageKnowledgeRef:
    if isinstance(value, ContextPackageKnowledgeRef):
        return _copy_reference(value, ContextSelectionReason.EXPLICIT)
    if isinstance(value, KnowledgeRevisionRef):
        return ContextPackageKnowledgeRef(
            page_id=value.page_id,
            revision_id=value.revision_id,
            revision_hash=value.revision_hash,
            section_key=value.section_key,
            section_hash=value.section_hash,
            source_ids=value.source_ids,
            selection_reason=ContextSelectionReason.EXPLICIT,
            trust_level=ContextTrustLevel.WORKSPACE_CONFIRMED,
            ordinal=value.ordinal,
        )
    mapping = _mapping(value, "explicit knowledge reference")
    return ContextPackageKnowledgeRef(
        page_id=_uuid(mapping.get("page_id"), "page_id"),
        revision_id=_uuid(mapping.get("revision_id"), "revision_id"),
        revision_hash=_text(mapping.get("revision_hash"), "revision_hash"),
        section_key=_text(mapping.get("section_key"), "section_key"),
        section_hash=_text(mapping.get("section_hash"), "section_hash"),
        source_ids=_uuid_sequence(mapping.get("source_ids", ()), "source_ids"),
        selection_reason=ContextSelectionReason.EXPLICIT,
        trust_level=ContextTrustLevel.WORKSPACE_CONFIRMED,
        ordinal=_integer(mapping.get("ordinal"), "ordinal"),
    )


def _work_context_reference(value: KnowledgeRevisionRef) -> ContextPackageKnowledgeRef:
    return ContextPackageKnowledgeRef(
        page_id=value.page_id,
        revision_id=value.revision_id,
        revision_hash=value.revision_hash,
        section_key=value.section_key,
        section_hash=value.section_hash,
        source_ids=value.source_ids,
        selection_reason=ContextSelectionReason.WORK_CONTEXT,
        trust_level=ContextTrustLevel.WORKSPACE_CONFIRMED,
        ordinal=value.ordinal,
    )


def _copy_reference(
    value: ContextPackageKnowledgeRef, selection_reason: ContextSelectionReason
) -> ContextPackageKnowledgeRef:
    return ContextPackageKnowledgeRef(
        page_id=value.page_id,
        revision_id=value.revision_id,
        revision_hash=value.revision_hash,
        section_key=value.section_key,
        section_hash=value.section_hash,
        source_ids=value.source_ids,
        selection_reason=selection_reason,
        trust_level=ContextTrustLevel.WORKSPACE_CONFIRMED,
        ordinal=value.ordinal,
    )


def _merge_references(
    existing: Sequence[ContextPackageKnowledgeRef],
    incoming: Sequence[ContextPackageKnowledgeRef],
) -> tuple[ContextPackageKnowledgeRef, ...]:
    result = list(existing)
    positions = {_reference_key(item): index for index, item in enumerate(result)}
    next_ordinal = max((item.ordinal for item in result), default=-1) + 1
    for item in incoming:
        key = _reference_key(item)
        position = positions.get(key)
        if position is not None:
            current = result[position]
            if (
                current.revision_hash != item.revision_hash
                or current.section_hash != item.section_hash
                or current.source_ids != item.source_ids
            ):
                raise ContextAssemblyError(
                    "context_hash_mismatch",
                    "duplicate Knowledge references disagree on exact hashes",
                )
            continue
        normalized = item
        if item.selection_reason is not ContextSelectionReason.EXPLICIT:
            normalized = ContextPackageKnowledgeRef(
                page_id=item.page_id,
                revision_id=item.revision_id,
                revision_hash=item.revision_hash,
                section_key=item.section_key,
                section_hash=item.section_hash,
                source_ids=item.source_ids,
                selection_reason=item.selection_reason,
                trust_level=item.trust_level,
                ordinal=next_ordinal,
            )
            next_ordinal += 1
        positions[key] = len(result)
        result.append(normalized)
    return tuple(result)


def _reference_key(value: ContextPackageKnowledgeRef) -> tuple[UUID, UUID, str]:
    return value.page_id, value.revision_id, value.section_key


def _fits(
    item: _ValidatedRef,
    *,
    section_count: int,
    used_chars: int,
    used_tokens: int,
    max_sections: int,
    max_chars: int,
    max_input_tokens: int,
) -> bool:
    return (
        section_count < max_sections
        and used_chars + item.estimated_chars <= max_chars
        and used_tokens + item.estimated_input_tokens <= max_input_tokens
    )


def _mapping(value: object, field_name: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise ContextAssemblyError("context_input_invalid", f"{field_name} must be a mapping")
    return value


def _text(value: object, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ContextAssemblyError("context_input_invalid", f"{field_name} must be text")
    return value.strip()


def _optional_text(value: object, field_name: str) -> str:
    if value is None:
        return ""
    return _text(value, field_name)


def _text_sequence(value: object, field_name: str) -> tuple[str, ...]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes, bytearray)):
        raise ContextAssemblyError("context_input_invalid", f"{field_name} must be a list")
    return tuple(_text(item, field_name) for item in value)


def _uuid(value: object, field_name: str) -> UUID:
    try:
        return value if isinstance(value, UUID) else UUID(str(value))
    except (TypeError, ValueError, AttributeError) as exc:
        raise ContextAssemblyError("context_input_invalid", f"{field_name} must be a UUID") from exc


def _uuid_sequence(value: object, field_name: str) -> tuple[UUID, ...]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes, bytearray)):
        raise ContextAssemblyError("context_input_invalid", f"{field_name} must be a list")
    return tuple(sorted({_uuid(item, field_name) for item in value}, key=str))


def _integer(value: object, field_name: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool):
        raise ContextAssemblyError("context_input_invalid", f"{field_name} must be an integer")
    return value


def _positive_or_default(value: object, default: int, field_name: str) -> int:
    if value is None:
        return default
    result = _integer(value, field_name)
    if result < 1:
        raise ContextAssemblyError("context_input_invalid", f"{field_name} must be positive")
    return result


def _positive_required(value: object, field_name: str) -> int:
    result = _integer(value, field_name)
    if result < 1:
        raise ContextAssemblyError("context_input_invalid", f"{field_name} must be positive")
    return result


def _json_size(value: Mapping[str, object]) -> int:
    return len(json.dumps(_plain_json(value), ensure_ascii=False, separators=(",", ":")))


def _plain_json(value: object) -> object:
    if isinstance(value, Mapping):
        return {str(key): _plain_json(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [_plain_json(item) for item in value]
    return value
