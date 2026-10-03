"""Exact, authority-scoped CP fact sources, not a prompt assembler."""

import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from hashlib import sha256
from uuid import UUID, uuid4

from sidera_agents.contracts import ContextItem, SourceContent, SourceRef
from sidera_agents.errors import AgentError

from sidera_control_plane.application.context_materialization import (
    MAX_CONTEXT_PAYLOAD_BYTES,
    ContextMaterializationError,
    ContextMaterializer,
    ValidatedContextFacts,
)
from sidera_control_plane.application.context_requests import (
    load_execution_context_selection,
    resolve_execution_context_authorization,
)
from sidera_control_plane.domain.context_packages import (
    ContextConsumerType,
    ContextMaterializationMode,
    ContextPackageManifestV2,
    ContextPackageManifestV3,
    ContextPurpose,
    ContextSelectionReason,
    ResultInputManifestV1,
)
from sidera_control_plane.domain.errors import DomainError

_VERSION = re.compile(r"cp1:([0-9a-f]{64}):([0-9a-f]{64}):([0-9a-f]{64})")
_KNOWLEDGE = re.compile(r"knowledge/(0|[1-9][0-9]*)")
_SECTIONS = {"task", "facts", "results", "code", "verification"}


def _plain(value):
    if isinstance(value, Mapping):
        return {key: _plain(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [_plain(item) for item in value]
    return value


def canonical_source_text(value: object) -> str:
    return json.dumps(
        _plain(value),
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _deny():
    raise AgentError("permission_denied", "Task source access denied.")


@dataclass(frozen=True, slots=True)
class _SourceIdentity:
    package_id: UUID
    revision_id: UUID
    section: str
    package_hash: str
    revision_hash: str
    body_hash: str


def _identity(ref: SourceRef) -> _SourceIdentity:
    if not isinstance(ref, SourceRef) or ref.provider != "control-plane":
        _deny()
    parts = ref.key.split("/")
    version = _VERSION.fullmatch(ref.version)
    if len(parts) not in {4, 5} or parts[0] != "context" or version is None:
        _deny()
    try:
        package_id, revision_id = UUID(parts[1]), UUID(parts[2])
    except ValueError:
        _deny()
    if str(package_id) != parts[1] or str(revision_id) != parts[2]:
        _deny()
    section = "/".join(parts[3:])
    if section not in _SECTIONS and _KNOWLEDGE.fullmatch(section) is None:
        _deny()
    return _SourceIdentity(package_id, revision_id, section, *version.groups())


def _sections(facts: ValidatedContextFacts) -> tuple[tuple[str, bool], ...]:
    manifest = facts.manifest
    result = [("task", True), ("facts", True)]
    result.extend(
        (
            f"knowledge/{item.ordinal}",
            item.selection_reason is not ContextSelectionReason.RETRIEVAL,
        )
        for item in sorted(manifest.knowledge_refs, key=lambda item: item.ordinal)
    )
    if (
        isinstance(manifest, (ContextPackageManifestV2, ContextPackageManifestV3))
        and manifest.result_input_manifest is not None
    ):
        result.append(("results", True))
    if isinstance(manifest, ContextPackageManifestV3):
        if manifest.code_input_manifest is not None:
            result.append(("code", True))
        if manifest.verification_manifest is not None:
            result.append(("verification", True))
    return tuple(result)


def _source_text(facts: ValidatedContextFacts, section: str) -> str:
    if section == "task":
        revision = facts.revision
        value = {
            "revision_id": str(revision.revision_id),
            "revision_number": revision.revision_number,
            "content_hash": revision.content_hash,
            "snapshot": revision.snapshot.to_mapping(),
        }
    elif section == "facts":
        value = facts.manifest.control_plane_fact_snapshot
    elif section == "results":
        value = {
            "manifest": facts.manifest.result_input_manifest.to_mapping(),
            "inputs": [item.to_mapping() for item in facts.result_inputs],
        }
    elif section in {"code", "verification"}:
        value = getattr(
            facts.manifest,
            f"{section}_input_manifest"
            if section == "code"
            else "verification_manifest",
        ).to_mapping()
    else:
        ordinal = int(section.split("/")[1])
        value = next(
            item for item in facts.project_knowledge if item.ordinal == ordinal
        ).to_mapping()
    return canonical_source_text(value)


def context_source_items(facts: ValidatedContextFacts) -> tuple[ContextItem, ...]:
    """Seal validated immutable sections for an explicitly created CP authority."""
    result = []
    for ordinal, (section, required) in enumerate(_sections(facts)):
        digest = sha256(_source_text(facts, section).encode("utf-8")).hexdigest()
        ref = SourceRef(
            provider="control-plane",
            key=f"context/{facts.manifest.package_id}/{facts.revision.revision_id}/{section}",
            version=f"cp1:{facts.manifest.package_hash}:{facts.revision.content_hash}:{digest}",
        )
        result.append(_item(ref, section, ordinal, required))
    return tuple(result)


def _item(ref: SourceRef, section: str, ordinal: int, required: bool) -> ContextItem:
    return ContextItem(
        item_id=section,
        origin="control-plane",
        required=required,
        ordinal=ordinal,
        version=ref.version,
        content_hash="sha256:" + _identity(ref).body_hash,
        source_ref=ref,
    )


class TaskContextSourceReader:
    def __init__(
        self, uow_factory, *, authorizer, catalog, result_input_content=None, clock=None
    ):
        self._uow_factory, self._authorizer = uow_factory, authorizer
        self._clock = clock or (lambda: datetime.now(UTC))
        self._materializer = ContextMaterializer(
            catalog, result_input_content=result_input_content
        )

    def require_execution(
        self, grant_id: UUID, workspace_id: UUID, execution_id: UUID, operation_id: UUID
    ) -> None:
        with self._uow_factory() as uow:
            authority = uow.execution_authorities.get(grant_id)
            if authority is None or not authority.sources:
                _deny()
            self._authorizer.require(grant_id, "source.read", authority.sources[0])
            link = uow.execution_authorities.get_link(
                authority.workspace_id, authority.execution_id
            )
            if (
                authority.workspace_id != workspace_id
                or authority.execution_id != execution_id
                or link is None
                or link.authority_id != grant_id
                or link.start_operation_id != operation_id
                or link.profile_ref != authority.profile_ref
                or link.binding_version != authority.binding_version
            ):
                _deny()

    def validated_facts(self, grant_id: UUID, ref: SourceRef) -> ValidatedContextFacts:
        # Authorization precedes package lookup and every external body read.
        self._authorizer.require(grant_id, "source.read", ref)
        identity = _identity(ref)
        try:
            with self._uow_factory() as uow:
                authority = uow.execution_authorities.get(grant_id)
                if authority is None:
                    _deny()
                execution = uow.executions.get(
                    authority.execution_id, authority.workspace_id
                )
                manifest = uow.context_packages.get(
                    identity.package_id, authority.workspace_id
                )
                revision = uow.work_contexts.get_revision(
                    identity.revision_id, authority.workspace_id
                )
                if execution is None or manifest is None or revision is None:
                    _deny()
                authorization = resolve_execution_context_authorization(uow, execution)
                if authorization is None:
                    _deny()
                request = authorization.request
                worker_item = uow.worker_items.get(
                    execution.worker_item_id, authority.workspace_id
                )
                context = uow.work_contexts.get_by_root_task(
                    request.root_task_id, authority.workspace_id
                )
                if (
                    manifest.consumer_type is not ContextConsumerType.WORKER
                    or manifest.consumer_id != execution.id
                    or manifest.worker_item_id != execution.worker_item_id
                    or worker_item is None
                    or request.subtask_id != execution.task_id
                    or worker_item.root_task_id != request.root_task_id
                    or manifest.context_version != worker_item.context_version
                    or context is None
                    or context.id != revision.work_context_id
                    or manifest.work_context_revision_id != revision.revision_id
                    or manifest.package_hash != identity.package_hash
                    or revision.content_hash != identity.revision_hash
                    or revision.snapshot.content_hash != revision.content_hash
                ):
                    _deny()
                snapshot = manifest.control_plane_fact_snapshot
                if (
                    snapshot.get("execution", {}).get("id") != str(execution.id)
                    or snapshot.get("execution_request", {}).get("id")
                    != str(request.id)
                    or snapshot.get("worker_item", {}).get("id")
                    != str(execution.worker_item_id)
                    or snapshot.get("plan", {}).get("id")
                    != str(request.plan_snapshot_id)
                    or snapshot.get("execution", {}).get("task_id")
                    != str(execution.task_id)
                    or tuple(
                        snapshot.get("execution_request", {}).get(
                            "expected_outputs", ()
                        )
                    )
                    != request.expected_outputs
                ):
                    _deny()
                selection = load_execution_context_selection(uow, request)
                pinned_results = getattr(manifest, "result_input_manifest", None)
                if selection is not None:
                    if (
                        pinned_results is None
                        or pinned_results
                        != ResultInputManifestV1.from_selection(
                            selection, budget=pinned_results.budget
                        )
                    ):
                        _deny()
                elif pinned_results is not None:
                    _deny()
                facts = self._materializer.validate_facts(
                    manifest,
                    revision,
                    mode=ContextMaterializationMode.CONSUMER_START,
                    workspace_id=authority.workspace_id,
                    consumer_id=execution.id,
                    purpose=ContextPurpose.EXECUTION,
                    context_version=manifest.context_version,
                    expected_package_hash=identity.package_hash,
                    now=self._clock(),
                    correlation_id=execution.id,
                    caller_request_id=uuid4(),
                    include_bodies=False,
                )
                if identity.section not in dict(_sections(facts)):
                    _deny()
                return facts
        except ContextMaterializationError as exc:
            raise AgentError(exc.code, "Pinned task context is unavailable.") from None
        except (DomainError, ValueError, TypeError, KeyError):
            raise AgentError(
                "context_invalid", "Pinned task context is invalid."
            ) from None

    def read(self, grant_id: UUID, ref: SourceRef) -> SourceContent:
        facts = self.validated_facts(grant_id, ref)
        identity = _identity(ref)
        try:
            if identity.section.startswith("knowledge/"):
                section = self._materializer.read_knowledge_section(
                    facts,
                    int(identity.section.split("/")[1]),
                    correlation_id=facts.manifest.consumer_id,
                    caller_request_id=uuid4(),
                )
                facts = ValidatedContextFacts(
                    facts.manifest, facts.revision, (section,), ()
                )
            elif identity.section == "results":
                facts = ValidatedContextFacts(
                    facts.manifest,
                    facts.revision,
                    (),
                    self._materializer.read_result_inputs(facts.manifest),
                )
            text = _source_text(facts, identity.section)
        except ContextMaterializationError as exc:
            raise AgentError(exc.code, "Pinned task source is unavailable.") from None
        digest = sha256(text.encode("utf-8")).hexdigest()
        if digest != identity.body_hash:
            raise AgentError(
                "context_hash_mismatch", "Pinned task source integrity failed."
            )
        if len(text.encode("utf-8")) > MAX_CONTEXT_PAYLOAD_BYTES:
            raise AgentError(
                "context_budget_exceeded", "Pinned task source exceeds its budget."
            )
        self._authorizer.require(grant_id, "source.read", ref)
        return SourceContent(
            text=text, actual_version=ref.version, content_hash="sha256:" + digest
        )

    def context_items(
        self, grant_id: UUID, execution_id: UUID, *, reserved_bytes: int = 0
    ) -> tuple[ContextItem, ...]:
        if type(reserved_bytes) is not int or reserved_bytes < 0:
            raise ValueError("Reserved input bytes must be non-negative")
        with self._uow_factory() as uow:
            authority = uow.execution_authorities.get(grant_id)
            if (
                authority is None
                or authority.execution_id != execution_id
                or not authority.sources
            ):
                _deny()
            refs = authority.sources
        facts = self.validated_facts(grant_id, refs[0])
        identities = tuple(_identity(ref) for ref in refs)
        if len({identity.section for identity in identities}) != len(refs):
            _deny()
        if any(
            identity.package_id != facts.manifest.package_id
            or identity.revision_id != facts.revision.revision_id
            for identity in identities
        ):
            _deny()
        by_section = {
            identity.section: ref
            for identity, ref in zip(identities, refs, strict=True)
        }
        if set(by_section) - dict(_sections(facts)).keys():
            _deny()
        result, total_bytes = [], reserved_bytes
        for section, required in _sections(facts):
            ref = by_section.get(section)
            if ref is None:
                if required:
                    _deny()
                continue
            item = _item(ref, section, len(result), required)
            try:
                content = self.read(grant_id, ref)
            except AgentError as exc:
                if required or exc.code not in {
                    "context_reference_not_found",
                    "context_service_unavailable",
                }:
                    raise
                # Keep the original optional pin; the agents assembler records omission.
            else:
                total_bytes += len(content.text.encode("utf-8"))
            result.append(item)
        limit = MAX_CONTEXT_PAYLOAD_BYTES
        if (
            isinstance(
                facts.manifest, (ContextPackageManifestV2, ContextPackageManifestV3)
            )
            and facts.manifest.result_input_manifest is not None
        ):
            limit = min(
                limit, facts.manifest.result_input_manifest.budget.max_payload_bytes
            )
        if (
            total_bytes > limit
            or (total_bytes + 3) // 4 > facts.manifest.budget.max_input_tokens
        ):
            raise AgentError(
                "context_budget_exceeded", "Pinned task context exceeds its budget."
            )
        return tuple(result)
